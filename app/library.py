"""Review-first inspection and duplicate awareness for a local music library."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm",
}
LOSSLESS_CODECS = {"flac", "wav", "alac"}
_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return _SPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", value)).strip()


def _filename_identity(path: Path) -> tuple[str, str] | None:
    parts = path.stem.rsplit(" - ", 1)
    if len(parts) != 2:
        return None
    artist, title = (part.strip() for part in parts)
    if not artist or not title:
        return None
    return artist, title


def _first(tags, key: str) -> str:
    if not tags:
        return ""
    value = tags.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _codec(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"m4a", "mp4", "aac"}:
        return "aac"
    return suffix


def _has_artwork(audio) -> bool:
    if audio is None:
        return False
    if getattr(audio, "pictures", None):
        return True
    tags = getattr(audio, "tags", None)
    if not tags:
        return False
    keys = [str(key).casefold() for key in tags.keys()]
    return any(
        key.startswith("apic")
        or key in {"covr", "metadata_block_picture", "coverart", "coverartmime"}
        for key in keys
    )


@dataclass(frozen=True)
class LibraryTrack:
    path: Path
    relative_path: str
    title: str
    artist: str
    album: str
    duration: float
    bitrate: int
    sample_rate: int
    channels: int
    codec: str
    size: int
    has_artwork: bool
    issues: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str] | None:
        fallback = _filename_identity(Path(self.relative_path))
        artist = self.artist or (fallback[0] if fallback else "")
        title = self.title or (fallback[1] if fallback else "")
        key = (_normalise(artist), _normalise(title))
        return key if all(key) else None

    @property
    def repairable(self) -> bool:
        return bool(
            _filename_identity(Path(self.relative_path))
            and ("missing_artist" in self.issues or "missing_title" in self.issues)
            and "unreadable" not in self.issues
        )

    def to_dict(self) -> dict:
        return {
            "path": self.relative_path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "display_title": self.title or (_filename_identity(Path(self.relative_path)) or ("", Path(self.relative_path).stem))[1],
            "display_artist": self.artist or (_filename_identity(Path(self.relative_path)) or ("Unknown artist", ""))[0],
            "duration": round(self.duration, 1),
            "bitrate": self.bitrate,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "codec": self.codec,
            "size": self.size,
            "has_artwork": self.has_artwork,
            "issues": list(self.issues),
            "repairable": self.repairable,
            "quality_score": quality_score(self),
        }


def quality_score(track: LibraryTrack) -> int:
    """Return a comparison score, not a claim about perceptual quality."""
    codec = track.codec.casefold()
    bitrate_k = max(track.bitrate, 0) // 1000
    if codec in LOSSLESS_CODECS:
        return min(100, 94 + (4 if track.sample_rate >= 48_000 else 0))
    if codec == "opus":
        return min(93, 78 + bitrate_k // 14)
    if codec in {"aac", "m4a"}:
        return min(91, 75 + bitrate_k // 15)
    if codec == "ogg":
        return min(88, 72 + bitrate_k // 15)
    if codec == "mp3":
        return min(86, 64 + bitrate_k // 16)
    return min(80, 55 + bitrate_k // 16)


def _compatible_duration(left: LibraryTrack, right: LibraryTrack, tolerance: float = 5.0) -> bool:
    return not left.duration or not right.duration or abs(left.duration - right.duration) <= tolerance


def find_duplicate_groups(tracks: Iterable[LibraryTrack]) -> list[list[LibraryTrack]]:
    buckets: dict[tuple[str, str], list[LibraryTrack]] = {}
    for item in tracks:
        if item.identity:
            buckets.setdefault(item.identity, []).append(item)

    groups: list[list[LibraryTrack]] = []
    for bucket in buckets.values():
        clusters: list[list[LibraryTrack]] = []
        for item in sorted(bucket, key=lambda track: track.duration):
            cluster = next(
                (candidate for candidate in clusters if _compatible_duration(candidate[0], item)),
                None,
            )
            if cluster is None:
                clusters.append([item])
            else:
                cluster.append(item)
        groups.extend(cluster for cluster in clusters if len(cluster) > 1)
    return groups


def _best_track(tracks: Iterable[LibraryTrack]) -> LibraryTrack:
    return max(
        tracks,
        key=lambda item: (quality_score(item), item.sample_rate, item.bitrate, item.size),
    )


class LibraryIndex:
    def __init__(self, tracks: Iterable[LibraryTrack]):
        self.tracks = list(tracks)
        self._by_identity: dict[tuple[str, str], list[LibraryTrack]] = {}
        for item in self.tracks:
            if item.identity:
                self._by_identity.setdefault(item.identity, []).append(item)

    def find(self, artist: str, title: str, duration: int = 0) -> LibraryTrack | None:
        matches = self._by_identity.get((_normalise(artist), _normalise(title)), [])
        if duration:
            matches = [
                item for item in matches
                if not item.duration or abs(item.duration - duration) <= 5
            ]
        return _best_track(matches) if matches else None


def inspect_audio_file(path: Path, root: Path) -> LibraryTrack:
    from mutagen import File

    relative = path.relative_to(root).as_posix()
    issues: list[str] = []
    title = artist = album = ""
    duration = 0.0
    bitrate = sample_rate = channels = 0
    artwork = False
    try:
        easy = File(str(path), easy=True)
        raw = File(str(path))
        if easy is None or raw is None:
            raise ValueError("unsupported or corrupt audio")
        tags = getattr(easy, "tags", None)
        title = _first(tags, "title")
        artist = _first(tags, "artist") or _first(tags, "albumartist")
        album = _first(tags, "album")
        info = getattr(raw, "info", None)
        duration = float(getattr(info, "length", 0) or 0)
        bitrate = int(getattr(info, "bitrate", 0) or 0)
        sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        channels = int(getattr(info, "channels", 0) or 0)
        artwork = _has_artwork(raw)
    except Exception:
        issues.append("unreadable")

    if not title:
        issues.append("missing_title")
    if not artist:
        issues.append("missing_artist")
    if not album:
        issues.append("missing_album")
    if not artwork:
        issues.append("missing_artwork")
    codec = _codec(path)
    if codec not in LOSSLESS_CODECS and bitrate and bitrate < 128_000:
        issues.append("low_bitrate")

    return LibraryTrack(
        path=path,
        relative_path=relative,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        codec=codec,
        size=path.stat().st_size,
        has_artwork=artwork,
        issues=tuple(issues),
    )


def _audio_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
    )


def build_library_index(root: Path) -> LibraryIndex:
    root = root.resolve()
    return LibraryIndex(inspect_audio_file(path, root) for path in _audio_paths(root))


def scan_library(root: Path) -> dict:
    root = root.resolve()
    tracks = [inspect_audio_file(path, root) for path in _audio_paths(root)]
    duplicate_groups = []
    duplicate_paths: set[str] = set()
    reclaimable = 0
    for number, group in enumerate(find_duplicate_groups(tracks), start=1):
        recommended = _best_track(group)
        duplicate_paths.update(item.relative_path for item in group)
        reclaimable += sum(item.size for item in group if item != recommended)
        duplicate_groups.append({
            "id": f"duplicate-{number}",
            "artist": recommended.artist or (_filename_identity(Path(recommended.relative_path)) or ("Unknown artist", ""))[0],
            "title": recommended.title or (_filename_identity(Path(recommended.relative_path)) or ("", Path(recommended.relative_path).stem))[1],
            "recommended_path": recommended.relative_path,
            "reclaimable_size": sum(item.size for item in group if item != recommended),
            "files": [
                {**item.to_dict(), "recommended": item == recommended}
                for item in sorted(group, key=quality_score, reverse=True)
            ],
        })

    return {
        "root": str(root),
        "summary": {
            "total_files": len(tracks),
            "total_size": sum(item.size for item in tracks),
            "files_with_issues": sum(bool(item.issues) for item in tracks),
            "repairable_files": sum(item.repairable for item in tracks),
            "duplicate_groups": len(duplicate_groups),
            "duplicate_files": len(duplicate_paths),
            "reclaimable_size": reclaimable,
        },
        "tracks": [item.to_dict() for item in tracks],
        "duplicate_groups": duplicate_groups,
    }


def _contained_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path is outside the configured library")
    return target


def repair_missing_tags(root: Path, relative_path: str) -> dict:
    """Fill only missing artist/title tags derived from `Artist - Title`; never overwrite."""
    from mutagen import File

    target = _contained_path(root, relative_path)
    if not target.is_file() or target.suffix.casefold() not in AUDIO_EXTENSIONS:
        raise ValueError("audio file was not found")
    parsed = _filename_identity(target)
    if parsed is None:
        raise ValueError("filename must use 'Artist - Title' for safe repair")

    audio = File(str(target), easy=True)
    if audio is None:
        raise ValueError("audio metadata is unreadable")
    if audio.tags is None:
        audio.add_tags()
    artist, title = parsed
    changed: list[str] = []
    if not _first(audio.tags, "artist"):
        audio["artist"] = [artist]
        changed.append("artist")
    if not _first(audio.tags, "title"):
        audio["title"] = [title]
        changed.append("title")
    if changed:
        audio.save()
    return {"ok": True, "path": target.relative_to(root.resolve()).as_posix(), "changed": changed}
