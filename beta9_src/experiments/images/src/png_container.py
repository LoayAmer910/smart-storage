"""Pure-stdlib PNG chunk parsing/rebuilding - no external PNG library used,
specifically so we control every byte and can guarantee exact reconstruction.

PNG structure: 8-byte signature, then a sequence of chunks
(4-byte length, 4-byte type, `length` bytes of data, 4-byte CRC32).
All IDAT chunks in a valid PNG are required to be contiguous, so a file
decomposes cleanly into: prefix chunks, the IDAT run, suffix chunks
(almost always just IEND, but ancillary chunks after IDAT are legal).
"""

import struct
import zlib

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


class NotAPngError(ValueError):
    pass


def _read_chunks(data):
    if not data.startswith(PNG_SIGNATURE):
        raise NotAPngError("missing PNG signature")
    chunks = []
    pos = len(PNG_SIGNATURE)
    while pos < len(data):
        if pos + 8 > len(data):
            raise NotAPngError("truncated chunk header")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        crc_offset = pos + 8 + length
        crc = data[crc_offset:crc_offset + 4]
        chunks.append((chunk_type, chunk_data, crc))
        pos = crc_offset + 4
    return chunks


def _encode_chunk(chunk_type, chunk_data):
    length = struct.pack(">I", len(chunk_data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
    return length + chunk_type + chunk_data + crc


def decompose(file_path):
    """Split a PNG file into (prefix_chunks, idat_chunk_lengths, idat_concat, suffix_chunks, idat_raw).

    prefix_chunks / suffix_chunks: list of (type, data) tuples, verbatim.
    idat_chunk_lengths: original per-chunk data lengths of the IDAT run, needed
    to re-split the concatenated stream back into byte-identical chunk boundaries.
    idat_concat: all IDAT chunk data concatenated, still zlib-compressed.
    idat_raw: idat_concat decompressed - the raw filtered scanline bytes.
    """
    with open(file_path, "rb") as f:
        data = f.read()

    chunks = _read_chunks(data)

    first_idat = next((i for i, c in enumerate(chunks) if c[0] == b"IDAT"), None)
    if first_idat is None:
        raise NotAPngError("no IDAT chunk found")
    last_idat = max(i for i, c in enumerate(chunks) if c[0] == b"IDAT")

    prefix_chunks = [(c[0], c[1]) for c in chunks[:first_idat]]
    idat_chunks = chunks[first_idat:last_idat + 1]
    if any(c[0] != b"IDAT" for c in idat_chunks):
        raise NotAPngError("IDAT chunks are not contiguous - unsupported by this prototype")
    suffix_chunks = [(c[0], c[1]) for c in chunks[last_idat + 1:]]

    idat_chunk_lengths = [len(c[1]) for c in idat_chunks]
    idat_concat = b"".join(c[1] for c in idat_chunks)
    idat_raw = zlib.decompress(idat_concat)

    return prefix_chunks, idat_chunk_lengths, idat_concat, suffix_chunks, idat_raw


def rebuild(prefix_chunks, idat_chunk_lengths, idat_concat, suffix_chunks):
    """Reassemble the exact original PNG file bytes from decomposed parts."""
    out = bytearray(PNG_SIGNATURE)
    for chunk_type, chunk_data in prefix_chunks:
        out += _encode_chunk(chunk_type, chunk_data)

    offset = 0
    for chunk_len in idat_chunk_lengths:
        out += _encode_chunk(b"IDAT", idat_concat[offset:offset + chunk_len])
        offset += chunk_len

    for chunk_type, chunk_data in suffix_chunks:
        out += _encode_chunk(chunk_type, chunk_data)

    return bytes(out)
