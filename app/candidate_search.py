"""Bounded external-catalog discovery for Spotify track matching."""
from __future__ import annotations

import re
from typing import Any

from . import ytmusic_match
from .matching import Candidate


def _first_artist(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("name"):
                return str(item["name"])
            if isinstance(item, str) and item:
                return item
    if isinstance(value, str):
        return value
    return ""


def candidates_from_ytmusic(items: list[dict]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for item in items:
        video_id = str(item.get("videoId") or "")
        title = str(item.get("title") or "")
        artist = _first_artist(item.get("artists"))
        if video_id and title and artist:
            candidates.append(Candidate(
                source="youtube_music",
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                artist=artist,
                duration=int(item.get("duration_seconds") or 0),
                official=True,
            ))
    return candidates


def candidates_from_ytdlp(items: list[dict], source: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for item in items:
        title = str(item.get("title") or "")
        artist = str(item.get("uploader") or item.get("channel") or item.get("artist") or "")
        url = str(item.get("webpage_url") or "")
        if not url and source == "youtube" and item.get("id"):
            url = f"https://www.youtube.com/watch?v={item['id']}"
        if title and artist and url:
            candidates.append(Candidate(
                source=source,
                url=url,
                title=title,
                artist=artist,
                duration=int(item.get("duration") or 0),
            ))
    return candidates


def search_ytmusic(artist: str, title: str) -> list[Candidate]:
    return candidates_from_ytmusic(ytmusic_match.search_songs(artist, title))


def search_ytdlp(query: str, prefix: str, source: str) -> list[Candidate]:
    try:
        import yt_dlp
        options = {"quiet": True, "skip_download": True, "extract_flat": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(f"{prefix}5:{query}", download=False) or {}
    except Exception:
        return []
    return candidates_from_ytdlp(info.get("entries") or [], source)


# Which searches each user-facing service runs. "auto" casts the widest net and lets the
# scorer decide; the others exist so a deliberate choice isn't quietly overridden by a
# better-scoring hit somewhere else. Spotify is absent on purpose: it has no usable public
# search (anonymous tokens are refused with 429 QUOTA_EXCEEDED) and serves no audio, so it
# can only ever be a *link* source, never a search one.
SERVICE_SOURCES: dict[str, tuple[str, ...]] = {
    "auto": ("youtube_music", "youtube", "soundcloud"),
    "youtube": ("youtube_music", "youtube"),
    "soundcloud": ("soundcloud",),
}


def search_all(artist: str, title: str, service: str = "auto") -> list[Candidate]:
    """Search every source the chosen service covers, newest-first by relevance."""
    wanted = SERVICE_SOURCES.get(service) or SERVICE_SOURCES["auto"]
    query = f"{artist} - {title}".strip(" -")
    found: list[Candidate] = []
    if "youtube_music" in wanted:
        found += search_ytmusic(artist, title)
    if "youtube" in wanted:
        found += search_ytdlp(query, "ytsearch", "youtube")
    if "soundcloud" in wanted:
        found += search_ytdlp(query, "scsearch", "soundcloud")
    return found


_GROUP_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


def _drop_unasked_groups(title: str, wanted: set[str]) -> str:
    """Remove bracketed extras the phrase never mentioned.

    The resolved title becomes the track's name — and its filename — so it should name the
    song, not one release of it: "On The Floor (Ven a Bailar) (Bonus Track)" -> "On The Floor".
    A group the phrase did mention stays ("Cinderella (feat. Ty Dolla $ign)").
    """
    from .matching import _core_tokens

    def keep(match: re.Match) -> str:
        return match.group(0) if _core_tokens(match.group(0)) & wanted else ""

    trimmed = " ".join(_GROUP_RE.sub(keep, title).split())
    return trimmed or title


def resolve_phrase(phrase: str) -> tuple[list[str], str] | None:
    """Work out which song a bare phrase ("drake one dance") names, as (artists, title).

    A phrase with no "Artist - Title" shape can't be matched on artist and title directly,
    so YouTube Music's song search names it first. Its answer is only taken when the phrase
    genuinely describes that song: the words that aren't the artist's name must mostly be in
    the title, and at least one must be in the song name itself. That is what stops "drake"
    resolving to whatever Drake song ranks first, and a vague phrase from being confidently
    named as some unrelated track.

    Durations are deliberately NOT taken from here — the top result can itself be an
    alternate edit, which is exactly what duration consensus exists to see past.
    """
    from .matching import _core_tokens, _strip_decorations, version_markers

    wanted = _core_tokens(phrase)
    if not wanted:
        return None
    asked_versions = version_markers(phrase)
    acceptable: list[tuple[int, int, list[str], str]] = []
    for position, item in enumerate(ytmusic_match.search_songs("", phrase, limit=5)[:5]):
        title = str(item.get("title") or "")
        names = [str(a.get("name")) for a in (item.get("artists") or [])
                 if isinstance(a, dict) and a.get("name")]
        if not title or not names:
            continue
        # Adopting "On The Floor (Radio Edit)" as the song's name would make the radio edit
        # the thing being matched. Only take a version the phrase itself asked for.
        if version_markers(title) - asked_versions:
            continue
        artist_words = _core_tokens(" ".join(names))
        title = _drop_unasked_groups(title, wanted)
        song_words = _core_tokens(_strip_decorations(title))
        # Credits count as naming the track ("Cinderella feat. Ty Dolla $ign") but can't be
        # the only thing that matches: "No Guidance (feat. Drake)" is not a song called Drake.
        described = artist_words | _core_tokens(title)
        rest = wanted - artist_words
        if not rest or not (rest & song_words):
            continue            # only an artist was named, or only a credit matched
        if len(rest & described) * 2 < len(rest):
            continue
        if len(wanted & described) * 3 < len(wanted) * 2:
            continue
        # Several results can fit. The one naming the song with the fewest words nobody asked
        # for wins, rather than simply the highest-ranked — a higher rank is often an alternate
        # release with a longer name.
        acceptable.append((len(song_words - wanted), position, names, title))
    if not acceptable:
        return None
    _, _, names, title = min(acceptable, key=lambda a: (a[0], a[1]))
    return names, title
