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
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import bulk_input, candidate_search, engines
from .library import build_library_index
from .matching import MatchDecision, choose, same_recording
from .review_report import TrackOutcome, write_review_report
from . import settings as settings_mod
from .settings import DOWNLOAD_ROOT, FILE_TTL_SECONDS, PROJECT_ROOT, load_settings
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
_HISTORY_MAX = 100
_HISTORY_OUTPUT_CAP = 12_000

# Cap how much output we retain per job for reconnect/replay (characters).
_OUTPUT_CAP = 200_000

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


def _short_error(lines: list[str]) -> str:
    text = "\n".join(lines)
    for pattern, label in _ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            return label or match.group(0).strip()[:60]
    last = lines[-1] if lines else ""
    return last.replace("ERROR:", "").strip()[:80]


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
        self.progress: int | None = None
        self.label = ""
        self.procs: set[asyncio.subprocess.Process] = set()  # live subprocesses

    def append(self, text: str) -> None:
        self.output += text
        if len(self.output) > _OUTPUT_CAP:
            self.output = self.output[-_OUTPUT_CAP:]

    def list_files(self) -> list[Path]:
        """Files this job produced, available to download (newest run only)."""
        if not self.out_dir or not self.out_dir.exists():
            return []
        return sorted(p for p in self.out_dir.iterdir() if p.is_file())

    def has_files(self) -> bool:
        return bool(self.list_files())

    def to_dict(self) -> dict:
        meta = engines.ENGINES.get(self.engine, {"label": self.engine, "tool": self.engine})
        return {
            "id": self.id,
            "input": self.input,
            "kind": self.kind,
            "engine": self.engine,
            "engine_label": _KIND_LABELS.get(self.kind, meta["label"]),
            "tool": meta["tool"],
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
            "has_files": self.status == "done" and self.has_files(),
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
        job.status = status
        job.code = rec.get("code")
        job.created = rec.get("created", time.time())
        job.started = rec.get("started")
        job.finished = rec.get("finished")
        job.progress = rec.get("progress")
        job.label = rec.get("label", "")
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

    # ----- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if not self._worker_tasks:
            self._load_history()
            self._worker_tasks = [asyncio.create_task(self._worker()) for _ in range(_WORKER_COUNT)]
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    # ----- persistence ---------------------------------------------------
    def _load_history(self) -> None:
        try:
            if not _HISTORY_PATH.exists():
                return
            for rec in json.loads(_HISTORY_PATH.read_text(encoding="utf-8")):
                job = Job.from_record(rec)
                self.jobs[job.id] = job
                self.order.append(job.id)
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            recs = [self.jobs[i].to_record() for i in self.order[-_HISTORY_MAX:] if i in self.jobs]
            _HISTORY_PATH.write_text(json.dumps(recs), encoding="utf-8")
        except Exception:
            pass

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
            out_dir = Path(s.get("output_dir") or DOWNLOAD_ROOT)
        else:
            # Hosted mode: each job gets its own isolated folder, so we know exactly which
            # files it produced and can hand only those to the visitor who created it.
            out_dir = DOWNLOAD_ROOT / session / job.id
            job.out_dir = out_dir
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
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

    async def retry(self, job_id: str, session: str, settings_dict: dict, ip: str = "") -> Job | str:
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
    def _remove_files(job: Job) -> None:
        if job.out_dir and job.out_dir.exists():
            shutil.rmtree(job.out_dir, ignore_errors=True)

    async def _cleanup_loop(self) -> None:
        """Delete finished jobs' files once they age out, to bound disk use."""
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for job in list(self.jobs.values()):
                if job.status in ("done", "error", "cancelled") and job.finished \
                        and now - job.finished > FILE_TTL_SECONDS:
                    if job.out_dir and job.out_dir.exists():
                        self._remove_files(job)
                        await self._send(job, {"type": "status", "job": job.to_dict()})

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
        await self._send(job, {"type": "output", "job_id": job.id, "data": text})
        if self._update_progress(job, text):
            await self._send(job, {
                "type": "progress", "job_id": job.id,
                "progress": job.progress, "label": job.label,
            })

    async def _emit_keyed(self, job: Job, key: str, text: str, store: bool = False) -> None:
        """Emit a line that updates in place on the client (one per concurrent track).
        Only the final state (store=True) is kept in the job log for reconnect replay."""
        if store:
            job.append(text)
        await self._send(job, {"type": "output", "job_id": job.id, "data": text, "key": key})

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
                job.code = await self._stream_subprocess(job, job.argv)
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
            job.procs.clear()
            colour = {"done": "32", "cancelled": "33", "error": "31"}.get(job.status, "0")
            await self._emit(
                job, f"\x1b[{colour}m[{job.status}] exit={job.code}\x1b[0m\r\n"
            )
            await self._send(job, {"type": "status", "job": job.to_dict()})
            self._persist()

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
            await self._emit(job, f"\x1b[1;32m\U0001f4cb {resolved.name}\x1b[0m \x1b[2m- {total} track{'s' if total != 1 else ''} ({resolved.kind})\x1b[0m\r\n")
        if resolved is not None and getattr(resolved, "truncated", False):
            # Spotify's embed tops out at 100 and its API refused the rest, so this is the
            # first 100 of a larger playlist. Say so unmistakably rather than let the run
            # look like a complete playlist.
            await self._emit(
                job,
                "\x1b[33m⚠ Spotify returned only the first 100 tracks of this playlist "
                "(its API is rate-limiting anonymous requests).\x1b[0m\r\n",
            )
            await self._emit(
                job,
                "\x1b[2m   Downloading those 100. Re-run the playlist later for the rest — "
                "already-downloaded tracks are skipped.\x1b[0m\r\n",
            )

        if settings.get("skip_existing", True):
            await self._emit(job, "\x1b[2m   Indexing existing music across folders and formats...\x1b[0m\r\n")
            try:
                settings["_library_index"] = await asyncio.to_thread(
                    build_library_index, Path(settings["output_dir"]),
                )
                indexed = len(settings["_library_index"].tracks)
                await self._emit(job, f"\x1b[2m   Indexed {indexed} existing audio file{'s' if indexed != 1 else ''}.\x1b[0m\r\n")
            except OSError as exc:
                await self._emit(job, f"\x1b[33m   Could not index existing music: {exc}\x1b[0m\r\n")

        concurrency = max(1, min(8, int(settings.get("concurrency", 1) or 1)))
        if total and concurrency > 1:
            await self._emit(job, f"\x1b[2m   ? downloading {concurrency} at a time (compact per-track log)\x1b[0m\r\n")

        outcomes: dict[int, TrackOutcome] = {}
        tracks_by_index: dict[int, object] = {}
        done = {"n": 0}
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(concurrency)
        detailed = concurrency == 1

        async def handle(index: int, track) -> None:
            async with sem:
                if job.status == "cancelled":
                    return
                async with lock:
                    job.label = track.query
                    job.progress = int((index - 1) / total * 100) if total else 0
                await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
                outcome = await self._fetch_track(job, track, settings, detailed, index, total)
                if outcome.status == "cancelled":
                    return
                async with lock:
                    outcomes[index] = outcome
                    tracks_by_index[index] = track
                    done["n"] += 1
                    job.progress = int(done["n"] / total * 100) if total else 100
                    job.label = track.query
                await self._send(job, {"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})

        await asyncio.gather(*(handle(i, track) for i, track in enumerate(tracks, start=1)))

        # Retry sweep: tracks that *failed to download* a real match (had attempts) usually
        # lost a transient YouTube-throttling roll during the burst. Re-try them one at a
        # time, after the burst, when the throttling has eased — the correct song is there.
        if job.status != "cancelled":
            retryable = [i for i, o in outcomes.items()
                         if o.status == "download_failed" and o.failed_attempts]
            if retryable:
                await self._emit(job, f"\r\n\x1b[33m? retrying {len(retryable)} track(s) that hit YouTube "
                                       f"throttling \x1b[2m(one at a time)\x1b[0m\r\n")
                for i in retryable:
                    if job.status == "cancelled":
                        break
                    await asyncio.sleep(4)  # let the rate-limit window pass
                    retry = await self._fetch_track(job, tracks_by_index[i], settings, True, i, total)
                    if retry.status != "cancelled":
                        outcomes[i] = retry

        if job.status != "cancelled":
            counts = {"ok": 0, "review": 0, "skip": 0, "fail": 0}
            unresolved: list[TrackOutcome] = []
            for outcome in outcomes.values():
                if outcome.status in ("downloaded", "downloaded_for_review"):
                    counts["ok"] += 1
                    counts["review"] += outcome.status == "downloaded_for_review"
                elif outcome.status == "skipped":
                    counts["skip"] += 1
                else:
                    counts["fail"] += 1
                if outcome.status in ("downloaded_for_review", "no_candidate", "rejected", "download_failed"):
                    unresolved.append(outcome)

            report_path: Path | None = None
            if unresolved:
                list_title = resolved.name if resolved is not None else (job.list_name or "Pasted list")
                report_path = await self._emit_review_report(job, Path(settings["output_dir"]), list_title, unresolved)
            job.progress = 100
            job.code = 0 if counts["fail"] == 0 else 1
            job.status = "done" if (counts["ok"] + counts["skip"]) > 0 or total == 0 else "error"
            bar = "-" * 40
            report_suffix = f"\x1b[2m report={report_path}\x1b[0m" if report_path else ""
            # "Needs review" used to count failures, so a run where every track was saved for
            # review reported "0 needs review" beside a review report listing all of them.
            review_note = f" \x1b[33m({counts['review']} to review)\x1b[0m" if counts["review"] else ""
            await self._emit(job, f"\r\n\x1b[36m{bar}\x1b[0m\r\n\x1b[1;32m\U00002705 {counts['ok']} downloaded\x1b[0m{review_note} \x1b[2m-\x1b[0m \x1b[33m{counts['skip']} skipped\x1b[0m \x1b[2m-\x1b[0m \x1b[31m{counts['fail']} failed\x1b[0m \x1b[2m({total} total)\x1b[0m{report_suffix}\r\n\x1b[36m{bar}\x1b[0m\r\n")

    async def _emit_review_report(self, job: Job, output_dir: Path, playlist_name: str, outcomes: list[TrackOutcome]) -> Path | None:
        if not outcomes:
            return None
        try:
            path = await asyncio.to_thread(write_review_report, output_dir, playlist_name, outcomes)
        except OSError as exc:
            await self._emit(job, f"\x1b[31m? Could not write review report: {exc}\x1b[0m\r\n")
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
        # A bare phrase ("drake one dance") has no artist to match on. Name the song first;
        # if nothing can be named it may not be a song at all, which is handled below.
        phrase = track.title if not track.artist else ""
        if phrase:
            named = await asyncio.to_thread(candidate_search.resolve_phrase, phrase)
            if named:
                artists, title = named
                track = Track(artist=artists[0], title=title, artists=artists)
                phrase = ""

        out_dir = Path(settings["output_dir"])
        ext = settings.get("audio_format", "opus")
        basename = track.filename_for(settings.get("naming_order", "artist-title"),
                                      settings.get("naming_artists", "all"))
        target = out_dir / f"{basename}.{ext}"
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

        async def download(url: str, source: str, what: str) -> tuple[bool, str]:
            """Fetch one URL, retrying the same candidate. Returns (ok, last error)."""
            async def progress(info: str) -> None:
                await status(f"\x1b[36m? {info} \x1b[2m({source})\x1b[0m")

            why = ""
            for attempt in range(1, _DL_ATTEMPTS + 1):
                if job.status == "cancelled":
                    return False, "cancelled"
                suffix = "" if attempt == 1 else f" \x1b[2m(retry {attempt - 1})\x1b[0m"
                await status(f"\x1b[2m? trying {source} ({what}){suffix}...\x1b[0m")
                errors: list[str] = []
                code = await self._stream_subprocess(
                    job, engines.media_url_command(url, basename, settings),
                    emit=detailed, progress_cb=None if detailed else progress, errors=errors)
                self._cleanup_sidecars(out_dir, basename, ext)
                if code == 0 and target.exists():
                    return True, ""
                why = _short_error(errors) or f"exit code {code}"
                if attempt < _DL_ATTEMPTS and job.status != "cancelled":
                    await status(f"\x1b[2m{source} hiccup ({why}) - retrying...\x1b[0m")
                    await asyncio.sleep(_DL_BACKOFF * attempt)
            return False, why

        if detailed:
            await self._emit(job, f"\r\n{label}\r\n")
        if settings.get("skip_existing", True) and target.exists():
            await status("\x1b[33m? already downloaded\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "skipped", "output file already exists", []))
        if settings.get("skip_existing", True) and settings.get("_library_index") and track.artist:
            existing = settings["_library_index"].find(track.artist, track.title, track.duration)
            if existing is not None:
                relative = existing.relative_path
                await status(f"\x1b[33m? already in library\x1b[0m \x1b[2m({relative})\x1b[0m", store=True)
                return await finish(TrackOutcome(
                    track, "skipped", f"matching library file already exists: {relative}", [],
                ))

        try:
            if phrase:
                # Not identifiable as a song (a mix, a podcast, a vague phrase). Do what a
                # YouTube search does — take its top result — but never call it verified,
                # and keep the upload's own tags rather than inventing song metadata.
                ok, why = await download(f"ytsearch1:{phrase}", "youtube", "top search result")
                if job.status == "cancelled":
                    return await finish(TrackOutcome(track, "cancelled", "job cancelled", []))
                if not ok:
                    await status(f"\x1b[31m? couldn't download ({why}); added to review\x1b[0m", store=True)
                    return await finish(TrackOutcome(
                        track, "download_failed", f"no song identified; search download failed ({why})", []))
                await status(f"\x1b[33m? saved for review\x1b[0m \x1b[2m{target.name} "
                             f"(no song identified - YouTube's top result)\x1b[0m", store=True)
                return await finish(TrackOutcome(
                    track, "downloaded_for_review",
                    "no song could be identified from this text, so YouTube's top result was saved",
                    [], saved_as=target.name))

            await status("\x1b[2m?? searching sources...\x1b[0m")
            candidates = await asyncio.to_thread(
                candidate_search.search_all, track.artist, track.title,
                settings.get("bulk_service", "auto"))
            choice = choose(
                track.artist, track.title, track.duration, candidates,
                prefer_ytmusic=settings.get("prefer_ytmusic", True),
                use_duration=settings.get("spotify_match_duration", True),
            )
            decisions = choice.decisions
            if not decisions:
                await status("\x1b[33m?? no source found; added to review report\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "no_candidate", "no external candidates found", []))

            # Only ever download a candidate that is this song AND this version (see choose()).
            # If the best one won't download, only the same recording may stand in for it:
            # under load the right version can fail and succeed a minute later, and taking
            # "the next plausible candidate" instead quietly saved a different edit that no
            # retry ever corrected. Failing here hands the track to the retry sweep instead.
            reference = choice.eligible[0] if choice.eligible else None
            stand_ins = [d for d in choice.eligible
                         if d is reference or same_recording(d, reference)][:5]
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
                else:
                    msg = "no confident match found (skipped to avoid a wrong version)"
                    reason = "no confident match available"
                await status(f"\x1b[31m? {msg}; added to review\x1b[0m", store=True)
                return await finish(TrackOutcome(
                    track, "download_failed", reason,
                    decisions[:5], failed_attempts=tuple(attempts),
                ))

            await asyncio.to_thread(self._tag, target, track)
            result = "downloaded" if selected.accepted else "downloaded_for_review"
            tail = f"\x1b[2m{target.name} ({selected.candidate.source}, {selected.score}/100)\x1b[0m"
            if selected.accepted:
                await status(f"\x1b[32m? saved\x1b[0m {tail}", store=True)
            else:
                await status(f"\x1b[33m? saved for review\x1b[0m {tail}", store=True)
            return await finish(TrackOutcome(
                track, result,
                "verified match" if selected.accepted else selected.reason,
                decisions[:5], selected=selected, saved_as=target.name,
                failed_attempts=tuple(attempts),
            ))
        except FileNotFoundError as exc:
            await status(f"\x1b[31m? {exc}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "download_failed", str(exc), []))
        except Exception as exc:  # noqa: BLE001
            await status(f"\x1b[31m? {exc}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "download_failed", str(exc), []))

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
