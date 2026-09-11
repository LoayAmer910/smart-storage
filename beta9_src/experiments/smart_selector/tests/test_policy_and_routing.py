"""Focused Phase 2 tests: policy decisions and strategy routing.

Run with: python -m pytest experiments/smart_selector/tests/ -v
(or run this file directly - it has its own runner at the bottom so no
pytest dependency is required, since none is currently installed in the
R&D venv and Phase 2 must not add new dependencies without approval).
"""

import os
import sys

_HERE = os.path.dirname(__file__)
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

import file_analysis  # noqa: E402
import policy  # noqa: E402
import strategy_registry  # noqa: E402

_FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _FAILURES.append(name)


def test_jpeg_routing():
    strategies = strategy_registry.get_candidate_strategies("JPEG", "jpg", 500_000)
    check("jpeg routing includes jpeg_xl", "jpeg_xl_lossless_jpeg" in strategies)
    check("jpeg routing includes all 4 generic", all(
        s in strategies for s in ("generic_zstd", "generic_brotli", "generic_lzma", "generic_zlib")
    ))


def test_generic_routing_webp_avif_cheap_only():
    for fmt in ("WEBP", "AVIF"):
        strategies = strategy_registry.get_candidate_strategies(fmt, fmt.lower(), 10_000_000)
        check(f"{fmt} stays cheap-only even when large", strategies == ["generic_zstd"], strategies)


def test_generic_routing_png_size_tiered():
    small = strategy_registry.get_candidate_strategies("PNG", "png", 1000)
    large = strategy_registry.get_candidate_strategies("PNG", "png", 10_000_000)
    check("small PNG gets cheap-only", small == ["generic_zstd"], small)
    check("large PNG gets full generic set", set(large) == {"generic_zstd", "generic_brotli", "generic_lzma", "generic_zlib"}, large)


def test_unknown_type_fallback_routing():
    strategies = strategy_registry.get_candidate_strategies("BINARY_UNKNOWN", "dat", 5_000_000)
    check("unknown binary type gets cheap-only probe (not full/expensive set)", strategies == ["generic_zstd"], strategies)


def test_sha_mismatch_rejection():
    decision = policy.evaluate(
        original_size=1_000_000, baseline_size=900_000, candidate_size=500_000,
        compression_time_s=0.1, restore_time_s=0.05, sha256_match=False,
    )
    check("SHA mismatch always rejected regardless of size win", not decision.accepted and decision.reason == "SHA256_MISMATCH")


def test_candidate_larger_than_baseline_rejected():
    decision = policy.evaluate(
        original_size=1_000_000, baseline_size=900_000, candidate_size=950_000,
        compression_time_s=0.01, restore_time_s=0.01, sha256_match=True,
    )
    check("candidate >= baseline rejected", not decision.accepted and decision.reason == "NOT_SMALLER_THAN_BASELINE")


def test_small_file_negligible_benefit_fallback():
    # tiny file, tiny saving (well under both absolute and percent bars), fast
    decision = policy.evaluate(
        original_size=8_000, baseline_size=8_000, candidate_size=7_960,
        compression_time_s=0.001, restore_time_s=0.001, sha256_match=True,
    )
    check("small file / negligible benefit -> rejected", not decision.accepted and decision.reason == "BENEFIT_TOO_SMALL")


def test_large_file_small_percent_meaningful_bytes_accepted():
    # the user's explicit example: ~2GB file, ~1% saving, but ~20MB absolute, fast (zstd-speed)
    two_gb = 2 * 1024 * 1024 * 1024
    candidate = two_gb - 20 * 1024 * 1024  # saves 20 MiB
    decision = policy.evaluate(
        original_size=two_gb, baseline_size=two_gb, candidate_size=candidate,
        compression_time_s=5.0, restore_time_s=2.5, sha256_match=True,  # realistic zstd-class throughput
    )
    check(
        "large file + small % + meaningful MB saved -> ACCEPTED",
        decision.accepted and decision.saved_percent < 3.0 and decision.saved_bytes >= 1024 * 1024,
        (decision.reason, decision.saved_percent, decision.saved_bytes),
    )


def test_large_saving_accepted_even_if_slower():
    # 61% saving (matches Phase 1 BMP median) with modest throughput - should
    # be accepted via the large-benefit override even below the throughput floor
    decision = policy.evaluate(
        original_size=20_000_000, baseline_size=20_000_000, candidate_size=7_800_000,
        compression_time_s=15.0, restore_time_s=1.0, sha256_match=True,
    )
    check("large saving (61%) accepted despite modest throughput", decision.accepted, decision.reason)


def test_excessive_cost_rejected():
    # candidate IS smaller (5%) but throughput is pathological and time isn't small
    decision = policy.evaluate(
        original_size=50_000_000, baseline_size=50_000_000, candidate_size=47_500_000,
        compression_time_s=55.0, restore_time_s=5.0, sha256_match=True,
    )
    check(
        "smaller-but-excessively-slow candidate rejected",
        not decision.accepted and decision.reason in ("EXCEEDS_ABSOLUTE_TIMEOUT", "COST_NOT_JUSTIFIED_BY_BENEFIT"),
        decision.reason,
    )


def test_tiny_file_prefilter():
    check("tiny file (below 4KB) is pre-filtered from candidate attempts", not policy.should_attempt_candidates(2000))
    check("file at/above 4KB is attempted", policy.should_attempt_candidates(4096))


def test_type_detection_png_signature():
    import tempfile
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f:
        f.write(png_bytes)
        path = f.name
    try:
        detected, is_binary, mismatch = file_analysis.detect_format(path, "dat")
        check("PNG signature detected despite .dat extension", detected == "PNG")
        check("extension mismatch correctly flagged", mismatch is True)
    finally:
        os.remove(path)


def test_deterministic_routing():
    a = strategy_registry.get_candidate_strategies("JPEG", "jpg", 500_000)
    b = strategy_registry.get_candidate_strategies("JPEG", "jpg", 500_000)
    check("routing is deterministic across repeated calls", a == b)


def test_candidate_exception_fallback():
    import selector

    class ExplodingStrategy:
        name = "exploding_strategy"
        applicable_formats = frozenset({"*"})

        def is_available(self):
            return True, ""

        def applies_to(self, ext):
            return True

        def compress(self, input_path, work_dir):
            raise RuntimeError("simulated strategy crash")

        def restore(self, compressed_path, work_dir, original_filename):
            raise RuntimeError("unreachable")

    import harness
    result = harness.run_strategy_on_file(
        ExplodingStrategy(), __file__, os.path.join(_HERE, "..", "scratch"), "test_exception"
    )
    check("exploding strategy is caught as ERROR, not raised", result["status"] == "ERROR")


class SlowStrategy:
    """Module-level (not local) so multiprocessing spawn can pickle it."""
    name = "slow_strategy"

    def is_available(self):
        return True, ""

    def compress(self, input_path, work_dir):
        import time
        time.sleep(5)
        return None

    def restore(self, compressed_path, work_dir, original_filename):
        return None


def test_candidate_timeout_fallback():
    import selector

    t0 = __import__("time").time()
    result = selector._run_with_timeout(
        SlowStrategy(), __file__, os.path.join(_HERE, "..", "scratch"), "test_timeout", timeout_s=0.2
    )
    elapsed = __import__("time").time() - t0
    check("slow strategy is caught as TIMEOUT, selector does not block", result["status"] == "TIMEOUT")
    check("timeout actually bounded wall-clock time (real kill, not wait-only)", elapsed < 4.0, elapsed)


def test_selector_deterministic_result():
    import selector
    photos_root = r"C:\Users\Loaya\StorageArch_SmartCompression_RnD\photos"
    scratch = os.path.join(_HERE, "..", "scratch")
    sample = os.path.join(photos_root, "MANIFEST.csv")
    if os.path.isfile(sample):
        r1 = selector.select(sample, photos_root, scratch, "det_test_1")
        r2 = selector.select(sample, photos_root, scratch, "det_test_2")
        check(
            "selector produces the same selected strategy across repeated runs",
            r1["selected_strategy"] == r2["selected_strategy"] and r1["selected_bytes"] == r2["selected_bytes"],
            (r1["selected_strategy"], r2["selected_strategy"]),
        )
    else:
        check("selector determinism test (sample file present)", False, "sample file missing")


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
