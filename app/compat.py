"""Small fixes for the pinned server stack."""
from __future__ import annotations


def fix_uvicorn_shutdown() -> bool:
    """Make shutdown ask open connections to close before waiting for them.

    uvicorn 0.23 (spotDL pins it) stops listening, then awaits asyncio's Server.wait_closed()
    and only afterwards tells open connections to close. Since Python 3.12, wait_closed()
    really does wait for every connection — so a single dashboard tab, or a kept-alive
    connection, hung shutdown forever, and Ctrl+C twice didn't help. uvicorn 0.29 fixed the
    order; this applies the same fix to older versions. Returns True if it patched.
    """
    import uvicorn
    import uvicorn.server as server_module

    try:
        major, minor = (int(part) for part in uvicorn.__version__.split(".")[:2])
    except ValueError:
        return False
    if (major, minor) >= (0, 29):
        return False
    original = server_module.Server.shutdown
    if getattr(original, "_omnidl_fixed", False):
        return False

    async def shutdown(self, sockets=None):
        for listener in self.servers:
            listener.close()                       # stop accepting first
        for connection in list(self.server_state.connections):
            connection.shutdown()                  # then ask what's open to finish
        await original(self, sockets)

    shutdown._omnidl_fixed = True
    server_module.Server.shutdown = shutdown
    return True
