"""Load / save persistent settings (config.json)."""
from __future__ import annotations

import json
import sys
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

# Audio formats we expose in the UI. opus first: it's YouTube's native codec, so it's
# remuxed (no re-encode) into the smallest file at full source quality.
AUDIO_FORMATS = ["opus", "m4a", "mp3", "flac", "wav", "ogg"]
BROWSERS = ["none", "chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi"]

DEFAULTS: dict[str, Any] = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "output_dir": str(DEFAULT_OUTPUT),
    # opus is YouTube's native audio codec -> remuxed without re-encoding: ~half the size
    # of a 320k mp3 at full source quality. mp3/m4a/flac still selectable in Settings.
    "audio_format": "opus",
    "bitrate": "320k",
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
    "concurrency": 3,
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
