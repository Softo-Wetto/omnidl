"""Job manager: async subprocess execution, live output streaming, FIFO queue, cancel."""
from __future__ import annotations

import asyncio
import codecs
import glob
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import candidate_search, engines
from .matching import MatchDecision, decide_match
from .review_report import TrackOutcome, write_review_report
from .settings import PROJECT_ROOT, load_settings

# Persisted job history (survives restarts).
_HISTORY_PATH = PROJECT_ROOT / "history.json"
_HISTORY_MAX = 100
_HISTORY_OUTPUT_CAP = 12_000

# Cap how much output we retain per job for reconnect/replay (characters).
_OUTPUT_CAP = 200_000

# Preferred download sources on score ties (YouTube is far more reliably fetchable than
# SoundCloud search hits, which often aren't downloadable).
_SOURCE_RANK = {"youtube_music": 3, "youtube": 2, "soundcloud": 1}

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
                 settings: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.input = input_text
        self.engine = engine
        self.argv = argv                 # None => resolver job (see _run_spotify_job)
        self.display_cmd = display_cmd
        self.spotify_url = spotify_url
        self.settings = settings or {}
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

    def to_dict(self) -> dict:
        meta = engines.ENGINES.get(self.engine, {"label": self.engine, "tool": self.engine})
        return {
            "id": self.id,
            "input": self.input,
            "engine": self.engine,
            "engine_label": meta["label"],
            "tool": meta["tool"],
            "status": self.status,
            "code": self.code,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "progress": self.progress,
            "label": self.label,
        }

    def to_record(self) -> dict:
        """Serializable history record (includes a trimmed log)."""
        rec = self.to_dict()
        rec["output"] = self.output[-_HISTORY_OUTPUT_CAP:]
        rec["spotify_url"] = self.spotify_url
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> "Job":
        job = cls(rec.get("input", ""), rec.get("engine", "youtube"), None, "",
                  spotify_url=rec.get("spotify_url"))
        job.id = rec.get("id", job.id)
        status = rec.get("status", "done")
        job.output = rec.get("output", "")
        if status in ("running", "queued"):  # never finished â€” server had stopped
            status = "cancelled"
            job.output += "\r\n\x1b[33m[interrupted â€” server was restarted]\x1b[0m\r\n"
        job.status = status
        job.code = rec.get("code")
        job.created = rec.get("created", time.time())
        job.started = rec.get("started")
        job.finished = rec.get("finished")
        job.progress = rec.get("progress")
        job.label = rec.get("label", "")
        return job


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set = set()
        self.current: Job | None = None
        self._worker_task: asyncio.Task | None = None

    # ----- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._worker_task is None:
            self._load_history()
            self._worker_task = asyncio.create_task(self._worker())

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
    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self.subscribers):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    def snapshot(self) -> dict:
        active = self.current
        if active is None and self.order:
            active = self.jobs.get(self.order[-1])
        return {
            "type": "snapshot",
            "jobs": [self.jobs[i].to_dict() for i in self.order if i in self.jobs],
            "active_id": active.id if active else None,
            "output": active.output if active else "",
        }

    # ----- submission ----------------------------------------------------
    async def submit(self, input_text: str, engine_override: str | None = None,
                     overrides: dict | None = None) -> Job:
        s = load_settings()
        if overrides:
            s = {**s, **{k: v for k, v in overrides.items() if v}}
        if engine_override in engines.ENGINES:
            engine = engine_override
        else:
            engine = engines.detect_engine(input_text, s)

        # Spotify (default "embed" method): no single command â€” resolve via the embed
        # page, then download each track with yt-dlp. spotdl method is opt-in.
        if engine == "spotify" and s.get("spotify_method", "embed") == "embed":
            job = Job(input_text, engine, None,
                      "resolve Spotify via public embed (no API / no Premium)",
                      spotify_url=input_text, settings=s)
        else:
            argv = engines.build_command(engine, input_text, s)
            job = Job(input_text, engine, argv, engines.describe_command(argv), settings=s)
        self.jobs[job.id] = job
        self.order.append(job.id)
        self.queue.put_nowait(job.id)
        await self.broadcast({"type": "job_added", "job": job.to_dict()})
        self._persist()
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status == "running":
            job.status = "cancelled"
            self._kill(job)
        elif job.status == "queued":
            job.status = "cancelled"
            await self.broadcast({"type": "status", "job": job.to_dict()})
            self._persist()
        return True

    async def delete(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status == "running":
            return False
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        await self.broadcast({"type": "removed", "job_id": job_id})
        self._persist()
        return True

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
        await self.broadcast({"type": "output", "job_id": job.id, "data": text})
        if self._update_progress(job, text):
            await self.broadcast({
                "type": "progress", "job_id": job.id,
                "progress": job.progress, "label": job.label,
            })

    async def _emit_keyed(self, job: Job, key: str, text: str, store: bool = False) -> None:
        """Emit a line that updates in place on the client (one per concurrent track).
        Only the final state (store=True) is kept in the job log for reconnect replay."""
        if store:
            job.append(text)
        await self.broadcast({"type": "output", "job_id": job.id, "data": text, "key": key})

    async def _key_done(self, job: Job, key: str) -> None:
        await self.broadcast({"type": "key_done", "job_id": job.id, "key": key})

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
                                 progress_cb=None) -> int:
        """Run one subprocess. emit=True streams formatted output live. Otherwise the output
        is drained silently, but if progress_cb is given the latest download %/stage is
        parsed and passed to it (used for the live per-track line in parallel mode).
        Returns the exit code."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        job.procs.add(proc)
        try:
            assert proc.stdout is not None
            if emit:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                formatter = ToolOutputFormatter()
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        tail = decoder.decode(b"", final=True)
                        if tail and (formatted := formatter.feed(tail)):
                            await self._emit(job, formatted)
                        if flushed := formatter.flush():
                            await self._emit(job, flushed)
                        break
                    if (text := decoder.decode(chunk)) and (formatted := formatter.feed(text)):
                        await self._emit(job, formatted)
            elif progress_cb is not None:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                last = None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if not text:
                        continue
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
                while await proc.stdout.read(8192):
                    pass
            return await proc.wait()
        finally:
            job.procs.discard(proc)

    async def _run_job(self, job: Job) -> None:
        self.current = job
        job.status = "running"
        job.started = time.time()
        await self.broadcast({"type": "status", "job": job.to_dict()})
        try:
            if job.argv is None:
                await self._run_spotify_job(job)
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
            self.current = None
            colour = {"done": "32", "cancelled": "33", "error": "31"}.get(job.status, "0")
            await self._emit(
                job, f"\x1b[{colour}m[{job.status}] exit={job.code}\x1b[0m\r\n"
            )
            await self.broadcast({"type": "status", "job": job.to_dict()})
            self._persist()

    async def _run_spotify_job(self, job: Job) -> None:
        """Resolve Spotify metadata, then download only strict external matches."""
        from . import spotify_resolver as sr

        settings = job.settings or load_settings()
        await self._emit(job, "\r\n\x1b[1;36m\U0001f50e Resolving Spotify link...\x1b[0m \x1b[2m(no API - no Premium)\x1b[0m\r\n")
        resolved = await asyncio.to_thread(sr.resolve, job.spotify_url)
        tracks = resolved.tracks
        total = len(tracks)
        await self._emit(job, f"\x1b[1;32m\U0001f4cb {resolved.name}\x1b[0m \x1b[2m- {total} track{'s' if total != 1 else ''} ({resolved.kind})\x1b[0m\r\n")

        concurrency = max(1, min(8, int(settings.get("concurrency", 1) or 1)))
        if total and concurrency > 1:
            await self._emit(job, f"\x1b[2m   ↳ downloading {concurrency} at a time (compact per-track log)\x1b[0m\r\n")

        counts = {"ok": 0, "skip": 0, "fail": 0, "done": 0}
        unresolved: list[TrackOutcome] = []
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(concurrency)

        async def handle(index: int, track) -> None:
            async with sem:
                if job.status == "cancelled":
                    return
                detailed = concurrency == 1
                async with lock:
                    job.label = track.query
                    job.progress = int((index - 1) / total * 100) if total else 0
                await self.broadcast({"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})
                outcome = await self._fetch_track(job, track, settings, detailed, index, total)
                if outcome.status == "cancelled":
                    return
                result = "ok" if outcome.status in ("downloaded", "downloaded_for_review") else "skip" if outcome.status == "skipped" else "fail"
                async with lock:
                    counts[result] += 1
                    counts["done"] += 1
                    if outcome.status in ("downloaded_for_review", "no_candidate", "rejected", "download_failed"):
                        unresolved.append(outcome)
                    job.progress = int(counts["done"] / total * 100) if total else 100
                    job.label = track.query
                await self.broadcast({"type": "progress", "job_id": job.id, "progress": job.progress, "label": job.label})

        await asyncio.gather(*(handle(i, track) for i, track in enumerate(tracks, start=1)))

        if job.status != "cancelled":
            report_path: Path | None = None
            if unresolved:
                report_path = await self._emit_review_report(job, Path(settings["output_dir"]), resolved.name, unresolved)
            job.progress = 100
            job.code = 0 if counts["fail"] == 0 else 1
            job.status = "done" if (counts["ok"] + counts["skip"]) > 0 or total == 0 else "error"
            bar = "-" * 40
            report_suffix = f"\x1b[2m report={report_path}\x1b[0m" if report_path else ""
            await self._emit(job, f"\r\n\x1b[36m{bar}\x1b[0m\r\n\x1b[1;32m\U00002705 {counts['ok']} downloaded\x1b[0m \x1b[2m-\x1b[0m \x1b[33m{counts['skip']} skipped\x1b[0m \x1b[2m-\x1b[0m \x1b[31m{counts['fail']} needs review\x1b[0m \x1b[2m({total} total)\x1b[0m{report_suffix}\r\n\x1b[36m{bar}\x1b[0m\r\n")

    async def _emit_review_report(self, job: Job, output_dir: Path, playlist_name: str, outcomes: list[TrackOutcome]) -> Path | None:
        if not outcomes:
            return None
        try:
            path = await asyncio.to_thread(write_review_report, output_dir, playlist_name, outcomes)
        except OSError as exc:
            await self._emit(job, f"\x1b[31m✗ Could not write review report: {exc}\x1b[0m\r\n")
            return None
        await self._emit(job, f"\x1b[33m\U0001f4c4 Review report: {path}\x1b[0m\r\n")
        return path

    async def _fetch_track(self, job: Job, track, settings: dict, detailed: bool,
                           index: int, total: int) -> TrackOutcome:
        """Download the best external candidate and flag non-exact choices for review.

        Detailed mode (concurrency 1) streams the full yt-dlp output live. Parallel mode
        shows ONE line per track that updates in place (search -> %, -> saved), so any
        number of concurrent tracks stay live and readable without interleaving.
        """
        out_dir = Path(settings["output_dir"])
        ext = settings.get("audio_format", "opus")
        target = out_dir / f"{track.filename}.{ext}"
        key = f"t{index}"
        label = f"\x1b[1;36m[{index}/{total}]\x1b[0m \x1b[1m{track.artist} - {track.title}\x1b[0m"

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

        if detailed:
            await self._emit(job, f"\r\n{label}\r\n")
        if settings.get("skip_existing", True) and target.exists():
            await status("\x1b[33m⏭ already downloaded\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "skipped", "output file already exists", []))

        try:
            await status("\x1b[2m🔍 searching sources...\x1b[0m")
            candidates = await asyncio.to_thread(candidate_search.search_all, track.artist, track.title)
            decisions = [decide_match(track.artist, track.title, track.duration, c) for c in candidates]
            # Highest score wins; tie-break: reliable source, then artist, title, closeness.
            decisions.sort(
                key=lambda d: (
                    d.accepted,
                    d.score,
                    _SOURCE_RANK.get(d.candidate.source, 0),
                    d.artist_similarity, d.title_similarity,
                    -(d.duration_difference if d.duration_difference is not None else 9999),
                ),
                reverse=True,
            )
            if not decisions:
                await status("\x1b[33m🚫 no source found; added to review report\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "no_candidate", "no external candidates found", []))

            # Try candidates best-first; fall back to the next when a download fails
            # (SoundCloud results in particular are frequently not actually fetchable).
            selected = None
            for decision in decisions[:5]:
                if job.status == "cancelled":
                    return await finish(TrackOutcome(track, "cancelled", "job cancelled", decisions[:5]))
                cand = decision.candidate

                async def progress(info: str, _c=cand) -> None:
                    await status(f"\x1b[36m⏳ {info} \x1b[2m({_c.source})\x1b[0m")

                await status(f"\x1b[2m⏳ trying {cand.source} (score {decision.score}/100)...\x1b[0m")
                code = await self._stream_subprocess(
                    job, engines.media_url_command(cand.url, track.filename, settings),
                    emit=detailed, progress_cb=None if detailed else progress)
                self._cleanup_sidecars(out_dir, track.filename, ext)
                if code == 0 and target.exists():
                    selected = decision
                    break
                await status(f"\x1b[2m{cand.source} unavailable - trying next source\x1b[0m")

            if job.status == "cancelled":
                return await finish(TrackOutcome(track, "cancelled", "job cancelled", decisions[:5]))
            if selected is None:
                await status("\x1b[31m✗ all sources failed; added to review\x1b[0m", store=True)
                return await finish(TrackOutcome(track, "download_failed", "all candidate downloads failed", decisions[:5]))

            await asyncio.to_thread(self._tag, target, track)
            review_reason = "verified match" if selected.accepted else selected.reason
            result = "downloaded" if selected.accepted else "downloaded_for_review"
            tail = f"\x1b[2m{target.name} ({selected.candidate.source}, {selected.score}/100)\x1b[0m"
            if selected.accepted:
                await status(f"\x1b[32m✓ saved\x1b[0m {tail}", store=True)
            else:
                await status(f"\x1b[33m✓ saved for review\x1b[0m {tail}", store=True)
            return await finish(TrackOutcome(track, result, "verified match" if selected.accepted else review_reason, decisions[:5]))
        except FileNotFoundError as exc:
            await status(f"\x1b[31m✗ {exc}\x1b[0m", store=True)
            return await finish(TrackOutcome(track, "download_failed", str(exc), []))
        except Exception as exc:  # noqa: BLE001
            await status(f"\x1b[31m✗ {exc}\x1b[0m", store=True)
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
