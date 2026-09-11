"""Strategy interface every compression candidate implements.

Kept deliberately small: a strategy only needs to compress a file to some
stored representation and restore it back. The harness owns timing,
hashing, the pass/fail gate, and cleanup - strategies never touch those.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CompressResult:
    output_path: str       # the stored representation (one file; may itself be a container)
    stored_size: int        # bytes on disk for the stored representation


@dataclass
class RestoreResult:
    output_path: str       # reconstructed file, expected byte-identical to the original


class CompressionStrategy(ABC):
    #: short machine-friendly identifier, used as strategy_used in reports
    name = "unnamed"

    #: lowercase extensions (no dot) this strategy applies to, or {"*"} for all formats
    applicable_formats = frozenset({"*"})

    def applies_to(self, extension):
        extension = extension.lower().lstrip(".")
        return "*" in self.applicable_formats or extension in self.applicable_formats

    def is_available(self):
        """Return (available: bool, reason: str). reason is only meaningful when False."""
        return True, ""

    @abstractmethod
    def compress(self, input_path, work_dir):
        """Compress input_path into work_dir. Must not modify input_path. Returns CompressResult."""
        raise NotImplementedError

    @abstractmethod
    def restore(self, compressed_path, work_dir, original_filename):
        """Reconstruct the original file into work_dir. Returns RestoreResult."""
        raise NotImplementedError
