"""Let the owner use their local OmniDL from other devices, through Cloudflare Tunnel.

Local mode trusts every request completely — no passphrase, no download limits, settings that
write to config.json, a scanner over the music library — because on 127.0.0.1 the only caller
is you. A tunnel breaks that assumption: cloudflared connects from 127.0.0.1 as well, so every
request from the internet would look local.

So a request that arrived through Cloudflare must carry a valid Cloudflare Access token for
this app, checked here, or it is refused. Access is still what keeps strangers out; this is
what makes OmniDL fail closed if Access is ever misconfigured or switched off, instead of
quietly opening your PC to anyone who finds the address.

Settings live in remote.json beside config.json, deliberately out of reach of the settings
API: the web UI is exactly what a remote caller can reach, so it must not be able to change
the rules that decide who is allowed in.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .settings import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "remote.json"

# DER prefix of the DigestInfo for SHA-256 (RFC 8017 §9.2): what RS256 signs, ahead of the hash.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_CLOCK_SKEW = 60                 # seconds of leeway on exp/nbf
_KEYS_TTL = 3600                 # re-read the team's signing keys hourly (Cloudflare rotates them)
_REFETCH_GAP = 60                # at most one key refresh a minute, however many bad tokens arrive
_DISCOVERY_GAP = 30              # at most one Access lookup per 30s


class AccessError(Exception):
    """Why a remote request was refused. The message is shown to the visitor."""


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(updates: dict) -> None:
    data = load_config()
    data.update(updates)
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def came_through_cloudflare(headers) -> bool:
    """True if Cloudflare's edge forwarded this request (i.e. it came in through the tunnel).

    Cloudflare sets these on every proxied request and a visitor can't strip them, so their
    presence is the reliable signal. A direct request to 127.0.0.1 never carries them.
    """
    return "cf-connecting-ip" in headers or "cf-ray" in headers


def token_from(headers, cookies) -> str:
    """Access puts its token in a header on proxied requests; the cookie covers anything that
    reaches us without it (some WebSocket upgrades)."""
    return headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization") or ""


# ---- RS256 ---------------------------------------------------------------------------------
def _b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _rsa_sha256_valid(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 — the RS256 check, with no crypto dependency.

    Verification needs no secrets, so plain integer maths is safe here. The one thing that
    matters is HOW the result is compared: rebuild the exact padded block the signer must have
    produced and compare it whole. Parsing the decrypted block leniently instead is the classic
    way RSA signature checks get forged (Bleichenbacher's e=3 attack).
    """
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = k - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(pow(s, e, n).to_bytes(k, "big"), expected)


class AccessVerifier:
    """Checks Cloudflare Access tokens against one pinned team and one pinned application.

    The team and application (its "AUD tag") are learned from Cloudflare itself — see
    `discover()` — so setting this up never means copying IDs out of the dashboard.
    """

    def __init__(self, team: str = "", aud: str = "", hostname: str = "",
                 fetch_json=None, fetch_redirect=None):
        self.team = team
        self.aud = aud
        self.hostname = hostname
        self._keys: dict[str, tuple[int, int]] = {}
        self._keys_at = 0.0
        self._last_refetch = 0.0
        self._last_discovery = 0.0
        self._discovering = False
        self._fetch_json = fetch_json or _fetch_json
        self._fetch_redirect = fetch_redirect or _fetch_redirect

    @classmethod
    def from_config(cls) -> "AccessVerifier":
        cfg = load_config()
        return cls(team=cfg.get("access_team", ""), aud=cfg.get("access_aud", ""),
                   hostname=cfg.get("hostname", ""))

    @property
    def issuer(self) -> str:
        return f"https://{self.team}.cloudflareaccess.com"

    # -- signing keys --------------------------------------------------------------------
    async def _key(self, kid: str) -> tuple[int, int] | None:
        stale = time.monotonic() - self._keys_at > _KEYS_TTL
        if (stale or kid not in self._keys) and time.monotonic() - self._last_refetch > _REFETCH_GAP:
            self._last_refetch = time.monotonic()
            try:
                data = await asyncio.to_thread(self._fetch_json, f"{self.issuer}/cdn-cgi/access/certs")
                keys = {}
                for jwk in data.get("keys") or []:
                    if jwk.get("kty") == "RSA" and jwk.get("kid") and jwk.get("n") and jwk.get("e"):
                        keys[jwk["kid"]] = (int.from_bytes(_b64url(jwk["n"]), "big"),
                                            int.from_bytes(_b64url(jwk["e"]), "big"))
                if keys:
                    self._keys, self._keys_at = keys, time.monotonic()
            except Exception:  # noqa: BLE001 — keep the old keys; a fetch blip mustn't lock you out
                pass
        return self._keys.get(kid)

    # -- verification --------------------------------------------------------------------
    async def verify(self, token: str) -> dict:
        """Return the token's claims if it's a live token for this app, else raise AccessError."""
        if not (self.team and self.aud):
            await self.discover()
            if not (self.team and self.aud):
                raise AccessError(
                    "Remote access is locked: no Cloudflare Access application protects this "
                    "address yet. Add one in the Cloudflare Zero Trust dashboard.")
        if not token:
            raise AccessError("Remote access needs a Cloudflare Access login.")
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = json.loads(_b64url(header_b64))
            claims = json.loads(_b64url(payload_b64))
            signature = _b64url(sig_b64)
        except (ValueError, json.JSONDecodeError):
            raise AccessError("Malformed Access token.") from None
        # Pin the algorithm: accepting whatever the token names is how "alg: none" and
        # HS256-with-the-public-key forgeries get through.
        if header.get("alg") != "RS256":
            raise AccessError("Unexpected Access token algorithm.")
        key = await self._key(str(header.get("kid") or ""))
        if key is None:
            raise AccessError("Access token signed by an unknown key.")
        if not _rsa_sha256_valid(key[0], key[1], f"{header_b64}.{payload_b64}".encode("ascii"), signature):
            raise AccessError("Access token signature is invalid.")
        if claims.get("iss") != self.issuer:
            raise AccessError("Access token is from a different Cloudflare team.")
        audiences = claims.get("aud")
        audiences = audiences if isinstance(audiences, list) else [audiences]
        if self.aud not in audiences:
            # Maybe the app was recreated and has a new tag; re-ask Cloudflare once, then re-check.
            if await self.discover() and self.aud in audiences:
                return claims
            raise AccessError("Access token is for a different application.")
        now = time.time()
        try:
            if float(claims["exp"]) < now - _CLOCK_SKEW:
                raise AccessError("Access login has expired — reload to sign in again.")
            if float(claims.get("nbf", 0)) > now + _CLOCK_SKEW:
                raise AccessError("Access token is not valid yet.")
        except (KeyError, TypeError, ValueError):
            raise AccessError("Access token has no valid expiry.") from None
        return claims

    # -- discovery -----------------------------------------------------------------------
    async def discover(self) -> bool:
        """Learn the team and AUD tag from Cloudflare's own login redirect for our hostname.

        A logged-out request to an Access-protected hostname is answered by Cloudflare's edge
        (never by us) with a redirect to `<team>.cloudflareaccess.com/.../login/<host>?kid=<AUD>`.
        That answer is authoritative — only the zone owner can attach Access to the hostname —
        so it's as good as copying the values off the dashboard.

        Returns True if it changed what is pinned. Never blocks: if the app isn't behind Access,
        Cloudflare forwards the lookup back through the tunnel to us, and that inner request must
        be refused, not left waiting for the lookup that caused it.
        """
        if (not self.hostname or self._discovering
                or time.monotonic() - self._last_discovery < _DISCOVERY_GAP):
            return False
        self._discovering = True
        self._last_discovery = time.monotonic()
        try:
            location = await asyncio.to_thread(self._fetch_redirect, f"https://{self.hostname}/")
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._discovering = False
        found = parse_access_redirect(location or "", self.hostname)
        if not found or found == (self.team, self.aud):
            return False
        self.team, self.aud = found
        self._keys, self._keys_at = {}, 0.0
        try:
            save_config({"access_team": self.team, "access_aud": self.aud})
        except OSError:
            pass
        print(f"[OmniDL] Remote access: pinned to Cloudflare Access team {self.team!r}.")
        return True


def parse_access_redirect(location: str, hostname: str) -> tuple[str, str] | None:
    """(team, aud) from an Access login redirect for `hostname`, or None if it isn't one."""
    url = urllib.parse.urlsplit(location)
    if url.scheme != "https" or not url.hostname or not url.hostname.endswith(".cloudflareaccess.com"):
        return None
    team = url.hostname[: -len(".cloudflareaccess.com")]
    if not team or "." in team:
        return None
    if url.path.rstrip("/") != f"/cdn-cgi/access/login/{hostname}":
        return None
    aud = (urllib.parse.parse_qs(url.query).get("kid") or [""])[0]
    if not aud or not all(c in "0123456789abcdef" for c in aud):
        return None
    return team, aud


# ---- network (swapped out in tests) --------------------------------------------------------
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_redirect(url: str) -> str:
    """The Location of the first redirect `url` answers with, or "" if it doesn't redirect."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=10):
            return ""
    except urllib.error.HTTPError as err:
        return err.headers.get("Location", "") if 300 <= err.code < 400 else ""


REFUSED_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OmniDL - locked</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.2rem;color:#222">
<h1 style="font-size:1.4rem">This OmniDL is locked</h1>
<p>{message}</p>
<p style="color:#777;font-size:.9rem">Remote access to a home OmniDL only works through a Cloudflare Access login.</p>
</body>"""
