"""OmniDL FastAPI application: routes, static files, and the live WebSocket."""
from __future__ import annotations

import asyncio
import mimetypes
import subprocess
import sys
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import engines, sessions, settings as settings_mod
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


def _log_pot_provider() -> None:
    if settings_mod.POT_PROVIDER_URL:
        print(f"[OmniDL] PO-token provider active: {settings_mod.POT_PROVIDER_URL} "
              "(needs the bgutil yt-dlp plugin installed)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start()
    _log_cookie_health()
    _log_pot_provider()
    yield


class NoCacheStaticFiles(StaticFiles):
    """Serve static files with revalidation so an updated app.js/style.css is never
    masked by a stale browser cache (the cause of "the button does nothing")."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
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


def _page(name: str) -> FileResponse:
    return FileResponse(str(STATIC_DIR / name), headers=_NO_CACHE)


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
async def meta():
    """Static info the frontend needs to build its forms."""
    return {
        "engines": engines.ENGINES,
        "formats": settings_mod.AUDIO_FORMATS,
        "video_qualities": settings_mod.VIDEO_QUALITIES,
        "video_containers": settings_mod.VIDEO_CONTAINERS,
        "local": settings_mod.LOCAL_MODE,
    }


@app.post("/api/download")
async def download(request: Request, payload: dict):
    text = (payload.get("input") or "").strip()
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    sid = _sid(request)
    limit = manager.check_limit(sid)
    if limit:
        return JSONResponse({"error": limit}, status_code=429)
    s = settings_mod.effective_settings(SESSION_PREFS.get(sid))
    if payload.get("format"):
        s["audio_format"] = payload["format"]
    if payload.get("media_type"):
        s["media_type"] = payload["media_type"]
    if payload.get("video_quality"):
        s["video_quality"] = payload["video_quality"]
    override = payload.get("engine_override") or None
    job = await manager.submit(text, s, sid, override)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs(request: Request):
    return manager.list_jobs(_sid(request))


def _owned(request: Request, job_id: str):
    """Return the job only if it belongs to the requesting session."""
    job = manager.jobs.get(job_id)
    if job is None or job.session != _sid(request):
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
