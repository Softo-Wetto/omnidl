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
from dataclasses import dataclass

# "Title by Artist". Matched on the LAST " by " so real titles containing the word survive:
# "Stand By Me by Ben E. King" splits after "Me", not after "Stand".
_BY_RE = re.compile(r"^(?P<title>.+)\s+by\s+(?P<artist>[^,]+.*)$", re.IGNORECASE)
# Leading list furniture: "1.", "1)", "-", "*", bullets. Stripped before anything else.
_MARKER_RE = re.compile(r"^\s*(?:\d+\s*[.)\]]|[-*•·–—])\s+")
_URL_RE = re.compile(r"^(?:https?://|spotify:)", re.IGNORECASE)
# Phones, Word and Notes silently turn " - " into an en or em dash. A spaced dash of any kind
# means the same thing, so they're folded to a hyphen before any splitting happens —
# otherwise "Katy Perry – Teenage Dream" isn't split at all.
_SPACED_DASH_RE = re.compile(r"\s+[‐-―−]\s+")
_SEP = " - "
_QUOTES = "\"'“”‘’«»"

# A " - " segment that belongs to the TITLE rather than being a label in front of it:
# "Black Betty - Single Edit", "Mr Brightside - Live", "Bohemian Rhapsody - Remastered 2011".
# Stripping one of these as a label would search for a song called "Single Edit", so the
# check deliberately errs towards "this is part of the title": leaving a stray label in a
# search costs a little relevance, while eating a real title part fetches the wrong song.
_TITLE_SUFFIX_RE = re.compile(
    r"\b(?:remaster(?:ed)?|version|edit|mix|remix|live|acoustic|instrumental|demo|mono|"
    r"stereo|deluxe|extended|radio|single|bonus|reprise|unplugged|sessions?|cover|"
    r"orchestral|lo\s*fi|sped|slowed|nightcore|karaoke|soundtrack|ost|theme)\b"
    r"|^(?:from|feat\.?|ft\.?|featuring|with|prod\.?)\b"
    r"|^\(?\d{4}\)?$",
    re.IGNORECASE,
)


@dataclass
class BulkEntry:
    """One parsed line. `kind` decides what the job layer does with it."""
    raw: str
    kind: str = "search"          # search | url
    artist: str = ""
    title: str = ""
    url: str = ""
    label: str = ""               # the prefix that was stripped, "" if none

    @property
    def query(self) -> str:
        """What actually gets searched."""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or self.raw


def _unquote(text: str) -> str:
    """Drop a quote pair wrapping the whole value ('"Shanghai Bund"'), but never a lone
    apostrophe that is part of the words ("Nothin'")."""
    text = text.strip()
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        return text[1:-1].strip()
    return text


def _split_by(text: str) -> tuple[str, str]:
    """Split "Title by Artist" into (title, artist), or ("", "") if there's no "by"."""
    m = _BY_RE.match(text)
    if not m:
        return "", ""
    title = m.group("title").strip(" -")
    artist = m.group("artist").strip(" -")
    if not title or not artist:
        return "", ""
    return title, artist


def _is_title_suffix(segment: str) -> bool:
    return bool(_TITLE_SUFFIX_RE.search(segment.strip()))


def _strip_label(text: str, force: bool) -> tuple[str, str]:
    """Separate a leading label from the song, returning (song, label).

    Walks the " - " segments from the end, keeping any that are title suffixes attached to
    the song: "First dance - Black Betty - Single Edit" -> ("Black Betty - Single Edit",
    "First dance"). With `force`, the last segment is taken as the song regardless.
    """
    segments = [s.strip() for s in text.split(_SEP)]
    if len(segments) < 2:
        return text, ""
    start = len(segments) - 1
    if not force:
        while start > 0 and _is_title_suffix(segments[start]):
            start -= 1
    if start == 0:
        return text, ""
    return _SEP.join(segments[start:]), _SEP.join(segments[:start])


def parse_bulk(text: str, strip_labels: bool | None = None) -> list[BulkEntry]:
    """Parse pasted text into one entry per line.

    Each line is judged on its own, so a line always parses the same way no matter what
    else is pasted with it. `strip_labels` overrides that judgement: True always removes a
    leading label from "X - Y by Z" lines, False never does, None decides per line.
    """
    entries: list[BulkEntry] = []
    for raw in (line.strip() for line in (text or "").splitlines()):
        # Comments and blanks never become entries — people annotate these lists.
        if not raw or raw.startswith("#"):
            continue
        line = _SPACED_DASH_RE.sub(_SEP, _MARKER_RE.sub("", raw).strip())
        if not line:
            continue
        if _URL_RE.match(line):
            entries.append(BulkEntry(raw=raw, kind="url", url=line))
            continue

        title, artist = _split_by(line)
        label = ""
        if title:
            # "X by Y" names the artist explicitly, so a " - " before it can only be a label
            # or part of the title — never the artist.
            if strip_labels is not False:
                title, label = _strip_label(title, force=bool(strip_labels))
        elif _SEP in line:
            # No "by", so " - " is the usual "Artist - Title" convention.
            artist, _, title = line.partition(_SEP)
        else:
            # A bare phrase. Search it verbatim rather than inventing an artist/title split.
            title = line

        entries.append(BulkEntry(raw=raw, kind="search", artist=_unquote(artist),
                                 title=_unquote(title), label=label))
    return entries


def summarise(entries: list[BulkEntry]) -> dict:
    """Counts for the preview header."""
    return {
        "total": len(entries),
        "searches": sum(1 for e in entries if e.kind == "search"),
        "urls": sum(1 for e in entries if e.kind == "url"),
        "labelled": sum(1 for e in entries if e.label),
    }
