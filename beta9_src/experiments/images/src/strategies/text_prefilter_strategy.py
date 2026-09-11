"""Text pre-filter strategy adapters (JSON/CSV/XML) - wire
text_prefilter.py's canonicalize+zstd+xdelta3 engine into the Smart
Compression pipeline's CompressionStrategy interface. Same thin-adapter
shape as video_optimizer_strategy.py.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))
_TEXT_FILTERS_SRC = os.path.join(_RND_ROOT, "experiments", "text_filters", "src")
if _TEXT_FILTERS_SRC not in sys.path:
    sys.path.insert(0, _TEXT_FILTERS_SRC)

import text_prefilter  # noqa: E402
from strategies.base import CompressionStrategy, CompressResult, RestoreResult  # noqa: E402


class _TextPrefilterStrategyBase(CompressionStrategy):
    def is_available(self):
        return text_prefilter.is_available()

    def compress(self, input_path, work_dir):
        result = text_prefilter.make_reversible_bundle(input_path, work_dir)
        if not result["success"]:
            # Mirrors every other strategy's contract (see
            # video_optimizer_strategy.py): a candidate that cannot
            # usefully run raises here, treated as a failed candidate by
            # the harness/worker (falls back to baseline), never a crash
            # that could abort the whole archive operation.
            raise RuntimeError(f"{self.name}: {result['reason']}")
        bundle_path = result["bundle_path"]
        return CompressResult(output_path=bundle_path, stored_size=os.path.getsize(bundle_path))

    def restore(self, compressed_path, work_dir, original_filename):
        result = text_prefilter.restore_from_bundle(compressed_path, work_dir)
        if not result["success"]:
            raise RuntimeError(f"{self.name} restore: {result['reason']}")
        final_path = os.path.join(work_dir, "restored_" + original_filename)
        os.replace(result["output_path"], final_path)
        return RestoreResult(output_path=final_path)


class JsonPrefilterStrategy(_TextPrefilterStrategyBase):
    name = "json_prefilter"
    applicable_formats = frozenset({"json"})


class CsvPrefilterStrategy(CompressionStrategy):
    # Uses its own permutation-array bundle format (make_csv_reversible_
    # bundle/restore_csv_reversible_bundle), not the xdelta3-based one the
    # base class's compress()/restore() call - see text_prefilter.py's
    # module docstring for why CSV needs a different reversibility
    # mechanism than JSON/XML. No xdelta3 dependency at all, only zstandard
    # (a hard top-level import in text_prefilter.py, so its absence would
    # already have failed that module's own import).
    name = "csv_prefilter"
    applicable_formats = frozenset({"csv"})

    def is_available(self):
        return True, ""

    def compress(self, input_path, work_dir):
        result = text_prefilter.make_csv_reversible_bundle(input_path, work_dir)
        if not result["success"]:
            raise RuntimeError(f"{self.name}: {result['reason']}")
        bundle_path = result["bundle_path"]
        return CompressResult(output_path=bundle_path, stored_size=os.path.getsize(bundle_path))

    def restore(self, compressed_path, work_dir, original_filename):
        result = text_prefilter.restore_csv_reversible_bundle(compressed_path, work_dir)
        if not result["success"]:
            raise RuntimeError(f"{self.name} restore: {result['reason']}")
        final_path = os.path.join(work_dir, "restored_" + original_filename)
        os.replace(result["output_path"], final_path)
        return RestoreResult(output_path=final_path)


class XmlPrefilterStrategy(_TextPrefilterStrategyBase):
    name = "xml_prefilter"
    applicable_formats = frozenset({"xml"})
