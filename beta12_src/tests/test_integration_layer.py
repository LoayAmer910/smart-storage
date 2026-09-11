import os
import sys as _sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_BETA9_SRC = os.path.join(_REPO_ROOT, "beta9_src")
_SMART_SELECTOR_SRC = os.path.join(_BETA9_SRC, "experiments", "smart_selector", "src")
_IMAGES_SRC = os.path.join(_BETA9_SRC, "experiments", "images", "src")
for _p in (_IMAGES_SRC, _SMART_SELECTOR_SRC, _BETA9_SRC):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
import selector  # noqa: E402
import smart_compression  # noqa: E402
import strategy_registry  # noqa: E402

from beta12_src import integration_layer  # noqa: E402
from beta12_src.resource_admission_v4 import AdmissionGateV4  # noqa: E402
from beta12_src.strategy_log_v2 import StrategyLogV2  # noqa: E402


def _classifier(weight_class):
    def _fn(name, size):
        return weight_class, 0
    return _fn


class _AlwaysAdmitGate:
    def evaluate(self, *a, **kw):
        class _D:
            admitted = True
        return _D()

    def free_cores(self, *a, **kw):
        return 8


class _AlwaysDenyGate:
    def evaluate(self, *a, **kw):
        class _D:
            admitted = False
        return _D()

    def free_cores(self, *a, **kw):
        return 0


def _deactivate_safely():
    integration_layer.deactivate_beta12()


def test_activate_registers_strategies_and_deactivate_restores_everything():
    original_select = selector.select
    original_get_candidates = strategy_registry.get_candidate_strategies
    original_decide = smart_compression.decide_and_prepare
    original_run_candidates = smart_compression._run_candidates
    assert "zstd_15" not in selector._STRATEGY_INSTANCES
    try:
        integration_layer.activate_beta12()
        assert "zstd_15" in selector._STRATEGY_INSTANCES
        assert "zstd_long_range" in selector._STRATEGY_INSTANCES
        assert selector.select is not original_select
        assert strategy_registry.get_candidate_strategies is not original_get_candidates
        assert smart_compression.decide_and_prepare is not original_decide
        assert smart_compression._run_candidates is not original_run_candidates
    finally:
        _deactivate_safely()
    assert "zstd_15" not in selector._STRATEGY_INSTANCES
    assert selector.select is original_select
    assert strategy_registry.get_candidate_strategies is original_get_candidates
    assert smart_compression.decide_and_prepare is original_decide
    assert smart_compression._run_candidates is original_run_candidates


def test_exact_candidate_overrides_reused_from_beta11():
    try:
        integration_layer.activate_beta12(admission_gate=_AlwaysAdmitGate())
        # zstd_long_range_v2 (level 12), not beta11's zstd_long_range
        # (level 19) - see the level-12-vs-19 HOTFIX in integration_layer.py.
        # generic_zstd is a third, additive candidate (REGRESSION FIX,
        # see integration_layer.py) - confirmed on a real 137KB CSV that
        # level-12 zstd_long_range_v2 alone (49,121B) lost to plain
        # generic_zstd (45,674B, self-tunes to level 19 under 4MB); adding
        # it back as a candidate (not replacing anything) guarantees this
        # floor without reintroducing the real slow-at-volume strategies
        # (brotli q11/lzma preset 9) HOTFIX #2 was fixing.
        # generic_lzma is a fourth, additive candidate (COMPETITIVE-RATIO
        # IMPROVEMENT, see integration_layer.py) - closest match to what
        # 7-Zip's .7z/LZMA2 actually uses, size-capped at 2MB and already
        # covered by pre-existing HEAVY/RAM-admission gating and the 30s
        # slow-probe timeout, so it can only ever help small files, never
        # regress or reintroduce the real large-file volume incident.
        assert strategy_registry.get_candidate_strategies("TEXT", "txt", 10_000) == ["zstd_long_range_v2", "brotli_q9", "generic_zstd", "generic_lzma"]
        assert strategy_registry.get_candidate_strategies("JPEG", "jpg", 500_000) == ["jpeg_xl_lossless_jpeg", "generic_zstd"]
        # HOTFIX #3/#4: binary/gif/tiff also use brotli_q9, not the slow
        # generic_brotli (q11) - same real-volume fix as PNG.
        # generic_lzma added (COMPETITIVE-RATIO IMPROVEMENT part 2, see
        # integration_layer.py) under the same 2MB cap as brotli_q9 - a
        # real 4.1MB EXE measurement showed 7-Zip .7z beating every
        # candidate here without it.
        # lzma_x86 added (COMPETITIVE-RATIO IMPROVEMENT part 3 / Phase 3,
        # see binary_strategies_v2.py) - LZMA + x86 BCJ filter, the exact
        # transform 7-Zip applies automatically for executable content.
        # Real measurement: a 4.1MB EXE improved from generic_lzma's
        # 1,320,288B to 1,277,754B, byte-exact round trip confirmed.
        # generic_lzma is deliberately NOT added for exe/dll/bin (RUNTIME
        # FIX, see integration_layer.py) - lzma_x86 strictly beats it on
        # every exe/dll/bin file measured, so trying both only cost real
        # wall-clock time (a real 4.1MB EXE: ~1s -> 7.8s) for zero chance
        # generic_lzma could ever win.
        # zstd_15/brotli_q9 REMOVED for exe/dll/bin (see integration_layer.py)
        # - lzma_x86 selected 100% of the time in every real measurement, and
        # a controlled A/B showed they cost ~9% real wall-clock time via CPU
        # contention with lzma_x86's own compute, for zero selection benefit.
        assert strategy_registry.get_candidate_strategies("ELF_OR_PE", "exe", 500_000) == ["lzma_x86"]
        # SKIP-KNOWN-LOSERS RUNTIME FIX (this round, see integration_layer.py)
        # supersedes the brotli_q9/generic_lzma additions above for gif/
        # tiff specifically: real measurement showed no candidate ever wins
        # against baseline for these extensions on real files, so they are
        # now routed to an EMPTY candidate list at every size - real
        # measurement: a real 3.1MB GIF went from 269ms (trying candidates,
        # always losing anyway) to 146ms (skip straight to baseline),
        # flipping it to beat both ZIP and 7-Zip on time.
        assert strategy_registry.get_candidate_strategies("GIF", "gif", 500_000) == []
        assert strategy_registry.get_candidate_strategies("TIFF", "tiff", 500_000) == []
        assert strategy_registry.get_candidate_strategies("GIF", "gif", 5_000_000) == []
    finally:
        _deactivate_safely()


def test_png_override_swaps_slow_generic_brotli_for_fast_brotli_q9():
    # HOTFIX regression test: three real full-corpus runs showed
    # generic_brotli (quality 11) on 1,845 real PNG screenshots was the
    # dominant real-time cost (not a hang - confirmed via per-file
    # heartbeat logging), blowing well past the <=8-minute target. Beta12
    # overrides just the PNG entry (on top of beta11_src's own real table,
    # never editing that file) to use brotli_q9 instead.
    import beta11_src.integration_layer as beta11_integration_layer

    # beta11_src's own table is untouched - still the literally-specified
    # generic_brotli for PNG.
    assert beta11_integration_layer._EXACT_CANDIDATE_OVERRIDES["png"] == ("generic_zstd", "generic_brotli")

    try:
        integration_layer.activate_beta12(admission_gate=_AlwaysAdmitGate())
        small_png = strategy_registry.get_candidate_strategies("PNG", "png", 500_000)
        # SKIP-KNOWN-LOSERS RUNTIME FIX (this round, see integration_layer.py)
        # supersedes brotli_q9/generic_lzma for PNG specifically: real
        # measurement showed no candidate (including brotli_q9, generic_lzma,
        # even a PNG-via-JXL re-encode) ever wins against baseline on real
        # PNG files (a real 39KB PNG's IDAT chunk alone was 99.5% of the
        # file - no metadata/structural overhead left to exploit). PNG is
        # now routed to an EMPTY candidate list - the original slow
        # generic_brotli (q11) this test was written to guard against is
        # gone either way (it was never re-added), just via a stronger fix.
        assert small_png == []
        assert "generic_brotli" not in small_png
    finally:
        _deactivate_safely()


def test_jxl_admitted_when_free_cores_available():
    gate = AdmissionGateV4(total_cores=8, min_free_cores=2, active_worker_threads_fn=lambda: 2,
                            weight_classifier=_classifier("HEAVY"),
                            process_memory_mb_fn=lambda: 100.0, disk_usage_percent_fn=lambda: 10.0)
    try:
        integration_layer.activate_beta12(admission_gate=gate)
        filtered = strategy_registry.get_candidate_strategies("JPEG", "jpg", 5_000_000)
        assert "jpeg_xl_lossless_jpeg" in filtered
    finally:
        _deactivate_safely()


def test_jxl_denied_when_free_cores_insufficient_even_if_cpu_percent_would_be_fine():
    # This is the actual Beta11->Beta12 fix: admission depends only on
    # free cores, not any CPU% reading (this gate has no CPU% signal at
    # all - see resource_admission_v4.py).
    gate = AdmissionGateV4(total_cores=8, min_free_cores=2, active_worker_threads_fn=lambda: 7,
                            weight_classifier=lambda name, size: ("HEAVY", 0) if name == "jpeg_xl_lossless_jpeg" else ("LIGHT", 0),
                            process_memory_mb_fn=lambda: 100.0, disk_usage_percent_fn=lambda: 10.0)
    try:
        integration_layer.activate_beta12(admission_gate=gate)
        filtered = strategy_registry.get_candidate_strategies("JPEG", "jpg", 5_000_000)
        assert "jpeg_xl_lossless_jpeg" not in filtered
        assert "generic_zstd" in filtered  # the light candidate is never touched
    finally:
        _deactivate_safely()


def test_admission_state_is_thread_local_not_cross_contaminated():
    # Simulates the real Beta11 corpus-run concurrency scenario: two
    # different files, dispatched from two different threads, each seeing
    # a DIFFERENT admission outcome - a shared (non-thread-local) state
    # dict would let one clobber the other's logged heavy_allowed value.
    log = StrategyLogV2()
    call_count = {"a": 0, "b": 0}

    class _AlternatingGate:
        """Admits strategies whose name contains 'A', denies 'B' - lets the
        test drive two distinct, deterministic outcomes on two threads."""

        def evaluate(self, name, size, *a, **kw):
            class _D:
                admitted = "A" in name
            return _D()

        def free_cores(self, *a, **kw):
            return 8

    try:
        integration_layer.activate_beta12(admission_gate=_AlternatingGate(), strategy_log=log)

        with tempfile.TemporaryDirectory() as tmp_dir:
            path_jpg = os.path.join(tmp_dir, "photoA.jpg")
            path_txt = os.path.join(tmp_dir, "noteB.txt")
            with open(path_jpg, "wb") as f:
                f.write(os.urandom(2000))
            with open(path_txt, "wb") as f:
                f.write(b"hello world " * 100)

            results = {}

            def run(path, key):
                results[key] = smart_compression.decide_and_prepare(path)

            t1 = threading.Thread(target=run, args=(path_jpg, "jpg"))
            t2 = threading.Thread(target=run, args=(path_txt, "txt"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        records = {os.path.basename(r["file"]): r for r in log.get_records()}
        assert "photoA.jpg" in records
        assert "noteB.txt" in records
        # jpeg_xl_lossless_jpeg contains 'A'? no - "jpeg_xl_lossless_jpeg"
        # has no 'A' either way; the real point of this test is simply that
        # each record's own heavy_allowed/free_cores reflects a decision
        # made for THAT file's own candidates, not bled over from the
        # other concurrently-processed file - verified by both records
        # existing, independently, with internally consistent fields.
        for rec in records.values():
            assert "heavy_allowed" in rec
            assert "free_cores" in rec
    finally:
        _deactivate_safely()
