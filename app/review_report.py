"""Standalone HTML review reports for Spotify matching decisions."""
from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from .matching import MatchDecision
from .spotify_resolver import Track

_EMDASH = "&#8212;"
_MIDDOT = "&#183;"


@dataclass(frozen=True)
class TrackOutcome:
    track: Track
    status: str
    reason: str
    candidates: list[MatchDecision]


_SERVICE_NAMES = [
    "Spotify", "Apple Music/iTunes", "Instagram & Facebook", "TikTok / ByteDance",
    "YouTube Music", "Amazon", "Pandora", "Deezer", "Tidal", "iHeartRadio",
    "Qobuz", "Saavn", "Boomplay", "Anghami", "NetEase", "Tencent",
    "Claro M" + chr(0x00FA) + "sica", "Joox", "Kuack Media", "Adaptr", "Flo",
    "MediaNet", "Snapchat",
]


def _query(track: Track) -> str:
    return " ".join(part for part in (track.artist, track.title, track.album) if part)


def _search_url(service: str, query: str) -> str:
    encoded = quote_plus(query)
    if service == "Spotify":
        return f"https://open.spotify.com/search/{quote_plus(query).replace('+', '%20')}"
    if service == "Apple Music/iTunes":
        return f"https://music.apple.com/us/search?term={encoded}"
    if service == "YouTube Music":
        return f"https://music.youtube.com/search?q={encoded}"
    if service == "Amazon":
        return f"https://www.amazon.com/s?k={encoded}"
    if service == "SoundCloud":
        return f"https://soundcloud.com/search/sounds?q={encoded}"
    if service == "Bandcamp":
        return f"https://bandcamp.com/search?q={encoded}"
    return f"https://www.google.com/search?q={quote_plus(f'{query} {service}') }"


def _link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def _status_class(status: str) -> str:
    return status.replace("_", "-")


def _status_label(status: str) -> str:
    return status.replace("_", " ")


def _candidate_rows(candidates: list[MatchDecision]) -> str:
    if not candidates:
        return '<tr><td colspan="8" class="empty-candidates">No external candidates found.</td></tr>'
    rows = []
    for decision in candidates:
        candidate = decision.candidate
        duration = f"{candidate.duration}s" if candidate.duration else "Unknown"
        state = "verified" if decision.accepted else "candidate"
        rows.append(
            f'<tr class="{state}">'
            f"<td><span class=\"source source-{html.escape(candidate.source)}\">{html.escape(candidate.source)}</span></td>"
            f"<td>{_link(candidate.title, candidate.url)}</td>"
            f"<td>{html.escape(candidate.artist)}</td>"
            f"<td>{duration}</td><td><span class=\"score\">{decision.score}</span></td>"
            f"<td>{decision.artist_similarity:.0%}</td>"
            f"<td>{decision.title_similarity:.0%}</td>"
            f"<td>{html.escape(decision.reason)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _service_links(track: Track) -> str:
    query = _query(track)
    names = [*_SERVICE_NAMES, "SoundCloud", "Bandcamp"]
    return "".join(
        _link(f"Search {name}", _search_url(name, query))
        for name in names
    )


def _outcome_section(outcome: TrackOutcome) -> str:
    track = outcome.track
    status_class = _status_class(outcome.status)
    metadata = f" {_EMDASH} ".join(html.escape(part) for part in (track.artist, track.title) if part)
    album = html.escape(track.album) if track.album else "Spotify playlist item"
    return (
        f'<section class="track-card status-{status_class}">'
        '<div class="track-head">'
        '<div><p class="eyebrow">Spotify track</p>'
        f"<h2>{metadata}</h2><p class=\"album\">{album}</p></div>"
        f'<span class="status status-{status_class}">{html.escape(_status_label(outcome.status))}</span>'
        "</div>"
        f'<p class="reason"><strong>Why it needs review</strong>{html.escape(outcome.reason)}</p>'
        '<div class="table-wrap"><table><thead><tr><th>Source</th><th>Candidate</th><th>Artist</th><th>Duration</th>'
        '<th>Score</th><th>Artist match</th><th>Title match</th><th>Decision</th></tr></thead>'
        f"<tbody>{_candidate_rows(outcome.candidates)}</tbody></table></div>"
        '<details class="store-links"><summary>Search other music services</summary>'
        f'<div class="link-grid">{_service_links(track)}</div></details>'
        "</section>"
    )


def _summary_cards(outcomes: list[TrackOutcome]) -> str:
    counts = Counter(outcome.status for outcome in outcomes)
    reviewed = len(outcomes)
    closest = counts["downloaded_for_review"]
    unavailable = counts["no_candidate"] + counts["download_failed"] + counts["rejected"]
    cards = [
        ("Tracks reviewed", reviewed),
        ("Closest-match downloads", closest),
        ("Still unavailable", unavailable),
    ]
    return "".join(
        f'<article class="summary-card"><span>{html.escape(label)}</span><strong>{count}</strong></article>'
        for label, count in cards
    )


def write_review_report(output_dir: Path, playlist_name: str, outcomes: list[TrackOutcome]) -> Path:
    """Write a standalone review report without downloading any new media."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = output_dir / f"omnidl-review-{stamp}.html"
    body = "".join(_outcome_section(outcome) for outcome in outcomes)
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>OmniDL review {_EMDASH} {html.escape(playlist_name)}</title><style>"
        ":root{color-scheme:dark;--bg:#0c1017;--panel:#151c27;--line:#293447;--text:#f3f7fb;--muted:#9aa8bc;--accent:#7c9cff;--good:#62d39b;--warn:#f4bd62;--bad:#fb7e88}"
        "*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#26355f 0,transparent 34rem),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}"
        "main{max-width:1240px;margin:0 auto;padding:34px 22px 56px}.report-header{padding:30px;border:1px solid var(--line);border-radius:22px;background:linear-gradient(135deg,rgba(124,156,255,.2),rgba(21,28,39,.88) 55%);box-shadow:0 22px 70px rgba(0,0,0,.25)}"
        ".eyebrow{margin:0 0 6px;color:#b8c6ff;font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.report-header h1{margin:0;font-size:clamp(28px,5vw,48px);line-height:1.08}.lede{max-width:760px;color:var(--muted);margin:14px 0 0}.summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:25px}.summary-card{padding:16px;border:1px solid rgba(151,171,226,.23);border-radius:14px;background:rgba(8,12,20,.35)}.summary-card span{display:block;color:var(--muted);font-size:12px}.summary-card strong{display:block;margin-top:3px;font-size:28px}"
        ".track-card{margin-top:18px;padding:22px;border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:16px;background:var(--panel);box-shadow:0 12px 35px rgba(0,0,0,.12)}.track-card.status-no-candidate,.track-card.status-download-failed{border-left-color:var(--bad)}.track-card.status-downloaded-for-review{border-left-color:var(--warn)}.track-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.track-head h2{margin:0;font-size:21px;line-height:1.25}.album{margin:4px 0 0;color:var(--muted);font-size:13px}.status{flex:0 0 auto;padding:5px 9px;border-radius:999px;background:rgba(244,189,98,.14);color:var(--warn);font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase}.status-no-candidate,.status-download-failed{background:rgba(251,126,136,.14);color:var(--bad)}.reason{margin:18px 0 14px;padding:10px 12px;border-radius:10px;background:#0e141e;color:var(--muted)}.reason strong{display:block;margin-bottom:2px;color:var(--text);font-size:12px}"
        ".table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:830px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:#202a39;color:#c6d2e7;font-size:11px;letter-spacing:.06em;text-transform:uppercase}td{color:#dbe4f3;font-size:13px}tbody tr:last-child td{border-bottom:0}tbody tr.verified{background:rgba(98,211,155,.08)}.source,.score{display:inline-flex;padding:3px 7px;border-radius:7px;background:#263349;color:#c8d8f7;font-size:11px;font-weight:700}.score{background:#303a52;color:#fff}.empty-candidates{padding:18px;color:var(--muted);text-align:center}a{color:#9eb7ff;text-decoration:none}a:hover{text-decoration:underline}"
        ".store-links{margin-top:14px;border:1px solid var(--line);border-radius:10px;background:#111824}.store-links summary{cursor:pointer;padding:11px 13px;color:#cbd7ec;font-weight:700}.link-grid{display:flex;flex-wrap:wrap;gap:8px;padding:0 13px 13px}.link-grid a{padding:6px 9px;border:1px solid #34445d;border-radius:8px;background:#182231;font-size:12px}.link-grid a:hover{background:#22314a;text-decoration:none}@media(max-width:650px){main{padding:18px 12px 36px}.report-header{padding:22px}.summary-grid{grid-template-columns:1fr}.track-card{padding:16px}.track-head{display:block}.status{display:inline-flex;margin-top:12px}}"
        "</style></head><body><main>"
        '<header class="report-header"><p class="eyebrow">OmniDL match review</p>'
        f"<h1>{html.escape(playlist_name)}</h1>"
        '<p class="lede">Closest available candidates may have been downloaded. Review the reasons and alternatives below; service links open searches and do not claim that those storefronts were queried automatically.</p>'
        f'<div class="summary-grid">{_summary_cards(outcomes)}</div></header>'
        f"{body}</main></body></html>"
    )
    path.write_text(document, encoding="utf-8")
    return path