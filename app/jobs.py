"""Job manager: async subprocess execution, live output streaming, FIFO queue, cancel."""
from __future__ import annotations

import asyncio
import codecs
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import bulk_input, candidate_search, engines
from .library import STAGING_DIR, build_library_index, _normalise
from .matching import MatchDecision, choose, same_recording
from .provenance import Provenance, same_song, tag_names, tag_urls
from .ytdlp_tool import source_key
from .review_report import TrackOutcome, write_review_report
from . import settings as settings_mod
from .settings import DOWNLOAD_ROOT, FILE_TTL_SECONDS, LEGACY_DOWNLOAD_ROOT, PROJECT_ROOT, load_settings
from .spotify_resolver import Track

# How many jobs run concurrently. Each job may itself spawn several track downloads, so
# keep this modest; tune with OMNIDL_WORKERS for your host. (Was effectively 1.)
_WORKER_COUNT = max(1, min(8, int(os.environ.get("OMNIDL_WORKERS", "2"))))

# Per-session abuse limits (independent of how many global workers exist).
_MAX_ACTIVE_PER_SESSION = 3          # queued + running at once
_RATE_MAX = 40                       # submissions ...
_RATE_WINDOW = 3600                  # ... per hour, per session

# Per-IP limits. Session limits alone are trivially bypassed by clearing cookies, so the
# real abuse ceiling is per address. Set higher than the session cap so shared/NAT networks
# (offices, dorms, mobile carriers) still work normally.
_MAX_ACTIVE_PER_IP = 6
_RATE_IP_MAX = 80

# YouTube's "Video unavailable" under load is usually transient, so re-try the SAME
# candidate a couple of times (with backoff) before giving up on it — this stops a momentary
# hiccup on the correct match from cascading into a wrong-song fallback.
_DL_ATTEMPTS = 3
_DL_BACKOFF = 2.5                    # seconds, multiplied by the attempt number

# Persisted job history (survives restarts).
_HISTORY_PATH = PROJECT_ROOT / "history.json"
_HISTORY_BACKUP = PROJECT_ROOT / "history.json.bak"
_HISTORY_MAX = 100
_HISTORY_OUTPUT_CAP = 12_000

# Cap how much output we retain per job for reconnect/replay (characters).
_OUTPUT_CAP = 200_000

_AUDIO_SUFFIXES = {".opus", ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".aac", ".webm", ".mp4", ".mkv"}
# yt-dlp's working files: "x.f251.webm" format pieces awaiting a merge, "x.temp.opus" and
# "x.orig.webm" mid-conversion (".part"/".ytdl" already fail the suffix test). A cancelled or
# failed job can leave them behind; none of them is a finished song.
_INTERMEDIATE_STEM_RE = re.compile(r"\.(?:part|ytdl|temp|orig|f\d+)$", re.IGNORECASE)


def _deliverable(path: str | Path) -> bool:
    """A finished media file that Save may hand over (not a log, report or half-written file)."""
    name = PurePosixPath(str(path).replace("\\", "/"))
    return name.suffix.lower() in _AUDIO_SUFFIXES and not _INTERMEDIATE_STEM_RE.search(name.stem)


# How long one search may take before it's treated as "didn't answer" (and retried at the end).
_SEARCH_DEADLINE = 60

# size, mtime -> seconds of audio (None = empty/unreadable), so a check costs one stat after the
# first read.
_MEDIA_CACHE: dict[str, tuple[int, int, float | None]] = {}


def _media_length(path: Path) -> float | None:
    """Seconds of audio in `path`, or None if it's empty or can't be read as audio."""
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size == 0:
        return None
    key = os.path.normcase(str(path))
    hit = _MEDIA_CACHE.get(key)
    if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
        return hit[2]
    length: float | None = None
    try:
        from mutagen import File
        audio = File(str(path))
        seconds = float(getattr(getattr(audio, "info", None), "length", 0) or 0) if audio is not None else 0.0
        length = seconds if seconds > 0 else None
    except Exception:  # noqa: BLE001 — anything mutagen can't parse is not usable audio
        length = None
    _MEDIA_CACHE[key] = (st.st_size, st.st_mtime_ns, length)
    return length


def _usable_media(path: Path, expected: int = 0) -> bool:
    """Real, playable audio — not an empty file from a failed conversion, not a cut-off one.

    An existing file used to count as "already downloaded" just by existing, so a 0-byte
    leftover made every retry skip the song for good and Save hand out a broken file.
    """
    length = _media_length(path)
    if length is None:
        return False
    return not (expected and length < expected * 0.5)


def _inside(path: Path, root: Path) -> bool:
    """`path` really is within `root` (no `..`, no link pointing elsewhere)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _finished_files_under(root: Path) -> list[Path]:
    """Every finished media file anywhere under `root` — scdl puts playlist tracks in a
    subfolder — skipping links, staging folders and anything that resolves outside it."""
    found: list[Path] = []
    try:
        for dirpath, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d != STAGING_DIR and not os.path.islink(os.path.join(dirpath, d))]
            for name in names:
                path = Path(dirpath) / name
                if path.is_symlink() or not _deliverable(path) or not _inside(path, root):
                    continue
                try:
                    if path.stat().st_size > 0:
                        found.append(path)
                except OSError:
                    pass
    except OSError:
        return []
    return sorted(found)


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (which is_symlink() doesn't report)."""
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# The file that says "OmniDL made this folder for this hosted job". Cleanup deletes nothing
# without it: being under the download root is no proof — that root used to be the library
# folder too, and a local install started in hosted mode lost its Artist/Album folders.
_JOB_MARKER = ".omnidl-job"
_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
# A download's own working folder inside the hidden staging folder: a link job's, or one track's.
_STAGE_NAME_RE = re.compile(r"^[0-9a-f]{12}(?:-\d+)?$")


def _hosted_roots() -> list[Path]:
    roots = [DOWNLOAD_ROOT]
    if not _same_path(LEGACY_DOWNLOAD_ROOT, DOWNLOAD_ROOT):
        roots.append(LEGACY_DOWNLOAD_ROOT)
    return roots


def _mark_hosted_job(folder: Path, job_id: str, how: str = "created") -> bool:
    try:
        (folder / _JOB_MARKER).write_text(
            json.dumps({"omnidl": "hosted-job", "job": job_id, "session": folder.parent.name, "how": how}),
            encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[OmniDL] Couldn't mark {folder} as a hosted job's folder ({exc}); "
              "it won't be cleaned up automatically.")
        return False


def _owned_job_dir(folder: Path, job_id: str | None = None) -> bool:
    """Proof that OmniDL made `folder` as a hosted job's temporary folder, so it may delete it:
    it is <hosted root>/<session>/<job id>, neither it nor its session folder is a link, and it
    holds the marker naming that job and session. Anything else — an album, a folder someone
    put there, a name that merely looks like a job ID — is left alone."""
    try:
        marker = folder / _JOB_MARKER
        if not marker.is_file() or _is_link(marker) or _is_link(folder) or _is_link(folder.parent):
            return False
        if not _JOB_ID_RE.match(folder.name) or (job_id is not None and folder.name != job_id):
            return False
        root = next((r for r in _hosted_roots() if _same_path(folder.parent.parent, r)), None)
        if root is None or not _inside(folder, root):
            return False
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("omnidl") == "hosted-job"
            and data.get("job") == folder.name and data.get("session") == folder.parent.name)


def _make_hidden_dir(path: Path) -> None:
    """Create the staging folder, hidden on Windows (a leading dot only hides it elsewhere),
    so it doesn't flash up in your music folder while a song downloads."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)   # FILE_ATTRIBUTE_HIDDEN
    except (OSError, AttributeError):
        pass


class _OutputClaims:
    """One writer per output file, across every track of every job.

    Duplicate rows in one list, or the same song in two playlists running side by side, used
    to both see "no file yet" and run converters onto the same name. A claim is held from the
    existing-file check until the finished file is in place, so the second one waits and then
    finds the first one's file. Matched songs and link downloads claim the same way, so neither
    can write a name the other is filling. Which recording ends up there is kept in Provenance.
    """

    def __init__(self) -> None:
        self._locks: dict[str, list] = {}           # key -> [lock, users]

    @staticmethod
    def key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    @asynccontextmanager
    async def claim(self, path: Path):
        key = self.key(path)
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield key
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)


def _identity(track) -> tuple[str, str]:
    """A loose artist/title key for a track (accents and punctuation folded). Not used to decide
    whether a file holds a song — see provenance.same_song — since it folds "A/B" into "a b"."""
    return _normalise(getattr(track, "all_artists", "") or track.artist), _normalise(track.title)


def _requested_as(s: dict) -> str:
    """What a link job was asked to save as, e.g. "audio:mp3" — a recording saved as MP3
    doesn't satisfy a request for Opus."""
    if s.get("media_type") == "video":
        return f"video:{s.get('video_container') or 'mp4'}"
    return f"audio:{s.get('audio_format') or 'opus'}"


def _read_manifest(path: str | None, offset: int = 0) -> tuple[list[dict], int]:
    """New entries in a staged link job's manifest (one JSON object per line) from byte
    `offset`, and where the next read starts. Only whole lines are taken — the downloader may be
    mid-write. A bare path on a line is a file with no source information."""
    if not path:
        return [], offset
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    entries: list[dict] = []
    for line in data[:end].decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line) if line.startswith("{") else {"filepath": line}
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries, offset + end


def _move_new(src: Path, dest: Path) -> None:
    """Move `src` to `dest`, never replacing a file already there (FileExistsError instead)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)                       # fails if dest exists, on every platform
    except FileExistsError:
        raise
    except OSError:
        if sys.platform != "win32" and dest.exists():
            raise FileExistsError(str(dest))
        os.rename(src, dest)                     # no hard links here; Windows' rename won't overwrite
        return
    try:
        src.unlink()          # dest is already complete; the staging cleanup retries a held file
    except OSError:
        pass


def _produced_list_path(job_id: str) -> Path:
    import tempfile
    folder = Path(tempfile.gettempdir()) / "omnidl-produced"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{job_id}.txt"


def _audio_snapshot(job: "Job") -> dict[str, float] | None:
    """Media files under the output folder before a job runs, for tools that can't report
    what they saved (scdl, spotdl). yt-dlp jobs report exactly, so they skip the walk."""
    if job.settings.get("_produced_file"):
        return None
    root = Path(job.settings.get("output_dir") or "")
    snapshot: dict[str, float] = {}
    try:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() in _AUDIO_SUFFIXES:
                    path = os.path.join(dirpath, name)
                    try:
                        snapshot[path] = os.path.getmtime(path)
                    except OSError:
                        pass
    except OSError:
        pass
    return snapshot


def _collect_produced(job: "Job", before: dict[str, float] | None) -> list[str]:
    report = job.settings.get("_produced_file")
    if report:
        path = Path(report)
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        except OSError:
            lines = []
        path.unlink(missing_ok=True)
        return [line for line in lines if line]
    if before is None:
        return []
    # New or rewritten since the job started. Another job writing into the same folder at the
    # same time could be picked up too — acceptable for the two tools that can't report.
    after = _audio_snapshot(job) or {}
    return sorted(p for p, mtime in after.items() if before.get(p) != mtime)


_SERVICE_NAMES = {"auto": "all services", "youtube": "YouTube", "soundcloud": "SoundCloud"}
# What the queue calls a job. A typed song isn't "YouTube" any more — it can come from any service.
_KIND_LABELS = {"search": "Song search", "bulk": "Bulk list"}

# The useful part of a failed yt-dlp run, for the terminal and the review report. In parallel
# mode the output is otherwise drained unread, which left "unavailable" with no way to tell a
# transient 403 from a region block or a deleted upload.
_ERROR_PATTERNS = (
    (re.compile(r"sign in to confirm", re.I), "YouTube bot check - cookies needed"),
    (re.compile(r"HTTP Error \d{3}[^\n]*", re.I), None),
    (re.compile(r"video unavailable", re.I), "video unavailable"),
    (re.compile(r"not available in your country|geo.?restrict", re.I), "region blocked"),
    (re.compile(r"private video", re.I), "private video"),
)


# Errors that mean "slow down" rather than "this video is gone". Retrying straight away just
# burns through the rest of a big playlist; pausing every download for a while doesn't.
_RATE_LIMIT_RE = re.compile(r"HTTP Error 429|Too Many Requests|rate.?limit|confirm you.?re not a bot",
                            re.IGNORECASE)
_SOURCE_NAMES = {"youtube_music": "YouTube Music", "youtube": "YouTube", "soundcloud": "SoundCloud"}


def _tally(outcomes, total: int) -> dict[str, int]:
    """Per-track results so far: what the card shows, live and at the end.

    "temporary" is the part of "failed" that hit a passing problem (a rate limit, a search that
    didn't answer, a download that kept failing) rather than "not found" — worth retrying later.
    """
    counts = {"downloaded": 0, "review": 0, "skipped": 0, "failed": 0, "temporary": 0, "total": total}
    for outcome in outcomes:
        if outcome.status in ("downloaded", "downloaded_for_review"):
            counts["downloaded"] += 1
            counts["review"] += outcome.status == "downloaded_for_review"
        elif outcome.status == "skipped":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
            counts["temporary"] += _worth_retrying(outcome)
    return counts


def _worth_retrying(outcome) -> bool:
    return (outcome.status == "download_failed" and bool(outcome.failed_attempts)) or \
        bool(getattr(outcome, "retryable", False))


def _short_error(lines: list[str]) -> str:
    text = "\n".join(lines)
    for pattern, label in _ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            return label or match.group(0).strip()[:60]
    last = lines[-1] if lines else ""
    return last.replace("ERROR:", "").strip()[:80]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _failure_note(output: str) -> str:
    """The most telling line of a failed job's log, for its card in the queue."""
    lines = [_ANSI_RE.sub("", line).strip() for line in output.replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line and not line.startswith("[error] exit=")
             and not line.startswith("[done]") and not line.startswith("[cancelled]")]
    for line in reversed(lines):
        if "ERROR" in line or line.startswith("[error]"):
            note = _short_error([line])
            return note if note else line[:120]
    return lines[-1][:120] if lines else ""


# Progress / current-item parsing (works across yt-dlp, spotdl, scdl).
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
_DEST_RE = re.compile(r"Destination:\s*(.+)")
_SAVING_RE = re.compile(r"Saving media:\s*(.+)")
_TITLE_RE = re.compile(r'Download(?:ing|ed)\s+"([^"]+)"')


def _display_name(path_or_text: str) -> str:
    text = path_or_text.strip().strip('"')
    if not text:
        return text
    if "\\" in text or re.match(r"^[A-Za-z]:", text):
        return PureWindowsPath(text).name
    return PurePosixPath(text).name


def _quoted_filename(text: str) -> str:
    match = re.search(r'"([^"]+)"', text)
    return _display_name(match.group(1) if match else text)


def format_tool_output_line(raw: str) -> str:
    """Convert noisy downloader output into concise, styled terminal lines."""
    ending = ""
    if raw.endswith("\r\n"):
        ending = "\r\n"
        line = raw[:-2]
    elif raw.endswith("\n") or raw.endswith("\r"):
        ending = raw[-1]
        line = raw[:-1]
    else:
        line = raw

    text = line.strip()
    if not text:
        return ending

    if text.startswith("ERROR:") or "[error]" in text.lower():
        return f"\x1b[31m\u2717 {text}\x1b[0m{ending}"
    if text.startswith("WARNING:") or "[warning]" in text.lower():
        return f"\x1b[33m\u2717 {text}\x1b[0m{ending}"

    match = re.match(r"\[youtube\]\s+Extracting URL:\s*(.+)", text)
    if match:
        return f"\x1b[1;36m\U0001f50e Extracting YouTube URL\x1b[0m \x1b[2m{match.group(1)}\x1b[0m{ending}"

    match = re.match(r"\[(youtube|soundcloud|generic|scdl)\]\s+(?:[^:]+:\s+)?(.+)", text, re.I)
    if match:
        return f"  \x1b[2m\u21b3 {match.group(2)}\x1b[0m{ending}"

    match = re.match(r"\[info\]\s+[^:]+:\s+Downloading\s+\d+\s+format\(s\):\s*(.+)", text)
    if match:
        return f"  \x1b[35m\U0001f39a Selected format: {match.group(1)}\x1b[0m{ending}"

    if text.startswith("[info] Downloading video thumbnail"):
        return f"  \x1b[35m\U0001f5bc Downloading thumbnail\x1b[0m{ending}"

    match = re.match(r"\[info\]\s+Writing video thumbnail.+\s+to:\s*(.+)", text)
    if match:
        return f"  \x1b[35m\U0001f5bc Writing thumbnail: {_display_name(match.group(1))}\x1b[0m{ending}"

    match = re.match(r"\[download\]\s+Destination:\s*(.+)", text)
    if match:
        return f"\x1b[1;36m\U0001f4e5 Saving media:\x1b[0m {_display_name(match.group(1))}{ending}"

    match = re.match(r"\[download\]\s+(.+)", text)
    if match:
        return f"  \x1b[36m\u23f3 {match.group(1)}\x1b[0m{ending}"

    match = re.match(r"\[ExtractAudio\]\s+Destination:\s*(.+)", text)
    if match:
        return f"\x1b[1;32m\U0001f3a7 Extracting audio:\x1b[0m {_display_name(match.group(1))}{ending}"

    match = re.match(r"Deleting original file\s+(.+?)\s+\(", text)
    if match:
        return f"  \x1b[2m\U0001f9f9 Removed temporary file: {_display_name(match.group(1))}\x1b[0m{ending}"

    if text.startswith("[Metadata]"):
        return f"  \x1b[32m\U0001f3f7 Adding metadata: {_quoted_filename(text)}\x1b[0m{ending}"

    if text.startswith("[ThumbnailsConvertor]"):
        return f"  \x1b[35m\U0001f5bc Converting thumbnail: {_quoted_filename(text)}\x1b[0m{ending}"

    if text.startswith("[EmbedThumbnail]"):
        return f"  \x1b[35m\U0001f5bc Embedding thumbnail: {_quoted_filename(text)}\x1b[0m{ending}"

    return raw


class ToolOutputFormatter:
    """Stateful formatter for streamed subprocess chunks."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> str:
        data = self._buffer + text
        parts = data.splitlines(keepends=True)
        if parts and not parts[-1].endswith(("\n", "\r")):
            self._buffer = parts.pop()
        else:
            self._buffer = ""
        return "".join(format_tool_output_line(part) for part in parts)

    def flush(self) -> str:
        if not self._buffer:
            return ""
        text = format_tool_output_line(self._buffer)
        self._buffer = ""
        return text


class Job:
    def __init__(self, input_text: str, engine: str, argv: list[str] | None,
                 display_cmd: str, *, spotify_url: str | None = None,
                 tracks: list | None = None, list_name: str = "", kind: str = "",
                 settings: dict | None = None, session: str = "", ip: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.input = input_text
        self.engine = engine
        self.argv = argv                 # None => track-list job (see _run_tracklist_job)
        self.display_cmd = display_cmd
        self.spotify_url = spotify_url
        # A bulk paste arrives with its track list already parsed, so there is nothing
        # to resolve; list_name is what the review report and header are titled with.
        self.tracks = tracks
        self.list_name = list_name
        # "search" (typed song), "bulk" (pasted list), "spotify", or "command" (a raw link run
        # through a downloader). Retry depends on it: re-running a pasted list by its display
        # text would search YouTube for the words "Wedding songs (3 tracks)".
        self.kind = kind or ("spotify" if spotify_url else "command")
        self.settings = settings or {}
        self.session = session           # owning visitor; scopes visibility + files
        self.ip = ip                     # client address, for per-IP abuse limits
        self.out_dir: Path | None = None  # per-job download folder
        self.status = "queued"  # queued | running | done | error | cancelled
        self.code: int | None = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.output = ""
        # Characters ever appended to `output` (which is trimmed from the front). Live output
        # carries it as "end", so a browser loading a log can tell which live lines the
        # snapshot already contains — without it, switching jobs mid-download lost or
        # repeated lines.
        self.appended = 0
        self.progress: int | None = None
        self.label = ""
        self.procs: set[asyncio.subprocess.Process] = set()  # live subprocesses
        # Local mode writes straight into your library (out_dir is None), so the files a job
        # made are recorded one by one instead — that's what Save hands to a remote device.
        self.produced: list[str] = []
        # The review report this run wrote (tracks saved for review or not found), and how many
        # tracks it lists — so the queue can offer it on any device, not just as a file on disk.
        self.report_path: str | None = None
        self.review_count = 0
        # One line on why a job failed, shown on its card without opening the log.
        self.note = ""
        # How a track list went, track by track: {"downloaded", "review", "skipped", "failed",
        # "total"}. A playlist with one saved song and nine misses is "done", but it isn't
        # finished business — the card says so and offers to retry just the misses.
        self.counts: dict[str, int] | None = None
        self.failed_tracks: list = []
        # Set when Spotify wouldn't hand over the whole playlist (see spotify_resolver).
        self.incomplete_list = False

    def append(self, text: str) -> None:
        self.appended += len(text)
        self.output += text
        if len(self.output) > _OUTPUT_CAP:
            self.output = self.output[-_OUTPUT_CAP:]

    def list_files(self) -> list[Path]:
        """Finished files this job produced or found already saved, available to download.

        The job's own record (`produced` — the downloader's manifest, including files it
        found already there) comes first. A hosted job without one falls back to walking its
        own folder, subfolders included; nothing outside that folder is ever offered.
        """
        files: list[Path] = []
        seen: set[str] = set()
        for raw in self.produced:
            path = Path(raw)
            key = os.path.normcase(raw)
            if key in seen or not _deliverable(raw):
                continue
            if self.out_dir is not None and not _inside(path, self.out_dir):
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    seen.add(key)
                    files.append(path)
            except OSError:
                pass
        if files or self.out_dir is None:
            return files
        if not self.out_dir.exists():
            return []
        return _finished_files_under(self.out_dir)

    def _recorded(self) -> list[str]:
        # Local mode answers from the record rather than the disk: this runs for every job in
        # every snapshot, and a 500-track list would otherwise stat 500 files each time.
        return list(dict.fromkeys(raw for raw in self.produced if _deliverable(raw)))

    def file_names(self) -> list[str]:
        if self.out_dir is None:
            return self._recorded()
        return [str(p) for p in self.list_files()]

    def has_files(self) -> bool:
        return bool(self.file_names())

    def to_dict(self) -> dict:
        meta = engines.ENGINES.get(self.engine, {"label": self.engine, "tool": self.engine})
        finished = self.status in ("done", "error", "cancelled")
        names = self.file_names() if finished else []
        return {
            "id": self.id,
            "input": self.input,
            "kind": self.kind,
            "engine": self.engine,
            "engine_label": _KIND_LABELS.get(self.kind, meta["label"]),
            # A matched song can come from any service, so say where it searched, not "yt-dlp".
            "tool": (_SERVICE_NAMES.get(self.settings.get("bulk_service") or "auto", "all services")
                     if self.kind in _KIND_LABELS else meta["tool"]),
            "status": self.status,
            "code": self.code,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "progress": self.progress,
            "label": self.label,
            # Mode used for this job, so "retry" reproduces it exactly (audio vs video).
            "media_type": self.settings.get("media_type", "audio"),
            "video_quality": self.settings.get("video_quality"),
            "audio_format": self.settings.get("audio_format"),
            # Whatever finished is deliverable, even from a run that failed or was cancelled
            # part-way: those songs are on disk and the other device should be able to get them.
            "has_files": finished and bool(names),
            "file_count": len(names) if finished else 0,
            # What the files actually are (".opus", ".mp3"...), not what was asked for: SoundCloud
            # links, for one, can't be converted to every format.
            "formats": sorted({PurePosixPath(n.replace("\\", "/")).suffix.lower().lstrip(".") for n in names}),
            "counts": self.counts,
            "incomplete_list": self.incomplete_list,
            "can_retry_failed": finished and bool(self.failed_tracks),
            "list_name": self.list_name,
            # Hosted copies are deleted after FILE_TTL_SECONDS; the card counts down to it.
            "expires": (self.finished + FILE_TTL_SECONDS
                        if self.out_dir is not None and self.finished else None),
            "review_count": self.review_count,
            "has_report": bool(self.report_path) and Path(self.report_path).is_file(),
            "note": self.note,
        }

    def to_record(self) -> dict:
        """Serializable history record (includes a trimmed log)."""
        rec = self.to_dict()
        rec["output"] = self.output[-_HISTORY_OUTPUT_CAP:]
        rec["spotify_url"] = self.spotify_url
        rec["list_name"] = self.list_name
        rec["bulk_service"] = self.settings.get("bulk_service")
        # The list itself, so a pasted list can be retried after a restart.
        rec["tracks"] = ([{"artist": t.artist, "title": t.title, "artists": list(t.artists),
                           "duration": t.duration} for t in self.tracks]
                         if self.tracks is not None else None)
        rec["session"] = self.session
        rec["produced"] = list(self.produced)
        rec["failed_tracks"] = [{"artist": t.artist, "title": t.title, "artists": list(t.artists),
                                 "duration": t.duration} for t in self.failed_tracks]
        rec["report_path"] = self.report_path
        rec["out_dir"] = str(self.out_dir) if self.out_dir else None
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> "Job":
        tracks = rec.get("tracks")
        kind = rec.get("kind") or ""
        if not kind and not rec.get("spotify_url") and re.search(r"\(\d+ tracks?\)$", rec.get("input", "")):
            kind = "bulk"      # saved before lists were persisted: known to be a list, list lost
        job = cls(rec.get("input", ""), rec.get("engine", "youtube"), None, "",
                  spotify_url=rec.get("spotify_url"), session=rec.get("session", ""),
                  tracks=[Track(artist=t.get("artist", ""), title=t.get("title", ""),
                                artists=list(t.get("artists") or []),
                                duration=int(t.get("duration") or 0)) for t in tracks]
                  if tracks is not None else None,
                  list_name=rec.get("list_name") or "", kind=kind)
        if rec.get("out_dir"):
            job.out_dir = Path(rec["out_dir"])
        job.id = rec.get("id", job.id)
        status = rec.get("status", "done")
        job.output = rec.get("output", "")
        if status in ("running", "queued"):  # never finished — server had stopped
            status = "cancelled"
            job.output += "\r\n\x1b[33m[interrupted — server was restarted]\x1b[0m\r\n"
        job.appended = len(job.output)
        interrupted = status != rec.get("status", "done")
        job.status = status
        job.code = rec.get("code")
        job.created = rec.get("created", time.time())
        job.started = rec.get("started")
        job.finished = rec.get("finished")
        if interrupted and not job.finished:
            # It never finished, so it had no finish time — and hosted cleanup only expires
            # finished jobs, so its files were kept forever. The restart is when it ended.
            job.finished = time.time()
        job.progress = rec.get("progress")
        job.label = rec.get("label", "")
        job.produced = [str(p) for p in rec.get("produced") or []]
        job.report_path = rec.get("report_path")
        job.review_count = int(rec.get("review_count") or 0)
        job.note = rec.get("note") or ""
        job.incomplete_list = bool(rec.get("incomplete_list"))
        counts = rec.get("counts")
        job.counts = ({k: int(v or 0) for k, v in counts.items()} if isinstance(counts, dict) else None)
        job.failed_tracks = [Track(artist=t.get("artist", ""), title=t.get("title", ""),
                                   artists=list(t.get("artists") or []),
                                   duration=int(t.get("duration") or 0))
                             for t in rec.get("failed_tracks") or [] if isinstance(t, dict)]
        # Keep the mode fields so a retry from history still reproduces the original job.
        job.settings = {
            "media_type": rec.get("media_type", "audio"),
            "video_quality": rec.get("video_quality"),
            "audio_format": rec.get("audio_format"),
            "bulk_service": rec.get("bulk_service") or "auto",
        }
        return job


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: dict = {}              # websocket -> session id
        self._worker_tasks: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None
        self._submit_times: dict[str, list[float]] = {}  # session -> recent submit times
        self._ip_times: dict[str, list[float]] = {}      # client IP -> recent submit times
        self._claims = _OutputClaims()
        # Which recording each saved file holds, across restarts (see provenance.py).
        self.owners = Provenance()
        self._manifest_pos: dict[str, int] = {}   # link job -> how far its manifest was read
        # YouTube rate limits are per connection/account, not per job: when one job is told to
        # slow down, every job waits (was per job, so a second playlist kept hammering).
        self.pause_until = 0.0
        self.rate_hits = 0

    # ----- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if not self._worker_tasks:
            self._load_history()
            self._adopt_hosted_folders()
            self._sweep_stale_staging()
            self._worker_tasks = [asyncio.create_task(self._worker()) for _ in range(_WORKER_COUNT)]
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    # ----- persistence ---------------------------------------------------
    def _load_history(self) -> None:
        """Restore the queue and history. A damaged history.json (a crash mid-write before
        writes were atomic, a full disk) falls back to the previous good copy."""
        for path in (_HISTORY_PATH, _HISTORY_BACKUP):
            try:
                if not path.exists():
                    continue
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("not a list of jobs")
            except (OSError, ValueError) as exc:
                print(f"[OmniDL] Couldn't read {path.name} ({exc}); trying the backup.")
                continue
            for rec in records:
                try:
                    job = Job.from_record(rec)
                except Exception as exc:  # noqa: BLE001 — one bad record mustn't lose the rest
                    print(f"[OmniDL] Skipped an unreadable history entry: {exc}")
                    continue
                self.jobs[job.id] = job
                self.order.append(job.id)
            if path is _HISTORY_BACKUP:
                print("[OmniDL] Restored history from history.json.bak.")
            return

    def _persist(self) -> None:
        """Save history atomically, keeping the previous version as history.json.bak — a crash
        or full disk mid-write used to leave a truncated file and lose the whole history."""
        try:
            recs = [self.jobs[i].to_record() for i in self.order[-_HISTORY_MAX:] if i in self.jobs]
            data = json.dumps(recs)
            tmp = _HISTORY_PATH.with_name(_HISTORY_PATH.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if _HISTORY_PATH.exists():
                try:
                    os.replace(_HISTORY_PATH, _HISTORY_BACKUP)
                except OSError:
                    pass
            os.replace(tmp, _HISTORY_PATH)
        except Exception as exc:  # noqa: BLE001 — never let saving history break a download
            if not getattr(self, "_persist_warned", False):
                self._persist_warned = True
                print(f"[OmniDL] Couldn't save history.json: {exc}")

    # ----- websocket fan-out --------------------------------------------
    async def broadcast(self, message: dict, session: str | None = None) -> None:
        """Send to every subscriber, or only those in `session` when given."""
        if settings_mod.LOCAL_MODE:
            session = None            # single user: every tab should see every job
        dead = []
        for ws, sid in list(self.subscribers.items()):
            if session is not None and sid != session:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.pop(ws, None)

    async def _send(self, job: Job, message: dict) -> None:
        """Broadcast a job event only to the visitor that owns the job."""
        await self.broadcast(message, session=job.session)

    def _session_jobs(self, session: str) -> list[Job]:
        # Local/personal mode has exactly one user, so scoping by session would only mean
        # "a new tab or a cleared cookie hides your own downloads". Show everything.
        if settings_mod.LOCAL_MODE:
            return [self.jobs[i] for i in self.order if i in self.jobs]
        return [self.jobs[i] for i in self.order if i in self.jobs and self.jobs[i].session == session]

    def list_jobs(self, session: str) -> list[dict]:
        return [j.to_dict() for j in self._session_jobs(session)]

    def snapshot(self, session: str) -> dict:
        jobs = self._session_jobs(session)
        running = [j for j in jobs if j.status == "running"]
        active = running[-1] if running else (jobs[-1] if jobs else None)
        return {
            "type": "snapshot",
            "jobs": [j.to_dict() for j in jobs],
            "active_id": active.id if active else None,
            "output": active.output if active else "",
            "end": active.appended if active else 0,
        }

    # ----- rate limiting -------------------------------------------------
    def _active_count(self, session: str) -> int:
        return sum(1 for j in self.jobs.values()
                   if j.session == session and j.status in ("queued", "running"))

    def _active_count_ip(self, ip: str) -> int:
        return sum(1 for j in self.jobs.values()
                   if j.ip == ip and j.status in ("queued", "running"))

    @staticmethod
    def _recent(times: list[float], now: float) -> list[float]:
        return [t for t in times if now - t < _RATE_WINDOW]

    def check_limit(self, session: str, ip: str = "", unlimited: bool = False) -> str | None:
        """Return an error message if this caller may not submit right now, else None.

        Enforced per session *and* per IP — a visitor who clears cookies gets a fresh
        session but still counts against their address.
        """
        # Local/personal mode is a single-user app on your own machine. These caps exist to
        # stop strangers exhausting a public server, so here they'd only throttle the owner.
        if settings_mod.LOCAL_MODE or unlimited:
            return None
        now = time.time()
        if self._active_count(session) >= _MAX_ACTIVE_PER_SESSION:
            return f"Too many active downloads (max {_MAX_ACTIVE_PER_SESSION}). Let one finish first."
        recent = self._recent(self._submit_times.get(session, []), now)
        self._submit_times[session] = recent
        if len(recent) >= _RATE_MAX:
            return "Hourly download limit reached for this session. Please try again later."

        if ip:
            if self._active_count_ip(ip) >= _MAX_ACTIVE_PER_IP:
                return f"Too many active downloads from your network (max {_MAX_ACTIVE_PER_IP})."
            ip_recent = self._recent(self._ip_times.get(ip, []), now)
            self._ip_times[ip] = ip_recent
            if len(ip_recent) >= _RATE_IP_MAX:
                return "Hourly download limit reached for your network. Please try again later."
        return None

    # ----- submission ----------------------------------------------------
    @staticmethod
    def _prepare_out_dir(job: Job, s: dict, session: str) -> None:
        """Point this job's downloads at the right folder for the deployment mode."""
        if settings_mod.LOCAL_MODE:
            # Personal mode: write straight to the user's configured folder. out_dir stays
            # None so nothing here is ever auto-deleted (it's the user's library).
            out_dir = Path(s.get("output_dir") or settings_mod.DEFAULT_OUTPUT)
        else:
            # Hosted mode: each job gets its own isolated folder, so we know exactly which
            # files it produced and can hand only those to the visitor who created it.
            out_dir = DOWNLOAD_ROOT / (session or "anonymous") / job.id
            job.out_dir = out_dir
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            if job.out_dir is not None:
                _mark_hosted_job(out_dir, job.id)    # what later lets cleanup delete it
        except OSError:
            pass
        s["output_dir"] = str(out_dir)

    async def _enqueue(self, job: Job, session: str, ip: str) -> Job:
        """Register a built job, count it against the abuse limits, and queue it."""
        self.jobs[job.id] = job
        self.order.append(job.id)
        now = time.time()
        self._submit_times.setdefault(session, []).append(now)
        if ip:
            self._ip_times.setdefault(ip, []).append(now)
        self.queue.put_nowait(job.id)
        await self._send(job, {"type": "job_added", "job": job.to_dict()})
        self._persist()
        return job

    async def submit_bulk(self, tracks: list, settings_dict: dict, session: str,
                          service: str = "auto", list_name: str = "",
                          ip: str = "", input_text: str = "", kind: str = "bulk") -> Job:
        """Queue a list of songs as ONE job — a pasted list, or a single typed song.

        Deliberately one job rather than one per line: the whole point of pasting a list is
        that it behaves like a playlist — a single progress view, one library index built
        once, shared concurrency, and one review report naming whatever could not be matched.
        """
        s = dict(settings_dict)
        s["bulk_service"] = service if service in candidate_search.SERVICE_SOURCES else "auto"
        s["media_type"] = "audio"          # matching a song is an audio operation
        name = list_name.strip() or "Pasted list"
        count = f"{len(tracks)} track{'s' if len(tracks) != 1 else ''}"
        job = Job(input_text or f"{name} ({count})", "youtube", None, "",
                  tracks=tracks, list_name=name, kind=kind, session=session, settings=s, ip=ip)
        self._prepare_out_dir(job, s, session)
        job.display_cmd = f"match {count} on {_SERVICE_NAMES[s['bulk_service']]}"
        return await self._enqueue(job, session, ip)

    async def retry(self, job_id: str, session: str, settings_dict: dict, ip: str = "",
                    failed_only: bool = False) -> Job | str:
        """Re-run a job exactly as it was asked for. Returns the new job, or an error message.

        Mode (audio/video, format, quality, search service) comes from the original job, not
        from whatever the toolbar happens to be set to now; everything else — output folder,
        naming — comes from current settings, as a fresh download would.
        """
        old = self.jobs.get(job_id)
        if old is None or (not settings_mod.LOCAL_MODE and old.session != session):
            return "That job no longer exists."
        s = dict(settings_dict)
        for field in ("media_type", "video_quality", "audio_format"):
            if old.settings.get(field):
                s[field] = old.settings[field]
        if failed_only:
            if not old.failed_tracks:
                return "Nothing failed in that job."
            name = old.list_name or old.input
            count = len(old.failed_tracks)
            return await self.submit_bulk(
                list(old.failed_tracks), s, session,
                service=old.settings.get("bulk_service") or "auto",
                list_name=f"{name} - {count} failed track{'s' if count != 1 else ''}", ip=ip)
        if old.kind in ("bulk", "search"):
            if not old.tracks:
                return ("This list was saved before OmniDL kept lists with their jobs, so it "
                        "can't be retried — paste it into Bulk again.")
            return await self.submit_bulk(
                list(old.tracks), s, session, service=old.settings.get("bulk_service") or "auto",
                list_name=old.list_name, ip=ip,
                input_text=old.input if old.kind == "search" else "", kind=old.kind)
        if old.kind == "spotify":
            return await self.submit(old.spotify_url or old.input, s, session, "spotify", ip=ip)
        return await self.submit(old.input, s, session, old.engine, ip=ip)

    async def submit(self, input_text: str, settings_dict: dict, session: str,
                     engine_override: str | None = None, ip: str = "") -> Job:
        s = dict(settings_dict)
        text = input_text.strip()
        # A typed song is matched exactly like a playlist track or a pasted line. It used to
        # be handed to yt-dlp as "ytsearch1:" — YouTube's #1 result — which is a different
        # algorithm: it mostly returned the music video (intros, skits, live cuts), and it
        # was why one song and many songs could come back as different versions. Video mode
        # still searches YouTube directly, since there the video is what's wanted.
        if (text and not engines.is_url(text) and s.get("media_type", "audio") != "video"):
            entries = [e for e in bulk_input.parse_bulk(text) if e.kind == "search"]
            if entries:
                e = entries[0]
                track = Track(artist=e.artist, title=e.title, artists=[e.artist] if e.artist else [])
                service = {"youtube": "youtube", "soundcloud": "soundcloud"}.get(engine_override or "", "auto")
                return await self.submit_bulk([track], s, session, service=service,
                                              list_name=text, ip=ip, input_text=text, kind="search")
        if engine_override in engines.ENGINES:
            engine = engine_override
        else:
            engine = engines.detect_engine(input_text, s)

        job = Job(input_text, engine, None, "", session=session, settings=s, ip=ip)
        self._prepare_out_dir(job, s, session)

        if engine == "spotify" and s.get("spotify_method", "embed") == "embed":
            job.spotify_url = input_text
            job.kind = "spotify"
            job.display_cmd = "resolve Spotify via public embed (no API / no Premium)"
        else:
            if engine in ("youtube", "soundcloud"):
                # A manifest of every file the job ends up with — including ones it found
                # already there — written by yt-dlp (scdl runs yt-dlp inside, and passes the
                # option through). Exact even with other jobs writing alongside, unlike
                # comparing the folder before and after, which missed re-runs entirely.
                s["_produced_file"] = str(_produced_list_path(job.id))
                # Downloads land in this job's own staging folder and are filed into the output
                # folder afterwards, each under a name no other recording holds; recordings the
                # folder already has (by source ID) are listed for the downloader to skip.
                s["_stage_dir"] = str(Path(s["output_dir"]) / STAGING_DIR / job.id)
                s["_known_file"] = str(_produced_list_path(job.id).with_suffix(".known.json"))
            job.argv = engines.build_command(engine, input_text, s)
            job.display_cmd = engines.describe_command(job.argv)

        return await self._enqueue(job, session, ip)

    async def cancel(self, job_id: str, session: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or (not settings_mod.LOCAL_MODE and job.session != session):
            return False
        if job.status == "running":
            job.status = "cancelled"
            self._kill(job)
        elif job.status == "queued":
            job.status = "cancelled"
            await self._send(job, {"type": "status", "job": job.to_dict()})
            self._persist()
        return True

    async def delete(self, job_id: str, session: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or (not settings_mod.LOCAL_MODE and job.session != session) or job.status == "running":
            return False
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._remove_files(job)
        await self._send(job, {"type": "removed", "job_id": job_id})
        self._persist()
        return True

    @staticmethod
    def _remove_files(job: Job) -> bool:
        """Delete a hosted job's own folder — only if it provably is one (see _owned_job_dir)."""
        if not (job.out_dir and job.out_dir.exists() and _owned_job_dir(job.out_dir, job.id)):
            return False
        shutil.rmtree(job.out_dir, ignore_errors=True)
        try:
            job.out_dir.parent.rmdir()        # the visitor's folder, once their last job is gone
        except OSError:
            pass
        return True

    async def _cleanup_loop(self) -> None:
        """Delete finished jobs' files once they age out, to bound disk use."""
        while True:
            await asyncio.sleep(300)
            await self._expire_files()

    async def _expire_files(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for job in list(self.jobs.values()):
            if job.status in ("done", "error", "cancelled") and job.finished \
                    and now - job.finished > FILE_TTL_SECONDS:
                if job.out_dir and job.out_dir.exists() and self._remove_files(job):
                    await self._send(job, {"type": "status", "job": job.to_dict()})
        self._reap_orphans(now)
        self.owners.prune()                   # forget files that were deleted

    def _reap_orphans(self, now: float) -> None:
        """Hosted only: delete job folders no job refers to any more (history trimmed, a crash
        before it was saved) once they're older than the file TTL.

        Only folders carrying OmniDL's hosted-job marker are candidates. Everything else under
        the root — a library's Artist/Album folders, anything put there by hand — is never
        touched, however old. Never runs on a local install at all.
        """
        if settings_mod.LOCAL_MODE:
            return
        known = {os.path.normcase(os.path.abspath(j.out_dir)) for j in self.jobs.values() if j.out_dir}
        for root in _hosted_roots():
            try:
                sessions = [d for d in root.iterdir() if d.is_dir() and not _is_link(d)]
            except OSError:
                continue
            for session_dir in sessions:
                try:
                    job_dirs = [d for d in session_dir.iterdir() if d.is_dir() and not _is_link(d)]
                except OSError:
                    continue
                removed = False
                for job_dir in job_dirs:
                    try:
                        if (os.path.normcase(os.path.abspath(job_dir)) not in known
                                and _owned_job_dir(job_dir)
                                and now - job_dir.stat().st_mtime > FILE_TTL_SECONDS):
                            shutil.rmtree(job_dir, ignore_errors=True)
                            removed = True
                    except OSError:
                        pass
                if removed:
                    try:
                        session_dir.rmdir()
                    except OSError:
                        pass

    @staticmethod
    def _sweep_stale_staging(output_dir: Path | None = None) -> None:
        """Local, on start: clear the working folders a crash left in the library's hidden
        staging folder — only ones named for a job ("<job id>" or "<job id>-<track>") and
        untouched for a day. Finished link downloads were already filed as they finished, so
        all that's left there is half-downloaded audio."""
        if not settings_mod.LOCAL_MODE:
            return
        try:
            out = Path(output_dir or load_settings().get("output_dir") or settings_mod.DEFAULT_OUTPUT)
            staging = out / STAGING_DIR
            if not staging.is_dir() or _is_link(staging):
                return
            cutoff = time.time() - 86400
            for entry in staging.iterdir():
                if (_STAGE_NAME_RE.match(entry.name) and entry.is_dir() and not _is_link(entry)
                        and entry.stat().st_mtime < cutoff):
                    shutil.rmtree(entry, ignore_errors=True)
            staging.rmdir()                           # only if that emptied it
        except Exception:  # noqa: BLE001 — housekeeping must never stop OmniDL starting
            pass

    def _adopt_hosted_folders(self) -> None:
        """Hosted only, on start: mark the folders of jobs in the history that predate the
        marker, so they still expire. Only a folder at exactly <root>/<session>/<job id> for a
        job the history says OmniDL created there qualifies; old folders no job refers to are
        reported, never deleted — look at them and remove them yourself."""
        if settings_mod.LOCAL_MODE:
            return
        for job in self.jobs.values():
            folder = job.out_dir
            if (folder is None or not folder.is_dir() or (folder / _JOB_MARKER).exists()
                    or _is_link(folder) or _is_link(folder.parent) or folder.name != job.id
                    or not _JOB_ID_RE.match(job.id) or folder.parent.name != (job.session or "anonymous")):
                continue
            if any(_same_path(folder.parent.parent, root) and _inside(folder, root) for root in _hosted_roots()):
                _mark_hosted_job(folder, job.id, how="history")
        known = {os.path.normcase(os.path.abspath(j.out_dir)) for j in self.jobs.values() if j.out_dir}
        leftovers = 0
        for root in _hosted_roots():
            try:
                for session_dir in root.iterdir():
                    if not session_dir.is_dir() or _is_link(session_dir):
                        continue
                    children = [d for d in session_dir.iterdir() if d.is_dir()]
                    if children and all(_JOB_ID_RE.match(d.name) for d in children):
                        leftovers += sum(1 for d in children if not (d / _JOB_MARKER).exists()
                                         and os.path.normcase(os.path.abspath(d)) not in known)
            except OSError:
                continue
        if leftovers:
            print(f"[OmniDL] {leftovers} old download folder(s) under "
                  f"{', '.join(str(r) for r in _hosted_roots())} aren't linked to any job, so they "
                  "won't be cleaned up automatically. Check and delete them yourself if they're "
                  "leftover hosted downloads.")

    # ----- internals -----------------------------------------------------
    def _kill(self, job: Job) -> None:
        for proc in list(job.procs):
            if proc.returncode is not None:
                continue
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True)
                else:
                    proc.terminate()
            except Exception:
                pass

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status == "cancelled":
                continue
            await self._run_job(job)

    async def _emit(self, job: Job, text: str) -> None:
        job.append(text)
        await self._send(job, {"type": "output", "job_id": job.id, "data": text, "end": job.appended})
        if self._update_progress(job, text):
            await self._send(job, {
                "type": "progress", "job_id": job.id,
                "progress": job.progress, "label": job.label,
            })

    async def _emit_keyed(self, job: Job, key: str, text: str, store: bool = False) -> None:
        """Emit a line that updates in place on the client (one per concurrent track).
        Only the final state (store=True) is kept in the job log for reconnect replay."""
        message = {"type": "output", "job_id": job.id, "data": text, "key": key}
        if store:
            job.append(text)
            message["end"] = job.appended
        await self._send(job, message)

    async def _key_done(self, job: Job, key: str) -> None:
        await self._send(job, {"type": "key_done", "job_id": job.id, "key": key})

    @staticmethod
    def _update_progress(job: Job, text: str) -> bool:
        changed = False
        pcts = _PCT_RE.findall(text)
        if pcts:
            try:
                val = max(0, min(100, int(float(pcts[-1]))))
                if val != job.progress:
                    job.progress = val
                    changed = True
            except ValueError:
                pass
        match = _TITLE_RE.search(text) or _DEST_RE.search(text) or _SAVING_RE.search(text)
        if match:
            label = match.group(1).strip().replace("\\", "/").split("/")[-1]
            if label and label != job.label:
                job.label = label
                changed = True
        return changed

    async def _stream_subprocess(self, job: Job, argv: list[str], emit: bool = True,
                                 progress_cb=None, errors: list[str] | None = None) -> int:
        """Run one subprocess. emit=True streams formatted output live. Otherwise the output
        is drained silently, but if progress_cb is given the latest download %/stage is
        parsed and passed to it (used for the live per-track line in parallel mode).
        If `errors` is given, the tool's ERROR lines are collected into it in every mode.
        Returns the exit code."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        job.procs.add(proc)
        pending = [""]

        def collect(text: str, final: bool = False) -> None:
            if errors is None:
                return
            lines = (pending[0] + text).split("\n")
            pending[0] = "" if final else lines.pop()
            errors.extend(line.strip() for line in lines if "ERROR" in line)
            del errors[:-10]    # a few recent lines are plenty

        try:
            assert proc.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            if emit:
                formatter = ToolOutputFormatter()
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        tail = decoder.decode(b"", final=True)
                        collect(tail, final=True)
                        if tail and (formatted := formatter.feed(tail)):
                            await self._emit(job, formatted)
                        if flushed := formatter.flush():
                            await self._emit(job, flushed)
                        break
                    text = decoder.decode(chunk)
                    collect(text)
                    if text and (formatted := formatter.feed(text)):
                        await self._emit(job, formatted)
            elif progress_cb is not None:
                last = None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        collect(decoder.decode(b"", final=True), final=True)
                        break
                    text = decoder.decode(chunk)
                    if not text:
                        continue
                    collect(text)
                    info = None
                    pcts = _PCT_RE.findall(text)
                    if pcts:
                        info = f"{int(float(pcts[-1]))}%"
                    elif "ExtractAudio" in text or "Saving media" in text or "Destination" in text:
                        info = "converting"
                    if info and info != last:
                        last = info
                        await progress_cb(info)
            else:
                while chunk := await proc.stdout.read(8192):
                    collect(decoder.decode(chunk))
                collect(decoder.decode(b"", final=True), final=True)
            return await proc.wait()
        finally:
            job.procs.discard(proc)

    async def _run_job(self, job: Job) -> None:
        job.status = "running"
        job.started = time.time()
        await self._send(job, {"type": "status", "job": job.to_dict()})
        try:
            if job.argv is None:
                await self._run_tracklist_job(job)
            else:
                await self._emit(job, f"\r\n\x1b[1;36m$ {job.display_cmd}\x1b[0m\r\n")
                staged = bool(job.settings.get("_stage_dir"))
                filing = done = None
                if staged:
                    self._write_known(job)
                    _make_hidden_dir(Path(job.settings["_stage_dir"]).parent)
                    Path(job.settings["_stage_dir"]).mkdir(parents=True, exist_ok=True)
                    # Each file is filed as soon as the downloader reports it, so a playlist's
                    # songs appear one by one — and survive a crash part-way through.
                    done = asyncio.Event()
                    filing = asyncio.create_task(self._file_while_running(job, done))
                before = _audio_snapshot(job) if job.out_dir is None and not staged else None
                try:
                    job.code = await self._stream_subprocess(job, job.argv)
                finally:
                    # Whatever finished is filed, even from a run that failed or was cancelled.
                    if staged:
                        done.set()
                        await filing                 # lets a file being moved finish, never cut short
                        await self._settle_staged(job)
                    else:
                        job.produced.extend(_collect_produced(job, before))
                if job.status != "cancelled":
                    job.status = "done" if job.code == 0 else "error"
                    if job.status == "done":
                        job.progress = 100
        except FileNotFoundError as exc:
            job.status = "error"
            await self._emit(job, f"\r\n\x1b[31m[error] executable not found: {exc}\x1b[0m\r\n")
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            await self._emit(job, f"\r\n\x1b[31m[error] {exc}\x1b[0m\r\n")
        finally:
            job.finished = time.time()
            if job.status == "error" and not job.note:
                job.note = _failure_note(job.output)
            job.procs.clear()
            self.owners.flush(force=True)
            colour = {"done": "32", "cancelled": "33", "error": "31"}.get(job.status, "0")
            await self._emit(
                job, f"\x1b[{colour}m[{job.status}] exit={job.code}\x1b[0m\r\n"
            )
            await self._send(job, {"type": "status", "job": job.to_dict()})
            self._persist()

    # ----- link jobs: staged downloads filed by recording -----------------
    def _write_known(self, job: Job) -> None:
        """Tell the downloader which recordings the output folder already has (source -> file),
        so a re-run skips them by ID rather than by a title another video might share."""
        path = job.settings.get("_known_file")
        if not path:
            return
        known: dict[str, str] = {}
        if job.settings.get("skip_existing", True):
            out_dir = Path(job.settings["output_dir"])
            for source, file in self.owners.links_in(out_dir, _requested_as(job.settings)).items():
                if _usable_media(Path(file)):
                    known[source] = file
        try:
            Path(path).write_text(json.dumps(known), encoding="utf-8")
        except OSError:
            pass

    # How often a running link job's newly finished files are filed (seconds).
    _FILE_EVERY = 1.0

    async def _file_while_running(self, job: Job, done: asyncio.Event) -> None:
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), self._FILE_EVERY)
            except asyncio.TimeoutError:
                try:
                    await self._file_reported(job)
                except Exception as exc:  # noqa: BLE001 — the final pass after the run tries again
                    print(f"[OmniDL] Filing {job.id}'s downloads hit a problem: {exc}")

    async def _settle_staged(self, job: Job) -> None:
        """After the downloader exits: file whatever is still waiting, then remove the staging
        folder (half-finished downloads with it) and the job's bookkeeping files."""
        s = job.settings
        try:
            await self._file_reported(job)
        finally:
            self._manifest_pos.pop(job.id, None)
            stage = Path(s["_stage_dir"])
            shutil.rmtree(stage, ignore_errors=True)
            try:
                stage.parent.rmdir()                  # the staging folder, once nothing's in it
            except OSError:
                pass
            for key in ("_produced_file", "_known_file"):
                if s.get(key):
                    Path(s[key]).unlink(missing_ok=True)

    async def _file_reported(self, job: Job) -> None:
        """File the downloads the manifest reported since the last look into the output folder.

        Each one goes under its own title unless a different recording already holds that name,
        in which case it becomes "Title (2)" — the other file is never replaced or taken for
        this one. The same recording already there (a re-run, or another job that just saved
        it) is reused.
        """
        s = job.settings
        stage = Path(s["_stage_dir"])
        out_dir = Path(s["output_dir"])
        flat = job.engine != "soundcloud"           # scdl keeps playlist tracks in a subfolder
        want = _requested_as(s)
        replace = not s.get("skip_existing", True)
        entries, self._manifest_pos[job.id] = _read_manifest(s.get("_produced_file"),
                                                             self._manifest_pos.get(job.id, 0))
        for entry in entries:
            try:
                if entry.get("existing"):
                    path = Path(entry["existing"])
                    if _inside(path, out_dir) and path.is_file():
                        job.produced.append(str(path))
                    continue
                staged = Path(entry.get("filepath") or "")
                if not staged.is_file() or not _inside(staged, stage):
                    continue
                parts = staged.resolve().relative_to(stage.resolve()).parts
                folder = out_dir if flat or len(parts) == 1 else out_dir.joinpath(*parts[:-1])
                source = entry.get("source") or source_key(entry)
                placed, how = await self._file_link_download(job, staged, folder, source, entry, want, replace)
                if placed is None:
                    await self._emit(job, f"\x1b[31m✗ couldn't save {staged.name}: every name for it "
                                          "is taken by other recordings\x1b[0m\r\n")
                    continue
                job.produced.append(str(placed))
                if how == "kept":
                    await self._emit(job, f"\x1b[33m⏭ already downloaded:\x1b[0m {placed.name}\r\n")
                elif placed.name != staged.name:
                    await self._emit(job, f"\x1b[33m↪ saved as \"{placed.name}\"\x1b[0m \x1b[2m(a different "
                                          f"recording already has the name \"{staged.name}\")\x1b[0m\r\n")
            except Exception as exc:  # noqa: BLE001 — one file that can't be filed mustn't stop the rest
                await self._emit(job, f"\x1b[31m[error] couldn't file {entry.get('filepath') or entry}: "
                                      f"{exc}\x1b[0m\r\n")

    async def _file_link_download(self, job: Job, staged: Path, folder: Path, source: str,
                                  entry: dict, want: str, replace: bool) -> tuple[Path | None, str]:
        """Put one staged link download in `folder`. Returns (path, "new" | "replaced" | "kept")."""
        record = {"kind": "link", "source": source, "title": entry.get("title"),
                  "url": entry.get("webpage_url") or entry.get("original_url"), "as": want, "job": job.id}
        for n in range(1, 100):
            target = folder / (staged.name if n == 1 else f"{staged.stem} ({n}){staged.suffix}")
            async with self._claims.claim(target):
                if not target.exists():
                    try:
                        await asyncio.to_thread(_move_new, staged, target)
                    except FileExistsError:
                        continue                       # appeared from outside OmniDL just now
                    self.owners.record(target, **record)
                    return target, "new"
                if await asyncio.to_thread(self._holds_link, target, source, record["url"]):
                    usable = await asyncio.to_thread(_usable_media, target)
                    if replace or not usable:
                        # Asked for a fresh copy (or the old one is broken, kept aside).
                        await asyncio.to_thread(self._publish, staged, target, None if usable else target)
                        self.owners.record(target, **record)
                        return target, "replaced"
                    staged.unlink(missing_ok=True)
                    self.owners.record(target, **record)
                    return target, "kept"
            # A different recording, or one that can't be told, has this name: try the next.
        return None, ""

    def _song_holder(self, target: Path, track) -> str:
        """Whose file is at a matched song's name: "free" (nothing usable there), "same" (this
        song) or "other" (a different recording — or one that can't be told, which is kept and
        never taken for this song).

        A filename isn't enough: "A/B - Song" and "A_B - Song" are both saved as "A_B - Song".
        OmniDL used to remember who wrote each name only until it restarted. Now: a file the
        lasting record says came from a pasted link is never a matched song; otherwise the
        file's own artist/title tags decide (they say what it holds now, even if someone
        retagged it), and the record stands in for tags a format couldn't keep.
        """
        if not target.exists():
            return "free"
        rec = self.owners.get(target)
        if rec is not None and rec.get("kind") == "link":
            return "other"                    # a pasted link's download, not a verified match
        artist, title = tag_names(target)
        if not (artist and title) and rec is not None and rec.get("kind") == "track":
            artist, title = rec.get("artists", ""), rec.get("title", "")
        if artist and title:
            return "same" if same_song(artist, title, track.all_artists, track.title) else "other"
        return "free" if _media_length(target) is None else "other"

    def _holds_link(self, target: Path, source: str, url: str | None) -> bool:
        """Is `target` this link's recording? By OmniDL's record, else (for a file saved
        before records were kept) by the page address yt-dlp/scdl embedded in its tags."""
        rec = self.owners.get(target)
        if rec is not None:
            return rec.get("kind") == "link" and rec.get("source") == source
        return bool(url) and url in tag_urls(target)

    async def _run_tracklist_job(self, job: Job) -> None:
        """Download a list of known tracks, taking only strict external matches.

        Two things produce that list: resolving a Spotify link, or parsing a pasted block
        of song names. Everything downstream of the list — scoring, the right-song gate,
        skip-existing, the retry sweep, the review report — is identical, so it lives here
        once instead of being reimplemented per source.
        """
        from . import spotify_resolver as sr

        settings = job.settings or load_settings()
        if job.tracks is not None:
            tracks = job.tracks
            total = len(tracks)
            where = _SERVICE_NAMES.get(settings.get("bulk_service", "auto"), "all services")
            resolved = None
            await self._emit(job, f"\r\n\x1b[1;32m\U0001f4cb {job.list_name or 'Pasted list'}\x1b[0m \x1b[2m- {total} track{'s' if total != 1 else ''} (searching {where})\x1b[0m\r\n")
        else:
            await self._emit(job, "\r\n\x1b[1;36m\U0001f50e Resolving Spotify link...\x1b[0m \x1b[2m(no API - no Premium)\x1b[0m\r\n")
            resolved = await asyncio.to_thread(sr.resolve, job.spotify_url)
            tracks = resolved.tracks
            total = len(tracks)
            job.list_name = resolved.name or job.list_name
            await self._send(job, {"type": "status", "job": job.to_dict()})
            await self._emit(job, f"\x1b[1;32m\U0001f4cb {resolved.name}\x1b[0m \x1b[2m- {total} track{'s' if total != 1 else ''} ({resolved.kind})\x1b[0m\r\n")
        if resolved is not None and getattr(resolved, "truncated", False):
            # Spotify kept refusing the rest of the list, so this is the start of a larger
            # playlist. Say so unmistakably rather than let the run look complete.
            await self._emit(
                job,
                f"\x1b[33m⚠ Spotify returned only the first {total} tracks of this playlist "
                "(it's rate-limiting requests for the rest).\x1b[0m\r\n",
            )
            await self._emit(
                job,
                f"\x1b[2m   Downloading those {total}. Re-run the playlist later for the rest — "
                "already-downloaded tracks are skipped.\x1b[0m\r\n",
            )
            job.note = f"Spotify returned only the first {total} tracks - re-run later for the rest"
            job.incomplete_list = True

        if settings.get("skip_existing", True):
            await self._emit(job, "\x1b[2m   Indexing existing music across folders and formats...\x1b[0m\r\n")
            try:
                settings["_library_index"] = await asyncio.to_thread(
                    build_library_index, Path(settings["output_dir"]),
                    settings.get("naming_order", "artist-title"),
                )
                indexed = len(settings["_library_index"].tracks)
                await self._emit(job, f"\x1b[2m   Indexed {indexed} existing audio file{'s' if indexed != 1 else ''}.\x1b[0m\r\n")
            except OSError as exc:
                await self._emit(job, f"\x1b[33m   Could not index existing music: {exc}\x1b[0m\r\n")

        concurrency = max(1, min(8, int(settings.get("concurrency", 1) or 1)))
        if total and concurrency > 1:
            await self._emit(job, f"\x1b[2m   \u21c9 downloading {concurrency} at a time (compact per-track log)\x1b[0m\r\n")

        outcomes: dict[int, TrackOutcome] = {}
        tracks_by_index: dict[int, object] = {}
        # Tracks that will get another go in a retry sweep: not finished business yet.
        awaiting_retry: set[int] = set()
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(concurrency)
        detailed = concurrency == 1
        job.progress = 0

        def advance() -> None:
            # Progress = tracks with a final result. Starting a track used to set it from the
            # track's position and finishing one from the count done, so with parallel
            # downloads it jumped back and forth (0, 25, 50, 25, 75, 50...).
            final = len(outcomes) - len(awaiting_retry & outcomes.keys())
            job.progress = max(job.progress or 0, int(final / total * 100) if total else 100)

        async def handle(index: int, track) -> None:
            async with sem:
                if job.status == "cancelled":
                    return
                job.label = track.query
                await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
                outcome = await self._fetch_track(job, track, settings, detailed, index, total)
                if outcome.status == "cancelled":
                    return
                async with lock:
                    outcomes[index] = outcome
                    tracks_by_index[index] = track
                    if _worth_retrying(outcome):
                        awaiting_retry.add(index)
                    advance()
                    job.label = track.query
                    # Live tally, so a 200-song playlist shows "57 saved · 2 not found" as it goes.
                    job.counts = _tally(outcomes.values(), total)
                await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress,
                                       "label": job.label, "counts": job.counts})

        await asyncio.gather(*(handle(i, track) for i, track in enumerate(tracks, start=1)))

        # Retry sweeps: a track whose right match wouldn't download, or whose search didn't
        # answer, usually lost a transient YouTube-throttling roll during the burst. Re-try
        # them one at a time after the burst — then once more after a longer break — when
        # the throttling has eased. The correct song is usually there.
        sweeps = ((4, 0), (8, 20))
        for sweep, (gap, breather) in enumerate(sweeps, start=1):
            if job.status == "cancelled":
                break
            retryable = [i for i, o in sorted(outcomes.items()) if _worth_retrying(o)]
            if not retryable:
                break
            last_sweep = sweep == len(sweeps)
            when = "" if sweep == 1 else " after a short break"
            await self._emit(job, f"\r\n\x1b[33m\u21bb retrying {len(retryable)} track(s) that hit a "
                                   f"temporary problem{when} \x1b[2m(one at a time)\x1b[0m\r\n")
            job.label = f"Waiting to retry {len(retryable)} track{'s' if len(retryable) != 1 else ''}"
            await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
            await self._sleep_unless_cancelled(job, breather)
            for n, i in enumerate(retryable, start=1):
                if job.status == "cancelled":
                    break
                await self._sleep_unless_cancelled(job, gap)   # let the rate-limit window pass
                job.label = f"Retrying {n}/{len(retryable)}: {tracks_by_index[i].query}"
                await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
                retry = await self._fetch_track(job, tracks_by_index[i], settings, True, i, total)
                if retry.status != "cancelled":
                    outcomes[i] = retry
                    if last_sweep or not _worth_retrying(retry):
                        awaiting_retry.discard(i)
                    advance()
                    job.counts = _tally(outcomes.values(), total)
                    await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress,
                                           "label": job.label, "counts": job.counts})

        if job.status != "cancelled":
            unresolved: list[TrackOutcome] = [
                o for o in outcomes.values()
                if o.status in ("downloaded_for_review", "no_candidate", "rejected", "download_failed")]
            job.counts = _tally(outcomes.values(), total)
            counts = {"ok": job.counts["downloaded"], "review": job.counts["review"],
                      "skip": job.counts["skipped"], "fail": job.counts["failed"]}
            job.failed_tracks = [tracks_by_index[i] for i, o in sorted(outcomes.items())
                                 if o.status in ("no_candidate", "rejected", "download_failed")]
            report_path: Path | None = None
            if unresolved:
                list_title = resolved.name if resolved is not None else (job.list_name or "Pasted list")
                report_path = await self._emit_review_report(job, Path(settings["output_dir"]), list_title, unresolved)
                job.review_count = len(unresolved)
            job.progress = 100
            job.code = 0 if counts["fail"] == 0 else 1
            job.status = "done" if (counts["ok"] + counts["skip"]) > 0 or total == 0 else "error"
            if job.status == "error" and unresolved:
                # The card's one line: the real reason for a single song ("type it as
                # 'Artist - Title'"), not the log's last line, which is the summary bar.
                job.note = (unresolved[0].reason if total == 1 else
                            f"none of the {total} tracks could be downloaded - see the review report")
            bar = "-" * 40
            report_suffix = f"\x1b[2m report={report_path}\x1b[0m" if report_path else ""
            # "Needs review" used to count failures, so a run where every track was saved for
            # review reported "0 needs review" beside a review report listing all of them.
            review_note = f" \x1b[33m({counts['review']} to review)\x1b[0m" if counts["review"] else ""
            await self._emit(job, f"\r\n\x1b[36m{bar}\x1b[0m\r\n\x1b[1;32m\U00002705 {counts['ok']} downloaded\x1b[0m{review_note} \x1b[2m-\x1b[0m \x1b[33m{counts['skip']} skipped\x1b[0m \x1b[2m-\x1b[0m \x1b[31m{counts['fail']} failed\x1b[0m \x1b[2m({total} total)\x1b[0m{report_suffix}\r\n\x1b[36m{bar}\x1b[0m\r\n")

    @staticmethod
    async def _sleep_unless_cancelled(job: Job, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while job.status != "cancelled" and (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(1.0, left))

    async def _wait_for_cooldown(self, job: Job) -> None:
        """Hold a download while OmniDL is backing off from a YouTube rate limit."""
        left = self.pause_until - time.time()
        if left > 0:
            previous = job.label
            job.label = f"Paused {int(left)}s - YouTube is rate-limiting"
            await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
            await self._sleep_unless_cancelled(job, left)
            job.label = previous

    async def _rate_limited(self, job: Job) -> None:
        """YouTube said "slow down": pause every download in every job, longer each time."""
        self.rate_hits += 1
        pause = min(30 * self.rate_hits, 180)
        until = time.time() + pause
        if until > self.pause_until + 1:
            self.pause_until = until
            await self._emit(job, f"\x1b[33m\u23f8 YouTube is rate-limiting this connection - pausing "
                                  f"downloads for {pause}s, then carrying on\x1b[0m\r\n")

    async def _emit_review_report(self, job: Job, output_dir: Path, playlist_name: str, outcomes: list[TrackOutcome]) -> Path | None:
        if not outcomes:
            return None
        try:
            path = await asyncio.to_thread(write_review_report, output_dir, playlist_name, outcomes)
            job.report_path = str(path)
        except OSError as exc:
            await self._emit(job, f"\x1b[31m\u2717 Could not write review report: {exc}\x1b[0m\r\n")
            return None
        await self._emit(job, f"\x1b[33m\U0001f4c4 Review report: {path}\x1b[0m\r\n")
        return path

    async def _fetch_track(self, job: Job, track, settings: dict, detailed: bool,
                           index: int, total: int) -> TrackOutcome:
        """Download the right version of one track and flag anything not verified for review.

        Every entry point — a Spotify playlist, a pasted list, a typed search — lands here, so
        a song is chosen the same way however it was asked for.

        Detailed mode (concurrency 1) streams the full yt-dlp output live. Parallel mode
        shows ONE line per track that updates in place (search -> %, -> saved), so any
        number of concurrent tracks stay live and readable without interleaving.
        """
        service = settings.get("bulk_service", "auto")
        if service not in candidate_search.SERVICE_SOURCES:
            service = "auto"
        if not settings.get("_youtube_ok", True):
            # Submitted by a visitor who hasn't unlocked YouTube: whatever the job says, only
            # SoundCloud may be searched or downloaded for it.
            service = "soundcloud"
        # A bare phrase ("drake one dance") has no artist to match on. Name the song first;
        # if nothing can be named it may not be a song at all, which is handled below.
        # Naming uses YouTube Music, so a SoundCloud-only search never does it: that choice
        # means YouTube isn't contacted at all (and its account isn't used).
        phrase = track.title if not track.artist else ""
        phrase_timed_out = False
        if phrase and service != "soundcloud":
            try:
                named = await asyncio.wait_for(
                    asyncio.to_thread(candidate_search.resolve_phrase, phrase), _SEARCH_DEADLINE)
            except asyncio.TimeoutError:
                named, phrase_timed_out = None, True
            if named:
                artists, title = named
                track = Track(artist=artists[0], title=title, artists=artists)
                phrase = ""

        out_dir = Path(settings["output_dir"])
        ext = settings.get("audio_format", "opus")
        basename = track.filename_for(settings.get("naming_order", "artist-title"),
                                      settings.get("naming_artists", "all"))
        key = f"t{index}"
        shown = f"{track.artist} - {track.title}" if track.artist else track.title
        label = f"\x1b[1;36m[{index}/{total}]\x1b[0m \x1b[1m{shown}\x1b[0m"

        async def status(state: str, store: bool = False) -> None:
            """Detailed mode: append a line. Parallel mode: update this track's one line."""
            if detailed:
                await self._emit(job, f"  {state}\r\n")
            else:
                await self._emit_keyed(job, key, f"{label}  {state}\r\n", store=store)

        async def finish(outcome: TrackOutcome) -> TrackOutcome:
            if not detailed:
                await self._key_done(job, key)
            return outcome

        async def phase(text: str) -> None:
            """What the card says this job is doing right now."""
            job.label = f"{text}: {shown}"
            await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress,
                                   "label": job.label})

        if detailed:
            await self._emit(job, f"\r\n{label}\r\n")

        attempt_name = 1
        elsewhere: set[str] = set()       # files under names this song passed over
        while True:
            name = basename if attempt_name == 1 else f"{basename} ({attempt_name})"
            target = out_dir / f"{name}.{ext}"
            # One writer per output file across every job (see _OutputClaims). Held until the
            # finished file is in place, so a duplicate row or the same song in another
            # playlist waits here and then finds this one's file.
            async with self._claims.claim(target):
                holder = await asyncio.to_thread(self._song_holder, target, track)
                if holder != "other":
                    return await self._fetch_claimed(
                        job, track, settings, detailed, index, out_dir, ext, name, target, holder,
                        elsewhere, phrase, phrase_timed_out, service, status, finish, phase)
            if attempt_name >= 20:
                await status("\x1b[31m✗ every name for this song is taken by other files\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "download_failed",
                                                 "every filename for it is taken by other recordings", []))
            # A different song — or one whose identity can't be told — already has this name
            # (two names that sanitise or truncate to the same thing, a file put there by hand).
            # It is left alone, and this one gets "(2)" instead of being "skipped" as if it
            # were the same song.
            elsewhere.add(os.path.normcase(os.path.realpath(target)))
            attempt_name += 1

    async def _fetch_claimed(self, job: Job, track, settings: dict, detailed: bool, index: int,
                             out_dir: Path, ext: str, basename: str, target: Path, holder: str,
                             elsewhere: set[str], phrase: str, phrase_timed_out: bool,
                             service: str, status, finish, phase) -> TrackOutcome:
        """The rest of _fetch_track, run while this track holds the claim on `target`.

        `holder` is "same" when the file at `target` is this song (by OmniDL's record or its
        tags), "free" when there is none \u2014 or only an empty/unreadable one, kept aside."""
        skip = settings.get("skip_existing", True)
        broken: Path | None = None
        if target.exists():
            if holder == "same" and not skip:
                pass                                   # replaced by the fresh download below
            elif holder == "same" and _usable_media(target, track.duration):
                await status("\x1b[33m\u23ed already downloaded\x1b[0m", store=True)
                # Still this job's result: from another device, Save should hand it over rather
                # than a re-run playlist finishing with nothing to save.
                job.produced.append(str(target))
                self.owners.record_track(target, track, job.id)
                return await finish(TrackOutcome(track, "skipped", "output file already exists", [],
                                                 saved_as=target.name))
            else:
                broken = target
                await status("\x1b[33m\u26a0 the existing file is empty or unreadable - "
                             "downloading a fresh copy\x1b[0m", store=True)
        if skip and broken is None and settings.get("_library_index") and track.artist:
            existing = settings["_library_index"].find(track.artist, track.title, track.duration,
                                                       exclude=elsewhere)
            if existing is not None:
                relative = existing.relative_path
                job.produced.append(str(existing.path))
                await status(f"\x1b[33m\u23ed already in library\x1b[0m \x1b[2m({relative})\x1b[0m", store=True)
                return await finish(TrackOutcome(
                    track, "skipped", f"matching library file already exists: {relative}", [],
                ))

        if phrase:
            if phrase_timed_out:
                msg = "YouTube Music didn't answer while naming this song - will retry at the end"
                await status(f"\x1b[33m\u26a0 {msg}\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "no_candidate", f"search failed: {msg}", [],
                                                 retryable=True))
            # Not identifiable as a song. This used to download YouTube's top result for
            # the words, unverified — exactly how a music video, live cut or the wrong song
            # gets saved. Nothing is downloaded on a guess now.
            hint = ("SoundCloud-only searches need the artist: type it as 'Artist - Title'"
                    if service == "soundcloud" else
                    "couldn't tell which song this is: type it as 'Artist - Title', or paste a link")
            await status(f"\x1b[33m\u26a0 {hint}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "no_candidate", hint, []))

        # Downloads land in a folder of their own and only move to their real name once
        # complete, so nothing half-written or mid-conversion ever sits at the final path, and
        # cleaning up a failed attempt can't touch another track's files.
        stage = out_dir / STAGING_DIR / f"{job.id}-{index}"
        staged = stage / f"{basename}.{ext}"
        stage_settings = {**settings, "output_dir": str(stage)}
        _make_hidden_dir(stage.parent)

        async def download(url: str, source: str, what: str) -> tuple[bool, str]:
            """Fetch one URL, retrying the same candidate. Returns (ok, last error)."""
            async def progress(info: str) -> None:
                await status(f"\x1b[36m\u2913 {info} \x1b[2m({source})\x1b[0m")

            why = ""
            for attempt in range(1, _DL_ATTEMPTS + 1):
                if job.status == "cancelled":
                    return False, "cancelled"
                await self._wait_for_cooldown(job)
                if job.status == "cancelled":
                    return False, "cancelled"
                suffix = "" if attempt == 1 else f" \x1b[2m(retry {attempt - 1})\x1b[0m"
                await status(f"\x1b[2m\u2192 trying {source} ({what}){suffix}...\x1b[0m")
                await phase("Downloading")
                errors: list[str] = []
                code = await self._stream_subprocess(
                    job, engines.media_url_command(url, basename, stage_settings),
                    emit=detailed, progress_cb=None if detailed else progress, errors=errors)
                self._cleanup_sidecars(stage, basename, ext)
                try:
                    complete = code == 0 and staged.stat().st_size > 0
                except OSError:
                    complete = False
                if complete:
                    self.rate_hits = max(0, self.rate_hits - 1)
                    return True, ""
                shutil.rmtree(stage, ignore_errors=True)      # no partial file carries over
                why = _short_error(errors) or ("empty file" if code == 0 else f"exit code {code}")
                if _RATE_LIMIT_RE.search("\n".join(errors)):
                    await self._rate_limited(job)
                if attempt < _DL_ATTEMPTS and job.status != "cancelled":
                    await status(f"\x1b[2m{source} hiccup ({why}) - retrying...\x1b[0m")
                    await asyncio.sleep(_DL_BACKOFF * attempt)
            return False, why

        try:
            await status("\x1b[2m\U0001f50e searching sources...\x1b[0m")
            await phase("Searching")
            allowed = candidate_search.SERVICE_SOURCES[service]
            # Strict matching (the default): only a verified match is ever downloaded. On 120
            # real songs every remaining wrong pick was an unverified "plausible" one, so those
            # now go to the review report — with their links — instead of into the library.
            strict = settings.get("strict_matching", True)

            async def search(deep: bool):
                try:
                    found = await asyncio.wait_for(asyncio.to_thread(
                        candidate_search.search_all, track.artist, track.title, service, deep=deep),
                        _SEARCH_DEADLINE)
                except asyncio.TimeoutError:
                    # A search that hangs mustn't hold this track (and a worker) forever; it
                    # counts as "didn't answer" and is retried at the end.
                    return [], set(allowed)
                failed = set(getattr(found, "failed", ()) or ())
                # Belt and braces for the source choice: never download from a service the
                # user (or the access gate) excluded, whatever the search layer returned.
                return [c for c in found if c.source in allowed], failed

            def pick(found):
                picked = choose(
                    track.artist, track.title, track.duration, found,
                    prefer_ytmusic=settings.get("prefer_ytmusic", True),
                    use_duration=settings.get("spotify_match_duration", True),
                )
                return picked, ([d for d in picked.eligible if d.accepted] if strict else picked.eligible)

            candidates, failed = await search(False)
            choice, pool = pick(candidates)
            if not pool and job.status != "cancelled":
                # Nothing safe to download yet. Before giving up, search again in other words
                # (plain song name, more results, audio uploads first) — this is what gets the
                # songs whose first page of results was all videos, covers and live cuts.
                await status("\x1b[2m\U0001f50e no verified match yet - searching again...\x1b[0m")
                more, failed_again = await search(True)
                seen = {c.url for c in candidates}
                candidates += [c for c in more if c.url not in seen]
                failed &= failed_again          # a source that answered the second time is fine
                choice, pool = pick(candidates)
            decisions = choice.decisions
            unanswered = ", ".join(_SOURCE_NAMES.get(src, src) for src in sorted(failed))
            if not decisions:
                if unanswered:
                    msg = f"{unanswered} didn't answer - will retry at the end"
                    await status(f"\x1b[33m\u26a0 {msg}\x1b[0m", store=True)
                    return await finish(TrackOutcome(track, "no_candidate", f"search failed: {msg}", [],
                                                     retryable=True))
                await status("\x1b[33m\u26a0 no source found; added to review report\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "no_candidate", "no external candidates found", []))

            # Only ever download a candidate that is this song AND this version (see choose()).
            # If the best one won't download, only the same recording may stand in for it:
            # under load the right version can fail and succeed a minute later, and taking
            # "the next plausible candidate" instead quietly saved a different edit that no
            # retry ever corrected. Failing here hands the track to the retry sweep instead.
            reference = pool[0] if pool else None
            stand_ins = [d for d in pool if d is reference or same_recording(d, reference)][:5]
            selected = None
            attempts: list[MatchDecision] = []
            last_error = ""
            for decision in stand_ins:
                if job.status == "cancelled":
                    return await finish(TrackOutcome(track, "cancelled", "job cancelled", decisions[:5]))
                cand = decision.candidate
                what = "verified match" if decision.accepted else f"score {decision.score}/100"
                ok, why = await download(cand.url, cand.source, what)
                if ok:
                    selected = decision
                    break
                if job.status == "cancelled":
                    break
                attempts.append(decision)
                last_error = why
                await status(f"\x1b[2m{cand.source} ({what}) unavailable: {why} - "
                             f"trying the same recording elsewhere\x1b[0m")

            if job.status == "cancelled":
                return await finish(TrackOutcome(track, "cancelled", "job cancelled", decisions[:5]))
            if selected is None:
                if attempts:
                    msg = f"the right version wouldn't download ({last_error})"
                    reason = f"all candidate downloads failed ({last_error})"
                elif strict and choice.eligible:
                    msg = "no verified match - not downloaded, to avoid a wrong version"
                    reason = ("no verified match: the closest candidates couldn't be confirmed as "
                              "this exact version (strict matching)")
                else:
                    msg = "no confident match found (skipped to avoid a wrong version)"
                    reason = "no confident match available"
                if unanswered and not attempts:
                    # Part of the search didn't answer, so the right match may simply not
                    # have been seen yet: worth another go at the end.
                    msg += f" ({unanswered} didn't answer - will retry)"
                    reason += f"; {unanswered} didn't answer"
                await status(f"\x1b[31m\u2717 {msg}; added to review\x1b[0m", store=True)
                return await finish(TrackOutcome(
                    track, "download_failed", reason,
                    decisions[:5], failed_attempts=tuple(attempts),
                    retryable=bool(unanswered) and not attempts,
                ))

            await phase("Saving")
            await asyncio.to_thread(self._tag, staged, track)
            await asyncio.to_thread(self._publish, staged, target, broken)
            self.owners.record_track(target, track, job.id)
            job.produced.append(str(target))
            result = "downloaded" if selected.accepted else "downloaded_for_review"
            tail = f"\x1b[2m{target.name} ({selected.candidate.source}, {selected.score}/100)\x1b[0m"
            if selected.accepted:
                await status(f"\x1b[32m\u2713 saved\x1b[0m {tail}", store=True)
            else:
                await status(f"\x1b[33m\u26a0 saved for review\x1b[0m {tail}", store=True)
            return await finish(TrackOutcome(
                track, result,
                "verified match" if selected.accepted else selected.reason,
                decisions[:5], selected=selected, saved_as=target.name,
                failed_attempts=tuple(attempts),
            ))
        except FileNotFoundError as exc:
            await status(f"\x1b[31m\u2717 {exc}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "download_failed", str(exc), []))
        except Exception as exc:  # noqa: BLE001
            await status(f"\x1b[31m\u2717 {exc}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "download_failed", str(exc), []))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            try:
                stage.parent.rmdir()                  # the staging folder, once nothing's in it
            except OSError:
                pass

    @staticmethod
    def _publish(staged: Path, target: Path, broken: Path | None) -> None:
        """Move a finished download to its real name in one step. A broken file that was in
        the way is kept beside it ("... (unreadable).opus"), never deleted."""
        if broken is not None and broken.exists():
            n = 1
            while True:
                suffix = " (unreadable)" if n == 1 else f" (unreadable {n})"
                keep = broken.with_name(f"{broken.stem}{suffix}{broken.suffix}")
                if not keep.exists():
                    break
                n += 1
            os.replace(broken, keep)
        os.replace(staged, target)

    @staticmethod
    def _cleanup_sidecars(out_dir: Path, basename: str, audio_ext: str) -> None:
        """Remove leftover thumbnail/partial files (e.g. a stray .webp left when audio
        extraction failed) so only the real audio file is kept."""
        junk = {".webp", ".png", ".jpg", ".jpeg", ".part", ".ytdl", ".temp", ".tmp"}
        try:
            for path in out_dir.glob(glob.escape(basename) + ".*"):
                if path.suffix.lower() != f".{audio_ext.lower()}" and path.suffix.lower() in junk:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    @staticmethod
    def _tag(target, track) -> None:
        try:
            from . import tagging
            tagging.apply_tags(target, track)
        except Exception:
            pass
