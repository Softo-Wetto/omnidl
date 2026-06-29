"""Launch OmniDL: starts a fresh local server and opens the browser."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlparse

import uvicorn

# Bind locally by default; set OMNIDL_HOST=0.0.0.0 (behind a reverse proxy) to publish.
HOST = os.environ.get("OMNIDL_HOST", "127.0.0.1")
PORT = int(os.environ.get("OMNIDL_PORT", "8000"))
PORT_WAIT_SECONDS = 8.0
_LOCAL_HOSTS = {"127.0.0.1", "localhost", ""}

# create_subprocess_exec needs the Proactor loop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _is_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def _listening_pids_on_port() -> set[int]:
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            local_address, state, pid_text = parts[1], parts[3], parts[4]
            if state.upper() == "LISTENING" and local_address.endswith(f":{PORT}"):
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                if pid != os.getpid():
                    pids.add(pid)
        return pids

    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{PORT}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        pid
        for line in result.stdout.splitlines()
        if line.strip().isdigit()
        for pid in [int(line.strip())]
        if pid != os.getpid()
    }


def _terminate_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    subprocess.run(["kill", "-TERM", str(pid)], check=False)


def _wait_for_port_to_close(timeout_seconds: float = PORT_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_port_open():
            return True
        time.sleep(0.2)
    return not _is_port_open()


def _restart_existing_server() -> None:
    pids = _listening_pids_on_port()
    if not pids:
        return

    print(f"Restarting OmniDL: stopping existing server on {HOST}:{PORT}...")
    for pid in sorted(pids):
        print(f"Stopping process {pid}")
        _terminate_process_tree(pid)

    if not _wait_for_port_to_close():
        raise SystemExit(
            f"Port {PORT} is still in use after stopping existing process(es): "
            f"{', '.join(str(pid) for pid in sorted(pids))}"
        )


def _open_browser() -> None:
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def _wait_tcp(url: str, timeout: float = 25.0) -> bool:
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.4)
    return False


def _start_pot_provider() -> subprocess.Popen | None:
    """Optionally launch the PO-token provider sidecar alongside OmniDL, so a single
    `python run.py` brings up both. Set OMNIDL_POT_PROVIDER_CMD to the command that starts
    it (e.g. a docker run or `node server.js`). No-op unless that env var is set."""
    cmd = os.environ.get("OMNIDL_POT_PROVIDER_CMD")
    if not cmd:
        return None
    url = os.environ.get("OMNIDL_POT_PROVIDER_URL") or "http://127.0.0.1:4416"
    print(f"Starting PO-token provider: {cmd}")
    try:
        proc = subprocess.Popen(cmd, shell=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start PO-token provider ({exc}); continuing without it.")
        return None
    if _wait_tcp(url):
        # Export so app.settings picks it up when uvicorn imports the app (same process).
        os.environ["OMNIDL_POT_PROVIDER_URL"] = url
        print(f"PO-token provider ready at {url}")
    else:
        print(f"PO-token provider did not respond at {url} in time; continuing without it.")
    return proc


if __name__ == "__main__":
    _restart_existing_server()
    pot_proc = _start_pot_provider()
    print(f"OmniDL running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    if HOST in _LOCAL_HOSTS and "--no-browser" not in sys.argv:
        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info", loop="asyncio")
    finally:
        if pot_proc is not None and pot_proc.poll() is None:
            print("Stopping PO-token provider...")
            _terminate_process_tree(pot_proc.pid)