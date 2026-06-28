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

import uvicorn

HOST = "127.0.0.1"
PORT = 8000
PORT_WAIT_SECONDS = 8.0

# create_subprocess_exec needs the Proactor loop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _is_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((HOST, PORT)) == 0


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
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    _restart_existing_server()
    print(f"OmniDL running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info", loop="asyncio")