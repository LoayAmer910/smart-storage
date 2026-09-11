"""Strategy routing table: detected type + size -> candidate strategy names.

Encodes Phase 1's findings so the Smart Selector does not blindly probe
every strategy on every file:

- JPEG: JPEG XL clearly won 18/20 files in Phase 1 (13.94% byte-weighted
  improvement) - always worth the full probe.
- WebP/AVIF: Phase 1 showed tiny gains (0.13%, 0.18%) - only the cheap,
  fast generic_zstd is worth trying; brotli/lzma's extra cost is not
  justified by evidence.
- PNG/BMP/TIFF/GIF: generic probing showed real value (1-17% byte-weighted),
  but cost must scale with size - small files get the cheap probe only,
  larger files (where absolute savings matter more, per the user's
  large-PNG example) get the full generic set.
- Unknown/text/binary-unknown/other test_files content: baseline + a
  single cheap zstd probe. Compiled build artifacts (exe/dll/pdb/obj/ipch
  etc.) are typically already dense - no evidence justifies expensive
  probing here, so only the cheap candidate is tried.
"""

_CHEAP_ONLY = ["generic_zstd"]
_FULL_GENERIC = ["generic_zstd", "generic_brotli", "generic_lzma", "generic_zlib"]
_JPEG_FULL = ["generic_zstd", "generic_brotli", "generic_lzma", "generic_zlib", "jpeg_xl_lossless_jpeg"]

# Beta 9 runtime fix: brotli/lzma at quality 11/preset 9 measured directly
# against real large already-compressed files in this corpus and PROVEN
# unable to win, not just slow:
#   - real 41MB JPEG: brotli quality 11 took 69.2s (>2x the existing 30s
#     policy timeout) and produced 40,983,517 bytes - LARGER than the
#     40,983,389-byte original. Guaranteed timeout+respawn, zero chance of
#     being selected, every single time.
#   - real 147MB BMP (via the isolated single-file candidate-gauntlet
#     benchmark, beta9_research/logs/STAGE_GATE_RESULTS.md section C.1):
#     both brotli and lzma consistently exceeded their 30s cap.
# Above this size, JPEG/BMP skip straight to the candidates that can
# actually win (zstd/zlib/JXL) instead of paying a guaranteed 30s timeout
# + worker respawn for two candidates proven incapable of completing, let
# alone winning. Scoped ONLY to the two formats/paths actually measured -
# PNG/TIFF/GIF's _FULL_GENERIC is untouched pending real evidence for
# those formats specifically (per the Preservation Rule: no removal
# without proof for that exact path).
_LARGE_ALREADY_COMPRESSED_BYTES = 15 * 1024 * 1024  # 15MB - well under the smallest proven-losing size (41MB)
_JPEG_FULL_LARGE = ["generic_zstd", "generic_zlib", "jpeg_xl_lossless_jpeg"]

# Above this size, "full generic" formats still get all 4 generic
# candidates (large-file absolute savings can matter even at low
# percentages - the point of the large-PNG scenario), but formats
# with only marginal historical value (WEBP/AVIF) stay cheap-only
# regardless of size, since Phase 1 gave no evidence extra cost pays off.
_SIZE_TIER_BYTES = 32 * 1024  # 32 KiB - below this, container/format overhead dominated Phase 1 results

_ALWAYS_CHEAP_ONLY_TYPES = {"WEBP", "AVIF", "HEIC"}
_GENERIC_CANDIDATE_TYPES = {"PNG", "BMP", "TIFF", "GIF"}

# Beta 9 additions - see beta9_research/logs/STAGE_GATE_RESULTS.md for the
# real-dataset evidence behind each. Both are additive candidates: the
# existing compress->restore->verify->benefit/cost policy still picks the
# actual winner per file, these just widen what it gets to choose from.
_BMP_FULL = ["generic_zstd", "generic_brotli", "generic_lzma", "generic_zlib", "bmp_jxl_lossless"]
_BMP_FULL_LARGE = ["generic_zstd", "generic_zlib", "bmp_jxl_lossless"]  # see _LARGE_ALREADY_COMPRESSED_BYTES

# Scoped to .xlsx only (not .docx/.pptx/.zip) - the real corpus evidence in
# Stage Gate section A showed docx/pptx/zip in this dataset are dominated
# by already-compressed embedded media where preflate gives ~0% net
# benefit, while xlsx (text/XML-heavy) cleared the gate at +14.21%.
_OOXML_XLSX_CANDIDATES = ["generic_zstd", "preflate_zstd_ooxml"]

# Beta 13: video containers (mp4/mkv/mov/avi/webm/m4v/mpg/mpeg) have no
# magic-byte signature in file_analysis.py's _BINARY_SIGNATURES today, so
# they always classify as BINARY_UNKNOWN - routed here purely by extension,
# same pattern as the JPEG/xlsx extension checks above. video_optimizer is
# the only candidate tried: it is lossless-only (see video_optimizer.py),
# so there is no benefit to also probing generic_zstd/brotli/lzma against
# already-compressed video stream bytes - none of them are expected to win
# and the extra wall-clock cost isn't justified (same reasoning as
# _ALWAYS_CHEAP_ONLY_TYPES above, taken one step further since even the
# cheap zstd probe is unlikely to help here).
_VIDEO_EXTENSIONS = frozenset({"mp4", "mkv", "mov", "avi", "webm", "m4v", "mpg", "mpeg"})
_VIDEO_CANDIDATES = ["video_optimizer"]

# Beta 13: reversible text pre-filters - see text_prefilter.py.
#
# JSON/XML canonicalize-then-xdelta3-patch was measured directly against
# realistic sample files and consistently LOST to plain generic_zstd on
# the raw bytes: the whitespace/formatting these formats strip is exactly
# the kind of redundancy zstd already compresses for free, so removing it
# first only forces paying an xdelta3 patch to put it back, for zero net
# gain (real measurement: JSON candidate_size 62,648B vs generic_zstd's
# 28,965B on the same 848KB file - 2.2x WORSE). json_prefilter/
# xml_prefilter stay registered in selector.py (available, harmless) but
# are deliberately NOT routed here - do not re-add them without new
# evidence they can beat generic_zstd on real files.
#
# CSV was also tried with a smarter reversibility mechanism (an explicit
# integer permutation array instead of an xdelta3 binary diff, since a
# full row reorder breaks binary diffing's locality assumption - see
# text_prefilter.make_csv_reversible_bundle) specifically to sidestep the
# JSON/XML failure mode above. Still lost on real measurement: sorting did
# shrink the compressed row data (42,817B vs 45,674B raw, ~6% better), but
# a uniformly-random-content permutation of 5000 rows costs ~11KB even
# zstd-compressed (near the ~N*log2(N) bit entropy floor for an arbitrary
# shuffle) - more than double what sorting saved. Total: 54,136B, 18.5%
# WORSE than plain generic_zstd on the same file. Byte-exact restore was
# confirmed working correctly; the mechanism is sound, the economics
# aren't. This would only plausibly win on data with heavy duplicate/
# near-duplicate rows (sorting gain >> permutation's log(N!) cost) or data
# that's already mostly-sorted (low-entropy permutation) - not verified
# here, so not routed. csv_prefilter stays registered (harmless) but
# unused, same as json_prefilter/xml_prefilter above.
_TEXT_PREFILTER_CANDIDATES = {}


def get_candidate_strategies(detected_type, extension, size_bytes):
    """Returns the ordered list of non-baseline strategy names to attempt."""
    if extension in _VIDEO_EXTENSIONS:
        return list(_VIDEO_CANDIDATES)

    if extension in _TEXT_PREFILTER_CANDIDATES:
        return list(_TEXT_PREFILTER_CANDIDATES[extension])

    if detected_type == "JPEG" or (detected_type == "BINARY_UNKNOWN" and extension in ("jpg", "jpeg")):
        if size_bytes >= _LARGE_ALREADY_COMPRESSED_BYTES:
            return list(_JPEG_FULL_LARGE)
        return list(_JPEG_FULL)

    if detected_type in _ALWAYS_CHEAP_ONLY_TYPES:
        return list(_CHEAP_ONLY)

    if detected_type == "BMP":
        if size_bytes >= _LARGE_ALREADY_COMPRESSED_BYTES:
            return list(_BMP_FULL_LARGE)
        return list(_BMP_FULL) if size_bytes >= _SIZE_TIER_BYTES else list(_CHEAP_ONLY)

    if detected_type in _GENERIC_CANDIDATE_TYPES:
        return list(_FULL_GENERIC) if size_bytes >= _SIZE_TIER_BYTES else list(_CHEAP_ONLY)

    if detected_type == "OOXML_ZIP" and extension == "xlsx":
        return list(_OOXML_XLSX_CANDIDATES)

    # Everything else (test_files corpus: OOXML/PDF/SQLITE/ZIP/text/source/
    # compiled artifacts/unknown): one cheap probe only. No format-aware
    # strategy exists for these, and Phase 1 established no evidence base
    # to justify expensive multi-candidate probing on arbitrary binaries.
    return list(_CHEAP_ONLY)
