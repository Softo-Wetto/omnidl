"""Strict, deterministic matching of Spotify metadata to external candidates."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class Candidate:
    source: str
    url: str
    title: str
    artist: str
    duration: int = 0
    official: bool = False


@dataclass(frozen=True)
class MatchDecision:
    candidate: Candidate
    title_similarity: float
    artist_similarity: float
    duration_difference: int | None
    score: int
    accepted: bool
    reason: str


def _normalise(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return " ".join(text.split())


def _similarity(left: str, right: str) -> float:
    left, right = _normalise(left), _normalise(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


# Words that describe a *version* or credit rather than the song itself. They're stripped
# before comparing the distinctive words of two titles (so "X (slowed)" still matches "X").
_TITLE_NOISE = {
    "slowed", "sped", "up", "down", "reverb", "remix", "remixed", "flip", "edit",
    "version", "feat", "ft", "featuring", "prod", "by", "official", "audio", "video",
    "lyric", "lyrics", "remaster", "remastered", "mix", "pluggnb", "plugg", "jerk",
    "the", "a", "an", "and", "x", "vs", "with",
}


def _core_tokens(title: str) -> set[str]:
    """Distinctive words of a title (version/credit words and stop-words removed)."""
    return {tok for tok in _normalise(title).split()
            if len(tok) >= 2 and tok not in _TITLE_NOISE}


def title_is_plausible(spotify_title: str, candidate_title: str) -> bool:
    """True only if the candidate plausibly IS the requested song.

    A candidate must contain at least half of the requested title's distinctive words.
    This stops OmniDL from grabbing a same-artist, duration-matched but completely
    different track (e.g. "Boogie (Slowed)" for "next door - jedag jedug - Slowed")
    when the real one can't be downloaded — better nothing than the wrong song.
    """
    core = _core_tokens(spotify_title)
    if not core:
        return True  # nothing distinctive to check against; don't over-reject
    needed = max(1, (len(core) + 1) // 2)
    return len(core & _core_tokens(candidate_title)) >= needed


def _duration_points(difference: int | None) -> int:
    if difference is None:
        return 0
    if difference <= 5:
        return 20
    if difference <= 10:
        return 15
    if difference <= 20:
        return 8
    return 0


def _score(title_similarity: float, artist_similarity: float,
           duration_difference: int | None, official: bool) -> int:
    # Artist match matters most: a right-title/wrong-artist hit (cover, remix, karaoke,
    # same-name song) is the classic wrong pick, so weight artist above title.
    score = round(artist_similarity * 50 + title_similarity * 30 + _duration_points(duration_difference))
    if official:
        score += 5
    return min(100, score)


def _reason(title_similarity: float, artist_similarity: float,
            duration_difference: int | None, score: int) -> str:
    if artist_similarity < 0.8:
        return "artist similarity below 80%"
    if title_similarity < 0.8:
        return "title similarity below 80%"
    if duration_difference is not None and duration_difference > 20:
        return "duration differs by more than 20 seconds"
    threshold = 85 if duration_difference is not None else 90
    if score < threshold:
        return f"match score below {threshold}"
    return "verified match"


def decide_match(artist: str, title: str, duration: int, candidate: Candidate) -> MatchDecision:
    """Score one external candidate and decide whether it is safe to download."""
    title_similarity = _similarity(title, candidate.title)
    artist_similarity = _similarity(artist, candidate.artist)
    duration_difference = abs(duration - candidate.duration) if duration and candidate.duration else None
    score = _score(title_similarity, artist_similarity, duration_difference, candidate.official)
    reason = _reason(title_similarity, artist_similarity, duration_difference, score)
    return MatchDecision(
        candidate=candidate,
        title_similarity=title_similarity,
        artist_similarity=artist_similarity,
        duration_difference=duration_difference,
        score=score,
        accepted=reason == "verified match",
        reason=reason,
    )
