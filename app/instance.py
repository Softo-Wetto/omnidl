"""Recognise a running OmniDL on our port, and decide what launching again should do.

The launcher used to force-kill every process listening on the port. That killed unrelated
programs that happened to use port 8000, and cut off OmniDL's own downloads whenever it was
opened a second time. Now the listener is asked who it is first, and only OmniDL is ever
stopped — politely, and never mid-download unless explicitly asked.

Deliberately light: run.py imports this before the app (and its heavy dependencies) loads.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "omnidl"
_APP_DIR = Path(__file__).resolve().parent
_BUILD_SUFFIXES = {".py", ".html", ".css", ".js", ".webmanifest"}


def build_id() -> str:
    """Identifies the code a process is running, so a relaunch after an update can tell that
    the instance already running is out of date."""
    if getattr(sys, "frozen", False):
        return format(int(os.path.getmtime(sys.executable)), "x")
    newest = 0.0
    for path in _APP_DIR.rglob("*"):
        if path.suffix in _BUILD_SUFFIXES and "__pycache__" not in path.parts:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    return format(int(newest), "x")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def probe(port: int, timeout: float = 2.0) -> dict | None:
    """The OmniDL instance answering on `port`, or None if whatever is there isn't OmniDL."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/instance",
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(65536))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_ID else None


def request_shutdown(port: int, timeout: float = 5.0) -> bool:
    origin = f"http://127.0.0.1:{port}"
    req = urllib.request.Request(f"{origin}/api/instance/shutdown", method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


def decide(is_open: bool, info: dict | None, my_build: str, force_restart: bool = False) -> str:
    """What launching should do about the port.

    start       nothing is listening
    occupied    something that isn't OmniDL is — leave it alone and say so
    reuse       OmniDL, current code, idle — just open it
    reuse-busy  OmniDL with downloads in progress — open it rather than cut them off
    restart     OmniDL running older code (or asked to restart) — stop it cleanly, start fresh
    """
    if not is_open:
        return "start"
    if info is None:
        return "occupied"
    if info.get("active") and not force_restart:
        return "reuse-busy"
    if force_restart or info.get("build") != my_build:
        return "restart"
    return "reuse"
