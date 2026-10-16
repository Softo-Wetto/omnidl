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
    """Fold a name to a comparison key: case, punctuation and accents all ignored.

    Accents matter here because sources disagree on them - Spotify returns "GIVEON" as
    "GIVEON" with a macron while spotdl tagged the same artist plainly. Comparing them
    literally means the same song is downloaded twice.
    """
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return _SPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", value)).strip()


class LibraryRootError(OSError):
    """The library folder itself is missing or unusable — not the same as an empty library."""


_SEPARATOR = " - "
NAME_ORDERS = ("artist-title", "title-artist")


def _name_halves(path: Path) -> tuple[str, str] | None:
    """The two sides of "X - Y", or None unless the name has exactly one separator.

    "Artist - Song - Live" can be read two ways, and the old last-separator split took
    "Artist - Song" as the artist; guessing wrong writes the title into the artist tag.
    """
    parts = [part.strip() for part in path.stem.split(_SEPARATOR)]
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def _filename_identity(path: Path, order: str = "artist-title") -> tuple[str, str] | None:
    """(artist, title) from a filename named in `order`, or None if it can't be read safely."""
    halves = _name_halves(path)
    if halves is None:
        return None
    first, second = halves
    return (second, first) if order == "title-artist" else (first, second)


def read_identity(path: Path, artist: str, title: str,
                  order: str = "artist-title") -> tuple[str, str, str]:
    """Artist and title for a file, from its tags first and its filename for what's missing.

    Returns (artist, title, problem); `problem` explains why a missing value couldn't be read
    from the name ("" when nothing is missing or it could). One rule for everything that
    reads names — display, duplicate grouping, skip-existing, and tag repair — so the library
    view never shows one reading while repair writes another.

    When one tag is present and equals one half of the name, the other half is the missing
    value whatever the naming order: the file says so itself. Otherwise the stated order
    decides, so it must be the convention these files were actually saved with.
    """
    artist, title = (artist or "").strip(), (title or "").strip()
    if artist and title:
        return artist, title, ""
    halves = _name_halves(path)
    if halves is None:
        reason = ("the filename isn't 'Artist - Title' or 'Title - Artist' with a single ' - ', "
                  "so which part is the artist can't be told")
        return artist, title, reason
    first, second = halves
    if artist and not title:
        if _normalise(artist) == _normalise(first):
            return artist, second, ""
        if _normalise(artist) == _normalise(second):
            return artist, first, ""
    if title and not artist:
        if _normalise(title) == _normalise(second):
            return first, title, ""
        if _normalise(title) == _normalise(first):
            return second, title, ""
    guess_artist, guess_title = (second, first) if order == "title-artist" else (first, second)
    return artist or guess_artist, title or guess_title, ""


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
    # How this library's untagged filenames are written ("artist-title" / "title-artist").
    order: str = "artist-title"

    def _read(self) -> tuple[str, str, str]:
        return read_identity(Path(self.relative_path), self.artist, self.title, self.order)

    @property
    def raw_artist(self) -> str:
        """The credit string as written, separators intact ("A/B", "A, B", "A feat. B").

        `identity` normalises those separators away, so alias splitting has to start here
        or a multi-artist file can never be matched by its lead artist alone.
        """
        return self._read()[0]

    @property
    def identity(self) -> tuple[str, str] | None:
        artist, title, _problem = self._read()
        key = (_normalise(artist), _normalise(title))
        return key if all(key) else None

    @property
    def repairable(self) -> bool:
        # Repair always shows what it would write and lets you correct it, so a name it can't
        # read is still fixable — you type the values instead.
        return bool(
            ("missing_artist" in self.issues or "missing_title" in self.issues)
            and "unreadable" not in self.issues
        )

    def to_dict(self) -> dict:
        artist, title, _problem = self._read()
        return {
            "path": self.relative_path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "display_title": title or Path(self.relative_path).stem,
            "display_artist": artist or "Unknown artist",
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


def _artist_aliases(artist: str) -> list[str]:
    """Every spelling of a credit list that should count as the same artist.

    A library is rarely written by one tool. spotdl names files with every credited
    artist ("Bieber, Daniel Caesar, Giveon - Peaches") while a lead-artist convention
    writes "Bieber - Peaches", and tag writers use "/" or "feat." instead of commas.
    Indexing only the literal string means the same song is re-downloaded under the
    other spelling, which is exactly the duplicate this index exists to prevent.
    """
    raw = (artist or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"\s*(?:,|/|;|&|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*",
                                         raw, flags=re.IGNORECASE) if p.strip()]
    aliases = {_normalise(raw)}
    if parts:
        aliases.add(_normalise(parts[0]))          # lead artist alone
        aliases.add(_normalise(", ".join(parts)))  # canonical comma form
    return [a for a in aliases if a]


class LibraryIndex:
    def __init__(self, tracks: Iterable[LibraryTrack]):
        self.tracks = list(tracks)
        self._by_identity: dict[tuple[str, str], list[LibraryTrack]] = {}
        for item in self.tracks:
            if not item.identity:
                continue
            artist, title = item.identity
            # Alias from the RAW credit string - item.identity has already collapsed the
            # "/" and "," separators, so splitting it would only ever yield one name.
            for alias in set(_artist_aliases(item.raw_artist)) | {artist}:
                self._by_identity.setdefault((alias, title), []).append(item)
            # A file with no tags at all may have been named by some other tool in the other
            # order. Recognising it either way only prevents a duplicate download; nothing is
            # written from this reading.
            halves = None if (item.artist or item.title) else _name_halves(Path(item.relative_path))
            if halves:
                other_artist, other_title = (halves[1], halves[0]) if item.order != "title-artist" else halves
                for alias in _artist_aliases(other_artist):
                    self._by_identity.setdefault((alias, _normalise(other_title)), []).append(item)

    def find(self, artist: str, title: str, duration: int = 0) -> LibraryTrack | None:
        key_title = _normalise(title)
        matches: list[LibraryTrack] = []
        seen: set[int] = set()
        for alias in _artist_aliases(artist) or [_normalise(artist)]:
            for item in self._by_identity.get((alias, key_title), []):
                if id(item) not in seen:
                    seen.add(id(item))
                    matches.append(item)
        if duration:
            matches = [
                item for item in matches
                if not item.duration or abs(item.duration - duration) <= 5
            ]
        return _best_track(matches) if matches else None


def inspect_audio_file(path: Path, root: Path, order: str = "artist-title") -> LibraryTrack:
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
        order=order if order in NAME_ORDERS else "artist-title",
    )


def _audio_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
    )


def build_library_index(root: Path, order: str = "artist-title") -> LibraryIndex:
    root = root.resolve()
    return LibraryIndex(inspect_audio_file(path, root, order) for path in _audio_paths(root))


def scan_library(root: Path, order: str = "artist-title") -> dict:
    """Inspect every audio file under `root`. Raises LibraryRootError if `root` itself is
    missing or not a folder: that used to scan as an empty — "clean" — library."""
    if not root.exists():
        raise LibraryRootError(f"The library folder doesn't exist: {root}. Check Output folder in Settings.")
    if not root.is_dir():
        raise LibraryRootError(f"The library path is a file, not a folder: {root}.")
    root = root.resolve()
    tracks = [inspect_audio_file(path, root, order) for path in _audio_paths(root)]
    duplicate_groups = []
    duplicate_paths: set[str] = set()
    reclaimable = 0
    for number, group in enumerate(find_duplicate_groups(tracks), start=1):
        recommended = _best_track(group)
        duplicate_paths.update(item.relative_path for item in group)
        reclaimable += sum(item.size for item in group if item != recommended)
        duplicate_groups.append({
            "id": f"duplicate-{number}",
            "artist": recommended.to_dict()["display_artist"],
            "title": recommended.to_dict()["display_title"],
            "recommended_path": recommended.relative_path,
            "reclaimable_size": sum(item.size for item in group if item != recommended),
            "files": [
                {**item.to_dict(), "recommended": item == recommended}
                for item in sorted(group, key=quality_score, reverse=True)
            ],
        })

    return {
        "root": str(root),
        "order": order,
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


def _current_tags(target: Path):
    from mutagen import File

    audio = File(str(target), easy=True)
    if audio is None:
        raise ValueError("audio metadata is unreadable")
    return audio, _first(audio.tags, "artist"), _first(audio.tags, "title")


def _repair_target(root: Path, relative_path: str) -> Path:
    target = _contained_path(root, relative_path)
    if not target.is_file() or target.suffix.casefold() not in AUDIO_EXTENSIONS:
        raise ValueError("audio file was not found")
    return target


def propose_tags(root: Path, relative_path: str, order: str = "artist-title") -> dict:
    """What a repair would write, without writing it — shown to you to confirm or correct."""
    target = _repair_target(root, relative_path)
    _audio, artist, title = _current_tags(target)
    read_artist, read_title, problem = read_identity(target, artist, title, order)
    return {
        "path": target.relative_to(root.resolve()).as_posix(),
        "filename": target.name,
        "order": order,
        "current": {"artist": artist, "title": title},
        # Only the missing fields; existing tags are never changed.
        "proposed": {} if problem else {
            key: value for key, value, have in (("artist", read_artist, artist), ("title", read_title, title))
            if not have and value
        },
        "problem": problem,
    }


def repair_missing_tags(root: Path, relative_path: str, order: str = "artist-title",
                        artist: str | None = None, title: str | None = None) -> dict:
    """Fill only missing artist/title tags; never overwrite one that's there.

    Values you confirmed (`artist`/`title`) are written as given. Without them the filename
    is read in `order`, and a name that can't be read unambiguously is refused rather than
    guessed — see read_identity.
    """
    target = _repair_target(root, relative_path)
    audio, have_artist, have_title = _current_tags(target)
    read_artist, read_title, problem = read_identity(target, have_artist, have_title, order)
    wanted = {"artist": (artist or "").strip(), "title": (title or "").strip()}
    for key, read in (("artist", read_artist), ("title", read_title)):
        if not wanted[key] and not problem:
            wanted[key] = read
    missing = [key for key, have in (("artist", have_artist), ("title", have_title)) if not have]
    if any(not wanted[key] for key in missing):
        raise ValueError(f"Can't fill this automatically: {problem or 'no value given'}. "
                         "Enter the artist and title (or rename it 'Artist - Title').")
    if audio.tags is None:
        audio.add_tags()
    changed: list[str] = []
    for key in missing:
        audio[key] = [wanted[key]]
        changed.append(key)
    if changed:
        audio.save()
    return {"ok": True, "path": target.relative_to(root.resolve()).as_posix(), "changed": changed,
            "artist": have_artist or wanted["artist"], "title": have_title or wanted["title"]}
