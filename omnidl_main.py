"""PyInstaller entry point for OmniDL.

Two roles:
  * normal launch  -> start the FastAPI server and open the browser
  * `--run-tool T ...` -> run a bundled downloader (spotdl / yt-dlp / scdl)

In the frozen .exe, `sys.executable` IS this program, so engines.py launches each
download as `OmniDL.exe --run-tool <tool> <args>` and we dispatch it here. This keeps
the live-streaming subprocess model intact without needing the tools on PATH.
"""
from __future__ import annotations

import multiprocessing
import sys

HOST = "127.0.0.1"
PORT = 8000


def _run_tool() -> int:
    # argv: [exe, "--run-tool", <tool>, *tool_args]
    tool = sys.argv[2]
    tool_args = sys.argv[3:]
    sys.argv = [tool, *tool_args]
    # Flush output immediately so the parent streams it live (no block buffering).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(write_through=True)
        except Exception:
            pass

    if tool == "yt-dlp":
        from yt_dlp import main as yt_main
        return yt_main(tool_args) or 0
    if tool == "spotdl":
        from spotdl import console_entry_point
        console_entry_point()
        return 0
    if tool == "scdl":
        # scdl's patches reference `yt_dlp.__init__` (the package module). When frozen,
        # PyInstaller resolves that to a method-wrapper, so patching attributes on it
        # fails. Point both the sys.modules entry and the attribute at the real module.
        import yt_dlp
        sys.modules["yt_dlp.__init__"] = yt_dlp
        yt_dlp.__init__ = yt_dlp  # type: ignore[attr-defined]
        from scdl.scdl import _main as scdl_main
        scdl_main()
        return 0
    sys.stderr.write(f"unknown tool: {tool}\n")
    return 2


def _serve() -> None:
    import asyncio
    import threading
    import time
    import webbrowser

    import uvicorn

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    def _open_browser() -> None:
        time.sleep(1.5)
        webbrowser.open(f"http://{HOST}:{PORT}")

    from app.main import app

    print(f"OmniDL running at http://{HOST}:{PORT}  (close this window to stop)")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", loop="asyncio")


def main() -> None:
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--run-tool":
        sys.exit(_run_tool())
    _serve()


if __name__ == "__main__":
    main()
