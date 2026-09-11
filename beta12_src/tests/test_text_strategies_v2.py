import hashlib
import os
import tempfile

from beta12_src.text_strategies_v2 import ZstdLongRangeFastStrategy


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def test_available():
    available, _reason = ZstdLongRangeFastStrategy().is_available()
    assert available is True


def test_level_is_12_not_19():
    assert ZstdLongRangeFastStrategy.LEVEL == 12
    assert ZstdLongRangeFastStrategy.WINDOW_LOG == 27


def test_exact_roundtrip_on_repetitive_text():
    strategy = ZstdLongRangeFastStrategy()
    data = b"the quick brown fox jumps over the lazy dog\n" * 20000
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "sample.csv")
        with open(input_path, "wb") as f:
            f.write(data)
        original_hash = _sha256_file(input_path)

        compress_result = strategy.compress(input_path, tmp_dir)
        restore_result = strategy.restore(compress_result.output_path, tmp_dir, "sample.csv")
        restored_hash = _sha256_file(restore_result.output_path)

        assert original_hash == restored_hash
        assert compress_result.stored_size < len(data)


def test_exact_roundtrip_on_random_bytes():
    strategy = ZstdLongRangeFastStrategy()
    data = os.urandom(50_000)
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "sample.bin")
        with open(input_path, "wb") as f:
            f.write(data)
        original_hash = _sha256_file(input_path)
        compress_result = strategy.compress(input_path, tmp_dir)
        restore_result = strategy.restore(compress_result.output_path, tmp_dir, "sample.bin")
        assert original_hash == _sha256_file(restore_result.output_path)
