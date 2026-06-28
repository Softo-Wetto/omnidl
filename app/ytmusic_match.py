"""Match a Spotify track to its YouTube Music official-audio counterpart."""
from __future__ import annotations

import threading

_client = None
_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from ytmusicapi import YTMusic
                _client = YTMusic()
    return _client


def search_songs(artist: str, title: str, limit: int = 5) -> list[dict]:
    """Return raw YouTube Music song results without choosing a candidate."""
    query = f"{artist} {title}".strip()
    if not query:
        return []
    try:
        return _get_client().search(query, filter="songs", limit=limit) or []
    except Exception:
        return []


def best_match(artist: str, title: str, duration: int = 0, limit: int = 5) -> str | None:
    """Return the closest YouTube Music videoId for compatibility callers."""
    results = search_songs(artist, title, limit)
    best_id: str | None = None
    best_score: int | None = None
    for result in results:
        video_id = result.get("videoId")
        if not video_id:
            continue
        seconds = result.get("duration_seconds") or 0
        score = abs(seconds - duration) if duration and seconds else 0
        if duration and seconds and score <= 2:
            return video_id
        if best_score is None or score < best_score:
            best_score, best_id = score, video_id
    return best_id