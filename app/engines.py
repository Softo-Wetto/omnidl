"""Engine detection and command (argv) construction for the three downloaders.

All commands are returned as argv lists and executed with shell=False, so a pasted
URL or search term is always a single argument and can never inject shell commands.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from . import settings as _settings

ENGINES = {
    "spotify": {"label": "Spotify", "tool": "spotdl"},
    "youtube": {"label": "YouTube", "tool": "yt-dlp"},
    "soundcloud": {"label": "SoundCloud", "tool": "scdl"},
}

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
    return "youtube"


_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")


def needs_youtube_account(text: str) -> bool:
    """True if this input will be fetched from YouTube, and so consumes the operator's cookies.

    Deliberately keyed on the *input*, not the engine: the "youtube" engine is really yt-dlp,
    which also handles direct file links and ~1000 other sites that work fine without cookies.
    Gating the engine would lock those out for no reason.

    Gated:   youtube.com / youtu.be links, Spotify (resolved via YouTube audio), and plain-text
             searches (they run as a YouTube search).
    Ungated: SoundCloud, direct media links, and any other site yt-dlp supports.
    """
    t = text.strip().lower()
    if any(h in t for h in _YOUTUBE_HOSTS):
        return True
    if "open.spotify.com" in t or t.startswith("spotify:"):
        return True
    if "soundcloud.com" in t:
        return False
    return not is_url(t)          # bare search text -> ytsearch


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


def _ytdlp_common(s: dict) -> list[str]:
    """yt-dlp args shared by the audio and video paths (JS runtime, cookies, SponsorBlock)."""
    cmd: list[str] = [
        "--retries", "5",
        "--extractor-retries", "3",
        "--retry-sleep", "3",
        "--socket-timeout", "20",
    ]
    node = shutil.which("node")
    if node:
        cmd += ["--js-runtimes", f"node:{node}"]
    cookie_file = s.get("cookie_file")
    browser = s.get("cookies_from_browser", "none")
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    elif browser and browser != "none":
        cmd += ["--cookies-from-browser", browser]
    if s.get("sponsorblock"):
        cmd += ["--sponsorblock-remove", "music_offtopic"]
    if _settings.POT_PROVIDER_URL:
        cmd += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={_settings.POT_PROVIDER_URL}"]
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
    # MP3 needs an ffmpeg transcode. 192K preserves excellent music quality without the
    # unexpectedly large files made by VBR-0 ("best"). Opus/m4a stay remuxed, not re-encoded.
    if s["audio_format"] == "mp3":
        cmd += ["--audio-quality", "192K"]
    if s["audio_format"] in _THUMBNAIL_OK:
        cmd.append("--embed-thumbnail")
    return cmd + _ytdlp_common(s)


_VIDEO_HEIGHTS = {"2160p": 2160, "1440p": 1440, "1080p": 1080,
                  "720p": 720, "480p": 480, "360p": 360}


def _video_format_selector(quality: str, container: str) -> str:
    """Return yt-dlp's video selector, capped by height and aware of container support."""
    height = _VIDEO_HEIGHTS.get(quality)
    cap = f"[height<={height}]" if height else ""
    if container == "mkv":
        return f"bv*{cap}+ba/b{cap}/bv*+ba/b"
    return (
        f"bv*{cap}[vcodec^=avc]+ba[ext=m4a]/"
        f"bv*{cap}+ba[ext=m4a]/"
        f"bv*{cap}+ba/"
        f"b{cap}/b"
    )


def _ytdlp_video_base(s: dict, out_template: str) -> list[str]:
    """yt-dlp args to download full video, merged to one container."""
    container = s.get("video_container") or "mp4"
    cmd = [
        *_tool_cmd("yt-dlp"),
        "-f", _video_format_selector(s.get("video_quality", "1080p"), container),
        "--merge-output-format", container,
        "--embed-metadata",
        "--newline",
        "-o", out_template,
    ]
    return cmd + _ytdlp_common(s)


def _youtube_cmd(text: str, s: dict) -> list[str]:
    out_template = str(Path(s["output_dir"]) / "%(title)s.%(ext)s")
    if s.get("media_type") == "video":
        cmd = _ytdlp_video_base(s, out_template)
    else:
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
    """yt-dlp command to fetch ONE track via search, named from Spotify metadata."""
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
    if len(tokens) >= 3 and tokens[1] == "--run-tool":
        tokens = tokens[2:]
    elif tokens[:4] == [sys.executable, "-u", "-m", "spotdl"]:
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
