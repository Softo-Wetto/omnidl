import tempfile
import unittest
from pathlib import Path

from app.review_report import TrackOutcome, write_review_report
from app.spotify_resolver import Track


class ReviewReportTests(unittest.TestCase):
    def test_report_contains_rejection_and_requested_service_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outcome = TrackOutcome(
                track=Track("Nova", "Midnight Run", 180),
                status="rejected",
                reason="artist similarity below 80%",
                candidates=[],
            )
            path = write_review_report(Path(temp_dir), "Night playlist", [outcome])
            html = path.read_text(encoding="utf-8")

        self.assertIn("artist similarity below 80%", html)
        self.assertIn("Apple Music/iTunes", html)
        self.assertIn("TikTok / ByteDance", html)
        self.assertIn("MediaNet", html)
        self.assertIn("Closest available candidates may have been downloaded", html)

    def test_report_escapes_track_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outcome = TrackOutcome(
                track=Track("A < B", "Song & Title"),
                status="no_candidate",
                reason="no candidate found",
                candidates=[],
            )
            html = write_review_report(Path(temp_dir), "<playlist>", [outcome]).read_text(encoding="utf-8")

        self.assertIn("A &lt; B", html)
        self.assertIn("Song &amp; Title", html)
        self.assertNotIn("<playlist>", html)


    def test_report_has_visual_summary_and_status_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outcomes = [
                TrackOutcome(Track("Nova", "Near Match", 180), "downloaded_for_review", "title similarity below 80%", []),
                TrackOutcome(Track("Nova", "Missing", 180), "no_candidate", "no candidate found", []),
            ]
            html = write_review_report(Path(temp_dir), "Night playlist", outcomes).read_text(encoding="utf-8")

        self.assertIn('class="report-header"', html)
        self.assertIn('class="summary-card"', html)
        self.assertIn('class="status status-downloaded-for-review"', html)
        self.assertIn('class="status status-no-candidate"', html)

if __name__ == "__main__":
    unittest.main()
