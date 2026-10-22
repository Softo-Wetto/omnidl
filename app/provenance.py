"""Which recording each saved file holds, remembered across restarts.

A filename can't tell two recordings apart: "A/B - Song" and "A_B - Song" both become
"A_B - Song", two videos can share a title, and long names are cut short. So every file OmniDL
saves — or recognises as one it already has — is recorded here with what it holds: a matched
song's artists and title, or a link's source (site + media ID). An existing file is only reused
for a request when this record, or the file's own tags, say it is the same recording; a file
whose recording can't be told is kept, and the request gets a name of its own.

Kept in provenance.json beside config.json (OMNIDL_PROVENANCE overrides the path).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

from .settings import PROJECT_ROOT


def _default_path() -> Path:
    return Path(os.environ.get("OMNIDL_PROVENANCE") or (PROJECT_ROOT / "provenance.json"))


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


# ---- comparing recordings ---------------------------------------------------------------

_CREDIT_SPLIT = re.compile(r"\s*(?:,|/|;|&|\b(?:feat|ft)\b\.?|\bwith\b)\s*", re.IGNORECASE)


def _fold(text) -> str:
    """Case, width and spacing ignored — but not punctuation: "Song?" and "Song*" are both
    saved as "Song_", and they are not the same song."""
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def credits(artists) -> list[str]:
    """The artists in a credit, however a tool wrote it ("A, B", "A/B", "A feat. B")."""
    return [part for part in (_fold(p) for p in _CREDIT_SPLIT.split(str(artists or ""))) if part]


def same_song(artists_a, title_a, artists_b, title_b) -> bool:
    """Do two artist/title readings name the same song?

    The titles must match and so must the artists — as a set, so tag formats don't matter — or
    one side may name only the lead artist (M4A files and "primary artist" naming carry just
    that). "A/B" (two artists) and "A_B" (one) are different, though both are saved as "A_B".
    """
    title = _fold(title_a)
    if not title or title != _fold(title_b):
        return False
    a, b = credits(artists_a), credits(artists_b)
    if not a or not b:
        return False
    return set(a) == set(b) or (len(a) == 1 and a[0] == b[0]) or (len(b) == 1 and b[0] == a[0])


# ---- what a file's tags say --------------------------------------------------------------

def tag_names(path: Path) -> tuple[str, str]:
    """(artist, title) from a file's tags, "" where missing or unreadable."""
    try:
        from mutagen import File
        audio = File(str(path), easy=True)
        tags = getattr(audio, "tags", None) if audio is not None else None
        if not tags:
            return "", ""

        def joined(key: str) -> str:
            value = tags.get(key) or []
            values = value if isinstance(value, (list, tuple)) else [value]
            return ", ".join(str(v).strip() for v in values if str(v).strip())

        return joined("artist"), joined("title")
    except Exception:  # noqa: BLE001 — unreadable tags are just "unknown"
        return "", ""


_URL_RE = re.compile(r"^(?:https?|file)://\S+$", re.IGNORECASE)


def tag_urls(path: Path) -> set[str]:
    """Page addresses embedded in a file: yt-dlp and scdl both write the source page into its
    tags (purl / comment / WOAF). How a link download from before this record existed is still
    recognised as the same recording rather than duplicated."""
    found: set[str] = set()
    try:
        from mutagen import File
        audio = File(str(path))
        tags = getattr(audio, "tags", None) if audio is not None else None
        if not tags:
            return found
        for key, value in tags.items():
            name = str(key).lower()
            if not any(part in name for part in ("purl", "comment", "comm", "woaf", "wwwaudiofile", "\xa9cmt")):
                continue
            for item in (value if isinstance(value, (list, tuple)) else [value]):
                for text in (getattr(item, "url", None), *(getattr(item, "text", None) or [item])):
                    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text or "")
                    if _URL_RE.match(text.strip()):
                        found.add(text.strip())
    except Exception:  # noqa: BLE001
        pass
    return found


# ---- the record --------------------------------------------------------------------------

class Provenance:
    """path -> what OmniDL saved there. Records for files that no longer exist are ignored, and
    dropped by prune() (every few minutes) once the file is really gone."""

    _WRITE_EVERY = 2.0          # seconds; a 200-song playlist shouldn't rewrite it 200 times

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._files: dict[str, dict] | None = None
        self._dirty = False
        self._written = 0.0
        self._warned = False
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path or _default_path()

    def _load(self) -> dict[str, dict]:
        with self._lock:
            if self._files is None:
                files: dict[str, dict] = {}
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    for rec in (data.get("files") or {}).values() if isinstance(data, dict) else ():
                        if isinstance(rec, dict) and isinstance(rec.get("path"), str) and rec["path"]:
                            files[_key(rec["path"])] = rec
                except (OSError, ValueError, AttributeError):
                    pass
                self._files = files
            return self._files

    @staticmethod
    def _gone(rec: dict) -> bool:
        """The file was deleted — not merely on a drive that isn't plugged in right now (whose
        records would be needed again when it is)."""
        path = rec["path"]
        return not os.path.isfile(path) and os.path.isdir(Path(path).anchor or os.sep)

    def get(self, path) -> dict | None:
        rec = self._load().get(_key(path))
        return rec if rec and os.path.isfile(rec["path"]) else None

    def record(self, path, **fields) -> None:
        rec = {k: v for k, v in fields.items() if v not in (None, "")}
        rec.update(path=os.path.abspath(str(path)), at=round(time.time()))
        with self._lock:
            self._load()[_key(path)] = rec
            self._dirty = True
        self.flush()

    def record_track(self, path, track, job: str = "") -> None:
        self.record(path, kind="track", artists=getattr(track, "all_artists", "") or track.artist,
                    title=track.title, job=job)

    def links_in(self, folder, as_: str) -> dict[str, str]:
        """source -> path of the link downloads saved under `folder` as `as_` ("audio:mp3")."""
        root = _key(folder).rstrip("\\/") + os.sep
        found: dict[str, str] = {}
        for key, rec in list(self._load().items()):
            if (rec.get("kind") == "link" and rec.get("as") == as_ and rec.get("source")
                    and key.startswith(root) and os.path.isfile(rec["path"])):
                found[rec["source"]] = rec["path"]
        return found

    def prune(self) -> None:
        with self._lock:
            files = self._load()
            gone = [k for k, rec in files.items() if self._gone(rec)]
            for key in gone:
                del files[key]
            self._dirty = self._dirty or bool(gone)
        self.flush()

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty or (not force and time.monotonic() - self._written < self._WRITE_EVERY):
                return
            files = dict(self._load())
            self._dirty = False
            self._written = time.monotonic()
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".provenance-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"version": 1, "files": files}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            with self._lock:
                self._dirty = True
            if not self._warned:
                self._warned = True
                print(f"[OmniDL] Couldn't save {self.path.name}: {exc}")
