"""OmniDL FastAPI application: routes, static files, and the live WebSocket."""
from __future__ import annotations

import asyncio
import mimetypes
import re
import subprocess
import sys
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import bulk_input, candidate_search, engines, library, sessions, settings as settings_mod
from . import spotify_resolver as sr
from .jobs import JobManager

# Per-session UI preferences (in-memory). Visitors only ever change these — never the
# server's config.json. See settings.SESSION_PREF_KEYS.
SESSION_PREFS: dict[str, dict] = {}

# create_subprocess_exec needs the Proactor loop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Static files live under the PyInstaller bundle when frozen, else next to this file.
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"
manager = JobManager()


def _asset_version() -> str:
    """Change cached asset URLs whenever a bundled CSS/JS/image file changes."""
    modified = (
        path.stat().st_mtime_ns
        for path in STATIC_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() != ".html"
    )
    return format(max(modified, default=0), "x")


ASSET_VERSION = _asset_version()
_STATIC_URL_RE = re.compile(r'(/static/[^"\s>]+)')
_PAGE_CACHE: dict[str, str] = {}


def _log_cookie_health() -> None:
    """One-line note at startup about the YouTube cookies situation."""
    path = settings_mod.effective_settings().get("cookie_file")
    if not path:
        print("[OmniDL] No cookies file set - YouTube may intermittently return 'Video "
              "unavailable'. Add one in Settings (local) or OMNIDL_COOKIE_FILE (hosted).")
        return
    status = settings_mod.cookie_file_status(path)
    if status:
        print(f"[OmniDL] WARNING - {status[1]}")
    else:
        print(f"[OmniDL] cookies file looks valid: {path}")


def _log_ytdlp_age() -> None:
    """Warn when yt-dlp is old enough that YouTube has likely broken it."""
    age = engines.ytdlp_age_days()
    version = engines.ytdlp_version() or "unknown"
    if age is None:
        print(f"[OmniDL] yt-dlp {version} (age unknown)")
    elif age > 30:
        print(f"[OmniDL] WARNING - yt-dlp {version} is {age} days old. YouTube breaks older "
              f"releases outright; update it or downloads will start failing with 403.")
    else:
        print(f"[OmniDL] yt-dlp {version} ({age} days old)")


def _log_pot_provider() -> None:
    if settings_mod.POT_PROVIDER_URL:
        print(f"[OmniDL] PO-token provider active: {settings_mod.POT_PROVIDER_URL} "
              "(needs the bgutil yt-dlp plugin installed)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start()
    _log_cookie_health()
    _log_pot_provider()
    _log_ytdlp_age()
    yield


class NoCacheStaticFiles(StaticFiles):
    """Cache only URLs tied to the current build; revalidate unversioned requests."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        query = parse_qs(scope.get("query_string", b"").decode("ascii", "ignore"))
        if query.get("v") == [ASSET_VERSION]:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app = FastAPI(title="OmniDL", lifespan=lifespan)
app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Give every visitor a stable, signed session id (set once via a cookie)."""
    sid = sessions.read_valid_sid(request.cookies.get(sessions.COOKIE_NAME))
    is_new = sid is None
    if is_new:
        sid = sessions.new_sid()
    request.state.sid = sid
    response = await call_next(request)
    if is_new:
        response.set_cookie(
            sessions.COOKIE_NAME, sessions.sign(sid),
            max_age=sessions.COOKIE_MAX_AGE, httponly=True, samesite="lax",
            secure=sessions.COOKIE_SECURE,
        )
    return response


def _sid(request: Request) -> str:
    return getattr(request.state, "sid", "") or sessions.new_sid()


def _tier(request: Request) -> str:
    """Access tier for this visitor: "owner", "user", or "" (locked).

    With no gate configured everyone is the owner — that's a personal/local install.
    """
    if not settings_mod.gate_enabled():
        return "owner"
    return sessions.unlock_tier(_sid(request), request.cookies.get(sessions.UNLOCK_COOKIE)) or ""


def _unlocked(request: Request) -> bool:
    """Has this visitor passed the access gate? (Always true when no gate is configured.)"""
    return bool(_tier(request))


def _client_ip(request: Request) -> str:
    """Real client address, for per-IP abuse limits.

    Behind a reverse proxy (Caddy) the socket peer is always 127.0.0.1. Caddy *appends* the
    true peer to X-Forwarded-For, so the right-most entry is the one it observed — take that
    rather than the left-most, which a client can forge.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


def _page(name: str) -> HTMLResponse:
    html = _PAGE_CACHE.get(name)
    if html is None:
        source = (STATIC_DIR / name).read_text(encoding="utf-8-sig")
        html = _STATIC_URL_RE.sub(lambda match: f"{match.group(1)}?v={ASSET_VERSION}", source)
        _PAGE_CACHE[name] = html
    return HTMLResponse(html, headers=_NO_CACHE)


@app.get("/robots.txt")
async def robots():
    """Keep the downloader out of search indexes — it's a personal tool, not something that
    benefits from search traffic, and being indexed invites abuse and rights-holder attention."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n", headers=_NO_CACHE)


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(str(STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/")
async def home():
    return _page("home.html")


@app.get("/dashboard")
async def dashboard():
    return _page("index.html")


@app.get("/about")
async def about():
    return _page("about.html")


@app.get("/privacy")
async def privacy():
    return _page("privacy.html")


@app.get("/terms")
async def terms():
    return _page("terms.html")


@app.get("/api/settings")
async def get_settings(request: Request):
    return settings_mod.public_settings(SESSION_PREFS.get(_sid(request)))


@app.post("/api/settings")
async def post_settings(request: Request, payload: dict):
    sid = _sid(request)
    prefs = SESSION_PREFS.setdefault(sid, {})
    prefs.update(settings_mod.clean_session_prefs(payload))
    # In local (personal) mode the user may also set their own output folder + cookies file.
    if settings_mod.LOCAL_MODE:
        updates = {}
        if payload.get("output_dir"):
            updates["output_dir"] = payload["output_dir"]
        if "cookie_file" in payload and isinstance(payload["cookie_file"], str):
            updates["cookie_file"] = payload["cookie_file"].strip()
        if updates:
            settings_mod.save_settings(updates)
    result = settings_mod.public_settings(prefs)
    if settings_mod.LOCAL_MODE and result.get("cookie_file"):
        status = settings_mod.cookie_file_status(result["cookie_file"])
        if status:
            result["cookie_warning"] = status[1]
    return result


@app.get("/api/meta")
async def meta(request: Request):
    """Static info the frontend needs to build its forms."""
    return {
        "engines": engines.ENGINES,
        "formats": settings_mod.AUDIO_FORMATS,
        "video_qualities": settings_mod.VIDEO_QUALITIES,
        "video_containers": settings_mod.VIDEO_CONTAINERS,
        "local": settings_mod.LOCAL_MODE,
        "max_concurrency": settings_mod.MAX_CONCURRENCY,
        "naming_orders": settings_mod.NAMING_ORDERS,
        "naming_artists": settings_mod.NAMING_ARTISTS,
        "ytdlp_version": engines.ytdlp_version(),
        "ytdlp_age_days": engines.ytdlp_age_days(),
        # Access gate: `gated` tells the UI to expect locked sources at all; `unlocked` is
        # this visitor's current state.
        "gated": settings_mod.gate_enabled(),
        "unlocked": _unlocked(request),
        "tier": _tier(request),
    }


# Failed unlock attempts per IP, to blunt passphrase guessing.
_UNLOCK_FAILS: dict[str, list[float]] = {}
_UNLOCK_MAX_FAILS = 8
_UNLOCK_WINDOW = 900          # 15 minutes


@app.post("/api/unlock")
async def unlock(request: Request, payload: dict):
    """Exchange the access passphrase for a signed unlock cookie."""
    import hmac as _hmac
    import time as _time

    if not settings_mod.gate_enabled():
        return {"ok": True, "unlocked": True}

    ip = _client_ip(request)
    now = _time.time()
    recent = [t for t in _UNLOCK_FAILS.get(ip, []) if now - t < _UNLOCK_WINDOW]
    _UNLOCK_FAILS[ip] = recent
    if len(recent) >= _UNLOCK_MAX_FAILS:
        return JSONResponse(
            {"error": "Too many incorrect attempts. Try again in 15 minutes."},
            status_code=429,
        )

    supplied = (payload.get("passphrase") or "").strip()
    # Constant-time compares so timing can't leak either passphrase. Owner is checked first
    # so it wins if both happen to be set to the same value.
    owner_pass = settings_mod.OWNER_PASSPHRASE
    if owner_pass and _hmac.compare_digest(supplied, owner_pass):
        tier = "owner"
    elif _hmac.compare_digest(supplied, settings_mod.ACCESS_PASSPHRASE):
        tier = "user"
    else:
        tier = ""
    if not tier:
        _UNLOCK_FAILS.setdefault(ip, []).append(now)
        left = _UNLOCK_MAX_FAILS - len(_UNLOCK_FAILS[ip])
        return JSONResponse(
            {"error": f"Incorrect passphrase. {left} attempt(s) left."}, status_code=403
        )

    sid = _sid(request)
    _UNLOCK_FAILS.pop(ip, None)
    response = JSONResponse({"ok": True, "unlocked": True, "tier": tier})
    response.set_cookie(
        sessions.UNLOCK_COOKIE, sessions.unlock_cookie(sid, tier),
        max_age=sessions.COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=sessions.COOKIE_SECURE,
    )
    return response


@app.post("/api/library/scan")
async def library_scan():
    """Inspect the configured local output directory without changing any files."""
    if not settings_mod.LOCAL_MODE:
        return JSONResponse({"error": "library management is available in local mode only"}, status_code=403)
    root = Path(settings_mod.load_settings()["output_dir"])
    try:
        return await asyncio.to_thread(library.scan_library, root)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/library/repair")
async def library_repair(payload: dict):
    """Apply an explicitly requested safe missing-tag repair to one local audio file."""
    if not settings_mod.LOCAL_MODE:
        return JSONResponse({"error": "library management is available in local mode only"}, status_code=403)
    relative_path = payload.get("path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        return JSONResponse({"error": "path is required"}, status_code=400)
    root = Path(settings_mod.load_settings()["output_dir"])
    try:
        return await asyncio.to_thread(library.repair_missing_tags, root, relative_path.strip())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

_TEXT_SERVICES = {"youtube": "youtube", "soundcloud": "soundcloud"}


def _needs_account(text: str, service: str = "auto") -> bool:
    """Would downloading this spend the operator's YouTube account?

    YouTube-backed downloads use the operator's cookies, so they're gated when a passphrase
    is configured; everything cookie-free stays open. It's keyed on the input and, for typed
    text, on where it will be searched — one rule for single downloads, pasted lists and
    retries alike, so the same request is never allowed one way and refused another.
    """
    if engines.is_url(text):
        return engines.needs_youtube_account(text)
    return service != "soundcloud"


def _locked(what: str = "YouTube and Spotify downloads") -> JSONResponse:
    return JSONResponse(
        {"error": f"{what} need the access passphrase. SoundCloud links and "
                  "SoundCloud-only searches work without it.",
         "needs_unlock": True},
        status_code=403,
    )


def _apply_mode(s: dict, payload: dict) -> None:
    """Take the per-request mode fields, but only valid values.

    Unchecked, a bulk list sent from Video mode arrived with "1080p" as its *audio* format,
    and every track then failed to convert.
    """
    if payload.get("format") in settings_mod.AUDIO_FORMATS:
        s["audio_format"] = payload["format"]
    if payload.get("media_type") in ("audio", "video"):
        s["media_type"] = payload["media_type"]
    if payload.get("video_quality") in settings_mod.VIDEO_QUALITIES:
        s["video_quality"] = payload["video_quality"]


@app.post("/api/download")
async def download(request: Request, payload: dict):
    text = (payload.get("input") or "").strip()
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    override = payload.get("engine_override") or None
    video = payload.get("media_type") == "video"
    service = "youtube" if video else _TEXT_SERVICES.get(override or "", "auto")
    if _needs_account(text, service) and not _unlocked(request):
        return _locked()

    sid = _sid(request)
    ip = _client_ip(request)
    limit = manager.check_limit(sid, ip, unlimited=_tier(request) == "owner")
    if limit:
        return JSONResponse({"error": limit}, status_code=429)
    s = settings_mod.effective_settings(SESSION_PREFS.get(sid))
    _apply_mode(s, payload)
    job = await manager.submit(text, s, sid, override, ip=ip)
    return job.to_dict()


# A pasted list is capped so one visitor can't turn one paste into an unbounded download
# run. Your own machine — or your own server, as the owner — is your bandwidth to spend, so
# the ceiling only binds strangers.
BULK_MAX_OWNER = 500
BULK_MAX_VISITOR = 100


def _bulk_max(request: Request) -> int:
    if settings_mod.LOCAL_MODE or _tier(request) == "owner":
        return BULK_MAX_OWNER
    return BULK_MAX_VISITOR


@app.post("/api/bulk/preview")
async def bulk_preview(request: Request, payload: dict):
    """Parse pasted text without downloading anything.

    Parsing runs server-side so the preview and the download agree exactly — a second
    implementation in JavaScript would eventually disagree with this one, and the whole
    point of the preview is that what you see is what gets searched.
    """
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": "nothing pasted"}, status_code=400)
    # The toggle means "remove labels where a line has one" (decided per line) or "never".
    # Forcing it on would strip genuine title parts ("Black Betty - Single Edit"), so only
    # an explicit false is honoured.
    strip = False if payload.get("strip_labels") is False else None
    max_lines = _bulk_max(request)
    entries = bulk_input.parse_bulk(text, strip)
    capped = len(entries) > max_lines
    entries = entries[:max_lines]
    return {
        "entries": [
            {"raw": e.raw, "kind": e.kind, "artist": e.artist, "title": e.title,
             "url": e.url, "label": e.label, "query": e.query}
            for e in entries
        ],
        "summary": bulk_input.summarise(entries),
        "capped": capped,
        "max_lines": max_lines,
        "strip_labels": strip is None,
    }


@app.post("/api/bulk/download")
async def bulk_download(request: Request, payload: dict):
    """Queue an edited song list: one track-list job, plus a job per pasted link."""
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        return JSONResponse({"error": "nothing to download"}, status_code=400)
    service = payload.get("service")
    service = service if service in candidate_search.SERVICE_SOURCES else "auto"

    tracks, urls = [], []
    for item in raw_entries[:_bulk_max(request)]:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "url":
            url = (item.get("url") or "").strip()
            if url:
                urls.append(url)
            continue
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        if not (artist or title):
            continue
        tracks.append(sr.Track(artist=artist, title=title,
                               artists=[artist] if artist else []))
    if not tracks and not urls:
        return JSONResponse({"error": "nothing usable in that list"}, status_code=400)
    # Same rule as a single download: a SoundCloud-only list or SoundCloud links don't touch
    # the operator's YouTube account, so they don't need the passphrase.
    if not _unlocked(request) and (
        (tracks and _needs_account("", service)) or any(_needs_account(u) for u in urls)
    ):
        return _locked("YouTube and Spotify searches")

    sid = _sid(request)
    ip = _client_ip(request)
    owner = _tier(request) == "owner"
    limit = manager.check_limit(sid, ip, unlimited=owner)
    if limit:
        return JSONResponse({"error": limit}, status_code=429)
    s = settings_mod.effective_settings(SESSION_PREFS.get(sid))
    _apply_mode(s, {"format": payload.get("format")})

    jobs, held_back = [], 0
    if tracks:
        jobs.append(await manager.submit_bulk(
            tracks, s, sid, service=service,
            list_name=(payload.get("name") or "").strip(), ip=ip))
    # Links are their own thing: a pasted playlist URL should still resolve as a playlist
    # rather than be flattened into a single search. Each becomes its own job, so each is
    # held to the same limits as a link pasted on its own — otherwise one paste of 100 links
    # was 100 jobs past a 3-at-a-time cap.
    for url in urls:
        if manager.check_limit(sid, ip, unlimited=owner):
            held_back += 1
            continue
        jobs.append(await manager.submit(url, dict(s), sid, ip=ip))
    return {"jobs": [j.to_dict() for j in jobs], "tracks": len(tracks),
            "links": len(urls) - held_back, "links_held_back": held_back}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str):
    """Re-run a job faithfully. The browser only knows a job's display text — for a pasted
    list that's "Wedding songs (3 tracks)", which it used to search YouTube for."""
    sid = _sid(request)
    old = manager.jobs.get(job_id)
    if old is None or (not settings_mod.LOCAL_MODE and old.session != sid):
        return JSONResponse({"error": "That job no longer exists."}, status_code=404)
    if old.kind in ("bulk", "search"):
        gated = _needs_account("", old.settings.get("bulk_service") or "auto")
    else:
        gated = _needs_account(old.spotify_url or old.input)
    if gated and not _unlocked(request):
        return _locked()
    ip = _client_ip(request)
    limit = manager.check_limit(sid, ip, unlimited=_tier(request) == "owner")
    if limit:
        return JSONResponse({"error": limit}, status_code=429)
    result = await manager.retry(job_id, sid, settings_mod.effective_settings(SESSION_PREFS.get(sid)), ip=ip)
    if isinstance(result, str):
        return JSONResponse({"error": result}, status_code=400)
    return result.to_dict()


@app.get("/api/jobs")
async def list_jobs(request: Request):
    return manager.list_jobs(_sid(request))


def _owned(request: Request, job_id: str):
    """Return the job only if it belongs to the requesting session."""
    job = manager.jobs.get(job_id)
    if job is None:
        return None
    # Single-user install: no other session to protect the job from.
    if settings_mod.LOCAL_MODE:
        return job
    if job.session != _sid(request):
        return None
    return job


@app.get("/api/jobs/{job_id}/output")
async def job_output(request: Request, job_id: str):
    job = _owned(request, job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": job.id, "output": job.output, "status": job.status}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    return {"ok": await manager.cancel(job_id, _sid(request))}


@app.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: str):
    return {"ok": await manager.delete(job_id, _sid(request))}


def _safe_name(name: str) -> str:
    """A header-safe ASCII fallback filename (the real UTF-8 name is sent separately)."""
    return "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name) or "download"


@app.get("/api/jobs/{job_id}/file")
async def job_file(request: Request, job_id: str):
    """Deliver a finished job's output to the browser: the file itself, or a zip if it
    produced several (e.g. a playlist)."""
    job = _owned(request, job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    files = job.list_files()
    if not files:
        return JSONResponse({"error": "no files (they may have expired)"}, status_code=404)
    if len(files) == 1:
        f = files[0]
        media_type = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        return FileResponse(str(f), media_type=media_type, filename=f.name)

    base = (job.label or job.input or "omnidl").rsplit("/", 1)[-1]
    zip_name = f"{_safe_name(base)[:60] or 'omnidl'}.zip"
    tmp = tempfile.NamedTemporaryFile(prefix="omnidl_zip_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return FileResponse(
        tmp.name, media_type="application/zip", filename=zip_name,
        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)),
    )


@app.post("/api/open-folder")
async def open_folder():
    """Open the download folder in the OS file manager — local (personal) mode only."""
    if not settings_mod.LOCAL_MODE:
        return JSONResponse({"error": "not available in hosted mode"}, status_code=403)
    path = settings_mod.load_settings()["output_dir"]
    settings_mod.ensure_output_dir(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "path": path}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    sid = sessions.read_valid_sid(websocket.cookies.get(sessions.COOKIE_NAME)) or ""
    manager.subscribers[websocket] = sid
    try:
        await websocket.send_json(manager.snapshot(sid))
        while True:
            # We don't need client messages; this just keeps the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.subscribers.pop(websocket, None)
