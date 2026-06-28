"""Engine detection and command (argv) construction for the three downloaders.

All commands are returned as argv *lists* and executed with shell=False, so a pasted
URL or search term is always a single argument and can never inject shell commands.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ENGINES = {
    "spotify": {"label": "Spotify", "tool": "spotdl"},
    "youtube": {"label": "YouTube", "tool": "yt-dlp"},
    "soundcloud": {"label": "SoundCloud", "tool": "scdl"},
}

# Formats that support an embedded cover thumbnail in yt-dlp.
_THUMBNAIL_OK = {"mp3", "m4a", "flac", "opus", "ogg"}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_url(text: str) -> bool:
    t = text.strip()
    return bool(_URL_RE.match(t)) or t.lower().startswith("spotify:")


def detect_engine(text: str, settings: dict) -> str:
    """Pick an engine from the raw input string."""
    t = text.strip().lower()
    if "open.spotify.com" in t or t.startswith("spotify:"):
        return "spotify"
    if "soundcloud.com" in t:
        return "soundcloud"
    if "youtube.com" in t or "youtu.be" in t:
        return "youtube"
    # Plain-text search: only YouTube can be searched (Spotify's API is dead and scdl
    # needs a URL), so a typed query always routes to yt-dlp.
    return "youtube"


FROZEN = getattr(sys, "frozen", False)


def _tool_path(name: str) -> str:
    """Resolve a console-script tool to an absolute path, falling back to its name."""
    return shutil.which(name) or name


def _tool_cmd(tool: str) -> list[str]:
    """Base argv that launches a downloader, in dev or frozen (.exe) mode.

    Frozen: re-invoke our own exe as `OmniDL.exe --run-tool <tool> ...`, which the
    entry point dispatches to the bundled module. Dev: use the installed tool.
    """
    if FROZEN:
        return [sys.executable, "--run-tool", tool]
    if tool == "spotdl":
        return [sys.executable, "-u", "-m", "spotdl"]
    return [_tool_path(tool)]


def _spotify_cmd(text: str, s: dict) -> list[str]:
    template = s.get("spotify_template") or "{artists} - {title}.{output-ext}"
    out_template = str(Path(s["output_dir"]) / template)
    cmd = [
        *_tool_cmd("spotdl"), "download", text,
        "--output", out_template,
        "--format", s["audio_format"],
        "--threads", str(s.get("threads", 4)),
    ]
    if s.get("bitrate"):
        cmd += ["--bitrate", s["bitrate"]]
    if s.get("spotify_client_id") and s.get("spotify_client_secret"):
        cmd += ["--client-id", s["spotify_client_id"],
                "--client-secret", s["spotify_client_secret"]]
    if s.get("cookie_file"):
        cmd += ["--cookie-file", s["cookie_file"]]
    return cmd


def _ytdlp_base(s: dict, out_template: str) -> list[str]:
    """Common yt-dlp args (audio extraction, metadata, cookies, JS runtime)."""
    cmd = [
        *_tool_cmd("yt-dlp"), "-x",
        "--audio-format", s["audio_format"],
        "--embed-metadata",
        "--newline",
        "-o", out_template,
    ]
    # Only force top quality for mp3 (a lossy transcode anyway). For opus/m4a this would
    # trigger a needless, bloated re-encode instead of remuxing the native stream.
    if s["audio_format"] == "mp3":
        cmd += ["--audio-quality", "0"]
    # Modern yt-dlp needs a JS runtime for YouTube; use Node if it's available
    # (silences the deprecation warning and unlocks all formats).
    node = shutil.which("node")
    if node:
        cmd += ["--js-runtimes", f"node:{node}"]
    if s["audio_format"] in _THUMBNAIL_OK:
        cmd.append("--embed-thumbnail")
    cookie_file = s.get("cookie_file")
    browser = s.get("cookies_from_browser", "none")
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    elif browser and browser != "none":
        cmd += ["--cookies-from-browser", browser]
    if s.get("sponsorblock"):
        # Trim non-music sections (intros/outros/offtopic) from music videos.
        cmd += ["--sponsorblock-remove", "music_offtopic"]
    return cmd


def _youtube_cmd(text: str, s: dict) -> list[str]:
    out_template = str(Path(s["output_dir"]) / "%(title)s.%(ext)s")
    cmd = _ytdlp_base(s, out_template)
    cmd.append(text if is_url(text) else f"ytsearch1:{text}")
    return cmd


def youtube_video_command(video_id: str, out_basename: str, s: dict) -> list[str]:
    """yt-dlp command to download one known YouTube video."""
    return media_url_command(f"https://www.youtube.com/watch?v={video_id}", out_basename, s)


def media_url_command(url: str, out_basename: str, s: dict) -> list[str]:
    """yt-dlp command to download one verified media URL with a Spotify-derived name."""
    out_template = str(Path(s["output_dir"]) / f"{out_basename}.%(ext)s")
    cmd = _ytdlp_base(s, out_template)
    cmd += ["--no-playlist", url]
    return cmd


def youtube_track_command(query: str, out_basename: str, s: dict,
                          duration: int = 0) -> list[str]:
    """yt-dlp command to fetch ONE track via search, named from Spotify metadata.

    When the Spotify track duration is known and matching is on, search the top few
    results and keep the first whose length is within a window of the real track —
    this avoids hour-long mixes, sped-up edits, and 10-minute live versions.
    """
    out_template = str(Path(s["output_dir"]) / f"{out_basename}.%(ext)s")
    cmd = _ytdlp_base(s, out_template)
    if duration and duration > 0 and s.get("spotify_match_duration", True):
        low = max(duration - 30, 0)
        high = duration + 45
        cmd += [
            "--match-filter", f"duration >= {low} & duration <= {high}",
            "--max-downloads", "1",
            f"ytsearch5:{query}",
        ]
    else:
        cmd += ["--no-playlist", f"ytsearch1:{query}"]
    return cmd


def _soundcloud_cmd(text: str, s: dict) -> list[str]:
    cmd = [*_tool_cmd("scdl"), "-l", text, "--path", s["output_dir"], "-c", "--addtofile"]
    if s["audio_format"] == "mp3":
        cmd.append("--onlymp3")
    return cmd


_BUILDERS = {
    "spotify": _spotify_cmd,
    "youtube": _youtube_cmd,
    "soundcloud": _soundcloud_cmd,
}


def build_command(engine: str, text: str, settings: dict) -> list[str]:
    if engine not in _BUILDERS:
        raise ValueError(f"unknown engine: {engine}")
    return _BUILDERS[engine](text, settings)


def describe_command(argv: list[str]) -> str:
    """Printable command line with secrets redacted, for the terminal header."""
    tokens = list(argv)
    # Collapse the launcher prefix down to the tool name for readability.
    if len(tokens) >= 3 and tokens[1] == "--run-tool":          # frozen: exe --run-tool TOOL
        tokens = tokens[2:]
    elif tokens[:4] == [sys.executable, "-u", "-m", "spotdl"]:   # dev: python -u -m spotdl
        tokens = ["spotdl", *tokens[4:]]
    elif tokens and tokens[0] == sys.executable:
        tokens = ["python", *tokens[1:]]

    redact_next = False
    parts: list[str] = []
    for token in tokens:
        if redact_next:
            parts.append("****")
            redact_next = False
            continue
        if token == "--client-secret":
            redact_next = True
        display = f'"{token}"' if " " in token else token
        parts.append(display)
    return " ".join(parts)
