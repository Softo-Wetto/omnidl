"""Engine detection and command (argv) construction for the three downloaders.

All commands are returned as argv lists and executed with shell=False, so a pasted
URL or search term is always a single argument and can never inject shell commands.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import date
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


def is_youtube_url(text: str) -> bool:
    """True for a youtube.com / youtu.be / music.youtube.com link."""
    t = text.strip().lower()
    return is_url(t) and any(h in t for h in _YOUTUBE_HOSTS)


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
        return [sys.executable, "--run-tool", "yt-dlp" if tool == "yt-dlp-link" else tool]
    if tool == "spotdl":
        return [sys.executable, "-u", "-m", "spotdl"]
    if tool == "scdl":
        # Through OmniDL's wrapper, which adds the per-job file manifest (see scdl_tool).
        return [sys.executable, "-u", str(Path(__file__).with_name("scdl_tool.py"))]
    if tool == "yt-dlp-link":
        # A pasted link: yt-dlp with OmniDL's manifest and known-recording hooks (see
        # ytdlp_tool). The .exe's "--run-tool yt-dlp" always goes through the wrapper.
        return [sys.executable, "-u", str(Path(__file__).with_name("ytdlp_tool.py"))]
    return [_tool_path(tool)]


# Bookkeeping flags for OmniDL's wrappers (ytdlp_tool, scdl_tool) — not the tools' own options.
_MANIFEST_FLAG = "--omnidl-manifest"
_KNOWN_FLAG = "--omnidl-known"


def _link_hooks(s: dict) -> list[str]:
    """The wrapper flags for a staged link job: report each file, skip recordings already saved."""
    cmd: list[str] = []
    if s.get("_produced_file"):
        cmd += [_MANIFEST_FLAG, s["_produced_file"]]
    if s.get("_known_file"):
        cmd += [_KNOWN_FLAG, s["_known_file"]]
    return cmd


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
        level = _settings.audio_quality(s["bitrate"])
        cmd += ["--bitrate", level.lower()]
    if s.get("spotify_client_id") and s.get("spotify_client_secret"):
        cmd += ["--client-id", s["spotify_client_id"],
                "--client-secret", s["spotify_client_secret"]]
    if s.get("cookie_file"):
        cmd += ["--cookie-file", s["cookie_file"]]
    return cmd


def _ytdlp_common(s: dict, youtube: bool = True) -> list[str]:
    """yt-dlp args shared by the audio and video paths (JS runtime, cookies, SponsorBlock).

    `youtube` is False when the request goes to another site (a SoundCloud match, a direct
    link). The operator's YouTube cookies and PO-token provider are then left out entirely:
    a SoundCloud-only search must not use — or let yt-dlp rewrite — the YouTube login.
    """
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
    if youtube and cookie_file:
        cmd += ["--cookies", cookie_file]
    elif youtube and browser and browser != "none":
        cmd += ["--cookies-from-browser", browser]
    if s.get("sponsorblock"):
        cmd += ["--sponsorblock-remove", "music_offtopic"]
    if s.get("_produced_file") and not s.get("_stage_dir"):
        # One line per final file — how a local job knows what it saved, for Save on another
        # device. (A staged link job gets a richer manifest from ytdlp_tool instead.)
        cmd += ["--print-to-file", "after_move:filepath", s["_produced_file"]]
    if youtube and _settings.POT_PROVIDER_URL:
        cmd += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={_settings.POT_PROVIDER_URL}"]
    return cmd


_LOSSY_FORMATS = {"mp3", "m4a", "ogg", "opus"}


def conversion_quality(s: dict) -> str:
    """yt-dlp's --audio-quality for the Quality setting: a variable-bitrate level for the
    presets ("balanced" -> "4", LAME -V4 for MP3; yt-dlp scales it for AAC/Vorbis), or a
    fixed rate ("320k" -> "320K"). See settings.QUALITY_PRESETS for what each one measures."""
    return _settings.audio_quality(s.get("bitrate"))


def _ytdlp_base(s: dict, out_template: str, youtube: bool = True, tool: str = "yt-dlp") -> list[str]:
    """Common yt-dlp args (audio extraction, metadata, cookies, JS runtime)."""
    cmd = [
        *_tool_cmd(tool), "-x",
        "--audio-format", s["audio_format"],
        "--embed-metadata",
        "--newline",
        "-o", out_template,
    ]
    # Quality only matters when ffmpeg converts: MP3 always, M4A/Ogg/Opus when the source is a
    # different codec. yt-dlp copies a stream that's already in the target codec (YouTube Opus
    # -> .opus) without touching it, so this can't inflate or degrade those.
    if s["audio_format"] in _LOSSY_FORMATS:
        cmd += ["--audio-quality", conversion_quality(s)]
    if s["audio_format"] in _THUMBNAIL_OK:
        cmd.append("--embed-thumbnail")
    return cmd + _ytdlp_common(s, youtube)


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


def _ytdlp_video_base(s: dict, out_template: str, youtube: bool = True, tool: str = "yt-dlp") -> list[str]:
    """yt-dlp args to download full video, merged to one container."""
    container = s.get("video_container") or "mp4"
    cmd = [
        *_tool_cmd(tool),
        "-f", _video_format_selector(s.get("video_quality", "1080p"), container),
        "--merge-output-format", container,
        "--embed-metadata",
        "--newline",
        "-o", out_template,
    ]
    return cmd + _ytdlp_common(s, youtube)


def _youtube_cmd(text: str, s: dict) -> list[str]:
    stage = s.get("_stage_dir")
    if stage:
        # A link job downloads into its own staging folder, each video in a numbered folder of
        # its own, and OmniDL files the results into the output folder afterwards (see
        # jobs._settle_staged). Named by title alone, two different videos called the same
        # thing used to be one file: the second "already downloaded", with the first's audio.
        out_template = str(Path(stage) / "%(autonumber)s" / "%(title)s.%(ext)s")
        tool = "yt-dlp-link"
    else:
        out_template = str(Path(s["output_dir"]) / "%(title)s.%(ext)s")
        tool = "yt-dlp"
    youtube = not is_url(text) or is_youtube_url(text)
    if s.get("media_type") == "video":
        cmd = _ytdlp_video_base(s, out_template, youtube, tool)
    else:
        cmd = _ytdlp_base(s, out_template, youtube, tool)
    if stage:
        cmd += _link_hooks(s)
    cmd.append(text if is_url(text) else f"ytsearch1:{text}")
    return cmd


def youtube_video_command(video_id: str, out_basename: str, s: dict) -> list[str]:
    """yt-dlp command to download one known YouTube video."""
    return media_url_command(f"https://www.youtube.com/watch?v={video_id}", out_basename, s)


def media_url_command(url: str, out_basename: str, s: dict) -> list[str]:
    """yt-dlp command to download one verified media URL with a Spotify-derived name."""
    out_template = str(Path(s["output_dir"]) / f"{out_basename}.%(ext)s")
    cmd = _ytdlp_base(s, out_template, youtube=is_youtube_url(url))
    cmd += ["--no-playlist", url]
    return cmd


# What a SoundCloud link is saved as, per chosen format. SoundCloud serves MP3/Opus/AAC
# streams plus, for some tracks, the uploader's original file. scdl can pick among those
# but doesn't convert lossy audio into another format — which would only make a bigger file
# of the same quality — so WAV/Ogg/M4A keep SoundCloud's own file (see soundcloud_format_note).
_SOUNDCLOUD_FORMAT_ARGS = {
    "mp3": ["--onlymp3"],
    "opus": ["--opus", "--no-original"],
    "flac": ["--flac"],        # lossless originals become FLAC; lossy uploads stay as they are
}


def soundcloud_format_note(audio_format: str) -> str:
    """Plain-language caveat when a SoundCloud link can't be saved exactly as chosen."""
    if audio_format in ("mp3", "opus"):
        return ""
    if audio_format == "flac":
        return ("SoundCloud links: only lossless original uploads become FLAC; "
                "everything else is saved in SoundCloud's own format.")
    return (f"SoundCloud links aren't converted to {audio_format.upper()}: they're saved in "
            "SoundCloud's own format (usually MP3 or Opus, or the original upload).")


def _soundcloud_cmd(text: str, s: dict) -> list[str]:
    # A staged job downloads into its own folder and OmniDL files the tracks into the output
    # folder afterwards, as for any link (see _youtube_cmd).
    cmd = [*_tool_cmd("scdl"), "-l", text, "--path", s.get("_stage_dir") or s["output_dir"], "-c", "--addtofile"]
    cmd += _SOUNDCLOUD_FORMAT_ARGS.get(s["audio_format"], [])
    if s.get("_stage_dir"):
        cmd += _link_hooks(s)
    elif s.get("_produced_file"):
        # One line per file the job ends up with, including tracks already there (see
        # scdl_tool) — how a re-run or hosted SoundCloud playlist still offers its files.
        cmd += [_MANIFEST_FLAG, s["_produced_file"]]
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
    for flag in (_MANIFEST_FLAG, _KNOWN_FLAG):         # bookkeeping, not part of the command
        if flag in tokens:
            at = tokens.index(flag)
            del tokens[at:at + 2]
    if len(tokens) >= 3 and tokens[1] == "--run-tool":
        tokens = tokens[2:]
    elif tokens[:4] == [sys.executable, "-u", "-m", "spotdl"]:
        tokens = ["spotdl", *tokens[4:]]
    elif len(tokens) >= 3 and tokens[0] == sys.executable and tokens[2].endswith("scdl_tool.py"):
        tokens = ["scdl", *tokens[3:]]
    elif len(tokens) >= 3 and tokens[0] == sys.executable and tokens[2].endswith("ytdlp_tool.py"):
        tokens = ["yt-dlp", *tokens[3:]]
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

# ---- yt-dlp freshness -------------------------------------------------------
# yt-dlp versions are release dates (e.g. 2026.08.19), so staleness is measurable with no
# network call. This matters more than it sounds: YouTube routinely breaks older releases
# outright — a 2026.06.09 build failed *every* YouTube download with a bare 403 until it was
# updated. The server auto-updates daily, but a frozen .exe bundles whatever was current at
# build time and never changes, so it needs to say something.
_YTDLP_AGE_CACHE: dict[str, object] = {}


def ytdlp_version() -> str:
    """Version string of the yt-dlp we actually invoke, or "" if it can't be determined."""
    if "v" in _YTDLP_AGE_CACHE:
        return str(_YTDLP_AGE_CACHE["v"])
    version = ""
    try:
        out = subprocess.run([*_tool_cmd("yt-dlp"), "--version"],
                             capture_output=True, text=True, timeout=30)
        version = (out.stdout or "").strip().splitlines()[0].strip()
    except Exception:
        pass
    _YTDLP_AGE_CACHE["v"] = version
    return version


def ytdlp_age_days() -> int | None:
    """How many days old the bundled/installed yt-dlp is, or None if unknown."""
    version = ytdlp_version()
    m = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})", version)
    if not m:
        return None
    try:
        released = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return (date.today() - released).days
