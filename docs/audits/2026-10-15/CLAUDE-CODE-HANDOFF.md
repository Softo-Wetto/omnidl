# OmniDL defect and UI review — 15 October 2026

Reviewed commit: `b553d6e`. This is an audit and implementation brief; application code has not been changed.

**Scope and evidence:** inspected the local FastAPI app, browser UI, launcher, download engine integration, library repair, and existing tests. All **194 Python tests and 4 JavaScript tests passed**. Additional headless Edge checks reproduced failures that those tests do not cover. Layout checks used widths 320, 390, 768, 820, 1024, and 1440 CSS pixels. Download jobs in screenshots are synthetic. Backend reproductions used temporary files and mocked downloads/process termination/tag writes. No live media download, real library repair, public deployment, or real-device Safari test was performed.

**Priority:** P1 = address first because of unintended process termination, incorrect metadata, wrong downloads, or a source-access boundary violation. P2 = functional reliability or accessibility. P3 = minor misleading feedback. Source references below use the reviewed revision's line numbers.

## Copy this instruction to Claude Code

> Review and fix the defects in this document in the OmniDL repository. First reproduce each relevant finding and add focused regression coverage. Address P1 items before UI polish. Keep the existing local/remote/hosted behavior and authentication boundaries intact. Do not run real downloads, alter my music library, terminate unrelated processes, publish, or change credentials as part of verification. Use temporary files and mocked external services for destructive or account-dependent paths. Implement the UI improvements after the functional fixes, retaining OmniDL's existing visual style. Run the Python and JavaScript suites, then browser checks for failed requests, reordered responses, keyboard use, and responsive layouts. Report what was fixed, what was tested, and anything deferred. Do not treat passing current tests as proof that these reproduced bugs are absent.

## Defects

### D01 · P1 · Launching OmniDL can kill an unrelated application

**Location:** `run.py:33–102`; `start.bat` invokes this launcher.

`_restart_existing_server()` kills every process found listening on the configured port without verifying that it belongs to OmniDL. On Windows it uses forced process-tree termination. A different development server on port 8000 can therefore be killed when OmniDL starts; reopening OmniDL can also abruptly interrupt its own active downloads.

**Evidence:** a mocked listener with PID 424242 was passed directly to `_terminate_process_tree`. No real process was killed during this reproduction.

**Fix / acceptance:** verify service ownership and instance identity. Reuse an existing healthy OmniDL instance where possible; report an occupied port or choose another port for an unrelated listener. A regression test must prove that an unknown listener is never terminated. Deliberate restart should shut down active work cleanly.

### D02 · P1 · Library repair can write artist/title backwards

**Location:** `app/library.py:30–37,351–377`; naming options in `app/settings.py` and the Settings UI.

The app supports `Title - Artist` filenames, but `_filename_identity()` and `repair_missing_tags()` always assume `Artist - Title`. For an untagged `Song - Artist.mp3`, “Fill artist/title” writes artist=`Song`, title=`Artist`. Splitting at the last separator also treats titles such as `Artist - Song - Live` as artist=`Artist - Song`, title=`Live`.

**Evidence:** mocked metadata repair produced exactly the reversed tags. This affects real metadata when the user invokes repair, rather than just the display.

**Fix / acceptance:** show the proposed tags before saving; support an explicit naming convention and reject ambiguous guesses. Do not assume the current global convention describes every imported file. Test both naming orders, multiple separators, partially populated tags, and preservation of existing values. Apply the same interpretation consistently to library display and duplicate detection.

### D03 · P1 · “SoundCloud only” can download from YouTube and bypass its access gate

**Location:** `app/jobs.py:1003–1010,1074–1093`; `app/candidate_search.py:120–140`; `app/main.py:524–535,561–569`.

A bare phrase is identified using YouTube Music regardless of the selected service. If identification fails, `_fetch_track()` downloads `ytsearch1:<phrase>` unconditionally. The request gate allows SoundCloud-only searches without unlocking YouTube, yet this fallback can use the configured YouTube cookies.

**Evidence:** a SoundCloud-only unresolved phrase required no unlock but produced a download command targeting `ytsearch1:unidentifiable phrase` with `--cookies` present. The actual download was mocked.

**Fix / acceptance:** carry the selected service through identification and fallback. SoundCloud-only must never contact YouTube/YouTube Music or consume YouTube credentials. Enforce authorization at the actual source-selection boundary as well as initial submission. Test typed phrases and bulk rows with and without identifiable artist/title metadata, including hosted locked sessions.

### D04 · P1 · Bulk can download the previous list instead of the current one

**Location:** `app/static/js/app.js:786–829`.

Preview refresh is debounced for 250 ms. While it is pending, Download remains enabled and reads the old preview DOM. Preview responses also have no version check: an older response arriving last overwrites the current preview.

**Evidence:** with “Old artist - Old song” previewed, replacing the textarea and immediately downloading submitted the old song. Separately, resolving the second preview request before the first left textarea=`Second`, preview=`First`.

**Fix / acceptance:** associate preview data with an input revision, discard outdated responses, and disable submission while the current text is unparsed. Keep edited rows in a deliberate draft model. Test clicking Download before the debounce fires, reversed response order, clearing text during a request, and preserving artist/title edits when changing the search service. Currently changing the service also reparses the original text and discards row edits.

### D05 · P2 · Request errors can look successful and corrupt browser state

**Location:** `app/static/js/app.js:544–565,617–631,683–708,1116–1130,1153–1189`.

The shared `api()` helper does not check HTTP status and converts non-JSON responses into `{}`. Callers often treat that as success. On a network exception it returns an error object, but `saveSettings()` still assigns that object to `state.settings` and announces success. During initial load, a failed metadata request replaces the safe default metadata; `fillSelect()` then throws and startup never reaches WebSocket connection.

**Evidence:**

- Simulated offline Save displayed both an error toast and “Settings saved”; the form status said “Saved ✓”, and `state.settings.audio_format` was lost.
- A simulated HTTP 500 HTML response caused Download to clear the song input despite no accepted job.
- Startup failure threw `TypeError: items is not iterable`, left no format choices, and never created the live socket.
- Cancel all / Clear finished increment their success counts without inspecting each API result.

**Fix / acceptance:** normalize transport, HTTP, and response-validation failures; require explicit success before updating state or clearing input. Preserve drafts and last good settings. Provide recoverable initialization with Retry and a persistent connection/error state. Test offline, 401/403, 429, JSON 500, HTML 500, malformed responses, and successful recovery. Count only confirmed cancellations/removals.

### D06 · P2 · Double-clicking Download queues duplicate jobs

**Location:** `app/static/js/app.js:617–631,1081–1083`; `app/main.py:561–586`.

There is no pending-submission guard. Repeated Enter or a double click sends the same input multiple times before the first response clears it.

**Evidence:** two concurrent `submitDownload()` calls made two API requests for the same song.

**Fix / acceptance:** disable the button and ignore repeated Enter while that submission is pending; restore the controls on failure. Add an idempotency token if retries of uncertain network outcomes should also be safe. Test slow responses and deliberate submission of a different song after completion.

### D07 · P2 · Switching jobs quickly mixes logs from different jobs

**Location:** `app/static/js/app.js:412–423`.

`viewJob()` starts a fetch but does not check that its job is still selected before appending the response. A slow response for A can append to B after the user switches. Live messages can also arrive while a historical snapshot is loading, so replay needs a clear ordering strategy.

**Evidence:** selected job B displayed `B_SELECTED_LOG` followed by `A_STALE_LOG` when responses completed out of order.

**Fix / acceptance:** cancel or ignore stale fetches and reconcile the snapshot with buffered live output. Test A→B and A→B→A, HTTP failures, and live messages arriving before the selected log response.

### D08 · P2 · Remote Save is missing for usable partial or already-existing files

**Location:** `app/jobs.py:302–323,346,1061–1071`; `app/static/js/app.js:341–348`; `app/main.py:753–785`.

Two paths prevent users on another device from getting available music:

- Failed/cancelled jobs advertise `has_files=false` even if completed files are recorded. The UI independently restricts Save to `done` jobs.
- Tracks skipped because they already exist are not added to the job's deliverable-file records. Repeating a playlist from a phone can finish successfully with no Save button, or return only the newly downloaded subset.

**Evidence:** failed/cancelled jobs each had one real temporary output file but `has_files=false`. An existing track returned `skipped` while `list_files()` was empty.

**Fix / acceptance:** distinguish complete deliverable files from overall job success and unfinished intermediates. Record matched existing paths, including cross-format matches, for remote delivery. Offer “Save available files” for partial results and explain the count. Verify that local library files remain intact when history is cleared.

### D09 · P2 · Partially failed playlists are marked “done” and cannot be retried from the card

**Location:** `app/jobs.py:950–979`; `app/static/js/app.js:341–362,526–541`.

One success or skip makes the entire job `done`, even with failed tracks and exit code 1. The Review button is helpful, but the green done badge and success toast hide the distinction. Retry appears only for `error` or `cancelled`, so users have to reconstruct or resubmit a partially failed list.

**Evidence:** one saved track plus one missing track produced status=`done`, exitCode=1, reviewCount=1.

**Fix / acceptance:** persist separate downloaded/skipped/failed/needs-review counts. Show “Completed with issues” and provide Retry failed tracks. Keep the review report accessible. Test all-success, partial failure, all-failure, all-skipped, and saved-but-unverified outcomes.

### D10 · P2 · Format and bitrate controls promise settings the engines ignore

**Location:** `app/engines.py:135–147,226–230`; `app/static/index.html:173–185`; `app/settings.py:216–234`.

- The visible Bitrate setting does not affect yt-dlp MP3 output: `_ytdlp_base()` always supplies `--audio-quality 192K`. The only engine builder consuming bitrate is spotDL, while `effective_settings()` forces the embed path for normal requests.
- Direct SoundCloud links only branch on MP3. Selecting FLAC, WAV, Opus, M4A, or Ogg does not result in a corresponding format instruction or application conversion step.

**Evidence:** bitrate settings `64k` and `320k` both produced `192K`; FLAC and WAV direct-SoundCloud requests produced identical commands.

**Fix / acceptance:** either honor the controls with supported conversion/quality behavior or disable unsupported choices with a clear explanation. Reflect actual output format on the job. Test command generation and resulting media metadata using short local fixtures; avoid claims that transcoding creates source quality that was absent.

### D11 · P2 · Invalid output folders are saved successfully

**Location:** `app/settings.py:148–153,172–194`; `app/main.py:391–409`; `app/library.py:289–305`.

`ensure_output_dir()` suppresses filesystem errors. Settings are persisted before successful directory validation. A nonexistent or unusable library root can then scan as an empty, apparently clean library rather than reporting a path problem.

**Evidence:** setting the output directory beneath a regular file persisted successfully, although the output path did not exist; scanning it returned zero files.

**Fix / acceptance:** validate the destination and report creation/write errors before replacing good settings. Use an atomic settings write. Distinguish an empty directory from an invalid/unreadable root. Test a file used as a parent directory, missing/unavailable paths, and permission failures with temporary fixtures/mocks.

### D12 · P2 · Preparing a large Save ZIP blocks the app's event loop

**Location:** `app/main.py:753–785`, particularly `774–780`.

The async request handler performs synchronous ZIP construction and file reads directly on the event loop. During a large export, other API requests, WebSocket progress, and cancellation handling can stall. Using uncompressed ZIP entries reduces CPU work but does not remove blocking disk I/O.

**Evidence level:** directly verified in source; no multi-gigabyte export or timing benchmark was run.

**Fix / acceptance:** move archive preparation off the event loop, clean up temporary files on success/failure, and bound simultaneous export work. Consider an asynchronous preparation state for exports that can exceed proxy request timeouts. A test with a deliberately slow archive writer should prove that unrelated API/live-feed work remains responsive.

### D13 · P2 · Dialogs and job selection are not fully keyboard accessible

**Location:** `app/static/index.html:73–74,139,233,272,297,340`; `app/static/js/app.js:393,710–719,1144–1150`.

Dialogs are ordinary divs without dialog semantics or focus management. Opening Settings leaves focus on its trigger behind the overlay. Queue rows use a click handler on an unfocusable `<li>`. The main song input lacks a persistent accessible label, and status toasts have no live-region semantics. Escape closes several dialogs but omits Bulk.

**Evidence:** opening Settings left `document.activeElement.id` as `open-settings`; dialog `role` and `aria-modal` were absent. Pressing Escape with Bulk open left it open.

**Fix / acceptance:** use native dialogs or a shared accessible modal controller with initial focus, focus containment, focus restoration, accessible headings/close buttons, and consistent Escape behavior. Make job selection operable with Enter/Space and expose selection state. Label inputs and announce meaningful errors/status changes. Expose Audio/Video state with appropriate semantics. Verify using the keyboard and browser accessibility tree.

### D14 · P3 · Filtering the library can incorrectly say it is clean

**Location:** `app/static/js/app.js:971–986`; `app/static/index.html:264–267`.

The “Your library looks clean” message is controlled by the filtered lists. A library with known problems appears clean when a search term matches none of those problems.

**Evidence level:** verified from the rendering condition and message text.

**Fix / acceptance:** distinguish “No matching results” from “No problems found in the scan,” keep total issue counts visible, and offer Clear filter. Test a nonempty problem report with a no-match query and a genuinely clean report.

## UI upgrades

These are design recommendations based on the inspected UI, rather than claims that the visual style itself is defective.

1. **Put current download controls before the terminal on phones.** At 390×844, the queue began around y=828: no job-level Cancel, Save, or Retry was visible in the initial viewport. At 320×740 the queue began around y=788. Use a compact active-job card near the input with progress, current song, Cancel, and completion actions. Make verbose output collapsible or place it after the queue. On desktop, allow the terminal/queue split to be resized.

2. **Fix cramped width ranges.** At 320 px the audio format select was effectively reduced to its arrow and Auto-detect was clipped. At 768 px the main input was only about 128 px wide because the full toolbar remains on one row until 760 px. Switch layouts earlier and allow source, media mode, and format to occupy separate rows as needed. Acceptance widths: 320, 360, 390, 768, 820, 1024, and 1440, plus browser zoom.

3. **Make results readable without opening the terminal.** Cards should show the resolved title/artist or playlist name, actual format, track outcome counts, and destination (“Home PC library” / “Save to this device”). Replace raw error status labels with plain text. Show an inline troubleshooting action for cookie/authentication errors and a persistent offline/reconnecting banner.

4. **Increase small text and action targets.** Job actions currently use 11 px text and 3 px vertical padding. Use larger touch targets, clearer secondary-text contrast, and visible keyboard focus. Preserve the current dark/light palette and visual identity. The screenshot evidence shows particularly faint current-track and queue-summary text; measure contrast before choosing final colors.

5. **Protect bulk work.** Keep drafts and edited rows when closing/reopening the modal. Add row removal and “N of M selected” feedback. When the server holds back links due to limits, retain those specific links for retry rather than clearing the entire textarea. Show parsing/queueing states and an explanation of any cap before submission.

6. **Make settings explain their scope and effect.** Group output, matching, authentication, and maintenance controls. Show supported format/quality options for the selected source, the effective output destination, validation next to the failing field, and whether changes are saved. Clearly say when a remote setting refers to a path on the home PC. Keep Save available while scrolling long settings panels.

7. **Separate active queue from history.** Keep current work visible, add status filters/search for history, and use dates rather than only times for older jobs. Provide deliberate handling for Cancel all and clarify what clearing history does to downloadable server files. Show hosted-file expiration on completed jobs using the server's actual configured TTL, rather than only a hard-coded empty-state hint.

## Validation and supporting material

Existing suites executed successfully:

```text
python -m unittest discover -s tests
194 tests passed

node --test tests/js/performance.test.cjs
4 tests passed
```

The Python run emitted resource-cleanup warnings but no test failures. The existing dashboard tests largely check HTML structure; they do not exercise the asynchronous UI races reproduced here.

- [Browser reproduction results](browser-results.json)
- [Backend reproduction results](backend-results.json)
- [Desktop screenshot, 1440 px](dashboard-1440.png)
- [Phone screenshot, 390 px](dashboard-390.png)
- [Narrow screenshot, 320 px](dashboard-320.png)

Suggested implementation order: D01–D04; then request handling and state consistency (D05–D07); then remote delivery/results/settings (D08–D12); then accessibility and feedback (D13–D14) together with the UI upgrades. Test source-selection changes with mocked providers and metadata repairs against temporary media fixtures. Do not use the real music library as the regression-test fixture.
