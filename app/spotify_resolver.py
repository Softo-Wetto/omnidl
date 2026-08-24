"""Resolve Spotify track/album/playlist URLs to a track list WITHOUT the Web API.

Spotify's Web API now returns 403 for any app whose owner isn't Premium (this includes
your own free developer app), which breaks spotDL entirely. Instead we read the public
**embed** page's `__NEXT_DATA__` JSON, which needs no auth, no cookie, and no Premium.
We then hand each "artist - title" to yt-dlp, which is where the audio comes from anyway.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from urllib.error import HTTPError

_ID_RE = re.compile(r"(track|album|playlist|artist)[/:]([A-Za-z0-9]+)")
_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Track:
    artist: str
    title: str
    duration: int = 0       # seconds (0 = unknown)
    album: str = ""
    track_number: int = 0
    cover_url: str = ""
    date: str = ""          # ISO release date, "" if unknown
    # Every credited artist, in Spotify's order. `artist` stays the primary one because
    # YouTube searches hit far more reliably with "Bieber - Peaches" than with the full
    # "Bieber, Daniel Caesar, Giveon - Peaches", but filenames and tags should credit all.
    artists: list[str] = field(default_factory=list)

    @property
    def all_artists(self) -> str:
        return ", ".join(self.artists) if self.artists else self.artist

    @property
    def query(self) -> str:
        """Search text — deliberately the primary artist only (best match rate)."""
        return f"{self.artist} - {self.title}".strip(" -")

    def filename_for(self, order: str = "artist-title", artists: str = "all") -> str:
        """Safe base filename (no extension) in the configured naming convention.

        Exists so a library built under one convention isn't duplicated by downloads
        under another: "Bieber - Peaches" and "Bieber, Daniel Caesar, Giveon - Peaches"
        are the same song but different files.
        """
        who = self.artist if artists == "primary" else self.all_artists
        if not who:
            base = self.title or "track"
        elif order == "title-artist":
            base = f"{self.title} - {who}"
        else:
            base = f"{who} - {self.title}"
        base = _ILLEGAL.sub("_", base.strip(" -"))
        return base[:180].strip().rstrip(".")

    @property
    def filename(self) -> str:
        return self.filename_for()


@dataclass
class Resolved:
    kind: str
    name: str
    tracks: list["Track"]
    # True when Spotify's embed cap (100) was hit and the API couldn't supply the rest,
    # so `tracks` is the first 100 of an unknown larger total.
    truncated: bool = False


def parse_spotify_url(text: str):
    m = _ID_RE.search(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def is_spotify(text: str) -> bool:
    t = text.strip().lower()
    return "open.spotify.com" in t or t.startswith("spotify:")


def _clean(s: str | None) -> str:
    if not s:
        return ""
    return (
        s.replace("\xa0", " ")
        .replace("ï¿½", " ")
        .replace("â€™", "'")
        .strip()
        .strip(",")
        .strip()
    )


def _primary_artist(subtitle: str | None) -> str:
    subtitle = _clean(subtitle)
    if not subtitle:
        return ""
    return subtitle.split(",")[0].strip()


def _subtitle_artists(subtitle: str | None) -> list[str]:
    """Split an embed "subtitle" ("Bieber, Daniel Caesar, GIVEON") into its artists."""
    return [p.strip() for p in _clean(subtitle).split(",") if p.strip()]


def _visual_cover(entity: dict) -> str:
    """Largest cover URL from an embed entity's visualIdentity, or ""."""
    images = _dig(entity, ["visualIdentity", "image"]) or []
    if not images:
        return ""
    return max(images, key=lambda i: i.get("maxWidth") or 0).get("url", "")


def _duration_s(value) -> int:
    """Spotify gives duration in milliseconds; return whole seconds."""
    try:
        return int(round(int(value) / 1000))
    except (TypeError, ValueError):
        return 0


def _dig(d, path):
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


# ---- embed page parsing (always works; capped at 100 for big playlists) ----

def _http(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _fetch_embed(kind: str, sid: str, timeout: int) -> str:
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    return _http(url, timeout=timeout).decode("utf-8", "replace")


def _parse_embed(html: str, kind: str):
    m = _NEXT_RE.search(html)
    if not m:
        raise ValueError("Could not read Spotify embed data (page format may have changed)")
    entity = _dig(json.loads(m.group(1)),
                  ["props", "pageProps", "state", "data", "entity"]) or {}
    name = _clean(entity.get("name") or entity.get("title") or kind)
    tracks: list[Track] = []
    for item in entity.get("trackList") or []:
        title = _clean(item.get("title"))
        names = _subtitle_artists(item.get("subtitle"))
        if title:
            tracks.append(Track(
                artist=names[0] if names else _clean(item.get("subtitle")),
                title=title, duration=_duration_s(item.get("duration")),
                artists=names, cover_url=_visual_cover(item),
            ))
    if not tracks and kind == "track":
        title = _clean(entity.get("title") or entity.get("name"))
        names = [_clean(a.get("name")) for a in (entity.get("artists") or []) if a.get("name")]
        if not names:
            names = _subtitle_artists(entity.get("subtitle"))
        if title:
            tracks.append(Track(
                artist=names[0] if names else "", title=title,
                duration=_duration_s(entity.get("duration")), artists=names,
                cover_url=_visual_cover(entity),
                date=_clean(_dig(entity, ["releaseDate", "isoString"]))[:10],
            ))
    return name, tracks


# ---- full fetch via the web player's GraphQL API (the only working >100 route) ----
#
# api.spotify.com refuses anonymous tokens outright (429 QUOTA_EXCEEDED on every endpoint),
# and the official credentials route needs the app owner to hold Premium. The web player
# itself doesn't use either: it queries api-partner.spotify.com/pathfinder, which *does*
# accept the anonymous embed token. That's what lifts the 100-track embed ceiling.
_PARTNER = "https://api-partner.spotify.com/pathfinder/v1/query"
# Persisted-query hash for fetchPlaylist. Spotify rotates it with web player releases, so it
# is re-scraped from the live bundles at runtime; this is only the fallback.
_FETCH_PLAYLIST_HASH = "86dde7b9d9356e2369414647cf6950cfed96e778e129cfdfc99aea6c1613b3b0"
_HASH_CACHE: dict[str, str] = {}


def _playlist_query_hash(timeout: int = 20) -> str:
    """Current fetchPlaylist hash, scraped from the web player and cached for the process."""
    if "h" in _HASH_CACHE:
        return _HASH_CACHE["h"]
    hash_value = _FETCH_PLAYLIST_HASH
    try:
        home = _http("https://open.spotify.com/", timeout=timeout).decode("utf-8", "replace")
        for src in dict.fromkeys(re.findall(r'src="(https://open[^"]*spotifycdn\.com/cdn/build/[^"]+\.js)"', home)):
            js = _http(src, timeout=timeout).decode("utf-8", "replace")
            m = (re.search(r'"fetchPlaylist"[^}]{0,200}?"([0-9a-f]{64})"', js)
                 or re.search(r'"([0-9a-f]{64})"[^}]{0,120}?"fetchPlaylist"', js))
            if m:
                hash_value = m.group(1)
                break
    except Exception:
        pass                       # keep the fallback; a stale hash just means we degrade
    _HASH_CACHE["h"] = hash_value
    return hash_value


def _partner_page(pid: str, token: str, offset: int, limit: int, timeout: int):
    variables = json.dumps({
        "uri": f"spotify:playlist:{pid}", "offset": offset, "limit": limit,
        # required Boolean! — omitting it is a hard validation error
        "enableWatchFeedEntrypoint": False,
    })
    extensions = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": _playlist_query_hash(timeout)}})
    url = (f"{_PARTNER}?operationName=fetchPlaylist"
           f"&variables={urllib.parse.quote(variables)}&extensions={urllib.parse.quote(extensions)}")
    raw = _http(url, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout)
    return json.loads(raw)


def _track_from_partner(item: dict, ) -> "Track | None":
    data = (item.get("itemV2") or {}).get("data") or {}
    name = _clean(data.get("name"))
    if not name:
        return None                # local files / unavailable items carry no playable name
    artists = _dig(data, ["artists", "items"]) or []
    names = [n for n in (_clean(_dig(a, ["profile", "name"])) for a in artists) if n]
    artist = names[0] if names else ""
    album = _dig(data, ["albumOfTrack", "name"]) or ""
    sources = _dig(data, ["albumOfTrack", "coverArt", "sources"]) or []
    cover = max(sources, key=lambda s: s.get("width") or 0).get("url", "") if sources else ""
    return Track(
        artist=artist, title=name, artists=names,
        duration=_duration_s(_dig(data, ["trackDuration", "totalMilliseconds"])),
        album=_clean(album), track_number=data.get("trackNumber") or 0, cover_url=cover,
    )


def _partner_playlist(pid: str, token: str, timeout: int) -> tuple[list["Track"], int]:
    """Every track in the playlist, plus Spotify's own total (which may exceed what's
    playable — unavailable/local items are counted but have no track data)."""
    tracks: list[Track] = []
    offset, total = 0, 0
    while True:
        payload = _partner_page(pid, token, offset, 100, timeout)
        if payload.get("errors"):
            raise ValueError(str(payload["errors"])[:200])
        content = _dig(payload, ["data", "playlistV2", "content"]) or {}
        items = content.get("items") or []
        total = content.get("totalCount") or total
        for item in items:
            track = _track_from_partner(item)
            if track:
                tracks.append(track)
        offset += len(items)
        if not items or offset >= total or len(tracks) > 10000:
            break
    return tracks, total


# ---- legacy path: Spotify's own anonymous token (now usually 429s, kept as a fallback) ----

def _token(html: str) -> str | None:
    m = re.search(r'"accessToken":"([^"]+)"', html)
    return m.group(1) if m else None


def _api(url: str, token: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    # Spotify's anonymous web-player token is periodically rate-limited. Its
    # Retry-After header tells us how long to wait; without this, a large
    # playlist silently falls back to the 100-track embed preview.
    for attempt in range(4):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            # Respect Retry-After when given, else back off exponentially. The anonymous
            # quota is per-IP, so a fresh token alone doesn't help — waiting does.
            raw = exc.headers.get("Retry-After") if exc.headers else None
            if raw is not None:
                try:                       # the server's own figure is authoritative
                    delay = min(float(raw), 30.0)
                except (TypeError, ValueError):
                    delay = 2.0 * (2 ** attempt)
            else:                          # no guidance -> exponential backoff
                delay = 2.0 * (2 ** attempt)
            time.sleep(delay)


def _cover(images) -> str:
    return images[0]["url"] if images else ""


def _track_from_api(t: dict, album_name: str = "", cover: str = "") -> Track | None:
    if not t or not t.get("name"):
        return None
    names = [_clean(a["name"]) for a in t.get("artists", []) if a.get("name")]
    album = t.get("album") or {}
    return Track(
        artist=names[0] if names else "",
        artists=names,
        title=_clean(t["name"]),
        duration=_duration_s(t.get("duration_ms")),
        album=_clean(album.get("name") or album_name),
        track_number=t.get("track_number") or 0,
        cover_url=_cover(album.get("images")) or cover,
    )


def _api_playlist(sid: str, token: str, timeout: int) -> list[Track]:
    tracks, offset = [], 0
    fields = "total,items(track(name,artists(name),duration_ms,track_number,album(name,images)))"
    while True:
        d = _api(f"https://api.spotify.com/v1/playlists/{sid}/tracks?limit=100&offset={offset}&fields={fields}", token, timeout)
        items = d.get("items", [])
        for it in items:
            tr = _track_from_api(it.get("track") or {})
            if tr:
                tracks.append(tr)
        offset += len(items)
        if not items or offset >= d.get("total", 0) or len(tracks) > 5000:
            break
    return tracks


def _api_album(sid: str, token: str, timeout: int):
    meta = _api(f"https://api.spotify.com/v1/albums/{sid}", token, timeout)
    name = _clean(meta.get("name"))
    cover = _cover(meta.get("images"))
    tracks = []
    for it in _dig(meta, ["tracks", "items"]) or []:
        tr = _track_from_api(it, name, cover)
        if tr:
            tr.album = name
            tracks.append(tr)
    offset = len(tracks)
    while offset < (_dig(meta, ["tracks", "total"]) or 0):
        d = _api(f"https://api.spotify.com/v1/albums/{sid}/tracks?limit=50&offset={offset}", token, timeout)
        items = d.get("items", [])
        for it in items:
            tr = _track_from_api(it, name, cover)
            if tr:
                tr.album, tr.cover_url = name, cover
                tracks.append(tr)
        offset += len(items)
        if not items:
            break
    return name, tracks


def resolve(text: str, timeout: int = 20) -> Resolved:
    parsed = parse_spotify_url(text)
    if not parsed:
        raise ValueError("Not a recognizable Spotify track/album/playlist URL")
    kind, sid = parsed

    html = _fetch_embed(kind, sid, timeout)
    name, embed_tracks = _parse_embed(html, kind)

    # Prefer Spotify's own anon token for the FULL list + rich metadata (album/cover/#).
    token = _token(html)
    if token:
        try:
            if kind == "playlist":
                # The web player's own GraphQL endpoint is the only route that still serves
                # anonymous callers, and it pages past the embed's 100-track ceiling.
                try:
                    full, spotify_total = _partner_playlist(sid, token, timeout)
                    if full:
                        # totalCount includes unavailable/local items that carry no track
                        # data, so only flag truncation if we clearly fell short.
                        return Resolved(kind, name, full,
                                        truncated=len(full) < spotify_total - 5)
                except Exception:
                    pass                    # fall through to the legacy API, then the embed
                full = _api_playlist(sid, token, timeout)
            elif kind == "album":
                name, full = _api_album(sid, token, timeout)
            elif kind == "track":
                full = [t for t in [_track_from_api(_api(f"https://api.spotify.com/v1/tracks/{sid}", token, timeout))] if t]
            else:
                full = []
            if full:
                return Resolved(kind, name, full)
        except Exception:
            # The full list needs Spotify's API, which is frequently rate-limited (429) for
            # anonymous callers. Rather than abandoning a big playlist entirely, fall back to
            # the 100 tracks the embed does give us and flag it loudly — 100 of N clearly
            # labelled beats downloading nothing. Short playlists are complete in the embed.
            if kind == "playlist" and len(embed_tracks) >= 100:
                return Resolved(kind, name, embed_tracks, truncated=True)

    if not embed_tracks:
        raise ValueError("No tracks found for this Spotify link")
    return Resolved(kind, name, embed_tracks)
