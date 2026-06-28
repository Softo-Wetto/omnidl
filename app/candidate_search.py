"""Bounded external-catalog discovery for Spotify track matching."""
from __future__ import annotations

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


def search_all(artist: str, title: str) -> list[Candidate]:
    query = f"{artist} - {title}".strip(" -")
    return [
        *search_ytmusic(artist, title),
        *search_ytdlp(query, "ytsearch", "youtube"),
        *search_ytdlp(query, "scsearch", "soundcloud"),
    ]