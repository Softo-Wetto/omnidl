"""Run cloudflared alongside OmniDL, so starting the app is all it takes to reach it remotely.

The tunnel lives exactly as long as OmniDL: started when the server starts, stopped when it
stops, and — through a Windows job object — killed by the OS if OmniDL dies some other way
(console window closed, crash, taskkill). A tunnel that outlives the app only ever serves
"bad gateway", and one left over from a previous run would pile up connections.

Configured by remote.json (see remote_access): `tunnel_config` points at the cloudflared
config file; nothing happens without it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

from .settings import PROJECT_ROOT

LOG_PATH = PROJECT_ROOT / "cloudflared.log"
_BACKOFF_MAX = 60
# cloudflared runs with self-update off (see Tunnel), and Cloudflare stops accepting releases
# about a year old — remote access would then just stop one day. Warn well before that.
_STALE_DAYS = 180
_BUILT_RE = re.compile(r"built (\d{4})-(\d{2})-(\d{2})")


def cloudflared_age_days(version_text: str) -> int | None:
    """Days since the cloudflared in `version_text` ("... (built 2026-09-24T08:31 UTC)") was built."""
    m = _BUILT_RE.search(version_text or "")
    if not m:
        return None
    try:
        return (date.today() - date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
    except ValueError:
        return None


def _warn_if_stale(exe: str) -> None:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return
    age = cloudflared_age_days(out.stdout + out.stderr)
    if age is not None and age > _STALE_DAYS:
        print(f"[OmniDL] Remote access: cloudflared is {age} days old. Cloudflare retires old "
              f"releases, so download the latest over {exe} before it stops connecting.")


def find_cloudflared(configured: str = "") -> str | None:
    """The cloudflared binary to run: remote.json's path, then PATH, then the per-user install."""
    candidates = [configured, shutil.which("cloudflared") or ""]
    if sys.platform == "win32":
        candidates.append(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                       "Programs", "cloudflared", "cloudflared.exe"))
    return next((c for c in candidates if c and Path(c).is_file()), None)


def close_job(job) -> None:
    """Release a job handle from kill_with_this_process (kills anything still inside it)."""
    if job and sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle(job)


def kill_with_this_process(proc: subprocess.Popen):
    """Make Windows kill `proc` when this process exits, however it exits.

    Returns the job handle, which must be kept alive (closing it kills the child). On other
    platforms, or if the OS refuses, returns None and the child is only stopped on a clean exit.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    if ok and kernel32.AssignProcessToJobObject(job, int(proc._handle)):
        return job
    kernel32.CloseHandle(job)
    return None


class Tunnel:
    """One supervised cloudflared process: restarted with backoff if it ever exits on its own."""

    def __init__(self, exe: str, config: str, hostname: str = "", argv: list[str] | None = None):
        self.exe, self.config, self.hostname = exe, config, hostname
        # --no-autoupdate: cloudflared would otherwise replace and relaunch itself, escaping the
        # job object and the supervisor. Update by re-downloading the binary instead.
        self.argv = argv or [exe, "tunnel", "--no-autoupdate", "--config", config, "run"]
        self.connected = False
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervise, name="cloudflared", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=8)

    def _supervise(self) -> None:
        _warn_if_stale(self.exe)            # here, not at startup: it shouldn't delay the server
        delay = 5
        while not self._stopping.is_set():
            started = time.monotonic()
            code = self._run_once()
            if self._stopping.is_set():
                break
            self.connected = False
            print(f"[OmniDL] Remote access: cloudflared exited (code {code}); restarting in {delay}s. "
                  f"See {LOG_PATH.name}.")
            if self._stopping.wait(delay):
                break
            delay = 5 if time.monotonic() - started > 300 else min(delay * 2, _BACKOFF_MAX)

    def _run_once(self) -> int | None:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            log = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
        except OSError:
            log = None
        try:
            self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                          errors="replace", creationflags=flags)
        except OSError as exc:
            print(f"[OmniDL] Remote access: could not start cloudflared ({exc}).")
            if log:
                log.close()
            return None
        self._job = kill_with_this_process(self._proc)
        try:
            for line in self._proc.stdout:
                if log:
                    log.write(line)
                    log.flush()
                if not self.connected and "Registered tunnel connection" in line:
                    self.connected = True
                    where = f"https://{self.hostname}" if self.hostname else "the tunnel"
                    print(f"[OmniDL] Remote access: connected — {where}")
        finally:
            if log:
                log.close()
        code = self._proc.wait()
        close_job(self._job)
        self._job = None
        return code


def start_from_config(cfg: dict) -> Tunnel | None:
    """Start the tunnel remote.json describes, or explain why not. None if not configured."""
    config = cfg.get("tunnel_config") or ""
    if not config:
        return None
    if not Path(config).is_file():
        print(f"[OmniDL] Remote access: tunnel config not found ({config}); remote access is off.")
        return None
    exe = find_cloudflared(cfg.get("cloudflared", ""))
    if not exe:
        print("[OmniDL] Remote access: cloudflared not found; remote access is off.")
        return None
    tunnel = Tunnel(exe, config, cfg.get("hostname", ""))
    tunnel.start()
    print(f"[OmniDL] Remote access: starting tunnel for https://{cfg.get('hostname') or '?'}")
    return tunnel
