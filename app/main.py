"""OmniDL FastAPI application: routes, static files, and the live WebSocket."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import engines, settings as settings_mod
from .jobs import JobManager

# create_subprocess_exec needs the Proactor loop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Static files live under the PyInstaller bundle when frozen, else next to this file.
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"
manager = JobManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start()
    yield


app = FastAPI(title="OmniDL", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _page(name: str) -> FileResponse:
    return FileResponse(str(STATIC_DIR / name))


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
async def get_settings():
    return settings_mod.load_settings()


@app.post("/api/settings")
async def post_settings(payload: dict):
    return settings_mod.save_settings(payload)


@app.get("/api/meta")
async def meta():
    """Static info the frontend needs to build its forms."""
    return {
        "engines": engines.ENGINES,
        "formats": settings_mod.AUDIO_FORMATS,
        "browsers": settings_mod.BROWSERS,
        "spotify_methods": ["embed", "spotdl"],
    }


@app.post("/api/download")
async def download(payload: dict):
    text = (payload.get("input") or "").strip()
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    override = payload.get("engine_override") or None
    extras = {}
    if payload.get("format"):
        extras["audio_format"] = payload["format"]
    job = await manager.submit(text, override, extras or None)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs():
    return [manager.jobs[i].to_dict() for i in manager.order if i in manager.jobs]


@app.get("/api/jobs/{job_id}/output")
async def job_output(job_id: str):
    job = manager.jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": job.id, "output": job.output, "status": job.status}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    return {"ok": await manager.cancel(job_id)}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    return {"ok": await manager.delete(job_id)}


@app.post("/api/open-folder")
async def open_folder():
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
    manager.subscribers.add(websocket)
    try:
        await websocket.send_json(manager.snapshot())
        while True:
            # We don't need client messages; this just keeps the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.subscribers.discard(websocket)
