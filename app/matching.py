"""Strict, deterministic matching of a requested track to external candidates.

Every way a song enters OmniDL — a Spotify playlist, a pasted list, a single typed search —
goes through `choose()`. That is deliberate: when single downloads and playlist downloads
ranked candidates differently, the same song came back as a different version depending
only on how it was asked for.
"""
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
    # Version markers the candidate carries that the request doesn't ("sped up", "live").
    version_mismatch: tuple[str, ...] = ()
    # Uncapped score. Two candidates can both cap at 100 while one is 1s off and the other
    # 8s off; ranking on the capped value would treat them as tied.
    raw_score: int = 0
    # Position within its own source's results — that service's relevance judgement.
    rank: int = 0


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


# ---- titles ------------------------------------------------------------------------------
# Credits and decorations say who is on a track and what kind of upload it is — not which
# song it is. Spotify lists a featured artist as an *artist* ("Payphone", Maroon 5 + Wiz
# Khalifa) while YouTube puts them in the *title* ("Payphone (feat. Wiz Khalifa)"). Comparing
# those literally scored the exact track 0.48 on title and verified a different, shorter
# version with a bare title instead.
_CREDIT_GROUP_RE = re.compile(
    r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with|prod\.?(?:\s+by)?)\s[^)\]]*[)\]]", re.IGNORECASE)
_CREDIT_TAIL_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
_DECORATION_RE = re.compile(
    r"[(\[]\s*(?:official\s*)?(?:music\s*video|lyric\s*video|lyrics?|video|audio|"
    r"visuali[sz]er|mv|hd|hq|4k|explicit|clean|official)\s*[)\]]",
    re.IGNORECASE)
# Upload titles glue the artist on with a dash ("Drake - One Dance") or a series name on with
# a pipe ("Higher | Open Mic").
_SEGMENT_SPLIT_RE = re.compile(r"\s+[-‐-―−:]\s+")
_PIPE_TAIL_RE = re.compile(r"\s+\|\s+.*$")
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|;|\+|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\band\b)\s*", re.IGNORECASE)
_UPLOADER_NOISE_RE = re.compile(r"\s*-\s*topic$|vevo$|\s+official$|\s+music$", re.IGNORECASE)


def artist_aliases(artist: str) -> list[str]:
    """The full credit and the lead artist alone: "Katy Perry, Snoop Dogg" -> both."""
    raw = (artist or "").strip()
    if not raw:
        return []
    parts = [p for p in _ARTIST_SPLIT_RE.split(raw) if p.strip()]
    aliases = [raw]
    if parts and parts[0].strip().casefold() != raw.casefold():
        aliases.append(parts[0].strip())
    return aliases


def _strip_decorations(title: str) -> str:
    text = _PIPE_TAIL_RE.sub("", title or "")
    text = _CREDIT_GROUP_RE.sub(" ", text)
    text = _DECORATION_RE.sub(" ", text)
    text = _CREDIT_TAIL_RE.sub("", text)
    return " ".join(text.split()).strip(" -–—\"'“”")


def _artist_segment_score(segment: str, aliases: list[str]) -> float:
    seg = _normalise(_strip_decorations(segment))
    best = 0.0
    for alias in aliases:
        name = _normalise(alias)
        if not name or not seg:
            continue
        if seg == name or seg.startswith(name + " "):
            return 1.0
        best = max(best, SequenceMatcher(None, seg, name).ratio())
    return best


def clean_title(title: str, artist: str = "") -> tuple[str, float]:
    """Reduce an upload title to the song name, and report how well it named the artist.

    "Drake - One Dance (Lyrics) ft. Wizkid & Kyla" -> ("One Dance", 1.0). The second value
    matters because fan and lyric channels upload under their own name: the uploader field
    says "Billion Stars" while the title says "Drake", and only reading the uploader scored
    every correct copy of the song as a wrong artist.
    """
    aliases = artist_aliases(artist)
    text = _PIPE_TAIL_RE.sub("", title or "")
    in_title = 0.0
    segments = _SEGMENT_SPLIT_RE.split(text)
    if aliases and len(segments) > 1:
        scores = [_artist_segment_score(s, aliases) for s in segments]
        best = max(range(len(segments)), key=lambda i: scores[i])
        if scores[best] >= 0.85:
            in_title = scores[best]
            segments = segments[:best] + segments[best + 1:]
            text = " - ".join(segments)
    # 'Tems "Higher" (Live Performance)': artist first, the song in quotes.
    if aliases and not in_title:
        quoted = re.match(r'^(?P<who>[^"“]+?)\s+["“](?P<song>[^"”]+)["”](?P<rest>.*)$', text)
        if quoted and _artist_segment_score(quoted.group("who"), aliases) >= 0.85:
            in_title = _artist_segment_score(quoted.group("who"), aliases)
            text = quoted.group("song") + quoted.group("rest")
    return _strip_decorations(text), in_title


# ---- versions ------------------------------------------------------------------------------
# Each marker names a *different recording or treatment* of the song. They are compared
# between the request and the candidate, so a Spotify track that is itself "Funk Wav Remix"
# or "Live" still matches its own version — only markers the candidate ADDS disqualify it.
_VERSION_PATTERNS = {
    "sped up": r"\b(?:sped|speed)\s*up\b|\bspedup\b",
    "slowed": r"\bslowed\b",
    "reverb": r"\breverb\b",
    "nightcore": r"\bnightcore\b",
    "daycore": r"\bdaycore\b",
    "8d": r"\b8d\b",
    "bass boosted": r"\bbass\s*boost(?:ed)?\b",
    "live": r"\blive\b",
    "acoustic": r"\bacoustic\b",
    "stripped": r"\bstripped\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b",
    "acapella": r"\ba\s*capp?ella\b|\bacapp?ella\b",
    # Not "(Cover Art)" / "Album Cover" — those are uploads of the real track with its artwork.
    "cover": r"(?<!album )\bcover\b(?!\s+art)",
    "remix": r"\bremix(?:ed)?\b|\bbootleg\b|\bflip\b|\b(?:radio|club|extended|dub|vip)\s+mix\b",
    "extended": r"\bextended\b",
    "edit": r"\bedit\b",
    "mashup": r"\bmash\s*up\b",
    "loop": r"\b\d+\s*hours?\b|\bloop(?:ed)?\b",
    "chopped": r"\bchopped\b|\bscrewed\b",
    "lofi": r"\blo\s*fi\b",
    "tiktok": r"\btik\s*tok\b",
    "demo": r"\bdemo\b",
    "orchestral": r"\borchestral\b",
    "piano": r"\bpiano\s+(?:version|cover)\b",
}
_VERSION_RE = {name: re.compile(pattern) for name, pattern in _VERSION_PATTERNS.items()}


def version_markers(title: str) -> frozenset[str]:
    text = _normalise(title)
    return frozenset(name for name, rx in _VERSION_RE.items() if rx.search(text))


# Words that describe a *version* or credit rather than the song itself. They're stripped
# before comparing the distinctive words of two titles, so this gate only answers "is it
# this song at all" — whether it's the right *version* is version_markers' job.
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


# ---- scoring -------------------------------------------------------------------------------
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
    return score


def _reason(title_similarity: float, artist_similarity: float,
            duration_difference: int | None, score: int, markers: tuple[str, ...]) -> str:
    if markers:
        return f"different version ({', '.join(markers)})"
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


def decide_match(artist: str, title: str, duration: int, candidate: Candidate, *,
                 prefer_official: bool = True, rank: int = 0) -> MatchDecision:
    """Score one external candidate and decide whether it is safe to download."""
    cleaned, artist_in_title = clean_title(candidate.title, artist)
    title_similarity = max(_similarity(title, candidate.title),
                           _similarity(_strip_decorations(title), cleaned))
    uploader = _UPLOADER_NOISE_RE.sub("", candidate.artist or "")
    artist_similarity = max([_similarity(alias, uploader) for alias in artist_aliases(artist)]
                            + [artist_in_title])
    duration_difference = abs(duration - candidate.duration) if duration and candidate.duration else None
    markers = tuple(sorted(version_markers(candidate.title) - version_markers(title)))
    raw = _score(title_similarity, artist_similarity, duration_difference,
                 candidate.official and prefer_official)
    score = min(100, raw)
    reason = _reason(title_similarity, artist_similarity, duration_difference, score, markers)
    return MatchDecision(
        candidate=candidate,
        title_similarity=title_similarity,
        artist_similarity=artist_similarity,
        duration_difference=duration_difference,
        score=score,
        accepted=reason == "verified match",
        reason=reason,
        version_mismatch=markers,
        raw_score=raw,
        rank=rank,
    )


# ---- previews ------------------------------------------------------------------------------
# SoundCloud (and some YouTube uploads) serve a ~30s preview for licensed tracks. It matches
# the artist and title perfectly, so it outscores the genuine recording and downloads a file
# that looks like a success and is useless as a song.
_PREVIEW_MAX_SECONDS = 70
_FULL_LENGTH_MIN_SECONDS = 90


def is_preview_clip(decisions, expected_duration: int = 0):
    """Return a predicate marking candidates that are short preview clips.

    Only applies when the real duration is unknown. When a duration IS known the ±20s
    window already rejects previews, so this stays out of the way.

    A short candidate is only judged a preview when a full-length alternative actually
    exists; otherwise the song genuinely is short (an interlude, a skit) and refusing it
    would mean downloading nothing at all.
    """
    if expected_duration:
        return lambda decision: False
    durations = [d.candidate.duration for d in decisions if d.candidate.duration]
    if not any(d >= _FULL_LENGTH_MIN_SECONDS for d in durations):
        return lambda decision: False
    return lambda decision: 0 < decision.candidate.duration <= _PREVIEW_MAX_SECONDS


# ---- duration consensus ----------------------------------------------------------------
_CONSENSUS_WINDOW = 5
_SOURCE_WEIGHT = {"youtube_music": 1.25, "youtube": 1.0, "soundcloud": 0.75}
_RANK_DECAY = 0.35


def _vote(decision: MatchDecision) -> float:
    """How much one upload's length counts. A service's top results speak louder than its
    sixth: counted equally, three deep radio-edit uploads (237s) outvoted "A Sky Full of
    Stars" being YouTube Music's #1 and YouTube's #2 result at the album length (268s)."""
    return _SOURCE_WEIGHT.get(decision.candidate.source, 0.5) / (1 + _RANK_DECAY * decision.rank)


def consensus_duration(decisions: list[MatchDecision]) -> int:
    """The length most independent copies of the song agree on, or 0 if they don't agree.

    A typed song name carries no duration, which left bulk and single searches unable to
    tell the studio recording from an alternate edit with an identical title. But the copies
    themselves vote: "One Dance" appears at 175-177s four times and at 227s once. Only
    candidates that are plausibly the requested song count, and at least two must agree —
    one upload agreeing with itself proves nothing.
    """
    voters = [d for d in decisions
              if d.candidate.duration > _PREVIEW_MAX_SECONDS and not d.version_mismatch
              and d.artist_similarity >= 0.8 and d.title_similarity >= 0.8]
    best: tuple[float, int, int, int] | None = None     # (support, has_ytm, -rank, duration)
    for pivot in voters:
        members = [v for v in voters
                   if abs(v.candidate.duration - pivot.candidate.duration) <= _CONSENSUS_WINDOW]
        if len(members) < 2:
            continue
        support = sum(_vote(m) for m in members)
        ytm = [m for m in members if m.candidate.source == "youtube_music"]
        anchor = min(ytm, key=lambda m: m.rank) if ytm else min(members, key=lambda m: m.rank)
        key = (support, 1 if ytm else 0, -anchor.rank, anchor.candidate.duration)
        if best is None or key > best:
            best = key
    return best[3] if best else 0


# ---- choosing ------------------------------------------------------------------------------
_MAX_DURATION_DIFFERENCE = 20
_ELIGIBLE_ARTIST_SIMILARITY = 0.6


@dataclass
class Choice:
    decisions: list[MatchDecision]      # every candidate, best first
    eligible: list[MatchDecision]       # safe to download, best first
    expected_duration: int              # Spotify's length, the consensus length, or 0
    anchored: bool = False              # expected_duration came from consensus


def choose(artist: str, title: str, duration: int, candidates: list[Candidate], *,
           prefer_ytmusic: bool = True, use_duration: bool = True) -> Choice:
    """Rank candidates for one requested track and decide which are safe to download.

    `prefer_ytmusic` and `use_duration` are the two matching switches in Settings.
    """
    per_source: dict[str, int] = {}
    ranks = []
    for candidate in candidates:
        ranks.append(per_source.get(candidate.source, 0))
        per_source[candidate.source] = ranks[-1] + 1

    def decide(expected: int) -> list[MatchDecision]:
        return [decide_match(artist, title, expected, c, prefer_official=prefer_ytmusic, rank=r)
                for c, r in zip(candidates, ranks)]

    expected = duration if use_duration else 0
    decisions = decide(expected)
    anchored = False
    if use_duration and not expected:
        anchor = consensus_duration(decisions)
        if anchor:
            expected, anchored = anchor, True
            decisions = decide(expected)

    preview = is_preview_clip(decisions, expected)
    source_rank = {"youtube_music": 3 if prefer_ytmusic else 2, "youtube": 2, "soundcloud": 1}

    def within_duration(d: MatchDecision) -> bool:
        return d.duration_difference is None or d.duration_difference <= _MAX_DURATION_DIFFERENCE

    decisions.sort(
        key=lambda d: (
            not preview(d),
            not d.version_mismatch,
            within_duration(d),
            d.accepted,
            d.raw_score,
            source_rank.get(d.candidate.source, 0),
            -(d.duration_difference if d.duration_difference is not None else 9999),
            d.artist_similarity, d.title_similarity,
            -d.rank,
        ),
        reverse=True,
    )
    eligible = [
        d for d in decisions
        if not preview(d)
        and not d.version_mismatch
        and within_duration(d)
        and d.artist_similarity >= _ELIGIBLE_ARTIST_SIMILARITY
        and (d.accepted or title_is_plausible(title, clean_title(d.candidate.title, artist)[0]))
    ]
    return Choice(decisions, eligible, expected, anchored)


def same_recording(fallback: MatchDecision, reference: MatchDecision) -> bool:
    """May `fallback` stand in for `reference` when the reference won't download?

    This is what stops a busy playlist from saving a different version. Under load the best
    candidate can fail a download that would succeed a minute later; falling through to
    "the next plausible candidate" then quietly swapped in a different edit, and because it
    downloaded, nothing ever retried the right one. A stand-in must be as trustworthy as the
    choice it replaces (verified if that was verified) AND the same length as it. Verified
    alone isn't enough: the verification window is ±20s, wide enough to hold a different edit.
    """
    if reference.accepted and not fallback.accepted:
        return False
    a, b = fallback.candidate.duration, reference.candidate.duration
    return bool(a and b and abs(a - b) <= 10)
