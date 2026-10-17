"""Smoke-test a built OmniDL.exe: it starts, serves the dashboard and its scripts, runs a bundled
tool, and shuts down cleanly. Uses a spare port and no tunnel; downloads nothing.

    python packaging/smoke_test.py [path/to/OmniDL.exe]
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "dist" / "OmniDL" / "OmniDL.exe"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    if not EXE.exists():
        print(f"No exe at {EXE} - build it first with packaging\\build_exe.bat.")
        return 1
    for name in ("yt-dlp", "scdl"):
        tool = subprocess.run([str(EXE), "--run-tool", name, "--version"], capture_output=True, text=True,
                              timeout=120)
        print(f"bundled {name}: {(tool.stdout.strip() or tool.stderr.strip())[:200]}")
        if tool.returncode != 0:
            return 1
    port = free_port()
    env = dict(os.environ, OMNIDL_PORT=str(port), OMNIDL_NO_TUNNEL="1")
    proc = subprocess.Popen([str(EXE), "--no-browser"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    ok = False
    try:
        for _ in range(120):
            try:
                info = json.load(urllib.request.urlopen(base + "/api/instance", timeout=2))
                ok = info.get("app") == "omnidl"
                break
            except OSError:
                time.sleep(0.5)
        print(f"server up: {ok}")
        for path in ("/dashboard", "/static/js/core.js", "/static/js/app.js", "/api/meta"):
            status = urllib.request.urlopen(base + path, timeout=10).status
            print(f"{path}: {status}")
            ok = ok and status == 200
        request = urllib.request.Request(base + "/api/instance/shutdown", method="POST",
                                         headers={"Origin": base})
        urllib.request.urlopen(request, timeout=10)
        proc.wait(30)
        print(f"shut down cleanly: {proc.returncode == 0}")
        ok = ok and proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        ok = False
    finally:
        if proc.poll() is None:
            proc.kill()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
