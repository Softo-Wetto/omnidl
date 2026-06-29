"""Per-visitor sessions via a signed, http-only cookie.

A hosted OmniDL serves many strangers at once, so each browser gets an opaque,
unguessable session id. Jobs, downloaded files, settings and the live socket are all
scoped to it, so no visitor can see or touch another's activity. The cookie is HMAC-signed
with a server secret so a client can't forge or swap in someone else's id.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

COOKIE_NAME = "omnidl_sid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Mark the cookie Secure (HTTPS-only) when served over TLS. Set OMNIDL_SECURE_COOKIES=1
# in production behind HTTPS; leave unset for plain-HTTP local development.
COOKIE_SECURE = os.environ.get("OMNIDL_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes", "on")

# Stable across restarts only if OMNIDL_SECRET is set; otherwise a fresh secret each boot
# (which simply invalidates old cookies — visitors transparently get a new session).
_SECRET = (os.environ.get("OMNIDL_SECRET") or secrets.token_hex(32)).encode()


def new_sid() -> str:
    return secrets.token_urlsafe(18)


def _mac(sid: str) -> str:
    return hmac.new(_SECRET, sid.encode(), hashlib.sha256).hexdigest()[:32]


def sign(sid: str) -> str:
    return f"{sid}.{_mac(sid)}"


def read_valid_sid(cookie_value: str | None) -> str | None:
    """Return the session id from a cookie value if its signature checks out, else None."""
    if not cookie_value or "." not in cookie_value:
        return None
    sid, _, mac = cookie_value.rpartition(".")
    if sid and hmac.compare_digest(mac, _mac(sid)):
        return sid
    return None
