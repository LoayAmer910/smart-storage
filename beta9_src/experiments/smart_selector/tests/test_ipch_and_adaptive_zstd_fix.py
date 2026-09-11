"""Focused regression tests for the post-run performance fix:
- .ipch (and friends) now classified as BUILD_ARTIFACT, not TEXT_UNKNOWN
- generic_zstd is now size-adaptive (was hardcoded level 19)

Run directly: python test_ipch_and_adaptive_zstd_fix.py
"""

import os
import sys
import tempfile
import time

_HERE = os.path.dirname(__file__)
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

import adaptive_zstd  # noqa: E402
import file_analysis  # noqa: E402
import selector  # noqa: E402
import strategy_registry  # noqa: E402

_FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _FAILURES.append(name)


def _make_file(tmp_dir, name, data):
    path = os.path.join(tmp_dir, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def test_ipch_routing():
    with tempfile.TemporaryDirectory() as tmp:
        # .ipch content that would previously pass the printable-byte text-sniff
        fake_ipch = _make_file(tmp, "HW1_MAIN.ipch", b"MSF\x00\x01\x02Program Files precompiled header data " + os.urandom(200))
        detected, is_binary, mismatch = file_analysis.detect_format(fake_ipch, "ipch")
        check(".ipch detected as BUILD_ARTIFACT (not TEXT_UNKNOWN)", detected == "BUILD_ARTIFACT", detected)
        check(".ipch correctly flagged as binary", is_binary is True)

        candidates = strategy_registry.get_candidate_strategies("BUILD_ARTIFACT", "ipch", 40_000_000)
        check("BUILD_ARTIFACT routes to cheap-only probe", candidates == ["generic_zstd"], candidates)

    for ext in ("pdb", "obj", "ilk"):
        detected, _, _ = file_analysis.detect_format(__file__, ext)
        check(f".{ext} also detected as BUILD_ARTIFACT", detected == "BUILD_ARTIFACT", detected)


def test_large_file_zstd_policy():
    mib = 1024 * 1024
    check("small file (1MiB) keeps level 19", adaptive_zstd.select_level(1 * mib) == 19)
    check("boundary just under 4MiB still level 19", adaptive_zstd.select_level(4 * mib - 1) == 19)
    check("at/above 4MiB drops to level 12", adaptive_zstd.select_level(5 * mib) == 12)
    check("40MB (the actual .ipch size that timed out) uses level 12", adaptive_zstd.select_level(40_000_000) == 12)


def test_large_file_completes_fast_no_timeout():
    # 20MB of semi-structured data (mimics the .ipch pattern that previously
    # caused level-19 zstd to blow past 30s) - must now complete quickly.
    with tempfile.TemporaryDirectory() as tmp:
        data = (os.urandom(64) + b"\x00" * 192) * (20_000_000 // 256)
        src = _make_file(tmp, "large_sample.ipch", data)
        strat = adaptive_zstd.AdaptiveZstdStrategy()
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        t0 = time.perf_counter()
        result = strat.compress(src, work)
        elapsed = time.perf_counter() - t0
        check("20MB adaptive-zstd compress finishes well under the 30s cap", elapsed < 10.0, elapsed)
        check("large file used a reduced level, not 19", strat._current_level < 19, strat._current_level)

        restored = strat.restore(result.output_path, work, "large_sample.ipch")
        with open(restored.output_path, "rb") as f:
            restored_data = f.read()
        check("large-file exact restoration (SHA-equivalent byte match)", restored_data == data)


class _SlowStrategyV2:
    """Module-level (not local) so multiprocessing spawn can pickle it."""
    name = "slow_strategy_v2"

    def is_available(self):
        return True, ""

    def compress(self, input_path, work_dir):
        import time as _t
        _t.sleep(5)
        return None

    def restore(self, *a, **k):
        return None


def test_timeout_fallback_still_works():
    # unchanged mechanism, verified again here in the context of this fix
    t0 = time.time()
    result = selector._run_with_timeout(_SlowStrategyV2(), __file__, tempfile.gettempdir(), "fix_timeout_test", timeout_s=0.3)
    elapsed = time.time() - t0
    check("timeout fallback still real-kills (bounded wall time)", elapsed < 4.0 and result["status"] == "TIMEOUT", (elapsed, result["status"]))


def test_exact_restoration_end_to_end():
    photos_root = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"
    scratch = os.path.join(os.path.dirname(_HERE), "scratch")
    sample = os.path.join(photos_root, "MANIFEST.csv")
    if os.path.isfile(sample):
        result = selector.select(sample, photos_root, scratch, "fix_exact_test")
        # every candidate must either be SHA-256 exact, or have failed for a
        # reason that is NOT a silent correctness issue (timeout/exception/unavailable)
        ok = all(c["sha256_pass"] is True or c["rejection_reason"] in ("TIMEOUT", "EXCEPTION", "UNAVAILABLE") for c in result["candidates"])
        check("all completed candidates are SHA-256 exact", ok, result["candidates"])
    else:
        check("exact restoration e2e test (sample present)", False, "sample missing")


def test_deterministic_selection_after_fix():
    photos_root = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"
    scratch = os.path.join(os.path.dirname(_HERE), "scratch")
    sample = os.path.join(photos_root, "MANIFEST.csv")
    if os.path.isfile(sample):
        r1 = selector.select(sample, photos_root, scratch, "fix_det_1")
        r2 = selector.select(sample, photos_root, scratch, "fix_det_2")
        check(
            "selection deterministic post-fix",
            r1["selected_strategy"] == r2["selected_strategy"] and r1["selected_bytes"] == r2["selected_bytes"],
            (r1["selected_strategy"], r2["selected_strategy"]),
        )
    else:
        check("determinism test (sample present)", False, "sample missing")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    print()
    if _FAILURES:
        print(f"RESULT: {len(_FAILURES)} FAILURE(S): {_FAILURES}")
        return 1
    print(f"RESULT: ALL {len(tests)} TEST FUNCTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
