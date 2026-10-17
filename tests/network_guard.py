"""No unit test may reach the internet or start a real download.

Installed by importing this module (support.py does, so every test file that uses the shared
helpers is covered whichever way the suite is run).

It guards the whole test process: name lookups and connections to anything but this machine
raise, and so does starting a downloader on a web address or a search. A fixture mistake once let a test fall through to a real YouTube search and
download; now that fails loudly instead.
"""
import asyncio
import socket
import subprocess

_LOCAL = ("127.", "::1", "localhost", "0.0.0.0")
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_exec = asyncio.create_subprocess_exec
_real_popen_init = subprocess.Popen.__init__


def _local(host) -> bool:
    return str(host).startswith(_LOCAL)


def _check(address):
    host = address[0] if isinstance(address, tuple) else address
    if not _local(host):
        raise RuntimeError(f"unit tests must not use the network (tried {address!r})")


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None and not _local(host):
        raise RuntimeError(f"unit tests must not look up {host!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


def _remote_target(argv) -> str | None:
    for arg in map(str, argv):
        if (arg.startswith(("http://", "https://")) and "127.0.0.1" not in arg and "localhost" not in arg) \
                or arg.startswith(("ytsearch", "scsearch")):
            return arg
    return None


async def _exec(*argv, **kwargs):
    target = _remote_target(argv)
    if target:
        raise RuntimeError(f"unit tests must not start a real download ({target})")
    return await _real_exec(*argv, **kwargs)


def _popen_init(self, args, *rest, **kwargs):
    target = _remote_target(args if isinstance(args, (list, tuple)) else [args])
    if target:
        raise RuntimeError(f"unit tests must not start a real download ({target})")
    return _real_popen_init(self, args, *rest, **kwargs)


if not getattr(socket, "_omnidl_guarded", False):
    socket._omnidl_guarded = True
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.getaddrinfo = _getaddrinfo
    asyncio.create_subprocess_exec = _exec
    subprocess.Popen.__init__ = _popen_init

