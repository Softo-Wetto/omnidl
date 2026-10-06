"""Keep Windows from sleeping while OmniDL has work to do, or must stay reachable.

A download interrupted by sleep is left half-written, and while remote access is on a sleeping
PC simply vanishes — there's no way to wake it from the Mac. So while either is the case,
OmniDL holds off *system* sleep. The display can still turn off, and the hold is released the
moment it's no longer needed (or OmniDL exits — Windows drops it with the thread).
"""
from __future__ import annotations

import sys
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter():
    if sys.platform != "win32":
        return None
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetThreadExecutionState.argtypes = (ctypes.c_uint32,)
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    return lambda flags: kernel32.SetThreadExecutionState(flags)


class KeepAwake:
    """Holds off sleep while `needed()` is true, re-checked every `interval` seconds.

    The hold belongs to the calling thread on Windows, so one dedicated thread both sets and
    clears it.
    """

    def __init__(self, needed, interval: float = 15, setter=None):
        self.needed = needed
        self.interval = interval
        self.awake = False
        self._set = setter or _windows_setter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._set is None:
            return                          # not Windows: nothing to do
        self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        try:
            while True:
                try:
                    want = bool(self.needed())
                except Exception:  # noqa: BLE001 — a bad check must not keep the PC up forever
                    want = False
                if want != self.awake:
                    self._set(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
                    self.awake = want
                if self._stop.wait(self.interval):
                    break
        finally:
            if self.awake:
                self._set(ES_CONTINUOUS)
                self.awake = False
