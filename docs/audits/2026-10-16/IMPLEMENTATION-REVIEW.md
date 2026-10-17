# OmniDL implementation review — 16 October 2026

**Verdict: substantial improvement, but not ready for an unqualified sign-off.** The main implementation commits address the original handoff, and the main browser regressions now pass. Additional tests reproduced 13 remaining/new defect groups covering delivery, concurrency, draft preservation, accessibility, cleanup, presentation, and build reporting.

Reviewed `e9f12dd`, `22b980c`, and the launcher/packaging follow-up `3531008`, with final HEAD at `3531008`, against the original `b553d6e` audit. The launcher commit arrived during review; its diff was inspected and both test suites were rerun afterward. The working tree was clean at the start. This review adds documentation/evidence only; it does not change the application.

**Verification:** 258 Python tests and 26 JavaScript tests passed. Additional checks used headless Edge, temporary files, mocked media providers/subprocesses, and a separate server with lifespan hooks disabled. The browser was checked at 320, 390, 768, 820, and 1440 CSS pixels, with dark and light screenshots. No real music was downloaded or repaired, and no deployed tunnel, production server, or user settings were changed. The stock Python suite emitted resource-cleanup warnings, but no failures. This does not establish live Spotify/YouTube/SoundCloud compatibility or Safari/iOS behavior.

The latest `start.bat` correctly forwarded `--restart --no-browser` to a stub server in an isolated copy. Packaging paths and module collection were inspected, and a copied build script was exercised with failing local stubs, exposing R13. A real standalone executable was not built or launched.

## Original handoff: implementation status

| Original item | Result of this review |
|---|---|
| D01: launcher kills unknown listeners | Fixed in the reviewed paths. Instance probing/ownership checks, reuse of busy instances, and guarded shutdown have regression coverage. |
| D02: reversed metadata repair | Fixed. Explicit naming order, proposed editable values, ambiguity rejection, and preservation of existing tags are implemented and tested with temporary media. |
| D03: SoundCloud-only falls back to YouTube | Fixed in the reviewed matching paths. Bare SoundCloud phrases no longer invoke YouTube identification/fallback; selected sources and account authorization constrain matching. |
| D04: stale bulk preview downloads old input | Original reproduction passes: stale/unparsed drafts cannot submit, and revisioned preview handling is covered. Other draft-loss cases remain in R07. |
| D05: failed requests look successful | Original reproductions pass. Failed Settings save preserves state; HTML 500 keeps the song input; startup failure keeps six format options, connects the socket, and displays Retry. The timeout gap in R08 remains. |
| D06: duplicate submissions | Browser double-click test now sends one request. Server idempotency is incomplete under overlapping requests: R06. |
| D07: mixed job logs | Original out-of-order response test passes: B remains selected and contains only B's log. The snapshot/live-output merge has additional unit coverage. |
| D08: unavailable Save for partial/existing results | Implemented for the tested track-list paths and partial outputs. Direct SoundCloud output discovery remains incomplete: R01. File-validity checks are also insufficient: R04. |
| D09: partial playlists look wholly successful | Implemented. Counts, “Completed with issues,” and Retry failed tracks are present; counts/failed tracks survive record round trips. Progress still regresses: R10. |
| D10: bitrate/format settings ignored | Implemented. MP3 bitrate has a real local-fixture conversion test; SoundCloud flags/caveats and actual file formats are exposed. Live source behavior was not tested. |
| D11: invalid output folders accepted | Common invalid/unwritable path cases are covered; settings writes are atomic. Expanded path validation and persistence disagree: R05. |
| D12: ZIP preparation blocks the event loop | Blocking preparation was replaced with threaded streaming and a two-export limit. Export integrity still has defects: R02. |
| D13: keyboard/dialog accessibility | Dialog semantics, focus containment, inert background, Escape, input labels, and real job buttons are implemented. Browser checks pass for opening Settings and escaping Bulk. Rendering still destroys keyboard focus: R09. |
| D14: filtered library falsely says “clean” | Fixed. Browser check displays “No matching results,” and the helper distinguishes empty, clean, and filtered-out reports. |

The seven original UI recommendations also received substantial implementation: mobile current-job card, earlier toolbar wrapping, outcome/destination cards, clearer typography, persistent bulk drafts, grouped/sticky settings, and separate active/history sections. The remaining refinements are listed below; a blanket redesign is unnecessary.

## Remaining and newly identified defects

Priorities: **P1** blocks a core workflow or can cause competing writes to a library file; **P2** is a functional reliability/data-integrity/accessibility problem; **P3** is misleading or clipped presentation. Each finding below has a focused reproduction in the saved evidence, except where explicitly described as supporting source inspection.

### R01 · P1 · Hosted SoundCloud playlists can finish with no downloadable files

**Source:** `app/jobs.py:355–367`, `app/engines.py:254–257`; local fallback collection at `app/jobs.py:95–111`.

In hosted mode, `Job.list_files()` checks only `out_dir.iterdir()`. The installed scdl implementation places playlist tracks in a playlist subdirectory unless `--no-playlist-folder` is supplied; OmniDL does not supply it. The files can therefore exist while the job reports `has_files=false` and the file endpoint returns no files.

**Reproduction:** a completed SoundCloud job containing `job/Playlist/Artist - Saved.mp3` had one actual media file, zero listed files, and `has_files=false`. The expected scdl directory layout was verified from its installed `_build_ytdl_output_filename()` implementation, without a network download.

There is a related local/remote gap: scdl collection compares before/after directory modification times. A successful rerun that skips existing files produces an empty change set, so those files are not available through Save. A fixture with an unchanged library reproduced zero recorded outputs.

**Fix / acceptance:** collect nested completed files safely within the job directory. Prefer an explicit per-job manifest that includes reused files over inference from directory changes. Test nested playlists, reruns that skip everything, mixtures of new/existing files, and partial output. Keep unfinished files excluded and prevent directory traversal/symlink escapes when extending enumeration.

### R02 · P2 · ZIP exports can have duplicate names or bad CRCs

**Source:** `app/zipstream.py:65–77,96–115`.

Two reproducible integrity problems remain:

- Collision handling prefixes the immediate parent only once. Three files named `Song.mp3` in different albums' `Disc 1` directories produce entries `Song.mp3`, `Disc 1 - Song.mp3`, and `Disc 1 - Song.mp3`. Extracting by name can overwrite one copy or require collision handling by the user.
- The plan records a file's original size, but the CRC pass reads its later complete contents. If the file grows between planning and streaming, the stream includes the old number of bytes with the CRC of the larger file. The response length still matches, yet `zipfile.testzip()` reports a bad CRC. Concurrent metadata repair or another write to the same output can trigger this class of race.

**Fix / acceptance:** guarantee unique archive paths, including repeated parent names and case-insensitive extraction collisions. Export a stable version of each file or validate that the file is unchanged and fail explicitly; do not silently stream bytes inconsistent with the headers. Test repeated basenames, file growth/shrinkage, modifications during export, cancellation, and ZIP64. The existing threading improvement should be retained.

### R03 · P1 · Concurrent tracks/jobs can write to the same output file

**Source:** `app/jobs.py:1013–1040,1155–1203,1209–1215` and `app/engines.py:225–230`.

The filename/existing-file check is separate from downloading, with no reservation or lock for the output path. Duplicate rows in a concurrent bulk list, or overlapping playlists running in different workers, can both see a missing file and start converters using the same final and intermediate filenames. Cleanup can also target another attempt's sidecar files.

**Reproduction:** two concurrent `_fetch_track()` calls for the same track both invoked the mocked downloader with the same output template, even with skip-existing enabled. Actual media corruption was not induced; competing writes are the confirmed defect.

**Fix / acceptance:** reserve output identity/path across all jobs, recheck existing files after acquiring the reservation, use unique staging paths, and publish finished files atomically. Test duplicate rows in one job, the same track in two workers, cancellation of one waiter, and distinct tracks whose sanitized/truncated filenames collide.

### R04 · P2 · Empty or corrupt files count as successfully existing music

**Source:** `app/jobs.py:61–64,1209–1224`; `app/library.py:300–357` and `LibraryIndex` matching.

The exact-target skip path checks only `target.exists()`. It records that path as a deliverable without confirming that it contains valid media. A failed conversion or interrupted write can therefore make later retries skip a song permanently and offer the unusable file through Save.

**Reproduction:** an empty `Artist - Empty.opus` was returned as `skipped` and advertised with `has_files=true`. Library scanning already detects unreadable media, but that information is not used by this fast path.

**Fix / acceptance:** reject zero-length and unreadable outputs for deduplication/delivery; use cached validation rather than rereading every file on every snapshot. Do not automatically delete a user's questionable file—report it or recover to a safe alternate/staging path. Test empty, truncated, valid cross-format, and media with missing tags.

### R05 · P2 · Output-path expansion is validated but not saved

**Source:** `app/settings.py:188–215,251–259`; consumers include `app/jobs.py:614–630` and `app/main.py:525–529`.

Validation expands environment variables and `~`, then checks the expanded absolute folder. Saving persists the original text, while consumers construct `Path(saved_value)` without the same expansion. The destination actually used can differ from the directory that passed validation.

**Reproduction:** `%OMNIDL_AUDIT_ROOT%\music` passed validation and created the intended temporary folder, but both saved and loaded settings retained the literal variable expression. `Path(loaded_value).is_absolute()` was false.

**Fix / acceptance:** use one normalization function and persist its canonical absolute result, or consistently expand at every use. Test `%VAR%`, `$VAR`, `~`, ordinary absolute paths, Unicode, missing variables, and reopening/restarting after Save. Validation, scan, download, and Folder must agree on the same location.

### R06 · P2 · Submission tokens do not prevent simultaneous duplicates

**Source:** `app/main.py:616–658,709–773`; `app/jobs.py:632–643`.

The response cache is populated only after awaiting submission. `_enqueue()` can yield while broadcasting the new job, so another request with the same session/token can pass the cache check and enqueue a second job. The browser's click guard helps, but overlapping retries after a lost/slow response still bypass server protection.

**Reproduction:** two simultaneous calls to the real download handler with the same token and a yielding mocked broadcast created two jobs with different IDs. No download workers were started.

**Fix / acceptance:** register an in-flight operation before yielding, and make followers await/reuse its result. Scope tokens to the session and operation, verify the payload fingerprint, and clean up failures/cancellations. Cover concurrent requests, sequential replay, differing payloads with the same token, and retry after failure.

### R07 · P2 · Bulk and Settings can still discard edits

**Source:** `app/static/js/app.js:1016–1060,1271–1321`; `app/static/js/core.js:168–176,249–260`.

Three verified draft cases need attention:

- While Bulk request A is pending, the textarea remains editable. Entering a new list B is allowed, but A's success clears and persists the current draft, erasing B and closing the dialog.
- While Settings is saving concurrency=3, changing the field to 7 is allowed. The response for 3 reloads the whole form, changes the visible value back to 3, resets dirty state, and says “Saved ✓”.
- Bulk row IDs are derived from raw text plus occurrence number. Removing the first of two identical source lines changes the surviving line's ID. An edit on the surviving row is lost on reparse: expected `Second edit`, actual `Song`.

**Fix / acceptance:** either lock affected controls while a submission is pending or reconcile responses against the submitted draft revision and preserve newer edits. Use stable row identities when removing identical lines. Cover edits during requests, closing/reopening during requests, duplicate raw lines with different edits, excluded rows, and held-back URLs.

### R08 · P2 · Request timeout stops when headers arrive, not when JSON finishes

**Source:** `app/static/js/core.js:29–56`; timeouts used by `app/static/js/app.js:1175–1186,1784–1792`.

The abort timer is cleared in the `fetch()` finally block before `await res.json()`. Fetch resolves once response headers are available; a stalled/truncated body can then leave metadata loading or bulk parsing pending indefinitely despite a configured timeout.

**Reproduction:** a response with immediate headers and a stalled `json()` remained pending past a 10 ms timeout (observed after 60 ms), with its signal still un-aborted. The existing timeout test covers a fetch that never returns headers, which does not catch this case.

**Fix / acceptance:** keep the timeout active through body consumption and response validation, then clear it in one outer finally block. Test delayed headers, delayed/partial body, malformed JSON, user cancellation, timeout, and successful retry. Apply intentional finite deadlines to other UI requests where indefinite busy controls are undesirable.

### R09 · P2 · Live rendering destroys keyboard focus

**Source:** `app/static/js/app.js:447–455,493–520,523–539,543–548`.

Both the queue and phone current-job card replace their button nodes during updates. The new buttons have good semantics, but the focused original node is removed.

**Reproduction:** focusing the phone card's Cancel button and updating progress moved focus to `BODY`. Selecting a job log likewise detached its focused title button and left focus on `BODY`. This is also disruptive for assistive technology and can interfere with activation while progress changes rapidly.

**Fix / acceptance:** update stable, keyed elements in place; preserve focused controls and selection. Avoid rebuilding the whole history for a single progress change. Test keyboard log selection, Cancel/Retry/Save focus during progress events, the once-per-minute refresh, and removal of the selected job.

### R10 · P3 · Concurrent playlist progress moves backwards

**Source:** `app/jobs.py:1013–1035`.

A track starting sets progress from its position in the list, while a track finishing sets it from the number completed. With parallel downloads these measure different things and overwrite each other.

**Reproduction:** a four-track list with three workers emitted `0, 25, 50, 25, 75, 50, 75, 100`. The same issue becomes more confusing with larger lists and retry sweeps.

**Fix / acceptance:** derive aggregate progress from completed outcomes, optionally incorporating weighted in-progress work. Represent retries as a distinct phase rather than silently jumping backwards or appearing finished at 100%. Test staggered completions, skips, retries, and cancellation.

### R11 · P2 · Interrupted hosted downloads can escape file expiration

**Source:** `app/jobs.py:445–470,770–780`.

When history restores a previously running/queued job, it becomes `cancelled` but keeps `finished=None`. Cleanup requires a truthy `job.finished`, so its hosted output folder never qualifies for TTL deletion. The UI also exposes no expiry for it.

**Reproduction:** restoring a running hosted job produced status=`cancelled`, finished=`None`, expires=`None`; it could not satisfy the cleanup predicate. The fixture folder remained associated with that job. No actual waiting or production cleanup was used.

**Fix / acceptance:** assign a defined interruption/expiry timestamp during recovery and reconcile orphaned hosted output folders safely. Apply this only to server-managed temporary storage, never the local music library. Test crashes/restarts with partial output, queued jobs, already-expired results, repeated restarts, and folders no longer referenced by history.

### R12 · P3 · Queue summary still clips on narrow screens

**Source:** `app/static/css/style.css:732`; summary construction at `app/static/js/app.js:463–474`.

The summary retains `white-space: nowrap`. With downloading, queued, done, issues, and failed counts present, its text extends beyond the 320 px viewport and is clipped. The provided screenshot reproduces it. The phone source/format controls and 768 px search input are now substantially better, so this is a focused remaining layout fix.

**Fix / acceptance:** wrap the counts, use compact independent status chips, or collapse secondary counts into a details control. Test all states simultaneously at 320/360/390 px and at browser zoom; do not hide essential failure counts through overflow clipping.

### R13 · P2 · The standalone build reports success after installation/build failure

**Source:** `packaging/build_exe.bat:10–29`.

The script does not check the exit status of pip or PyInstaller. It proceeds through restoration and unconditionally prints “Done. The app is in: …”, then returns success. A missing dependency or failed build therefore looks complete even when no executable exists.

**Reproduction:** an isolated copy used local stub modules that made pip exit 7 and PyInstaller exit 23. Both failure messages appeared, followed by the success message; the script returned 0 and there was no `OmniDL.exe`. No real package installation/build was attempted.

**Fix / acceptance:** stop on installation or backup failure; retain the build exit code while restoring user data; check restoration and the expected executable before reporting success. Exit nonzero and give a useful error on failure. Also check the currently unchecked backup/restore moves before any destructive rebuild—the source inspection shows these errors are ignored, although actual data loss was not induced. Test success, failed installation, failed build, backup/restore failures, and a retry after an interrupted build using isolated fixtures.

## Recommended system improvements


1. **Unify output ownership and delivery records.** A manifest per job should record final path, original requested track, actual format, validation result, and whether a file was reused. Combine it with path reservations/staging (R01–R04) rather than maintaining several inconsistent discovery paths.
2. **Make job history crash-safe.** Settings are now atomic, but `JobManager._persist()` still overwrites `history.json` directly and suppresses all exceptions. At minimum use atomic replacement, a recoverable backup, explicit diagnostics, and startup reconciliation. A transactional local store is an option if the queue/history grows; avoid a database migration solely for cosmetic reasons.
3. **Bound and coordinate external work.** Search calls are synchronous work run in threads; cancellation primarily changes state/kills downloader subprocesses. Add explicit provider deadlines and ensure a cancelled search does not occupy a queue worker indefinitely. Consider shared cooldown/concurrency control across jobs, since per-job backoff does not slow another job using the same connection/account.
4. **Add browser integration checks to the regular verification command.** Keep the useful pure-JavaScript tests, but add real DOM checks for edits during requests, focus preservation, nested output delivery, startup retry, and 320/768 px layouts. These are the gaps this review found despite all current tests passing.
5. **Make dependency/build reproduction explicit.** Record the tested dependency set and provide a launch/packaging smoke check. The application now carries a version-specific uvicorn shutdown compatibility patch, so its behavior should be tested against the installed/frozen dependency set, rather than relying only on unconstrained installation requirements.

## Recommended UI refinements

1. **Keep the improved phone hierarchy; reduce repetition.** The current-job card now exposes Cancel around the middle of the first screen. The same active job is repeated immediately below in the queue, consuming most of a phone screen. Keep the compact current card and prioritize other queued work/history underneath it.
2. **Give titles room beside long status labels.** “Completed with issues” takes much of a narrow job header. Put status on its own line where necessary and expose the full title without hover. Preserve outcome counts and destination, which are meaningful improvements.
3. **Use a deliberate export state.** Save still assigns `window.location.href` to the file endpoint. If a file was moved, expired, or cannot be exported, the browser can leave the dashboard for an error response. Keep the dashboard visible, show preparing/downloading/failed feedback, and offer retry after validating availability. Avoid loading a multi-gigabyte file into browser memory as a workaround.
4. **Improve narrow touch targets and contrast selectively.** Some job actions remain around 32 px tall. Increase the most-used mobile controls and measure contrast of secondary text/error text in both themes. Preserve the existing palette and component style; another wholesale restyle would add little value.
5. **Represent unresolved work explicitly.** Use distinct copy for searching, downloading, converting, cooling down, retrying, and partially resolved playlists. Keep “not found” separate from temporary provider errors so users understand when retrying is worthwhile.

## Claude Code follow-up instruction

> Use this review together with the 15 October handoff. Preserve the verified fixes. First reproduce and fix R01 and R03, then the export/file-validation, settings, request-state, accessibility, cleanup, presentation, and build-reporting issues through R13. Add focused regressions for every fix and run the existing Python/JavaScript suites plus browser integration checks. Use temporary files/mocked providers; do not test against the real music library or live accounts. Keep any optional system/UI improvements separate from required defect fixes and report what remains deferred. Do not mark all original items complete merely because their basic unit tests pass.

## Evidence

- [Browser results and original-regression checks](browser-results.json)
- [Backend reproduction results](backend-results.json)
- [Core-helper reproduction results](core-results.json)
- [Launcher/build-script reproduction results](packaging-results.json)
- [390 px dark screenshot](dashboard-390-dark.png)
- [320 px dark screenshot showing clipped summary](dashboard-320-dark.png)
- [1440 px light screenshot](dashboard-1440-light.png)

Focused reproduction scripts are also available locally in `F:\Websites\omnidl-review-20261016`. They are audit harnesses, not application changes. The backend scripts operate on temporary fixtures; the browser script expects the isolated review server/browser and replaces request handling before mutations.
