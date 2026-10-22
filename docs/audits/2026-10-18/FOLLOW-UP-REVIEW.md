# OmniDL follow-up review — 18 October 2026

**Verdict: the previous work is substantially implemented. Two areas still deserve attention: deletion ownership and recording identity. No major UI redesign is needed based on the checked flows.**

Reviewed commits `341d0ff` and `88d6cd0`; final reviewed HEAD is `88d6cd0`. The working tree was clean before this review. This handoff adds review documents and evidence only.

## Verification

- `python -m unittest discover -s tests`: **305 passed**. The suite blocks outbound network and uses temporary media. It still emits resource-cleanup warnings, without failures.
- `node --test tests/js/*.cjs`: **26 passed**.
- `python tests/browser/checks.py --shots <existing folder>`: **65/65 passed** in an isolated application copy with mutating endpoints intercepted. The first attempt could not save screenshots because the supplied destination did not exist; after creating it, all layout checks and the complete suite passed.
- Inspected screenshots at 390 and 1440 px; the browser suite checked 320, 360, 390, 768, 820, 1024, and 1440 px.
- Additional independent reproductions used disposable folders and locally generated tones. The direct-link collision test ran real yt-dlp and ffmpeg against local file URLs; no provider account or internet media was used.

Live Spotify/YouTube/SoundCloud behavior, Safari/iOS, and the standalone executable were not verified in this review. No real library files, settings, tunnel, or deployed server were changed.

## Previous findings

| Previous item | Current assessment |
|---|---|
| R01: nested/reused SoundCloud delivery | Manifest wrapper and recursive hosted fallback implemented; covered by passing tests. |
| R02: ZIP names and changing files | Unique names and change detection implemented; regression tests pass. |
| R03: competing output writers | Matched-track locking/staging works in its tested cases. Direct links bypass it, and identity is not durable: F02/F03 below. |
| R04: unusable existing files | Empty/unreadable-file checks and preservation of replaced files implemented for matched downloads; baseline tests pass. |
| R05: expanded output paths | Canonicalization implemented; tests pass. |
| R06: overlapping submission tokens | In-flight coordination and payload checks implemented; tests pass. |
| R07: edits lost during saving | Bulk locking, Settings revision handling, and duplicate-row handling implemented; unit/browser checks pass. |
| R08: timeout ends at headers | Deadline now covers JSON consumption; regression coverage passes. |
| R09: keyboard focus lost | Stable cards/focus restoration implemented; real-browser checks pass. |
| R10: backwards playlist progress | Completed-outcome progress and retry phase implemented; tests pass. |
| R11: interrupted hosted expiry | Interrupted jobs receive an expiry timestamp. New orphan cleanup is too broad: F01 below. |
| R12: mobile summary clipping | Wrapping chips implemented; 320 px browser check passes. |
| R13: build falsely reports success | Exit/status checks implemented; packaging regression tests pass. Actual executable not rebuilt here. |

The new quality picker also has command-generation, local conversion, and browser coverage that passed. Crash-safe history, clearer job phases, improved touch targets, and removal of the duplicate phone job card are present. These improvements should be preserved.

## F01 · P1 · Hosted orphan cleanup can delete unrelated library folders

**Source:** `app/jobs.py:965–986`; shared default roots and mode selection at `app/settings.py:25–44`.

`_reap_orphans()` considers every second-level directory under `DOWNLOAD_ROOT` disposable if it is old and absent from the in-memory jobs. It does not verify that OmniDL created the directory as hosted temporary storage. Being inside the root is not proof of ownership.

Both local library output and hosted temporary storage default to the project's `downloads` directory. A local install later started in hosted mode (`OMNIDL_HOST=0.0.0.0` without an explicit local-mode override), or a hosted root pointed at an existing organized library, can therefore lose `downloads/Artist/Album` directories. With the defaults, cleanup checks every five minutes and treats folders older than one hour as expired.

**Confirmed reproduction:** create `temporary-root/My Artist/My Album/keep.mp3`, age the album directory beyond the TTL, set the temporary root as `DOWNLOAD_ROOT`, set hosted mode, and invoke the real orphan reaper with an empty manager. Both the album directory and file were deleted. This directory was never a hosted job.

The normal home-tunnel setup that remains in local mode does not run this orphan sweep. The defect concerns hosted mode and storage-root reuse; no production data loss was observed or induced.

**Required fix:** give hosted storage a dedicated root and require explicit ownership evidence before automatic deletion. Preserve unknown directories, including legacy local libraries. A UUID-looking name alone is not sufficient ownership evidence. Handle old hosted storage through a deliberate migration/reconciliation path.

**Acceptance:** an aged unowned artist/album directory survives; owned expired job storage is removed; active jobs survive; switching a local install to hosted mode does not erase the library; symlink/junction escape protections remain intact.

## F02 · P1 · Different pasted links with the same title can return the first recording

**Source:** `app/engines.py:211–220`; command-job construction at `app/jobs.py:900–915`; execution at `app/jobs.py:1123–1136`.

Direct yt-dlp link downloads use `%(title)s.%(ext)s`, with no extractor/media ID in the file identity. A later link with the same title can be treated by yt-dlp as already downloaded, even though it refers to different audio. OmniDL marks the second job done and offers the old audio through Save. The new matched-track reservation/staging code is not used by this path.

**Confirmed reproduction:** create two local WAV recordings named `Same title.wav` in different source directories, one two seconds long and the other four seconds long. Submit two distinct link jobs through the real manager. Replace their remote input with fixture metadata containing distinct media IDs, identical titles, and each local file URL (`--load-info-json` and `--enable-file-urls`). Run the actual yt-dlp commands, retaining the application's output, conversion, and manifest options.

| Job | Requested recording | Delivered recording | Outcome |
|---|---:|---:|---|
| First link | 2 seconds | 2.04-second MP3 | Done |
| Second link | 4 seconds | 2.04-second MP3 | Done; reports already downloaded |

The decoded audio hashes were identical. Container hashes differed because metadata processing can still alter the file; the second recording's audio was not delivered. This is a deterministic local reproduction of the filename collision, independent of live provider availability.

**Required fix:** associate direct-link outputs with stable source identity, such as extractor plus media ID, and disambiguate titles without overwriting unrelated files. Apply output ownership/staging to direct links and playlist items as well as matched tracks. Keep reruns of the same recording reusable.

**Acceptance:** distinct source IDs sharing a title produce the correct separate recordings; retrying the same ID reuses its own file; overlapping link jobs and mixed link/search jobs cannot share an unreserved output path; Save returns each job's correct result.

## F03 · P2 · Matched-track collision protection is lost after restart

**Source:** in-memory `written_by` at `app/jobs.py:165`; target choice at `app/jobs.py:1418–1435`; fast skip at `app/jobs.py:1444–1454`.

The new filename collision handling remembers track ownership only for the lifetime of `JobManager`. After a restart, a playable file at the expected path is accepted without checking its artist/title identity. A different track whose name sanitizes or truncates to the same filename is then skipped and offered as though it were the requested song.

**Confirmed reproduction:** use the same artist collision as the new regression test: `A/B - Song` and `A_B - Song` both sanitize to `A_B - Song.opus`. Save playable audio tagged as artist `A/B`, create a fresh manager, then request artist `A_B`. The result was `skipped`, no search occurred, and Save offered the file still tagged `A/B`. The two internal track identities were different.

The current collision test succeeds because both tracks run under one manager. It does not exercise recovery from existing files.

**Required fix:** preserve or reconstruct file identity across restarts. Check persisted provenance and reliable tags before declaring a filename match; treat a conflicting identity as a separate recording. Preserve an existing file when its identity is unknown rather than silently assigning it to the new request.

**Acceptance:** run the existing sanitized-name collision test across a manager/process restart; cover truncated names, manually imported files, and distinct recordings with similar titles. Both requested recordings must remain separately retrievable.

## System/UI recommendation

The useful next system improvement is a shared, durable record of **who owns each file and which recording it contains**, used by downloading, reuse, Save, restart recovery, and cleanup. This can be a small manifest/store; a broad database migration is not required to fix these cases.

No further major UI work is warranted from this review. The existing navigation, mobile current-job card, wrapping counts, keyboard focus, save feedback, and quality picker passed the checked flows. Prioritize the file-safety corrections above; cosmetic refinements can wait.

## Claude Code handoff

> Preserve the verified fixes. Address F01 first, then F02 and F03. Use disposable folders and local generated media to reproduce them; never use the real music library for deletion or collision tests. Add regressions covering unowned storage, distinct source IDs with identical titles, mixed download entry points, and ownership after restart. Run the Python, JavaScript, and browser suites. Keep optional cosmetic work out of these fixes.

## Evidence

- [Independent reproduction results](backend-results.json)
- [Reproduction script](backend-review.py)
- [390 px screenshot](dashboard-390.png)
- [1440 px screenshot](dashboard-1440.png)

Run the reproduction script from this checkout with `python docs/audits/2026-10-18/backend-review.py`. It reports observed behavior rather than acting as a passing regression test; after repairs, the failure indicators and incorrect-audio result should change.
