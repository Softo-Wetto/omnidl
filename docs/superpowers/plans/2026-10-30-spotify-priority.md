# Spotify source priority

The requested behavior is SpotipyFree first for Spotify metadata, with yt-dlp fetching
verified YouTube Music audio and regular YouTube serving as fallback. SpotipyFree does not
download audio. Keep the existing Spotify web/embed resolver as metadata recovery, including
when SpotipyFree returns only part of a list. Preserve preferred metadata, ordering, repeated
playlist entries, and the visible incomplete-list warning when recovery also falls short.

Implement in the existing Python download pipeline. Replacing that pipeline with a separate
spotDL command would lose its matching, retry, output ownership, and result reporting. Use the
installed SpotipyFree API directly instead. Add the explicit dependency and packaged imports.
Keep ordinary text searches and SoundCloud-only restrictions working as before.

1. Add failing resolver tests for primary metadata, paging, partial recovery, duplicates,
   provider failure, and visible truncation. Add audio tests for Music priority and fallback.
2. Add the SpotipyFree adapter and retain the old resolver as recovery; reconcile partial lists.
3. Route Spotify jobs through that resolver and prefer verified Music candidates, with safe
   fallback. Update source labels and documentation to describe the actual providers.
4. Run focused tests, then Python/JavaScript/browser checks; inspect the final diff. Use mocks
   for provider responses and temporary local audio, without downloading into the real library.

## Implemented and verified

- SpotipyFree is the primary metadata resolver; partial collections use independent web recovery.
- Verified Music candidates use music.youtube.com and have first priority for Spotify. A
  separate bounded attempt budget keeps regular YouTube available after Music failures.
- Failed-track retries preserve the preference through saved history and server restarts.
- Existing source restrictions, strict matching, staging, and file ownership remain in use.
- `python tests/run_all.py`: 356 Python tests, 26 JavaScript tests, 65/65 browser checks passed.
  The pre-existing resource-cleanup warnings remain. Versions are recorded in docs/tested-versions.txt.
- Read-only live lookup of the public Spotify track “Blinding Lights” returned The Weeknd,
  200 seconds, through SpotipyFree. Audio fallback was tested with mocked providers and
  temporary local audio; no live music download was performed. The standalone exe was not rebuilt.

Restart the source app with start.bat to load the change. There is no remaining Claude handoff
for this implementation. A recording still needs to be available on the chosen audio sources;
missing/unverified matches continue to be reported rather than silently substituted.
