"""Load / save persistent settings (config.json).

config.json holds the SERVER defaults (operator-controlled). Individual visitors never
write it; instead each session keeps its own small set of preferences (see
``SESSION_PREF_KEYS``) and the rest of the values come from these defaults.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# When frozen (PyInstaller), keep config + downloads next to the .exe so they
# persist across runs; otherwise use the project root.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "downloads"

# Where per-session/per-job downloads land before they're handed to the browser, and how
# long finished files survive before the cleanup task removes them.
DOWNLOAD_ROOT = Path(os.environ.get("OMNIDL_DOWNLOAD_ROOT") or (PROJECT_ROOT / "downloads"))
FILE_TTL_SECONDS = int(os.environ.get("OMNIDL_FILE_TTL") or 3600)

# Local (personal) mode: write straight into the configured output folder and offer a
# "Open folder" button instead of per-download "Save". Hosted mode (the default when you
# publish with OMNIDL_HOST=0.0.0.0) keeps the temp-folder + browser-delivery flow.
# Defaults to ON for a local bind, OFF when bound publicly; override with OMNIDL_LOCAL.
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


_HOST_IS_LOCAL = os.environ.get("OMNIDL_HOST", "127.0.0.1") in ("127.0.0.1", "localhost", "")
LOCAL_MODE = _env_bool("OMNIDL_LOCAL", default=_HOST_IS_LOCAL)

# Access gate. YouTube bot-walls datacenter IPs, so those downloads need the operator's
# cookies — and one Google account cannot safely serve the public (it gets rate-limited and
# banned, and every download is attributed to it). So when a passphrase is set, the sources
# that consume those cookies (YouTube, and Spotify because it resolves via YouTube) require
# unlocking, while everything that works cookie-free stays open to anyone.
# Unset = no gate at all (the normal case for a local/personal install).
ACCESS_PASSPHRASE = os.environ.get("OMNIDL_ACCESS_PASSPHRASE", "").strip()


def gate_enabled() -> bool:
    return bool(ACCESS_PASSPHRASE) and not LOCAL_MODE


# Optional yt-dlp PO-token provider (e.g. bgutil-ytdlp-pot-provider's HTTP sidecar).
# Dormant unless OMNIDL_POT_PROVIDER_URL is set. When set, yt-dlp is told to fetch
# Proof-of-Origin tokens from it, which clears YouTube "confirm you're not a bot" walls on
# a busy/hosted IP. Requires the matching yt-dlp plugin installed in the environment.
POT_PROVIDER_URL = os.environ.get("OMNIDL_POT_PROVIDER_URL", "").strip()

# Settings a visitor is allowed to change for their own session. Everything else
# (output dir, Spotify credentials, cookies, threads, spotify method) is server-controlled
# and never exposed to or writable by clients.
SESSION_PREF_KEYS = {
    "audio_format", "video_container", "bitrate", "concurrency",
    "prefer_ytmusic", "spotify_match_duration", "sponsorblock", "skip_existing",
}
# Never send these back to a browser.
_SECRET_KEYS = {"spotify_client_id", "spotify_client_secret", "cookie_file"}
# A home/residential IP tolerates far more parallel YouTube requests than a datacenter
# one, so the local cap is higher; hosted stays conservative to avoid bot-detection.
MAX_CONCURRENCY = 8 if LOCAL_MODE else 4

# Audio formats we expose in the UI. opus first: it's YouTube's native codec, so it's
# remuxed (no re-encode) into the smallest file at full source quality.
AUDIO_FORMATS = ["opus", "m4a", "mp3", "flac", "wav", "ogg"]
# Video quality options (YouTube & any yt-dlp-supported link). "Best" = no height cap;
# the rest cap the picked video stream at that height. Merged to mp4 via the bundled ffmpeg.
VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
# Output container for video downloads. mp4 = most compatible; mkv = more robust for
# odd/unusual codecs (it can hold almost anything without re-encoding).
VIDEO_CONTAINERS = ["mp4", "mkv"]
BROWSERS = ["none", "chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]

DEFAULTS: dict[str, Any] = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "output_dir": str(DEFAULT_OUTPUT),
    # opus is YouTube's native audio codec -> remuxed without re-encoding: ~half the size
    # of a 320k mp3 at full source quality. mp3/m4a/flac still selectable in Settings.
    "audio_format": "opus",
    "bitrate": "320k",
    # Download mode for YouTube / pasted links: "audio" (extract audio) or "video"
    # (full video, merged to mp4). Spotify & SoundCloud always download audio.
    "media_type": "audio",
    "video_quality": "1080p",
    "video_container": "mp4",
    "spotify_template": "{artists} - {title}.{output-ext}",
    # How to handle Spotify links:
    #   "embed"  -> scrape the public embed page for the track list, download via yt-dlp
    #               (free, no API, no Premium — the default since Spotify locked the API).
    #   "spotdl" -> use spotdl (only works if your dev app's owner has Premium).
    "spotify_method": "embed",
    "cookie_file": "",          # path to a cookies.txt (used by spotdl + yt-dlp)
    "cookies_from_browser": "none",  # yt-dlp only: pull cookies straight from a browser
    "default_text_engine": "youtube",  # plain-text searches always go to yt-dlp now
    "threads": 4,
    # How many playlist tracks to download at once. Live output works at any level:
    # 1 = full streaming log; >1 = one live, in-place updating line per concurrent track.
    # Kept low (2) on purpose: more parallel YouTube requests => more transient
    # rate-limit "Video unavailable" failures. Raise only if your network/cookies allow.
    "concurrency": 2,
    # Spotify resolver: prefer YouTube Music official audio ("Topic" channel) over a
    # plain YouTube search — clean studio audio, no music-video intros/outros.
    "prefer_ytmusic": True,
    # Pick the result whose length matches the Spotify track (avoids mixes / live edits).
    "spotify_match_duration": True,
    # yt-dlp SponsorBlock: trim non-music segments (intros/outros/ads) from downloads.
    "sponsorblock": False,
    # Skip tracks already present in the output folder (resume playlists).
    "skip_existing": True,
}

BOOL_KEYS = {"prefer_ytmusic", "spotify_match_duration", "sponsorblock", "skip_existing"}


def ensure_output_dir(path: str) -> None:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def load_settings() -> dict:
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for key in DEFAULTS:
                    if key in stored:
                        data[key] = stored[key]
        except (json.JSONDecodeError, OSError):
            pass
    if not data.get("output_dir"):
        data["output_dir"] = str(DEFAULT_OUTPUT)
    ensure_output_dir(data["output_dir"])
    return data


def save_settings(new: dict) -> dict:
    data = load_settings()
    for key in DEFAULTS:
        if key in new and new[key] is not None:
            value = new[key]
            if key == "threads":
                try:
                    value = max(1, int(value))
                except (TypeError, ValueError):
                    value = DEFAULTS["threads"]
            elif key == "concurrency":
                try:
                    value = max(1, min(8, int(value)))
                except (TypeError, ValueError):
                    value = DEFAULTS["concurrency"]
            elif key in BOOL_KEYS:
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            data[key] = value
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    ensure_output_dir(data["output_dir"])
    return data


# ----- per-session preferences (hosted, multi-user) ----------------------------------

def _coerce_pref(key: str, value: Any) -> Any:
    if key == "concurrency":
        try:
            return max(1, min(MAX_CONCURRENCY, int(value)))
        except (TypeError, ValueError):
            return DEFAULTS["concurrency"]
    if key in BOOL_KEYS:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if key == "audio_format":
        return value if value in AUDIO_FORMATS else DEFAULTS["audio_format"]
    if key == "video_container":
        return value if value in VIDEO_CONTAINERS else DEFAULTS["video_container"]
    return value


def clean_session_prefs(new: dict) -> dict:
    """Keep only the keys a visitor is allowed to set, coerced to safe values."""
    out: dict[str, Any] = {}
    for key in SESSION_PREF_KEYS:
        if key in new and new[key] is not None:
            out[key] = _coerce_pref(key, new[key])
    return out


def effective_settings(prefs: dict | None = None) -> dict:
    """Server defaults overlaid with one session's allowed preferences. Spotify is forced
    to the credential-free embed method (no per-user API keys on a public service)."""
    data = load_settings()
    if prefs:
        for key in SESSION_PREF_KEYS:
            if key in prefs:
                data[key] = prefs[key]
    data["spotify_method"] = "embed"
    # A cookies.txt (from a logged-in YouTube account) is the strongest defence against
    # bot-gating. Hosted operators point to one with OMNIDL_COOKIE_FILE.
    env_cookie = os.environ.get("OMNIDL_COOKIE_FILE")
    if env_cookie:
        data["cookie_file"] = env_cookie
    return data


# Durable YouTube/Google login cookies (the short-lived *PSIDTS pair is ignored on purpose).
_AUTH_COOKIES = {
    "sid", "hsid", "ssid", "apisid", "sapisid", "login_info",
    "__secure-1psid", "__secure-3psid",
}


def cookie_file_status(path: str) -> tuple[str, str] | None:
    """Best-effort health check of a cookies.txt. Returns (level, message), or None if fine.

    level is one of: missing, foreign (not a YouTube export), expired, soon.
    Never raises — a cookies problem must never stop the app from starting.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return ("missing", f"cookies file not found: {path}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ("missing", f"cookies file unreadable: {exc}")
    now = time.time()
    found_youtube = found_auth = False
    latest = 0.0
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, expiry, name = parts[0], parts[4], parts[5]
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        found_youtube = True
        if name.strip().lower() in _AUTH_COOKIES:
            found_auth = True
            try:
                latest = max(latest, float(expiry))
            except ValueError:
                pass
    if not found_youtube:
        return ("foreign", "cookies file has no YouTube/Google cookies - is it the right export?")
    if not found_auth:
        return ("foreign", "cookies file has no YouTube login cookies - export while signed in to youtube.com")
    if latest and latest < now:
        return ("expired", "YouTube cookies appear expired - re-export cookies.txt while signed in")
    if latest and latest < now + 7 * 86400:
        return ("soon", "YouTube cookies expire within a week - consider re-exporting soon")
    return None


def public_settings(prefs: dict | None = None) -> dict:
    """The settings a browser may see: no secrets, no server-only paths/fields.
    In local (personal) mode the output folder + cookies file are editable, so kept."""
    data = effective_settings(prefs)
    drop = ["spotify_client_id", "spotify_client_secret", "cookies_from_browser",
            "threads", "spotify_method", "spotify_template", "default_text_engine"]
    if not LOCAL_MODE:
        drop += ["output_dir", "cookie_file"]
    for key in drop:
        data.pop(key, None)
    return data
