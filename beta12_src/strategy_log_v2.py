"""Beta12 Priority: strategy_log_v2 - extends beta11_src.strategy_log_v1
with two new per-decision fields this round's spec requires: the
heavy_allowed state and free_cores reading AT THE MOMENT that file's
admission decision was made. Everything else (candidates tested,
compressed sizes, selected strategy, total_saved_bytes/baseline_selected_count
aggregation) is reused UNCHANGED from v1 by subclassing, not copying.
"""

from beta11_src.strategy_log_v1 import StrategyLog as _StrategyLogV1


class StrategyLogV2(_StrategyLogV1):
    def record_decision(self, file_path, original_size, selected_strategy, selected_size,
                         heavy_allowed=None, free_cores=None):
        super().record_decision(file_path, original_size, selected_strategy, selected_size)
        with self._lock:
            for rec in reversed(self._records):
                if rec["file"] == file_path and rec.get("selected_strategy") == selected_strategy \
                        and "heavy_allowed" not in rec:
                    rec["heavy_allowed"] = heavy_allowed
                    rec["free_cores"] = free_cores
                    return

    def record_candidates(self, file_path, candidates, heavy_allowed=None, free_cores=None):
        super().record_candidates(file_path, candidates)
        with self._lock:
            for rec in reversed(self._records):
                if rec["file"] == file_path and "heavy_allowed" not in rec:
                    rec["heavy_allowed"] = heavy_allowed
                    rec["free_cores"] = free_cores
                    return
