"""Parse a pasted block of song names into searchable tracks.

People keep song lists as prose, not as data — an order of service, a note on a phone,
a message from someone else. Lines look like:

    Bridesmaid entrance song - Con Gai Mien Tay by Luong Khanh Vy
    2. Mr Strong Man by George Lam
    https://open.spotify.com/track/...

so a line carries up to three things: a label that is *not* part of the song, the song
itself, and the artist. This module separates them.

Nothing here guesses silently. Every line comes back with what it parsed and why, so the
UI can show the split before anything is downloaded — a mis-split is then one edit to fix
rather than a wrong file on disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# "Title by Artist". Matched on the LAST " by " so real titles containing the word survive:
# "Stand By Me by Ben E. King" splits after "Me", not after "Stand".
_BY_RE = re.compile(r"^(?P<title>.+)\s+by\s+(?P<artist>[^,]+.*)$", re.IGNORECASE)
# Leading list furniture: "1.", "1)", "-", "*", bullets. Stripped before anything else.
_MARKER_RE = re.compile(r"^\s*(?:\d+\s*[.)\]]|[-*•·–—])\s+")
_URL_RE = re.compile(r"^(?:https?://|spotify:)", re.IGNORECASE)
# The separator between a descriptive label and the song ("Bridesmaid entrance song - X").
_LABEL_SEP = " - "


@dataclass
class BulkEntry:
    """One parsed line. `kind` decides what the job layer does with it."""
    raw: str
    kind: str = "search"          # search | url | skipped
    artist: str = ""
    title: str = ""
    url: str = ""
    label: str = ""               # the prefix that was stripped, "" if none
    note: str = ""                # why it was skipped

    @property
    def query(self) -> str:
        """What actually gets searched."""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or self.raw


def _split_by(text: str) -> tuple[str, str]:
    """Split "Title by Artist" into (title, artist), or ("", "") if there's no "by"."""
    m = _BY_RE.match(text)
    if not m:
        return "", ""
    title = m.group("title").strip(" -–—")
    artist = m.group("artist").strip(" -–—")
    if not title or not artist:
        return "", ""
    return title, artist


def _strip_label(text: str) -> tuple[str, str]:
    """Drop a leading "Something song - " label, returning (song, label)."""
    if _LABEL_SEP not in text:
        return text, ""
    label, _, song = text.rpartition(_LABEL_SEP)
    song, label = song.strip(), label.strip()
    if not song:
        return text, ""
    return song, label


def _looks_labelled(lines: list[str]) -> bool:
    """True when the paste is a labelled running order rather than a plain song list.

    The tell is a line carrying BOTH a label separator and an artist ("Bridesmaid entrance
    song - X by Y"): with "by" already naming the artist, a " - " earlier in the line is
    describing the moment, not the artist. Plain "Artist - Title" lists have no "by" and so
    never trip this, which is what keeps label-stripping from eating real artist names.
    """
    both = sum(1 for line in lines if _LABEL_SEP in line and _BY_RE.match(line))
    return both * 2 >= len(lines) if lines else False


def parse_bulk(text: str, strip_labels: bool | None = None) -> list[BulkEntry]:
    """Parse pasted text into one entry per line.

    `strip_labels=None` decides per-paste (see `_looks_labelled`); pass True/False to force
    it, which is what the UI's toggle does.
    """
    raw_lines = [line.strip() for line in (text or "").splitlines()]
    # Comments and blanks never become entries — people annotate these lists.
    candidates = [line for line in raw_lines if line and not line.startswith("#")]
    cleaned = [_MARKER_RE.sub("", line).strip() for line in candidates]
    if strip_labels is None:
        strip_labels = _looks_labelled([c for c in cleaned if not _URL_RE.match(c)])

    entries: list[BulkEntry] = []
    for raw, line in zip(candidates, cleaned):
        if not line:
            continue
        if _URL_RE.match(line):
            entries.append(BulkEntry(raw=raw, kind="url", url=line))
            continue

        title, artist = _split_by(line)
        label = ""
        if title:
            # "X by Y" — anything before a " - " inside X is a label, not part of the song.
            if strip_labels:
                title, label = _strip_label(title)
        elif _LABEL_SEP in line:
            # No "by", so treat " - " as the usual "Artist - Title" convention rather than
            # a label: that is what a normal song list looks like.
            artist, _, title = line.partition(_LABEL_SEP)
            artist, title = artist.strip(), title.strip()
        else:
            # A bare phrase. Search it verbatim rather than inventing an artist/title split.
            title = line

        entries.append(BulkEntry(raw=raw, kind="search", artist=artist,
                                 title=title, label=label))
    return entries


def summarise(entries: list[BulkEntry]) -> dict:
    """Counts for the preview header."""
    return {
        "total": len(entries),
        "searches": sum(1 for e in entries if e.kind == "search"),
        "urls": sum(1 for e in entries if e.kind == "url"),
        "labelled": sum(1 for e in entries if e.label),
    }
