# Strict external-source matching and review reports

## Goal

For Spotify playlist jobs, download only a high-confidence external audio match. Do
not place a guessed or uncertain match in the user's output directory. At the end
of the job, create a review report for every track that was not downloaded.

Spotify remains metadata-only: it supplies the artist, title, duration, album,
cover art, and track number. OmniDL does not access, decrypt, or export Spotify
audio streams.

## Source scope

OmniDL will search these download-capable sources, in priority order:

1. YouTube Music official song results.
2. YouTube search results.
3. SoundCloud search results, using yt-dlp's `soundcloud:search` extractor to
   discover candidates; successful SoundCloud URLs remain downloadable by the
   existing SoundCloud tooling.

Only candidates available through the application's existing download engines are
eligible for automatic download. Restricted streaming services (including Spotify,
Apple Music, Amazon Music, Tidal, Deezer, and Qobuz) are not download sources.

## Candidate matching

Each candidate is scored against Spotify metadata using:

- normalized title similarity;
- normalized primary-artist similarity;
- duration distance;
- source trust, with a preference for YouTube Music song results.

The score is out of 100: title similarity contributes 45 points, artist
similarity 35 points, duration contributes 20 points (20 at <=5 seconds, 15 at
<=10 seconds, 8 at <=20 seconds, and 0 otherwise), and an official YouTube
Music song adds a 5-point source bonus capped at 100. A candidate can be
downloaded only when its score is at least 85, its title and artist similarity
are each at least 80%, and its duration is no more than 20 seconds from Spotify
when both durations are known. Tracks with an unknown Spotify or candidate
duration are review-only and are never downloaded automatically.

The matcher returns structured candidate data, not only a video ID. It records the
source, candidate URL/ID, title, artist, duration, score, and rejection reason.

Only candidates meeting the strict automatic-download threshold are downloaded. The
current unrestricted generic YouTube top-result fallback is removed. A candidate
below the threshold is rejected even if it is the best candidate.

## Spotify job flow

For each Spotify track:

1. Search and score external candidates in the stated priority order.
2. Download the highest-scoring candidate only when it meets the threshold.
3. Tag successful downloads with Spotify metadata.
4. Record an outcome for every non-download: no candidate found, candidate rejected
   as uncertain, download failure, or cancellation.

The terminal retains concise per-track status output and adds the review-report
path to the end-of-job summary when there are unresolved tracks.

## Review report

Write an HTML report beside the selected output directory at the end of a Spotify
job when one or more tracks were not downloaded. It will include:

- original Spotify track metadata and a Spotify link;
- outcome and reason;
- the best rejected candidates, their scores, reasons, and source links;
- direct service search links for Spotify, Apple Music/iTunes, YouTube Music,
  Amazon, Pandora, Deezer, Tidal, Qobuz, SoundCloud, Bandcamp, and a general web
  search covering the remaining requested storefronts.

The report will describe these as **search links**. It will not assert that the
restricted storefronts were queried automatically. Rejected candidates are never
downloaded or written into the user's output directory.

## Error handling

- An unavailable or temporarily failing source is recorded without ending the
  playlist job.
- A candidate download failure is listed separately from matching failure.
- If report writing fails, the terminal reports that failure while preserving the
  track outcome in the job log.
- Cancellation stops further work and does not present unfinished tracks as
  confidently reviewed.

## Testing

Tests will establish, before implementation:

- exact/high-confidence candidates are selected;
- near-title, wrong-artist, and out-of-range-duration candidates are rejected;
- rejected candidates do not invoke a download command or create an output file;
- outcomes carry clear reasons;
- reports contain rejected candidates and service search links;
- a Spotify job reports the review-report path when appropriate.

## Out of scope

- Downloading, decrypting, recording, or bypassing access controls for Spotify or
  other DRM-protected music services.
- API integrations or automated catalog searches for every listed storefront.
- Interactive candidate selection in the first version.