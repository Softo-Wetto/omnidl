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
import urllib.request
from dataclasses import dataclass
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

    @property
    def query(self) -> str:
        return f"{self.artist} - {self.title}".strip(" -")

    @property
    def filename(self) -> str:
        """Safe base filename derived from Spotify metadata (no extension)."""
        base = self.query or self.title or "track"
        base = _ILLEGAL.sub("_", base)
        return base[:180].strip().rstrip(".")


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

def _fetch_embed(kind: str, sid: str, timeout: int) -> str:
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


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
        artist = _primary_artist(item.get("subtitle")) or _clean(item.get("subtitle"))
        if title:
            tracks.append(Track(artist, title, _duration_s(item.get("duration"))))
    if not tracks and kind == "track":
        title = _clean(entity.get("title") or entity.get("name"))
        artists = entity.get("artists") or []
        artist = _clean(artists[0].get("name")) if artists else _primary_artist(entity.get("subtitle"))
        if title:
            tracks.append(Track(artist, title, _duration_s(entity.get("duration"))))
    return name, tracks


# ---- full fetch via Spotify's own anonymous token (handles >100 + rich tags) ----

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
    artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
    album = t.get("album") or {}
    return Track(
        artist=_clean(artists.split(",")[0]) or _clean(artists),
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
