# Strict Source Matching and Review Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Download a Spotify playlist track only when an external YouTube Music, YouTube, or SoundCloud candidate is a strict match, and generate a review report for every track not downloaded.

**Architecture:** Keep matching, provider discovery, and report rendering in small pure-Python modules. `JobManager` coordinates them during Spotify jobs, uses the existing yt-dlp subprocess path only for approved URLs, and carries structured per-track outcomes into the final report.

**Tech Stack:** Python 3.14, FastAPI, asyncio, `ytmusicapi`, installed `yt-dlp` Python module/CLI, `unittest`, HTML generated with the standard library.

---

## File structure

- Create: `app/matching.py` â€” `Candidate`, `MatchDecision`, text normalization, scoring, and strict acceptance rules.
- Create: `app/candidate_search.py` â€” converts YT Music and yt-dlp search results into `Candidate` records. Network calls remain isolated here.
- Create: `app/review_report.py` â€” `TrackOutcome`, service search-link catalogue, and safe HTML report generation.
- Modify: `app/ytmusic_match.py` â€” expose a list of raw official-song candidates instead of making an untestable final selection internally.
- Modify: `app/engines.py` â€” create a yt-dlp command for any verified media URL, including a SoundCloud URL, while keeping the Spotify-derived output filename.
- Modify: `app/jobs.py` â€” replace permissive fallback downloads with candidate search, strict selection, structured outcomes, and report emission.
- Create: `tests/test_matching.py` â€” pure scoring and acceptance tests.
- Create: `tests/test_candidate_search.py` â€” conversion tests with injected provider output; no live network calls.
- Create: `tests/test_review_report.py` â€” report links, escaping, and output-content tests.
- Create: `tests/test_spotify_job.py` â€” job orchestration tests proving rejected candidates never start a download and a report path is emitted.
- Modify: `README.md` â€” document strict Spotify matching and review reports.

There is no Git repository at `omnidl`, so do not add commit commands. Preserve unrelated user changes.

### Task 1: Implement deterministic candidate matching

**Files:**
- Create: `app/matching.py`
- Test: `tests/test_matching.py`

- [ ] **Step 1: Write the failing matching tests**

```python
from app.matching import Candidate, decide_match

def candidate(**changes):
    data = dict(source="youtube", url="https://example.test/a", title="Midnight Run",
                artist="Nova", duration=181, official=False)
    data.update(changes)
    return Candidate(**data)

def test_accepts_exact_official_candidate():
    decision = decide_match("Nova", "Midnight Run", 180,
                             candidate(source="youtube_music", official=True, duration=182))
    assert decision.accepted is True
    assert decision.score == 100

def test_rejects_wrong_artist_even_when_duration_matches():
    decision = decide_match("Nova", "Midnight Run", 180,
                             candidate(artist="Different Artist", duration=180))
    assert decision.accepted is False
    assert decision.reason == "artist similarity below 80%"

def test_rejects_duration_over_twenty_seconds():
    decision = decide_match("Nova", "Midnight Run", 180, candidate(duration=205))
    assert decision.accepted is False
    assert decision.reason == "duration differs by more than 20 seconds"

def test_unknown_duration_is_rejected_for_review():
    decision = decide_match("Nova", "Midnight Run", 0,
                             candidate(source="youtube_music", official=True, duration=180))
    assert decision.accepted is False
    assert decision.reason == "match score below 90"
```

- [ ] **Step 2: Run the new tests and verify the expected failure**

Run: `python -m unittest tests.test_matching -v`

Expected: import failure because `app.matching` does not exist yet.

- [ ] **Step 3: Add the smallest scoring implementation**

Create `app/matching.py` with these public types and behavior:

```python
@dataclass(frozen=True)
class Candidate:
    source: str
    url: str
    title: str
    artist: str
    duration: int = 0
    official: bool = False

@dataclass(frozen=True)
class MatchDecision:
    candidate: Candidate
    title_similarity: float
    artist_similarity: float
    duration_difference: int | None
    score: int
    accepted: bool
    reason: str

def decide_match(artist: str, title: str, duration: int, candidate: Candidate) -> MatchDecision:
    title_similarity = _similarity(title, candidate.title)
    artist_similarity = _similarity(artist, candidate.artist)
    duration_difference = abs(duration - candidate.duration) if duration and candidate.duration else None
    score = _score(title_similarity, artist_similarity, duration_difference, candidate.official)
    reason = _rejection_reason(title_similarity, artist_similarity, duration_difference, score)
    return MatchDecision(candidate, title_similarity, artist_similarity, duration_difference,
                         score, reason == "verified match", reason)
```

Normalize with Unicode case-folding, punctuation removal, whitespace collapse, and
`difflib.SequenceMatcher`. Score title with 45 points, artist with 35, duration
with 20/15/8/0 at <=5/<=10/<=20/>20 seconds, then add a capped 5-point bonus for
an official YouTube Music song. Reject in this order: empty candidate metadata,
title similarity <80%, artist similarity <80%, known duration difference >20,
known-duration score <85, then reject unknown-duration candidates for review. Use the rejected rule
as the exact `reason` string so reports and tests remain stable.

- [ ] **Step 4: Run the matching tests and verify they pass**

Run: `python -m unittest tests.test_matching -v`

Expected: 4 tests pass.

### Task 2: Search providers without coupling scoring to network calls

**Files:**
- Modify: `app/ytmusic_match.py`
- Create: `app/candidate_search.py`
- Test: `tests/test_candidate_search.py`

- [ ] **Step 1: Write failing candidate-conversion tests**

```python
from app.candidate_search import candidates_from_ytdlp, candidates_from_ytmusic

def test_converts_youtube_music_song_to_official_candidate():
    items = [{"videoId": "abc", "title": "Midnight Run", "artists": [{"name": "Nova"}],
              "duration_seconds": 180}]
    result = candidates_from_ytmusic(items)
    assert result[0].source == "youtube_music"
    assert result[0].official is True
    assert result[0].url == "https://www.youtube.com/watch?v=abc"

def test_converts_soundcloud_flat_search_entry_to_candidate():
    items = [{"webpage_url": "https://soundcloud.com/nova/midnight-run",
              "title": "Midnight Run", "uploader": "Nova", "duration": 180}]
    result = candidates_from_ytdlp(items, "soundcloud")
    assert result[0].source == "soundcloud"
    assert result[0].artist == "Nova"
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `python -m unittest tests.test_candidate_search -v`

Expected: import failure because `app.candidate_search` does not exist.

- [ ] **Step 3: Implement provider adapters and bounded searches**

Add `ytmusic_match.search_songs(artist, title, limit=5) -> list[dict]`; it calls
the existing unauthenticated `YTMusic().search(query, filter="songs", limit=limit)` and returns
an empty list on provider failure. Retain `best_match` as a compatibility wrapper
only if another caller still needs it.

Create `candidate_search.py` with pure converters plus:

```python
def search_ytmusic(artist: str, title: str) -> list[Candidate]:
    return candidates_from_ytmusic(ytmusic_match.search_songs(artist, title))

def search_ytdlp(query: str, prefix: str, source: str) -> list[Candidate]:
    options = {"quiet": True, "skip_download": True, "extract_flat": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"{prefix}5:{query}", download=False) or {}
    except Exception:
        return []
    return candidates_from_ytdlp(info.get("entries") or [], source)

def search_all(artist: str, title: str) -> list[Candidate]:
    query = f"{artist} - {title}".strip(" -")
    return [*search_ytmusic(artist, title), *search_ytdlp(query, "ytsearch", "youtube"),
            *search_ytdlp(query, "scsearch", "soundcloud")]
```

`search_ytdlp` must instantiate `yt_dlp.YoutubeDL` with `quiet=True`,
`skip_download=True`, `extract_flat=True`, and `noplaylist=True`; call
`extract_info(f"{prefix}5:{query}", download=False)` and convert its `entries`.
Use `ytsearch` for YouTube and `scsearch` for SoundCloud (verified by the installed
extractor). A provider failure returns an empty list rather than aborting a playlist.
`search_all` concatenates results in source-priority order: YouTube Music, YouTube,
then SoundCloud.

- [ ] **Step 4: Run the conversion tests and existing matcher tests**

Run: `python -m unittest tests.test_candidate_search tests.test_matching -v`

Expected: all tests pass without network access.

### Task 3: Generate a safe unresolved-track HTML report

**Files:**
- Create: `app/review_report.py`
- Test: `tests/test_review_report.py`

- [ ] **Step 1: Write the failing report tests**

```python
from pathlib import Path
from app.review_report import TrackOutcome, write_review_report
from app.spotify_resolver import Track

def test_report_contains_rejection_and_requested_service_links(tmp_path: Path):
    outcome = TrackOutcome(
        track=Track("Nova", "Midnight Run", 180), status="rejected",
        reason="artist similarity below 80%", candidates=[])
    path = write_review_report(tmp_path, "Night playlist", [outcome])
    html = path.read_text(encoding="utf-8")
    assert "artist similarity below 80%" in html
    assert "Apple Music/iTunes" in html
    assert "TikTok / ByteDance" in html
    assert "MediaNet" in html

def test_report_escapes_track_metadata(tmp_path: Path):
    outcome = TrackOutcome(Track("A < B", "Song & Title"), "no_candidate", "no candidate found", [])
    html = write_review_report(tmp_path, "<playlist>", [outcome]).read_text(encoding="utf-8")
    assert "A &lt; B" in html
    assert "Song &amp; Title" in html
    assert "<playlist>" not in html
```

- [ ] **Step 2: Run the report tests and verify the expected failure**

Run: `python -m unittest tests.test_review_report -v`

Expected: import failure because `app.review_report` does not exist.

- [ ] **Step 3: Implement outcome records and HTML rendering**

Create `review_report.py` with:

```python
@dataclass(frozen=True)
class TrackOutcome:
    track: Track
    status: str  # skipped | no_candidate | rejected | download_failed | cancelled
    reason: str
    candidates: list[MatchDecision]
```

Use `html.escape` for every visible value and `urllib.parse.urlencode` for queries.
Write `omnidl-review-<UTC timestamp>.html` in `output_dir`. Include a candidate
table (source, title, artist, duration, score, rejection reason, URL) and an
unresolved-track table. Create a service-search mapping that includes every service
the user listed: Spotify, Apple Music, iTunes, Instagram/Facebook, TikTok/ByteDance,
YouTube Music, Amazon, Pandora, Deezer, Tidal, iHeartRadio, Qobuz, Saavn, Boomplay,
Anghami, NetEase, Tencent, Claro MÃºsica, Joox, Kuack Media, Adaptr, Flo, MediaNet,
and Snapchat. For services without a stable public search URL, create a labelled
Google query of `"artist title" "service name"`; label all links `Search link`.

- [ ] **Step 4: Run the report tests**

Run: `python -m unittest tests.test_review_report -v`

Expected: 2 tests pass and a report is written only inside the test temporary directory.

### Task 4: Download only approved candidates in Spotify jobs

**Files:**
- Modify: `app/engines.py`
- Modify: `app/jobs.py`
- Test: `tests/test_spotify_job.py`

- [ ] **Step 1: Write failing job-orchestration tests**

```python
import asyncio
from unittest.mock import AsyncMock, patch
from app.jobs import Job, JobManager
from app.matching import Candidate
from app.spotify_resolver import Track

def test_rejected_candidates_do_not_start_subprocess(tmp_path):
    manager = JobManager()
    job = Job("spotify:test", "spotify", None, "", settings={"output_dir": str(tmp_path)})
    rejected = Candidate("youtube", "https://youtube.test/x", "Wrong Title", "Nova", 180)
    with patch("app.jobs.candidate_search.search_all", return_value=[rejected]), \
         patch.object(manager, "_stream_subprocess", new=AsyncMock()) as run:
        result = asyncio.run(manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False))
    assert result.status == "rejected"
    run.assert_not_awaited()

def test_unresolved_outcomes_write_report_and_emit_path(tmp_path):
    manager = JobManager()
    job = Job("spotify:test", "spotify", None, "", settings={"output_dir": str(tmp_path)})
    outcome = TrackOutcome(Track("Nova", "Missing Song", 180), "no_candidate", "no candidate found", [])
    path = tmp_path / "omnidl-review-test.html"
    with patch("app.jobs.write_review_report", return_value=path):
        asyncio.run(manager._emit_review_report(job, tmp_path, "Test playlist", [outcome]))
    assert str(path) in job.output
```

- [ ] **Step 2: Run the job tests and verify the expected failure**

Run: `python -m unittest tests.test_spotify_job -v`

Expected: failure because `_fetch_track` currently returns strings and does not use candidate decisions.

- [ ] **Step 3: Add a generic verified-media command**

In `engines.py`, add:

```python
def media_url_command(url: str, out_basename: str, s: dict) -> list[str]:
    out_template = str(Path(s["output_dir"]) / f"{out_basename}.%(ext)s")
    cmd = _ytdlp_base(s, out_template)
    return [*cmd, "--no-playlist", url]
```

Make `youtube_video_command` delegate to it with the canonical YouTube watch URL.
This keeps Spotify-based filenames for both verified YouTube and SoundCloud URLs.

- [ ] **Step 4: Replace permissive Spotify fallback orchestration**

In `jobs.py`, import `candidate_search`, `decide_match`, and `TrackOutcome`.
Change `_fetch_track` to return a `TrackOutcome` rather than a status string. Its
algorithm is:

```python
candidates = await asyncio.to_thread(candidate_search.search_all, track.artist, track.title)
decisions = [decide_match(track.artist, track.title, track.duration, item) for item in candidates]
accepted = max((d for d in decisions if d.accepted), key=lambda d: d.score, default=None)
if accepted is None:
    return TrackOutcome(track, "no_candidate" if not decisions else "rejected", reason, decisions[:5])
code = await self._stream_subprocess(job, engines.media_url_command(accepted.candidate.url, track.filename, s), emit=detailed)
if code != 0 or not target.exists():
    return TrackOutcome(track, "download_failed", f"{accepted.candidate.source} download failed", decisions[:5])
await asyncio.to_thread(self._tag, target, track)
return TrackOutcome(track, "downloaded", "verified match", decisions[:5])
```

Treat an existing file as `skipped`, retain cancellation as `cancelled`, and never
invoke `youtube_track_command` for Spotify tracks. Accumulate non-downloaded,
non-skipped outcomes in `_run_spotify_job`; after `asyncio.gather`, call
`await self._emit_review_report(job, Path(s["output_dir"]), resolved.name, outcomes)`.
Implement `_emit_review_report` as:

```python
async def _emit_review_report(self, job: Job, output_dir: Path, playlist_name: str,
                              outcomes: list[TrackOutcome]) -> None:
    if not outcomes:
        return
    try:
        path = await asyncio.to_thread(write_review_report, output_dir, playlist_name, outcomes)
    except OSError as exc:
        await self._emit(job, f"\x1b[31m? Could not write review report: {exc}\x1b[0m\r\n")
        return
    await self._emit(job, f"\x1b[33mReview report: {path}\x1b[0m\r\n")
```

Include `report=<path>` in the final summary. If report rendering fails, emit the
report error but preserve the playlist job's track outcomes.

- [ ] **Step 5: Run job and regression tests**

Run: `python -m unittest tests.test_spotify_job tests.test_matching tests.test_candidate_search tests.test_review_report tests.test_terminal_formatting -v`

Expected: all tests pass. Confirm the rejected-candidate test did not await a subprocess.

### Task 5: Document the user-visible behavior

**Files:**
- Modify: `README.md`
- Test: all test modules

- [ ] **Step 1: Add strict-match and report documentation**

Replace the claim that a generic top YouTube result is a last resort. State that
OmniDL searches YouTube Music, YouTube, and SoundCloud; downloads only a strict
match; and creates an `omnidl-review-*.html` report for unresolved tracks. Clarify
that storefront entries in the report are search links, not automated catalog
checks or download sources.

- [ ] **Step 2: Run the full automated suite and syntax checks**

Run: `python -m unittest discover -s tests -v; python -m py_compile app\matching.py app\candidate_search.py app\review_report.py app\ytmusic_match.py app\engines.py app\jobs.py`

Expected: every test passes and `py_compile` exits with code 0.

- [ ] **Step 3: Perform a manual local verification**

Run: `python run.py`

Submit a short Spotify playlist containing one common song and one deliberately
unavailable/ambiguous track. Verify the common song downloads only after a
high-confidence decision, the uncertain track is absent from the output folder,
and the terminal links to an HTML report that contains the rejected reason and
service search links.
