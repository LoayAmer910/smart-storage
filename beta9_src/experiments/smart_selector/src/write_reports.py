"""Writes PHASE2_SMART_SELECTOR_REPORT.md and SMART_SELECTOR_STATUS.md from
the already-computed phase2_overview.json (produced by aggregate_phase2.py)."""

import json
import os

_HERE = os.path.dirname(__file__)
_PHASE2_ROOT = os.path.dirname(_HERE)
_RND_ROOT = os.path.dirname(os.path.dirname(_PHASE2_ROOT))
_RESULTS_DIR = os.path.join(_PHASE2_ROOT, "results")


def main():
    with open(os.path.join(_RESULTS_DIR, "phase2_overview.json"), encoding="utf-8") as f:
        data = json.load(f)
    overview, per_type, special = data["overview"], data["per_type"], data["special"]

    lines = ["# Phase 2 Smart Selector Report", "", "## Overview", ""]
    for k, v in overview.items():
        lines.append(f"- **{k}**: {v}")

    lines += ["", "## Per-type results", "", "| Type | Files | Exact | Original | Current StorageArch | Smart | Improvement | Bytes Saved | Fallback | Wins |", "|---|---|---|---|---|---|---|---|---|---|"]
    for t, s in sorted(per_type.items(), key=lambda kv: -kv[1]["total_original_bytes"]):
        wins = ", ".join(f"{k}={v}" for k, v in sorted(s["strategy_win_distribution"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"| {t} | {s['files_tested']} | {s['exact_passes']}/{s['exact_attempts']} | "
            f"{s['total_original_bytes']} | {s['total_current_storagearch_bytes']} | {s['total_smart_bytes']} | "
            f"{s['weighted_improvement_pct']}% | {s['actual_bytes_saved']} | {s['fallback_count']} | {wins} |"
        )

    lines += ["", "## Special cost/benefit report", ""]
    lines += ["### Large files where Smart compression clearly helped (>=10% saved)", ""]
    for x in special["large_files_that_benefited"]:
        lines.append(f"- `{x['relpath']}`: {x['original_bytes']} bytes, saved {x['saved_bytes']} bytes ({x['saved_percent']}%)")
    if not special["large_files_that_benefited"]:
        lines.append("- (none in this corpus)")

    lines += ["", "### Small percentage but meaningful absolute bytes saved", ""]
    for x in special["small_percent_but_meaningful_bytes"]:
        lines.append(f"- `{x['relpath']}`: {x['original_bytes']} bytes, saved {x['saved_bytes']} bytes ({x['saved_percent']}%)")
    if not special["small_percent_but_meaningful_bytes"]:
        lines.append("- (none in this corpus at the >1MB / >100KB-saved thresholds used)")

    lines += ["", "### Candidates rejected because processing cost was not justified by benefit", ""]
    for x in special["rejected_for_cost_not_justified"]:
        lines.append(f"- `{x['relpath']}` / {x['strategy']}: would store {x['stored_bytes']} bytes ({x['saved_percent']}% saved) but took {x['time_s']:.3f}s")
    if not special["rejected_for_cost_not_justified"]:
        lines.append("- (none - no candidate in this corpus hit this rejection path)")

    lines += ["", f"### Intentional baseline fallback count: {special['intentional_baseline_count']}", ""]

    sim = special["simulated_large_png_scenario"]
    lines += [
        "### Simulated large-PNG scenario (no real 2GB+ file exists in this corpus)", "",
        f"{sim['description']}", "",
        f"- Original: {sim['original_bytes']} bytes",
        f"- Candidate: {sim['candidate_bytes']} bytes",
        f"- Saved: {sim['saved_bytes']} bytes ({sim['saved_percent']}%)",
        f"- Policy decision: **{sim['decision']}** (accepted={sim['accepted']})",
    ]

    with open(os.path.join(_RESULTS_DIR, "PHASE2_SMART_SELECTOR_REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # concise status file for future sessions
    status_lines = [
        "# Smart Selector — Phase 2 Status", "",
        f"Mixed corpus: {overview['files_tested']} files tested, {overview['files_skipped']} skipped "
        f"({overview['skip_reasons']}), run took {overview['run_elapsed_s']:.1f}s.",
        "",
        f"- Total original: {overview['total_original_bytes']} bytes",
        f"- Total current StorageArch: {overview['total_current_storagearch_bytes']} bytes",
        f"- Total Smart selected: {overview['total_smart_bytes']} bytes",
        f"- Bytes saved by Smart vs. current StorageArch: {overview['total_bytes_saved_by_smart']} "
        f"({overview['overall_improvement_pct']}%)",
        f"- Hash mismatches anywhere: {overview['hash_mismatches_anywhere']} (must be 0)",
        f"- Any selected result exceeds baseline: {overview['any_selected_result_exceeds_baseline']} (must be 0)",
        f"- Smart selections: {overview['smart_selection_count']}, fallbacks: {overview['fallback_total_count']}",
        "",
        "Video files: none exist anywhere in photos/ (confirmed by recursive signature scan across all "
        "2562 unique + 60 duplicate + 41 invalid files) - Phase 2's video-exclusion rule had nothing to exclude.",
        "",
        "Known issue observed during the run: `generic_zstd`/`generic_brotli` at current settings can take "
        "30s+ (hitting the real-kill cap) on specific ~40MB Visual Studio `.ipch` precompiled-header files - "
        "13 such timeouts occurred, all cleanly caught and falled back, zero correctness impact. Worth tuning "
        "compression level/size threshold for this file class before Phase 3.",
        "",
        "## Architecture (for Phase 3)",
        "",
        "- `experiments/smart_selector/src/file_analysis.py` — signature-based real type detection",
        "- `experiments/smart_selector/src/strategy_registry.py` — type+size -> candidate strategy routing",
        "- `experiments/smart_selector/src/policy.py` — configurable benefit/cost decision (PolicyConfig dataclass)",
        "- `experiments/smart_selector/src/selector.py` — orchestrator; each candidate runs in a spawned child "
        "process (multiprocessing) with a genuine terminate()/kill() on timeout, not a thread-based wait-only timeout",
        "",
        "## Recommended strategies for Phase 3",
        "",
        "- `baseline_storagearch_zip` (always, mandatory fallback)",
        "- `jpeg_xl_lossless_jpeg` for JPEG/JPG (clear, consistent win)",
        "- `generic_zstd` as the default cheap probe for everything else",
        "- `generic_brotli`/`generic_lzma`/`generic_zlib` only for PNG/BMP/TIFF/GIF above ~32KB (size-tiered)",
        "",
        "This file is a summary only — see `experiments/smart_selector/results/PHASE2_SMART_SELECTOR_REPORT.md` for full detail.",
    ]
    with open(os.path.join(_RND_ROOT, "SMART_SELECTOR_STATUS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(status_lines) + "\n")


if __name__ == "__main__":
    main()
