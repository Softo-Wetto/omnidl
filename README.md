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

## Use your home OmniDL from anywhere (Cloudflare Tunnel)

Running OmniDL on your own PC avoids YouTube's bot checks (they target datacenter IPs, not
home connections), but it only answers on `127.0.0.1`. A Cloudflare Tunnel lets you reach it
from a Mac, a phone, anywhere — at **<https://omnidl-home.softowetto.com>** — while it keeps
downloading from your home connection.

**Using it:** start OmniDL on the PC as usual. It starts the tunnel itself (the console shows
`Remote access: connected`), and the tunnel stops when OmniDL does. Open the address on the
other device and sign in with Cloudflare Access (an emailed one-time code). The PC has to
stay on and awake.

From another device, downloads still go into your PC's library as normal, and each finished
job gets a **⤓ Save** button for a copy on the device you're using. *Folder* is hidden there,
since it would open Explorer on the PC.

**How it's kept private.** At the PC, OmniDL trusts every request completely — no passphrase,
no limits, settings that write files — because only you can reach `127.0.0.1`. The tunnel
connects from `127.0.0.1` too, so without a lock anyone with the address would get the same
trust. Two locks, either of which is enough on its own:

1. **Cloudflare Access** keeps anyone who isn't you from reaching the PC at all.
2. **OmniDL checks Access's signed login token itself** on every request that came through
   Cloudflare — the page, the API and the live-output socket — and refuses anything without a
   valid, current token for this app (the signature, team, app and expiry are all checked).
   So if Access is ever switched off or misconfigured, OmniDL stays locked rather than opening
   your PC up.

OmniDL learns which Access app to trust from Cloudflare itself (the login redirect for the
address), so no IDs are copied around. Using it at the PC needs no login.

**Set-up (once):**

1. Install `cloudflared` — the signed binary in `%LOCALAPPDATA%\Programs\cloudflared\` is
   picked up automatically, or put it on `PATH`.
2. `cloudflared tunnel login` → pick `softowetto.com` in the browser.
3. `cloudflared tunnel create omnidl-home`, then
   `cloudflared tunnel route dns omnidl-home omnidl-home.softowetto.com`.
4. Write `%USERPROFILE%\.cloudflared\omnidl-home.yml`:
   ```yaml
   tunnel: <tunnel id>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel id>.json
   ingress:
     - hostname: omnidl-home.softowetto.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```
5. Write `remote.json` next to `config.json` (gitignored; deliberately *not* editable from the
   web UI, since the UI is what remote visitors reach):
   ```json
   { "hostname": "omnidl-home.softowetto.com",
     "tunnel_config": "C:\\Users\\<you>\\.cloudflared\\omnidl-home.yml" }
   ```
6. In the Cloudflare Zero Trust dashboard: **Access → Applications → Add an application →
   Self-hosted**, public hostname `omnidl-home.softowetto.com`, with a policy that **Allows**
   only your email. Until this exists, remote visitors get a "This OmniDL is locked" page.

**Notes.** cloudflared runs with `--no-autoupdate` (a self-replacing binary would escape the
supervisor); update it by downloading the new release over the old file. Its log is
`cloudflared.log` beside `config.json`. If you change OmniDL's port (`OMNIDL_PORT`), change
`service:` in the tunnel config to match. Big **Save** zips are built before sending, and
Cloudflare drops a response that hasn't started within 100 s — fine for hundreds of tracks,
but not for a whole multi-gigabyte library at once.

## Usage

- **Paste a link** — the engine chip shows which tool will be used (auto-detected):
  - `open.spotify.com` / `spotify:` → **Embed scrape → yt-dlp** (per track)
  - `youtube.com` / `youtu.be` → **yt-dlp**
  - `soundcloud.com` → **scdl**
- **Type a song** (no URL) — matched exactly like a playlist track or a Bulk line, so one song
  and many songs come back as the same version. `Artist - Title`, `Title by Artist` and loose
  phrases like `drake one dance` all work. In **Video** mode typed text is a YouTube video
  search instead. See [How a song is matched](#how-a-song-is-matched).
- **☰ Bulk** — paste a whole list, one song per line, and download it as a single job.
  See [Bulk paste](#bulk-paste).
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

### Bulk paste

Song lists are usually written as prose, not data, so a line can carry three things: a
label that isn't part of the song, the song, and the artist. **☰ Bulk** takes them as they
come:

```
Bridesmaid entrance song - Con Gai Mien Tay by Luong Khanh Vy
Groomsman entrance song - Mr Strong Man by George Lam
2. Katy Perry - Teenage Dream
https://open.spotify.com/playlist/…
```

It parses each line and shows you the result **before** downloading — artist and title in
editable boxes, with any dropped label struck through — so a mis-read line costs a
keystroke instead of a wrong file. Numbering, bullets, blank lines and `#` comments are
ignored.

Labels are judged **line by line**, so a line parses the same way whatever it's pasted with.
A label is only removed from a line that names its artist with `by` (`… - X by Y`), which is
what stops an ordinary `Artist - Title` list from having its artists mistaken for labels. Parts
that belong to the title stay: `Black Betty - Single Edit by Spiderbait` keeps "Single Edit",
`Mr Brightside - Live by The Killers` keeps "Live". Dashes that phones and Word autocorrect
(`–`, `—`) split like hyphens, and quotes around a title are dropped. Untick **Ignore labels**
to keep every line exactly as written.

The whole list runs as **one job**, not one per line: a single progress view, the library
index built once, shared concurrency, and one review report listing anything that couldn't
be matched confidently.

**Search** picks where song *names* are looked up:

| Option | Searches |
|---|---|
| **Auto** (default) | YouTube Music, YouTube and SoundCloud — best scoring match wins |
| **YouTube only** | YouTube Music + YouTube |
| **SoundCloud only** | SoundCloud |

> Spotify isn't a search option. Anonymous tokens are refused (`429 QUOTA_EXCEEDED`) and it
> serves no audio, so it can only ever be a *link* source — pasted Spotify links in the box
> still resolve through Spotify as normal.

Pasted links are queued as their own jobs, so a playlist URL still resolves as a playlist
rather than being flattened into one search. Each is held to the same download limits as a
link pasted on its own.

A SoundCloud-only list needs no access passphrase, the same as a single SoundCloud link.
**↻ Retry** re-runs the list itself (it's saved with the job, so this works after a restart
too) rather than searching for the job's display name.

### How a song is matched

Every song — a Spotify playlist track, a Bulk line, or a typed search — goes through the
same matcher. That's deliberate: typed searches used to take YouTube's #1 result instead,
which usually meant the music video (intros, skits, sometimes a live cut), so the same song
could come back as a different version depending on how you asked for it.

1. **Search** YouTube Music (official audio), YouTube and SoundCloud.
2. **Score** each result on artist, title and length. Credits and upload decorations don't
   count against a match — `Payphone (feat. Wiz Khalifa)`, `Drake - One Dance (Lyrics)` and
   `Justin Bieber, Nicki Minaj – Beauty And A Beat` all name the song — and the artist is read
   from the title when a lyrics or fan channel uploaded it.
3. **Exclude other versions.** Sped up, slowed, reverb, nightcore, 8D, live, acoustic,
   instrumental, karaoke, covers, remixes/radio mixes, extended edits, loops and 30-second
   previews are never downloaded unless the song you asked for is itself that version.
4. **Check the length.** A Spotify track has an exact length; anything more than 20s off is a
   different edit. A typed song has no length, so the uploads *vote*: the length most copies
   agree on (weighted towards each service's top results) becomes the reference.
5. **Download the best match.** If it won't download — YouTube throttles busy downloads — only
   the *same recording* from another source may stand in (verified if the choice was, and
   within 10s of it). Otherwise the track fails *and is retried* after the rest, one at a time.
   Saving a different version just because the right one was busy is what made playlists
   unreliable before.

On 80 real songs checked against Spotify's own lengths, the right version was chosen for
80/80 playlist tracks, 79/80 bulk lines and 79–80/80 typed searches (previously 78, 76 and
45), and all three paths picked the identical upload for 74 of them.

The two matching switches in **Settings** apply to all of this: **Prefer YouTube Music**
(official audio wins ties) and **Match result to track length** (step 4).

### Spotify match quality

A Spotify link resolves to a track list, then each track is matched to the cleanest source:

1. **YouTube Music official audio** (default, **Prefer YouTube Music** on).
2. **YouTube search**.
3. **SoundCloud search**.

Every candidate is scored against the Spotify title, artist, and duration — see
[How a song is matched](#how-a-song-is-matched). OmniDL prefers a verified match; a plausible
non-exact one of the right version is downloaded but marked for review rather than presented
as verified. A different version is never downloaded, and tracks with no safe candidate stay
undownloaded and are listed in the review report.

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
