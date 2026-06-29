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
_LOGO = "&#9680;"       # half-filled circle brand mark
_SUN = "&#9728;"        # light-theme toggle glyph
_MOON = "&#9790;"       # dark-theme toggle glyph
_COPY = "&#10697;"      # copy glyph
_ARROW_UR = "&#8599;"   # up-right arrow
_CHECK = "&#10003;"     # check mark
_SEARCH = "&#128269;"   # magnifier


@dataclass(frozen=True)
class TrackOutcome:
    track: Track
    status: str
    reason: str
    candidates: list[MatchDecision]
    selected: MatchDecision | None = None
    saved_as: str | None = None
    failed_attempts: tuple[MatchDecision, ...] = ()


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


def _tier(value: int) -> str:
    """Map a 0-100 quality value to a colour tier."""
    if value >= 85:
        return "good"
    if value >= 70:
        return "mid"
    return "low"


def _score_cell(score: int) -> str:
    return f'<td><span class="score {_tier(score)}">{score}</span></td>'


def _pct_cell(value: float) -> str:
    pct = round(value * 100)
    return (
        f'<td><span class="pct {_tier(pct)}">'
        f'<i style="width:{pct}%"></i><b>{pct}%</b></span></td>'
    )


def _candidate_rows(candidates: list[MatchDecision], selected: MatchDecision | None,
                    failed_attempts: tuple[MatchDecision, ...] = ()) -> str:
    if not candidates:
        return '<tr><td colspan="8" class="empty-candidates">No external candidates found.</td></tr>'
    failed_ids = {id(decision) for decision in failed_attempts}
    rows = []
    for decision in candidates:
        candidate = decision.candidate
        duration = f"{candidate.duration}s" if candidate.duration else "Unknown"
        is_selected = selected is not None and decision is selected
        is_failed = id(decision) in failed_ids
        # Row tint: what was saved wins, then a failed attempt, then a clean verified row.
        if is_selected:
            state = "downloaded"
        elif is_failed:
            state = "failed"
        elif decision.accepted:
            state = "verified"
        else:
            state = "candidate"
        badges = ""
        if decision.accepted:
            badges += '<span class="badge badge-verified">Verified</span> '
        if is_selected:
            badges += '<span class="badge badge-downloaded">Downloaded</span> '
        elif is_failed:
            badges += '<span class="badge badge-failed">Tried &#183; failed</span> '
        rows.append(
            f'<tr class="{state}">'
            f"<td><span class=\"source source-{html.escape(candidate.source)}\">{html.escape(candidate.source)}</span></td>"
            f"<td>{_link(candidate.title, candidate.url)}</td>"
            f"<td>{html.escape(candidate.artist)}</td>"
            f"<td>{duration}</td>"
            f"{_score_cell(decision.score)}"
            f"{_pct_cell(decision.artist_similarity)}"
            f"{_pct_cell(decision.title_similarity)}"
            f'<td class="decision">{badges}{html.escape(decision.reason)}</td>'
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
    search_attr = html.escape(
        " ".join(part for part in (track.artist, track.title, track.album) if part).lower(),
        quote=True,
    )
    copy_attr = html.escape(track.query, quote=True)
    notes = ""
    # A higher-ranked match was tried but its audio could not be fetched, so OmniDL fell
    # back to a lower candidate. Spell that out so the saved row never looks arbitrary.
    if outcome.failed_attempts and outcome.selected is not None:
        verified_failed = sum(1 for d in outcome.failed_attempts if d.accepted)
        failed_total = len(outcome.failed_attempts)
        if verified_failed:
            notes += (
                '<p class="note-fallback"><strong>Heads up</strong>'
                f"A higher-ranked verified match couldn't be downloaded (source unavailable), "
                f"so OmniDL fell back to the next playable candidate below.</p>"
            )
        elif failed_total:
            label = "candidate" if failed_total == 1 else "candidates"
            notes += (
                '<p class="note-fallback"><strong>Heads up</strong>'
                f"{failed_total} higher-ranked {label} couldn't be downloaded; "
                f"saved the next playable one below.</p>"
            )
    if outcome.saved_as:
        notes += (
            f'<p class="saved">{_CHECK} Closest match downloaded as '
            f"<code>{html.escape(outcome.saved_as)}</code></p>"
        )
    return (
        f'<section class="track-card status-{status_class}" data-status="{status_class}" data-search="{search_attr}">'
        '<div class="track-head">'
        '<div class="track-id"><p class="eyebrow">Spotify track</p>'
        f'<h2>{metadata}<button class="copy" type="button" data-copy="{copy_attr}" '
        f'title="Copy artist - title">{_COPY}</button></h2>'
        f'<p class="album">{album}</p></div>'
        f'<span class="status status-{status_class}">{html.escape(_status_label(outcome.status))}</span>'
        "</div>"
        f'<p class="reason"><strong>Why it needs review</strong>{html.escape(outcome.reason)}</p>'
        f"{notes}"
        '<div class="table-wrap"><table><thead><tr><th>Source</th><th>Candidate</th><th>Artist</th><th>Duration</th>'
        '<th>Score</th><th>Artist match</th><th>Title match</th><th>Decision</th></tr></thead>'
        f"<tbody>{_candidate_rows(outcome.candidates, outcome.selected, outcome.failed_attempts)}</tbody></table></div>"
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
        ("Tracks reviewed", reviewed, ""),
        ("Closest-match downloads", closest, "warn"),
        ("Still unavailable", unavailable, "bad"),
    ]
    return "".join(
        f'<article class="summary-card{(" " + cls) if cls else ""}">'
        f"<span>{html.escape(label)}</span><strong>{count}</strong></article>"
        for label, count, cls in cards
    )


def _toolbar(outcomes: list[TrackOutcome]) -> str:
    counts = Counter(_status_class(outcome.status) for outcome in outcomes)
    total = len(outcomes)
    chip_defs = [
        ("all", "All", total),
        ("downloaded-for-review", "Closest match", counts.get("downloaded-for-review", 0)),
        ("no-candidate", "No source", counts.get("no-candidate", 0)),
        ("download-failed", "Download failed", counts.get("download-failed", 0)),
        ("rejected", "Rejected", counts.get("rejected", 0)),
    ]
    chips = []
    for key, label, count in chip_defs:
        if key != "all" and count == 0:
            continue
        active = " active" if key == "all" else ""
        chips.append(
            f'<button type="button" class="chip{active}" data-f="{key}">'
            f"{html.escape(label)} <b>{count}</b></button>"
        )
    return (
        '<div class="toolbar"><div class="search"><span class="i">' + _SEARCH + "</span>"
        '<input id="q" type="search" placeholder="Filter by artist, title or album..." autocomplete="off"></div>'
        f'<div class="filters">{"".join(chips)}</div>'
        f'<span class="shown">Showing <b id="shown">{total}</b> of {total}</span></div>'
    )


_PREPAINT = (
    "<script>(function(){try{var t=localStorage.getItem('omnidl-review-theme');"
    "if(!t)t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';"
    "if(t==='light')document.documentElement.classList.add('light');}catch(e){}})();</script>"
)

_STYLE = (
    ":root{color-scheme:dark;--bg:#0c1017;--panel:#151c27;--panel-2:#0e141e;--line:#293447;"
    "--text:#f3f7fb;--muted:#9aa8bc;--accent:#7c9cff;--good:#62d39b;--warn:#f4bd62;--bad:#fb7e88;"
    "--head-bg:#202a39;--chip-bg:#1b2433;--chip-text:#c6d2e7;--track-bg:#0d1420;--link:#9eb7ff;"
    "--shadow:0 12px 35px rgba(0,0,0,.18)}"
    "html.light{color-scheme:light;--bg:#eef2f9;--panel:#ffffff;--panel-2:#f2f5fb;--line:#dde4ef;"
    "--text:#0e1726;--muted:#5a6a85;--accent:#4663d8;--good:#1f9d63;--warn:#b9810f;--bad:#d8434f;"
    "--head-bg:#eef2f8;--chip-bg:#eef2f8;--chip-text:#33425c;--track-bg:#eef2f8;--link:#3a55c7;"
    "--shadow:0 12px 30px rgba(40,60,110,.10)}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:radial-gradient(circle at 15% 0,color-mix(in srgb,var(--accent) 26%,transparent) 0,transparent 34rem),var(--bg);"
    "color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}"
    "a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}"
    "main{max-width:1240px;margin:0 auto;padding:28px 22px 56px}"
    ".report-header{position:relative;padding:30px;border:1px solid var(--line);border-radius:22px;"
    "background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 22%,var(--panel)),var(--panel) 60%);box-shadow:var(--shadow)}"
    ".brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:16px;letter-spacing:-.01em}"
    ".brand .logo{color:var(--accent);font-size:20px}.brand .accent{color:var(--accent)}"
    ".head-actions{position:absolute;top:24px;right:24px;display:flex;gap:8px}"
    ".icon-btn,.dash-link{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 12px;border:1px solid var(--line);"
    "border-radius:10px;background:color-mix(in srgb,var(--panel) 80%,transparent);color:var(--text);font:inherit;font-size:13px;cursor:pointer;transition:.15s}"
    ".icon-btn{width:36px;padding:0;justify-content:center;font-size:16px}"
    ".icon-btn:hover,.dash-link:hover{border-color:var(--accent);text-decoration:none}"
    ".eyebrow{margin:18px 0 6px;color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}"
    ".report-header h1{margin:0;font-size:clamp(26px,5vw,46px);line-height:1.08}"
    ".meta{margin:10px 0 0;color:var(--muted);font-size:13px}"
    ".lede{max-width:760px;color:var(--muted);margin:12px 0 0}"
    ".summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:24px}"
    ".summary-card{padding:16px;border:1px solid var(--line);border-radius:14px;background:color-mix(in srgb,var(--accent) 5%,var(--panel))}"
    ".summary-card span{display:block;color:var(--muted);font-size:12px}"
    ".summary-card strong{display:block;margin-top:3px;font-size:28px}"
    ".summary-card.warn strong{color:var(--warn)}.summary-card.bad strong{color:var(--bad)}"
    ".toolbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:18px 0 4px;padding:12px;"
    "border:1px solid var(--line);border-radius:14px;background:color-mix(in srgb,var(--panel) 86%,transparent);backdrop-filter:blur(10px)}"
    ".search{position:relative;flex:1 1 240px;min-width:200px}"
    ".search .i{position:absolute;left:11px;top:50%;transform:translateY(-50%);opacity:.6;font-size:13px}"
    ".search input{width:100%;height:38px;padding:0 12px 0 32px;border:1px solid var(--line);border-radius:10px;background:var(--track-bg);color:var(--text);font:inherit;font-size:14px}"
    ".search input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}"
    ".filters{display:flex;flex-wrap:wrap;gap:7px}"
    ".chip{display:inline-flex;align-items:center;gap:6px;padding:7px 11px;border:1px solid var(--line);border-radius:999px;background:var(--chip-bg);"
    "color:var(--chip-text);font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:.15s}"
    ".chip b{opacity:.7;font-weight:800}.chip:hover{border-color:var(--accent)}"
    ".chip.active{background:var(--accent);border-color:var(--accent);color:#fff}.chip.active b{opacity:.85;color:#fff}"
    ".shown{color:var(--muted);font-size:12px;margin-left:auto}"
    ".noresults{margin:26px 0;text-align:center;color:var(--muted)}"
    ".track-card{margin-top:16px;padding:22px;border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:16px;background:var(--panel);box-shadow:var(--shadow)}"
    ".track-card.status-no-candidate,.track-card.status-download-failed,.track-card.status-rejected{border-left-color:var(--bad)}"
    ".track-card.status-downloaded-for-review{border-left-color:var(--warn)}"
    ".track-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}"
    ".track-head h2{margin:0;font-size:21px;line-height:1.25;display:flex;align-items:center;gap:8px;flex-wrap:wrap}"
    ".copy{border:1px solid var(--line);background:var(--track-bg);color:var(--muted);width:26px;height:26px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1;transition:.15s}"
    ".copy:hover{border-color:var(--accent);color:var(--accent)}.copy.ok{color:var(--good);border-color:var(--good)}"
    ".album{margin:4px 0 0;color:var(--muted);font-size:13px}"
    ".status{flex:0 0 auto;padding:5px 9px;border-radius:999px;background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn);"
    "font-size:11px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;white-space:nowrap}"
    ".status-no-candidate,.status-download-failed,.status-rejected{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}"
    ".reason{margin:16px 0 0;padding:10px 12px;border-radius:10px;background:var(--panel-2);color:var(--muted)}"
    ".reason strong{display:block;margin-bottom:2px;color:var(--text);font-size:12px}"
    ".note-fallback{margin:10px 0 0;padding:10px 12px;border-radius:10px;font-size:13px;color:var(--text);"
    "background:color-mix(in srgb,var(--warn) 12%,transparent);border:1px solid color-mix(in srgb,var(--warn) 35%,transparent)}"
    ".note-fallback strong{display:block;margin-bottom:2px;color:var(--warn);font-size:11px;letter-spacing:.05em;text-transform:uppercase}"
    ".saved{margin:10px 0 0;color:var(--good);font-size:13px}.saved code{background:var(--panel-2);padding:2px 6px;border-radius:6px;font-size:12px}"
    ".table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;margin-top:14px}"
    "table{border-collapse:collapse;width:100%;min-width:860px}"
    "th,td{padding:11px 12px;text-align:left;border-bottom:1px solid var(--line)}"
    "th{position:sticky;top:0;background:var(--head-bg);color:var(--muted);font-size:11px;letter-spacing:.06em;text-transform:uppercase}"
    "td{color:var(--text);font-size:13px}tbody tr:last-child td{border-bottom:0}"
    "tbody tr.verified{background:color-mix(in srgb,var(--good) 10%,transparent)}"
    "tbody tr.downloaded{background:color-mix(in srgb,var(--accent) 12%,transparent)}"
    "tbody tr.failed{background:color-mix(in srgb,var(--bad) 7%,transparent)}"
    "tbody tr.failed td:nth-child(2) a{color:var(--muted);text-decoration:line-through}"
    ".source{display:inline-flex;padding:3px 7px;border-radius:7px;background:var(--chip-bg);color:var(--chip-text);font-size:11px;font-weight:700;text-transform:capitalize}"
    ".score{display:inline-flex;min-width:34px;justify-content:center;padding:3px 8px;border-radius:7px;font-size:12px;font-weight:800;background:var(--chip-bg);color:var(--text)}"
    ".score.good{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}"
    ".score.mid{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}"
    ".score.low{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}"
    ".pct{position:relative;display:inline-block;min-width:62px;height:18px;border-radius:6px;background:var(--track-bg);overflow:hidden;vertical-align:middle}"
    ".pct i{position:absolute;left:0;top:0;bottom:0;opacity:.30}"
    ".pct b{position:relative;display:block;text-align:center;line-height:18px;font-size:11px;font-weight:700}"
    ".pct.good i{background:var(--good)}.pct.good b{color:var(--good)}"
    ".pct.mid i{background:var(--warn)}.pct.mid b{color:var(--warn)}"
    ".pct.low i{background:var(--bad)}.pct.low b{color:var(--bad)}"
    ".decision .badge{display:inline-block;margin-right:6px;padding:2px 7px;border-radius:6px;font-size:10px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}"
    ".badge-downloaded{background:color-mix(in srgb,var(--accent) 20%,transparent);color:var(--accent)}"
    ".badge-verified{background:color-mix(in srgb,var(--good) 20%,transparent);color:var(--good)}"
    ".badge-failed{background:color-mix(in srgb,var(--bad) 20%,transparent);color:var(--bad)}"
    ".empty-candidates{padding:18px;color:var(--muted);text-align:center}"
    ".store-links{margin-top:14px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2)}"
    ".store-links summary{cursor:pointer;padding:11px 13px;color:var(--text);font-weight:700}"
    ".link-grid{display:flex;flex-wrap:wrap;gap:8px;padding:0 13px 13px}"
    ".link-grid a{padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);font-size:12px}"
    ".link-grid a:hover{border-color:var(--accent);text-decoration:none}"
    "@media(max-width:650px){main{padding:16px 12px 36px}.report-header{padding:20px}.head-actions{position:static;margin-bottom:8px}"
    ".summary-grid{grid-template-columns:1fr}.track-card{padding:16px}.track-head{display:block}.status{display:inline-flex;margin-top:12px}.shown{display:none}}"
)

_SCRIPT = (
    "(function(){"
    "var r=document.documentElement,btn=document.getElementById('theme');"
    "function ico(){if(btn)btn.innerHTML=r.classList.contains('light')?'" + _MOON + "':'" + _SUN + "';}"
    "function set(t){var l=t==='light';r.classList.toggle('light',l);"
    "try{localStorage.setItem('omnidl-review-theme',t);}catch(e){}ico();}"
    "if(btn)btn.addEventListener('click',function(){set(r.classList.contains('light')?'dark':'light');});ico();"
    "var q=document.getElementById('q'),none=document.getElementById('noresults'),shown=document.getElementById('shown');"
    "var chips=[].slice.call(document.querySelectorAll('.chip')),"
    "cards=[].slice.call(document.querySelectorAll('.track-card')),f='all';"
    "function apply(){var term=((q&&q.value)||'').trim().toLowerCase(),n=0;"
    "cards.forEach(function(c){var okS=(f==='all'||c.getAttribute('data-status')===f);"
    "var okT=(!term||c.getAttribute('data-search').indexOf(term)>-1);var s=okS&&okT;"
    "c.style.display=s?'':'none';if(s)n++;});if(none)none.hidden=n>0;if(shown)shown.textContent=n;}"
    "chips.forEach(function(ch){ch.addEventListener('click',function(){f=ch.getAttribute('data-f');"
    "chips.forEach(function(x){x.classList.toggle('active',x===ch);});apply();});});"
    "if(q)q.addEventListener('input',apply);apply();"
    "function fb(t){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';"
    "document.body.appendChild(a);a.focus();a.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(a);}"
    "function copy(t,b){function ok(){b.classList.add('ok');b.innerHTML='" + _CHECK + "';"
    "setTimeout(function(){b.classList.remove('ok');b.innerHTML='" + _COPY + "';},1200);}"
    "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(ok,function(){fb(t);ok();});}else{fb(t);ok();}}"
    "[].slice.call(document.querySelectorAll('.copy')).forEach(function(b){"
    "b.addEventListener('click',function(){copy(b.getAttribute('data-copy'),b);});});"
    "})();"
)


def write_review_report(output_dir: Path, playlist_name: str, outcomes: list[TrackOutcome]) -> Path:
    """Write a standalone review report without downloading any new media."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    generated = now.strftime("%B %d, %Y at %H:%M UTC")
    path = output_dir / f"omnidl-review-{stamp}.html"
    flagged = len(outcomes)
    flagged_label = f"{flagged} track{'' if flagged == 1 else 's'} flagged for review"
    body = "".join(_outcome_section(outcome) for outcome in outcomes)
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>OmniDL review {_EMDASH} {html.escape(playlist_name)}</title>"
        f"{_PREPAINT}<style>{_STYLE}</style></head><body><main>"
        '<header class="report-header">'
        f'<div class="brand"><span class="logo">{_LOGO}</span> Omni<span class="accent">DL</span></div>'
        '<div class="head-actions">'
        '<a class="dash-link" href="http://127.0.0.1:8000/dashboard" target="_blank" rel="noopener">'
        f"Dashboard {_ARROW_UR}</a>"
        f'<button id="theme" class="icon-btn" type="button" title="Toggle light / dark theme">{_SUN}</button>'
        "</div>"
        '<p class="eyebrow">OmniDL match review</p>'
        f"<h1>{html.escape(playlist_name)}</h1>"
        f'<p class="meta">Generated {generated} {_MIDDOT} {html.escape(flagged_label)}</p>'
        '<p class="lede">Closest available candidates may have been downloaded. Review the reasons and alternatives below; service links open searches and do not claim that those storefronts were queried automatically.</p>'
        f'<div class="summary-grid">{_summary_cards(outcomes)}</div></header>'
        f"{_toolbar(outcomes)}"
        f'<div id="tracks">{body}</div>'
        '<p id="noresults" class="noresults" hidden>No tracks match your filters.</p>'
        f"</main><script>{_SCRIPT}</script></body></html>"
    )
    path.write_text(document, encoding="utf-8")
    return path
