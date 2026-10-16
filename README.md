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

Launching it again is safe. If OmniDL is already running it just opens it; if it's idle and
the code has been updated, it's shut down cleanly and restarted; if it's busy downloading it's
left alone (`python run.py --restart` restarts it anyway, cancelling cleanly). If some
*other* program is using the port, OmniDL says so and leaves it running — set
`OMNIDL_PORT=8001` to use another port.

## Use your home OmniDL from anywhere (Cloudflare Tunnel)

Running OmniDL on your own PC avoids YouTube's bot checks (they target datacenter IPs, not
home connections), but it only answers on `127.0.0.1`. A Cloudflare Tunnel lets you reach it
from a Mac, a phone, anywhere — at **<https://omnidl-home.softowetto.com>** — while it keeps
downloading from your home connection.

**Using it:** start OmniDL on the PC as usual. It starts the tunnel itself (the console shows
`Remote access: connected`), and the tunnel stops when OmniDL does. Open the address on the
other device and sign in with Cloudflare Access (an emailed one-time code). A **🏠 Home PC**
badge in the header shows you're controlling the PC remotely.

From another device, downloads still go into your PC's library as normal, and each finished
job gets a **⤓ Save** button for a copy on the device you're using. *Folder* is hidden there,
since it would open Explorer on the PC. Settings are saved on the PC, so they're the same
whichever device you change them from.

**Staying reachable.** While remote access is on, OmniDL stops Windows from *sleeping* (a
sleeping PC can't be reached or woken from the Mac); the screen can still turn off. It also
holds off sleep while any download is running, wherever it was started. To allow sleep while
idle, add `"keep_awake": false` to `remote.json`.

**Install it like an app.** On the Mac, Safari → *File → Add to Dock* (or Chrome/Edge →
*Install*); on a phone, *Add to Home Screen*. It opens in its own window with its own icon.

If the PC goes off or your Cloudflare login expires, the dashboard says so instead of buttons
silently doing nothing — reload to sign in again.

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

The local app is also protected from the websites *you* visit, tunnel or not: it only answers
to its own names (`127.0.0.1`, `localhost`, the tunnel address), which stops DNS-rebinding
pages from taking it over; anything that changes state — and the live-output socket, which
browsers don't otherwise restrict — must come from OmniDL's own page; and no page may embed it
in a frame (clickjacking).

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
supervisor); update it by downloading the new release over the old file — OmniDL warns at
startup once it's six months old, before Cloudflare stops accepting it. Its log is
`cloudflared.log` beside `config.json`. If you change OmniDL's port (`OMNIDL_PORT`), change
`service:` in the tunnel config to match. Big **Save** zips are built before sending (in
the background, two at a time, so the rest of OmniDL stays responsive), and Cloudflare drops a
response that hasn't started within 100 s — fine for hundreds of tracks, but not for a whole
multi-gigabyte library at once. **Save** also covers songs a re-run found already in your
library (in any format), and a job that failed or was cancelled part-way offers **Save available
files (N)** for whatever it finished.

## Usage

- **Paste a link** — the engine chip shows which tool will be used (auto-detected):
  - `open.spotify.com` / `spotify:` → **Embed scrape → yt-dlp** (per track)
  - `youtube.com` / `youtu.be` → **yt-dlp**
  - `soundcloud.com` → **scdl**
- **Type a song** (no URL) — matched exactly like a playlist track or a Bulk line, so one song
  and many songs come back as the same version. `Artist - Title`, `Title by Artist` and loose
  phrases like `drake one dance` all work. A phrase that can't be identified as a song isn't
  downloaded on a guess — the card asks you to type it as `Artist - Title` or paste a link. In
  **Video** mode typed text is a YouTube video search instead. See
  [How a song is matched](#how-a-song-is-matched).
- **☰ Bulk** — paste a whole list, one song per line, and download it as a single job.
  See [Bulk paste](#bulk-paste).
- Use the **engine dropdown** to force a specific tool, and the **format dropdown** for a
  one-off format override.
- The **Downloads** panel has two parts: **In progress** (live progress bar and current track)
  and **History** (newest first, with dates for older jobs, filters for completed / with issues /
  failed / cancelled, and search). Click a job — or Tab to it and press Enter — to view its
  log. Drag the line between the log and the panel (or focus it and use ← →) to resize it.
- **Cancel** kills the running process tree; **Cancel all** and **Clear history** ask for a
  second press. Clearing history never touches the music in your library (on a hosted server it
  deletes the server copies, so Save stops working for those jobs). **↻ Retry** re-runs a failed
  or cancelled job; **↻ Retry N failed** re-runs just the tracks a playlist or list couldn't get.
- **Results on the card** — the list or playlist name, what happened to each track
  ("8 saved · 2 already had · 3 not found"), the actual file format, and where the files are.
  A list with misses says **Completed with issues** rather than a plain green "done".
- **Phones** — the download in progress (with Cancel / Save / Retry) sits right under the
  search box, the queue comes before the log, and the log folds away (**Show** / **Hide**).
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
- **At-a-glance results** — a finished job with tracks to check shows **⚠ Review N**, which
  opens its review report from any device; a failed job shows why on its card (e.g. "YouTube
  bot check - cookies needed", with a **How to fix** button) without opening the log. If the
  live connection drops, a banner says so until it's back.
- **Keeping yt-dlp current** — YouTube breaks old yt-dlp releases outright. On your own install,
  *Settings → Maintenance → Update yt-dlp* upgrades it in one click (after downloads finish);
  you're warned once it's three weeks old.
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
keystroke instead of a wrong file. Untick a row to leave it out (the summary shows "N of M
selected") or ✕ to delete the line. Numbering, bullets, blank lines and `#` comments are
ignored.

Your list is a draft: closing the dialog or reloading the page keeps it, edits survive
changing the search service, and **Download** is only available once the preview matches
what's in the box (so you can't send the previous list by clicking quickly). If the server's
limit holds some links back, exactly those links stay in the box for later. A list longer
than the limit (500 lines on your own install, 100 for visitors) says so before you
download.

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
| **SoundCloud only** | SoundCloud — YouTube and YouTube Music are never contacted, and the YouTube login isn't used |

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
4. **Check the length.** A Spotify track has an exact length; anything more than 10s off is a
   different edit. A typed song has no length, so the uploads *vote*: the length most copies
   agree on becomes the reference, with YouTube Music's own entry for the song counting double
   (it can settle a song alone, but several uploads that agree outvote it). Failing that, the
   artist's own upload of exactly that title sets it. Music videos never vote, and are only
   downloaded when their length matches the song within 3s. `- Remastered 2011` style labels
   count as the same recording.
5. **Download only a verified match** (*Only download verified matches*, on by default).
   Anything that can't be confirmed as this exact song and version isn't downloaded; it goes to
   the review report with its closest candidates and their links. If the right match won't
   download — YouTube throttles busy downloads — only the *same recording* from another source
   may stand in. Otherwise the track fails *and is retried* after the rest, one at a time.

On 120 real songs checked against Spotify's own lengths (an offline benchmark of the actual
search results), strict matching downloaded the right version for 120/120 playlist tracks,
119/120 typed `Artist - Title` searches and 119/120 loose phrases — the one miss each time was
left undownloaded, not replaced by a wrong version. Zero wrong versions were downloaded. With
strict matching off, one unverified pick in 360 was the wrong edit.

The matching switches in **Settings** apply to all of this: **Only download verified
matches** (step 5), **Prefer YouTube Music** (official audio wins ties) and **Match result to
track length** (step 4).

### Spotify match quality

A Spotify link resolves to a track list, then each track is matched to the cleanest source:

1. **YouTube Music official audio** (default, **Prefer YouTube Music** on).
2. **YouTube search**.
3. **SoundCloud search**.

Every candidate is scored against the Spotify title, artist, and duration — see
[How a song is matched](#how-a-song-is-matched). Only a verified match is downloaded; with
*Only download verified matches* turned off, a plausible non-exact one of the right version is
also downloaded but marked for review. A different version is never downloaded, and tracks
with no safe candidate stay undownloaded and are listed in the review report.

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

**File format.** Default is **opus** — YouTube's own audio codec, kept without re-encoding, so
it's the smallest file at full source quality. Pick m4a/mp3/flac/wav/ogg in Settings if you
prefer: the **Bitrate** setting applies whenever audio has to be converted (always for MP3).
FLAC/WAV made from YouTube or SoundCloud audio are bigger files with no extra quality.
**SoundCloud links** are saved as MP3 or Opus when you choose those, and lossless original
uploads become FLAC; other formats keep SoundCloud's own file (the card shows the real
format).

### Music library review

Click **Library** in local mode to scan the configured output folder recursively. The review
shows total size, metadata issues, duplicate groups, comparable quality scores, the recommended
copy to keep, and potential space savings. It covers files OmniDL downloaded and music that was
already in nested folders.

Repairs are deliberately opt-in, and you see exactly what will be written first. **Fill
artist/title…** reads the filename the way you say your untagged files are named (*Artist -
Title* or *Title - Artist*, chosen above the list), shows the proposed tags in editable boxes,
and writes only the empty ones when you press **Save tags** — existing tags are never
changed. A name it can't read unambiguously (no `-`, or several, like
`Nova - Midnight Run - Live`) isn't guessed at; you type the tags instead. The same reading is
used for the library view and duplicate grouping. Filtering the list never claims the library
is clean when the scan found problems, and a missing or invalid library folder is reported as
an error rather than as an empty library. Library review never deletes, renames, or
overwrites audio files.

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
| Output folder | all | Defaults to `./downloads`. Checked before saving: a path that can't be created or written to is refused next to the field, and the old setting stays. |
| Audio format | all | opus (default) / m4a / mp3 / flac / wav / ogg. |
| Bitrate | yt-dlp + spotDL | Used when audio is converted (always for MP3). `auto` = 192 kbps. |
| Only download verified matches | all matching | On by default — see [How a song is matched](#how-a-song-is-matched). |
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
