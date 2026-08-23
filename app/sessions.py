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


UNLOCK_COOKIE = "omnidl_unlock"


def unlock_token(sid: str, tier: str = "user") -> str:
    """Proof that this session passed the access gate.

    Kept in its own signed cookie rather than server memory so an unlocked visitor stays
    unlocked across restarts and deploys. Bound to the session id, so it can't be lifted
    from one browser and replayed in another.
    """
    return hmac.new(_SECRET, f"unlock:{tier}:{sid}".encode(), hashlib.sha256).hexdigest()[:32]


def unlock_cookie(sid: str, tier: str) -> str:
    """Cookie value carrying the tier, signed so the tier itself can't be edited upward."""
    return f"{tier}|{unlock_token(sid, tier)}"


def unlock_tier(sid: str, cookie_value: str | None) -> str | None:
    """Return the granted tier ("owner"/"user") for this session, or None."""
    if not cookie_value or "|" not in cookie_value:
        return None
    tier, _, mac = cookie_value.partition("|")
    if tier in ("user", "owner") and hmac.compare_digest(mac, unlock_token(sid, tier)):
        return tier
    return None


def unlock_valid(sid: str, cookie_value: str | None) -> bool:
    return unlock_tier(sid, cookie_value) is not None


def read_valid_sid(cookie_value: str | None) -> str | None:
    """Return the session id from a cookie value if its signature checks out, else None."""
    if not cookie_value or "." not in cookie_value:
        return None
    sid, _, mac = cookie_value.rpartition(".")
    if sid and hmac.compare_digest(mac, _mac(sid)):
        return sid
    return None
