# OmniDL Library Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a review-first music library manager that scans existing and newly downloaded audio, reports quality and metadata problems, identifies duplicates, offers safe tag repair, and prevents cross-format duplicate downloads.

**Architecture:** A focused `app/library.py` module owns file inspection, identity normalization, quality ranking, duplicate grouping, and safe missing-tag repair. FastAPI exposes local-only library endpoints, the dashboard renders a library modal, and Spotify playlist jobs reuse a prebuilt library index when `skip_existing` is enabled.

**Tech Stack:** Python 3.14, FastAPI, Mutagen, vanilla JavaScript, HTML/CSS, unittest.

## Global Constraints

- Scan only the configured OmniDL output directory.
- Never delete, rename, move, overwrite, or transcode an existing file automatically.
- Safe repair may only fill missing artist/title values derived from an unambiguous `Artist - Title` filename.
- Reject paths that escape the configured library root.
- Library APIs remain unavailable in hosted mode.

---

### Task 1: Library scanner and duplicate analysis

**Files:**
- Create: `app/library.py`
- Create: `tests/test_library.py`

**Interfaces:**
- Produces: `scan_library(root: Path) -> dict`
- Produces: `build_library_index(root: Path) -> LibraryIndex`
- Produces: `LibraryIndex.find(artist: str, title: str, duration: int = 0) -> LibraryTrack | None`

- [ ] Write tests using temporary audio files and patched Mutagen inspection to verify recursive discovery, missing metadata issues, quality ordering, duplicate grouping, and duration-aware lookup.
- [ ] Run `python -m unittest tests.test_library -v` and confirm failures are caused by the missing module.
- [ ] Implement immutable track records, normalization, quality ranking, summary generation, and the index lookup.
- [ ] Run `python -m unittest tests.test_library -v` and confirm all scanner tests pass.

### Task 2: Safe missing-tag repair

**Files:**
- Modify: `app/library.py`
- Modify: `tests/test_library.py`

**Interfaces:**
- Produces: `repair_missing_tags(root: Path, relative_path: str) -> dict`

- [ ] Write tests proving repair fills only absent artist/title fields, preserves existing values, rejects ambiguous filenames, and rejects path traversal.
- [ ] Run the focused tests and confirm the new cases fail before implementation.
- [ ] Implement root containment validation and Mutagen easy-tag persistence.
- [ ] Run the focused and complete library tests.

### Task 3: Local-only library API

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_library_api.py`

**Interfaces:**
- Produces: `POST /api/library/scan`
- Produces: `POST /api/library/repair` with `{ "path": "relative/file.mp3" }`

- [ ] Write API tests for scan success, hosted-mode rejection, repair validation, and safe error responses.
- [ ] Run the API tests and confirm route-not-found failures.
- [ ] Add asynchronous `to_thread` calls around scanner and repair operations.
- [ ] Run API tests and the full suite.

### Task 4: Library dashboard

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/js/app.js`
- Modify: `app/static/css/style.css`

**Interfaces:**
- Consumes: scan report `summary`, `tracks`, and `duplicate_groups`.
- Consumes: repair response and refreshes the report after a successful repair.

- [ ] Add a Library top-bar button and responsive modal with scan progress, summary cards, metadata issue rows, duplicate comparison cards, quality labels, and safe repair buttons.
- [ ] Render all API values with DOM text nodes rather than HTML interpolation.
- [ ] Add empty, loading, error, and unsupported-hosted states.
- [ ] Verify the dashboard serves updated static assets and remains usable at narrow widths.

### Task 5: Cross-format duplicate-aware downloads

**Files:**
- Modify: `app/jobs.py`
- Modify: `tests/test_spotify_job.py`

**Interfaces:**
- Consumes: `LibraryIndex.find(...)`.
- Preserves: exact target-path skip behavior.

- [ ] Write a Spotify job test where an `.mp3` library copy prevents a requested `.opus` download for the same artist/title/duration.
- [ ] Run the focused job test and confirm it fails by attempting a source search.
- [ ] Build one library index per playlist job when skip-existing is enabled and pass it to track workers.
- [ ] Emit a clear cross-format skip reason containing the existing relative path.
- [ ] Run job tests and the full suite.

### Task 6: Verification and documentation

**Files:**
- Modify: `README.md`

- [ ] Document library scanning, diagnostics, safe repair, duplicate recommendations, and cross-format skip behavior.
- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Run `python -m py_compile app\library.py app\main.py app\jobs.py`.
- [ ] Restart with `python run.py --no-browser` and verify `/api/meta` and `/api/library/scan`.
