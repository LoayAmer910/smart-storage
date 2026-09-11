"""Generates the deterministic synthetic test corpus for the image R&D harness.

Purpose: a fixed, reproducible set of edge cases (metadata-heavy files,
already-optimized files, tiny/large files, flat-color "screenshot" content,
noisy "photograph" content) that gives the same benchmark numbers on every
run - useful for regression, not a substitute for real-world files.

The real-world corpus (test_data/real_world/) is supplied by the user
separately and is never touched by this script.

Re-running this script regenerates the synthetic corpus from scratch
(same seed -> byte-identical output every time).
"""

import os

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

_SEED = 20260813

_HERE = os.path.dirname(__file__)
_SYNTHETIC_ROOT = os.path.join(os.path.dirname(_HERE), "test_data", "synthetic")


def _rng():
    return np.random.default_rng(_SEED)


def _gradient_noise_array(width, height, noise_strength, rng):
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    xv, yv = np.meshgrid(x, y)
    r = (xv * 180 + 40)
    g = (yv * 180 + 30)
    b = ((xv + yv) / 2 * 180 + 20)
    base = np.stack([r, g, b], axis=-1)
    noise = rng.normal(0, noise_strength, size=base.shape)
    pixels = np.clip(base + noise, 0, 255).astype(np.uint8)
    return pixels


def _flat_block_array(width, height, rng):
    palette = np.array([
        [245, 245, 245], [33, 150, 243], [255, 255, 255],
        [76, 175, 80], [244, 67, 54], [20, 20, 20],
    ], dtype=np.uint8)
    pixels = np.full((height, width, 3), palette[0], dtype=np.uint8)
    block_h, block_w = height // 6, width // 6
    for row in range(6):
        for col in range(6):
            color = palette[(row + col) % len(palette)]
            pixels[row * block_h:(row + 1) * block_h, col * block_w:(col + 1) * block_w] = color
    # a few thin dark "text" lines
    for line_y in range(10, height - 10, 40):
        pixels[line_y:line_y + 2, 20:width - 20] = [10, 10, 10]
    return pixels


def _save_png(pixels, path, pnginfo=None):
    Image.fromarray(pixels, mode="RGB").save(path, "PNG", optimize=False, pnginfo=pnginfo)


def _save_jpeg(pixels, path, quality, optimize=False, comment=None):
    kwargs = {"quality": quality, "optimize": optimize}
    if comment is not None:
        kwargs["comment"] = comment
    Image.fromarray(pixels, mode="RGB").save(path, "JPEG", **kwargs)


def _ensure_dir(name):
    path = os.path.join(_SYNTHETIC_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


def generate_all():
    rng = _rng()
    created = []

    # photographs/ - smooth gradient + noise, simulating photographic content
    photo_dir = _ensure_dir("photographs")
    photo_pixels_a = _gradient_noise_array(800, 600, noise_strength=12, rng=rng)
    photo_pixels_b = _gradient_noise_array(800, 600, noise_strength=35, rng=rng)
    _save_jpeg(photo_pixels_a, os.path.join(photo_dir, "photo_smooth_q95.jpg"), quality=95)
    _save_jpeg(photo_pixels_b, os.path.join(photo_dir, "photo_noisy_q75.jpg"), quality=75)
    _save_png(photo_pixels_a, os.path.join(photo_dir, "photo_smooth.png"))
    created += [
        os.path.join(photo_dir, "photo_smooth_q95.jpg"),
        os.path.join(photo_dir, "photo_noisy_q75.jpg"),
        os.path.join(photo_dir, "photo_smooth.png"),
    ]

    # screenshots/ - flat color blocks, simulating UI screenshots
    screenshot_dir = _ensure_dir("screenshots")
    screenshot_pixels = _flat_block_array(1024, 768, rng=rng)
    _save_png(screenshot_pixels, os.path.join(screenshot_dir, "screenshot_ui.png"))
    _save_jpeg(screenshot_pixels, os.path.join(screenshot_dir, "screenshot_ui.jpg"), quality=90)
    created += [
        os.path.join(screenshot_dir, "screenshot_ui.png"),
        os.path.join(screenshot_dir, "screenshot_ui.jpg"),
    ]

    # already_optimized/ - content already run through a decent encoder once
    opt_dir = _ensure_dir("already_optimized")
    opt_pixels = _gradient_noise_array(640, 480, noise_strength=8, rng=rng)
    _save_png(opt_pixels, os.path.join(opt_dir, "already_optimized.png"), pnginfo=None)
    _save_jpeg(opt_pixels, os.path.join(opt_dir, "already_optimized.jpg"), quality=90, optimize=True)
    created += [
        os.path.join(opt_dir, "already_optimized.png"),
        os.path.join(opt_dir, "already_optimized.jpg"),
    ]

    # small_files/ - tiny icon-sized images where overhead may exceed savings
    small_dir = _ensure_dir("small_files")
    for size in (16, 32):
        pixels = _gradient_noise_array(size, size, noise_strength=5, rng=rng)
        _save_png(pixels, os.path.join(small_dir, f"icon_{size}.png"))
        _save_jpeg(pixels, os.path.join(small_dir, f"icon_{size}.jpg"), quality=90)
        created += [
            os.path.join(small_dir, f"icon_{size}.png"),
            os.path.join(small_dir, f"icon_{size}.jpg"),
        ]

    # large_files/ - higher-entropy noise at larger resolution (R&D-scale, not production-scale)
    large_dir = _ensure_dir("large_files")
    large_pixels = _gradient_noise_array(1600, 1200, noise_strength=45, rng=rng)
    _save_png(large_pixels, os.path.join(large_dir, "large_noise.png"))
    _save_jpeg(large_pixels, os.path.join(large_dir, "large_noise.jpg"), quality=90)
    created += [
        os.path.join(large_dir, "large_noise.png"),
        os.path.join(large_dir, "large_noise.jpg"),
    ]

    # metadata_heavy/ - ancillary chunks / EXIF-comment-style metadata, a known
    # source of silent round-trip corruption in third-party re-encoders
    meta_dir = _ensure_dir("metadata_heavy")
    meta_pixels = _gradient_noise_array(400, 300, noise_strength=10, rng=rng)
    png_info = PngInfo()
    png_info.add_text("Title", "Synthetic Metadata Heavy Test Image")
    png_info.add_text("Author", "StorageArch Phase B R&D")
    png_info.add_text("Description", "Lorem ipsum " * 200)
    png_info.add_text("Software", "corpus_synthetic.py")
    png_info.add_text("Comment", "x" * 4000)
    _save_png(meta_pixels, os.path.join(meta_dir, "metadata_heavy.png"), pnginfo=png_info)
    _save_jpeg(
        meta_pixels, os.path.join(meta_dir, "metadata_heavy.jpg"), quality=90,
        comment=("synthetic metadata heavy jpeg comment block " * 100).encode("utf-8"),
    )
    created += [
        os.path.join(meta_dir, "metadata_heavy.png"),
        os.path.join(meta_dir, "metadata_heavy.jpg"),
    ]

    return created


if __name__ == "__main__":
    files = generate_all()
    print(f"Generated {len(files)} synthetic test files under {_SYNTHETIC_ROOT}")
    for f in files:
        print(" -", os.path.relpath(f, _SYNTHETIC_ROOT), f"({os.path.getsize(f)} bytes)")
