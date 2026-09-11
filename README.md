# SmartArchive

A desktop archiving tool that compresses files by choosing a compression strategy per file, based on its detected type and size, instead of applying one algorithm to everything.

## Features

- Archives folders/files into a custom container format, evaluating multiple compression candidates per file based on the file's detected type and size: zstd, LZMA, Brotli, zlib, plus a few format-aware strategies for images and Office documents.
- Every candidate is verified before being accepted: compressed, restored, and checked against the original file's SHA-256 hash. A candidate that doesn't restore to an exact match is discarded, and the file falls back to plain ZIP instead.
- The candidate with the smallest verified output is kept; if none pass, the file falls back to plain ZIP storage.
- SQLite-backed catalog of archived items, organized into categories.
- Optional removal of the original file after archiving, via Send2Trash (recoverable, not a permanent delete).
- Desktop GUI with categories, items, dashboard, and settings tabs; long-running archive operations run on background workers so the UI doesn't freeze.

### Under the hood

Compression candidates run in separate worker processes (so a stuck candidate
can be killed without crashing the app), pooled and reused across files, with
basic memory-aware scheduling to avoid running too many heavy candidates at
once.

## Tech Stack

- **Python**
- **PySide6 / Qt** — desktop GUI
- **SQLite** — archived-item catalog
- **zstandard**, **brotli**, **lzma** (stdlib), **zlib** (stdlib) — compression backends
- **Pillow**, **numpy** — used by the image-compression strategy experiments
- **Send2Trash** — recoverable file removal
- **pytest**, **pytest-qt** — test suite

## How to Run

From the project root, with dependencies from `requirements.txt` and `requirements-image-rnd.txt` installed:

```bash
python entrypoint/gui_final_entry.py
```

This launches the desktop GUI. It resolves all imports from `beta9_src/` (the entry point sets up `sys.path` itself — no extra configuration needed) and activates the `beta12_src` concurrency/scheduling overlay on startup.

To run the test suite:

```bash
pytest tests/
```

## Folder Structure

```
beta9_src/                       core engine
  app.py                         Qt application bootstrap
  main.py                        archive/category business logic
  archive_manager.py             low-level archive read/write
  archive_manager_smart.py       per-file strategy orchestration + verification
  smart_compression.py           candidate evaluation and decision policy
  storage_config.py              data/workspace location config
  database_manager.py            SQLite catalog
  gui/                           PySide6 UI (tabs, dialogs, background workers)
  experiments/
    smart_selector/src/          strategy registry, routing policy, resource scheduling
    images/src/                  compression strategy implementations
    video/src/                   video compression experiment
    text_filters/src/            text prefiltering strategy

beta12_src/                      concurrency + admission-control overlay
entrypoint/                      application entry points (GUI + headless)
tests/                           pytest suite
```
