import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.jobs import Job, JobManager
from app.matching import Candidate, MatchDecision
from app.review_report import TrackOutcome
from app.spotify_resolver import Track


class SpotifyJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_candidate_is_attempted_before_higher_scored_unverified_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job(
                "spotify:test", "spotify", None, "",
                settings={"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus"},
            )
            unverified = Candidate("youtube", "https://youtube.test/unverified", "Near Title", "Nova", 180)
            verified = Candidate("youtube_music", "https://youtube.test/verified", "Midnight Run", "Nova", 180, True)
            decisions = [
                MatchDecision(unverified, 0.95, 1.0, 0, 99, False, "duration differs by more than 20 seconds"),
                MatchDecision(verified, 1.0, 1.0, 0, 85, True, "verified match"),
            ]
            target = Path(temp_dir) / "Nova - Midnight Run.opus"

            attempts = []

            async def download(_job, argv, emit, **_kwargs):
                attempts.append(argv[-1])
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[unverified, verified]), \
                 patch("app.jobs.decide_match", side_effect=decisions), \
                 patch.object(manager, "_stream_subprocess", side_effect=download):
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False, 1, 1)

        self.assertEqual("downloaded", result.status)
    async def test_best_rejected_candidate_is_downloaded_and_marked_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job(
                "spotify:test", "spotify", None, "",
                settings={"output_dir": temp_dir, "skip_existing": False, "audio_format": "opus"},
            )
            rejected = Candidate(
                "youtube", "https://youtube.test/x", "Wrong Title", "Nova", 180,
            )
            target = Path(temp_dir) / "Nova - Midnight Run.opus"

            async def download(_job, _argv, emit, **_kwargs):
                target.touch()
                return 0

            with patch("app.jobs.candidate_search.search_all", return_value=[rejected]), \
                 patch.object(manager, "_stream_subprocess", side_effect=download) as run:
                result = await manager._fetch_track(job, Track("Nova", "Midnight Run", 180), job.settings, False, 1, 1)

        self.assertEqual("downloaded_for_review", result.status)
        run.assert_awaited_once()
        self.assertIn("saved for review", job.output)

    async def test_unresolved_outcomes_write_report_and_emit_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager()
            job = Job("spotify:test", "spotify", None, "", settings={"output_dir": temp_dir})
            outcome = TrackOutcome(Track("Nova", "Missing Song", 180), "no_candidate", "no candidate found", [])
            path = Path(temp_dir) / "omnidl-review-test.html"
            with patch("app.jobs.write_review_report", return_value=path):
                await manager._emit_review_report(job, Path(temp_dir), "Test playlist", [outcome])

        self.assertIn(str(path), job.output)


if __name__ == "__main__":
    unittest.main()