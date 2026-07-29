# OmniDL

One local web app that wraps **spotDL**, **yt-dlp**, and **scdl** behind a single smart
input bar and one live terminal. Paste any Spotify / YouTube / SoundCloud link (or type a
search) — OmniDL auto-detects the right engine, streams the live download output into an
in-browser terminal, and drops the audio into your output folder.

```
┌───────────────────────────────────────────────┐
│ Paste link or search…            [Download ▸]  │
│ detected: ● Spotify · embed → yt-dlp           │
├───────────────────────────────────────────────┤
│ Resolving Spotify link via public embed…       │
│ Found 23 track(s) in playlist "My Mix".        │
│ [1/23] Artist A - Song A   [######    ] 61%    │
└───────────────────────────────────────────────┘
```

## Why this exists — the Spotify 403

spotDL fails with:

```
SpotifyException: http status: 403 — Active premium subscription required for the owner of the app.
```

This is **not** a spotDL bug, and the old advice to "make your own free developer app" **no
longer works** — Spotify's Web API now requires the *owner of the app* to have Premium, and
that applies to your own free app too (verified with a direct API call: a freshly-minted
token from a free-account app still gets 403 on the playlist endpoint).

**OmniDL's fix: skip the Web API entirely.** For Spotify links it uses the **Embed scrape**
method by default — it reads the public `open.spotify.com/embed/...` page for the track list
(artist + title), then downloads each track with **yt-dlp**. That needs **no API, no login,
no credentials, and no Premium**. Just paste a Spotify track / album / playlist URL and go.
Files are named `Artist - Title` from the Spotify metadata.

> spotDL is still bundled and selectable (**Settings → Spotify source → spotdl**) for anyone
> whose developer app *is* owned by a Premium account, but **Embed** is the default and the
> one that works for free.

## Requirements

- Python 3.10+ (developed on 3.14)
- `ffmpeg` on your PATH (already required by spotDL/yt-dlp)

## Setup

```bash
pip install -r requirements.txt
```

This installs `fastapi`, `uvicorn`, `spotdl`, `yt-dlp`, and `scdl`.

## Run

```bash
python run.py
```

…or just double-click **`start.bat`** on Windows. It serves on
<http://127.0.0.1:8000> (local-only — not exposed to your network) and opens your browser.

## Usage

- **Paste a link** — the engine chip shows which tool will be used (auto-detected):
  - `open.spotify.com` / `spotify:` → **Embed scrape → yt-dlp** (per track)
  - `youtube.com` / `youtu.be` → **yt-dlp**
  - `soundcloud.com` → **scdl**
- **Type a search** (no URL) — routed to a **yt-dlp** YouTube search.
- Use the **engine dropdown** to force a specific tool, and the **format dropdown** for a
  one-off format override.
- The **queue** runs jobs one at a time (avoids YouTube rate-limits) and shows a live
  per-job progress bar, current track, and a status summary. Click any job to view its log.
- **Cancel** kills the running process tree; **Cancel all** / **Clear finished** manage the
  whole queue; **↻ Retry** re-runs a failed or cancelled job. Toasts confirm each action.
- The **live output** panel is a clean log: repeating progress lines collapse into one
  updating line, output is colour-coded, and it scrolls natively (drag the bar or use the
  wheel). **Copy** grabs the whole log; a **↓ Jump to latest** pill appears if you scroll up.
- **Skip already-downloaded tracks** (on by default) indexes nested folders and matches
  artist, title, and duration across formats, so an existing MP3 can prevent a duplicate Opus download.
- **Parallel downloads** — Settings → *Parallel* (default 3, up to 8) downloads that many
  playlist tracks at once for a big speedup. At >1 the log switches to a compact per-track
  checklist (`✓ [12/64] Artist — Title`); at 1 you get the detailed per-track stream.
- **Persistent history** — your queue/history and each job's log are saved to `history.json`
  and restored on restart. Jobs that were mid-download when the app closed show as
  *cancelled (interrupted)*.
- **Light / dark theme** — the ☀ / 🌙 button in the top bar toggles a light theme; your
  choice is remembered and applied with no flash on reload.

### Spotify match quality

A Spotify link resolves to a track list, then each track is matched to the cleanest source:

1. **YouTube Music official audio** (default, **Prefer YouTube Music** on).
2. **YouTube search**.
3. **SoundCloud search**.

Every candidate is scored against the Spotify title, primary artist, and duration. OmniDL
prefers a strict match, then downloads the highest-scoring available candidate when no exact
match exists. Non-exact selections are explicitly marked for review rather than presented as
verified matches. Tracks with no external candidate still remain undownloaded.

OmniDL writes an `omnidl-review-*.html` report in the output directory for every non-exact
selection and every unavailable/failed track. It contains candidates, scores, reasons, and
search links for Spotify, Apple Music/iTunes, YouTube Music, Amazon, Pandora, Deezer, Tidal,
Qobuz, SoundCloud, Bandcamp, and the remaining requested storefronts. Storefront entries are
search links, not automated catalog checks or download sources.

You can't download Spotify's own (DRM-protected) file without Premium + credentials, so the
YT Music official audio is the closest clean equivalent. Optionally enable **Trim non-music
segments** (SponsorBlock) for any source that still has intro/outro chatter.

**Big playlists (100+).** The embed page only lists 100 tracks, so OmniDL also reads
Spotify's *own* anonymous web-player token (embedded in that page) to page through the full
track list via `api.spotify.com`. That token is minted by Spotify's first-party app, so it
isn't subject to the "app owner must be Premium" block. If it's ever unavailable it falls
back to the first 100 from the embed.

**Real Spotify tags.** When the token path is used, OmniDL also gets each track's album,
track number, and cover art, and writes them onto the file with `mutagen` (overriding the
YouTube source's tags) so your library shows correct artist / title / album / artwork.

**File format.** Default is now **m4a** — YouTube Music serves AAC, so it's remuxed without a
quality-losing re-encode and is much smaller than a transcoded 320k MP3. Pick mp3/flac/opus
in Settings if you prefer.

### Music library review

Click **Library** in local mode to scan the configured output folder recursively. The review
shows total size, metadata issues, duplicate groups, comparable quality scores, the recommended
copy to keep, and potential space savings. It covers files OmniDL downloaded and music that was
already in nested folders.

Repairs are deliberately opt-in: **Fill artist/title** only fills missing fields when the filename
unambiguously follows `Artist - Title`; it never overwrites existing tags. Library review never
deletes, renames, or overwrites audio files.

## Build a standalone app (no Python needed to run)

To get a double-click `OmniDL.exe` that bundles spotDL, yt-dlp, and scdl (so it runs on a
machine without Python installed):

```bash
build_exe.bat            # or:  python -m PyInstaller OmniDL.spec --noconfirm
```

The result is **`dist/OmniDL/OmniDL.exe`** (a folder bundle — ship the whole `OmniDL`
folder). `config.json` and `downloads/` are created next to the exe on first run.

**ffmpeg is still required** and must be on your PATH (or drop `ffmpeg.exe` into the
`OmniDL` folder). The bundle does not include ffmpeg.

> How it works: in frozen mode OmniDL re-invokes itself as
> `OmniDL.exe --run-tool <spotdl|yt-dlp|scdl> …` and dispatches to the bundled module, so
> each download is still a separate streaming subprocess — no tools on PATH needed.

## Settings reference

| Setting | Used by | Notes |
| --- | --- | --- |
| Spotify source | Spotify | `embed` (free, default) or `spotdl` (needs a Premium-owned app). |
| Match to track length | Spotify (embed) | Pick the YouTube result whose duration matches the track. |
| Spotify Client ID / Secret | spotDL method | Only used if Source = spotdl. |
| Spotify filename template | spotDL method | e.g. `{artists} - {title}.{output-ext}`. |
| Output folder | all | Defaults to `./downloads`. |
| Audio format | all | mp3 / flac / opus / m4a / wav / ogg. |
| Bitrate | spotDL method | e.g. `320k`, `auto`, `disable`. |
| Threads | spotDL method | Parallel track downloads. |
| Trim non-music (SponsorBlock) | yt-dlp | Strip intros/outros/offtopic from music videos. |
| Cookie file (cookies.txt) | spotDL + yt-dlp | Download as an authenticated user to dodge rate-limits. |
| Cookies from browser | yt-dlp | Pull cookies straight from Chrome/Firefox/Edge/etc. |

### Avoiding YouTube rate-limits

Large playlists can trip YouTube's bot checks. Export a `cookies.txt` (e.g. with a
"Get cookies.txt" browser extension) and set its path in **Settings → Cookie file**, or pick
a browser under **Cookies from browser** so yt-dlp authenticates as you.

## Notes

- Local-only by design: binds `127.0.0.1`, no login. Commands are executed as argv lists
  with `shell=False`, so a pasted link can never inject shell commands.
- Built on the spirit of [SomeDL](https://github.com/ChemistryGull/SomeDL), extended to
  three engines with a unified streaming terminal.
- Please only download content you have the right to.
