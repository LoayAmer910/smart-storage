"""Generic whole-file byte-exact compressors.

These treat the image file as an opaque blob and run it through a
general-purpose lossless compressor. There is no image-specific
transform, so exact reversibility holds by construction - decompression
is the exact mathematical inverse of compression for all four codecs
below. This makes them the safe, low-risk baseline improvement over
ZIP/DEFLATE, especially for PNG where no image-aware exact method is
proven yet.

zlib and lzma are stdlib (always available). zstandard and brotli are
optional third-party libraries - if either failed to install in this
environment, that strategy reports itself unavailable rather than
crashing the benchmark.
"""

import lzma
import os
import zlib

from strategies.base import CompressionStrategy, CompressResult, RestoreResult

try:
    import zstandard
except ImportError:
    zstandard = None

try:
    import brotli
except ImportError:
    brotli = None


def _write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class _GenericByteStrategy(CompressionStrategy):
    """Shared compress/restore plumbing; subclasses only supply codec + extension."""

    applicable_formats = frozenset({"*"})
    extension = ".bin"

    def _encode(self, data):
        raise NotImplementedError

    def _decode(self, data):
        raise NotImplementedError

    def compress(self, input_path, work_dir):
        arcname = os.path.basename(input_path)
        out_path = os.path.join(work_dir, arcname + self.extension)
        original_bytes = _read_bytes(input_path)
        encoded = self._encode(original_bytes)
        _write_bytes(out_path, encoded)
        return CompressResult(output_path=out_path, stored_size=os.path.getsize(out_path))

    def restore(self, compressed_path, work_dir, original_filename):
        out_path = os.path.join(work_dir, "restored_" + original_filename)
        encoded = _read_bytes(compressed_path)
        decoded = self._decode(encoded)
        _write_bytes(out_path, decoded)
        return RestoreResult(output_path=out_path)


_LARGE_FILE_PRESET_TIER_BYTES = 15 * 1024 * 1024  # 15MB, matches strategy_registry.py


class GenericLzmaStrategy(_GenericByteStrategy):
    name = "generic_lzma"
    extension = ".xz"

    def _encode(self, data):
        preset = 6 if len(data) >= _LARGE_FILE_PRESET_TIER_BYTES else 9
        return lzma.compress(data, preset=preset)

    def _decode(self, data):
        return lzma.decompress(data)


class GenericZlibStrategy(_GenericByteStrategy):
    name = "generic_zlib"
    extension = ".zlib"

    def _encode(self, data):
        return zlib.compress(data, level=9)

    def _decode(self, data):
        return zlib.decompress(data)


class GenericZstdStrategy(_GenericByteStrategy):
    name = "generic_zstd"
    extension = ".zst"

    def is_available(self):
        if zstandard is None:
            return False, "zstandard package not installed in this environment"
        return True, ""

    def _encode(self, data):
        level = 15 if len(data) >= _LARGE_FILE_PRESET_TIER_BYTES else 19
        return zstandard.ZstdCompressor(level=level).compress(data)

    def _decode(self, data):
        return zstandard.ZstdDecompressor().decompress(data)


class GenericBrotliStrategy(_GenericByteStrategy):
    name = "generic_brotli"
    extension = ".br"

    def is_available(self):
        if brotli is None:
            return False, "brotli package not installed in this environment"
        return True, ""

    def _encode(self, data):
        return brotli.compress(data, quality=11)

    def _decode(self, data):
        return brotli.decompress(data)
