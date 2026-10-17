"""Shared test helpers: fake downloads that behave like yt-dlp, and real (tiny) audio files."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import network_guard  # noqa: F401  — no test using these helpers can reach the internet

_TEMPLATES: dict[str, Path] = {}
_TEMPLATE_DIR = Path(tempfile.mkdtemp(prefix="omnidl-test-audio-"))


def output_of(argv: list[str]) -> Path:
    """The file a yt-dlp command would produce: its -o template with the audio format filled in."""
    template = argv[argv.index("-o") + 1]
    ext = argv[argv.index("--audio-format") + 1] if "--audio-format" in argv else "opus"
    return Path(template.replace("%(ext)s", ext))


def write_output(argv: list[str], data: bytes = b"\x00" * 4096) -> Path:
    """Do what a successful download does: write the file where the command said to."""
    path = output_of(argv)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_audio_output(argv: list[str], seconds: float = 3.0) -> Path:
    """Like write_output, but the file is real audio (so it counts as an existing song)."""
    return make_audio(output_of(argv), seconds)


def make_audio(path: Path, seconds: float = 3.0) -> Path:
    """A real, playable audio file at `path` (codec from its suffix), made once with ffmpeg."""
    suffix = path.suffix.lower()
    key = f"{suffix}-{seconds}"
    if key not in _TEMPLATES:
        template = _TEMPLATE_DIR / f"template-{seconds}{suffix}"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={seconds}", "-ac", "2", str(template)], check=True)
        _TEMPLATES[key] = template
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_TEMPLATES[key], path)
    return path
