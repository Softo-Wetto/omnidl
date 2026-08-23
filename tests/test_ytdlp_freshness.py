import unittest
from datetime import date, timedelta
from unittest.mock import patch

from app import engines


class YtdlpFreshnessTests(unittest.TestCase):
    """A stale yt-dlp is a hard outage, not a slow degradation: YouTube breaks old releases
    and every download fails with a bare 403. These guard the detector that says so."""

    def setUp(self):
        engines._YTDLP_AGE_CACHE.clear()
        self.addCleanup(engines._YTDLP_AGE_CACHE.clear)

    def _age_for(self, version):
        engines._YTDLP_AGE_CACHE["v"] = version
        return engines.ytdlp_age_days()

    def test_age_is_measured_from_the_date_based_version(self):
        recent = date.today() - timedelta(days=5)
        self.assertEqual(5, self._age_for(recent.strftime("%Y.%m.%d")))

    def test_the_release_that_broke_every_download_reads_as_badly_stale(self):
        """2026.06.09 was live locally while the server ran 2026.08.19; it 403'd on everything."""
        self.assertGreater(self._age_for("2026.06.09"), 30)

    def test_unparseable_and_missing_versions_are_unknown_not_zero(self):
        """None must not be confused with "fresh" — `0 > 30` is False and would hide the fault."""
        for version in ("", "unknown", "nightly", "2026.13.99"):
            with self.subTest(version=version):
                self.assertIsNone(self._age_for(version))

    def test_a_version_with_a_release_suffix_still_parses(self):
        self.assertEqual(0, self._age_for(date.today().strftime("%Y.%m.%d") + ".123456"))

    def test_version_is_cached_so_startup_spawns_one_subprocess(self):
        with patch("app.engines.subprocess.run") as run:
            run.return_value.stdout = "2026.08.19\n"
            engines.ytdlp_version()
            engines.ytdlp_version()
            engines.ytdlp_age_days()
        self.assertEqual(1, run.call_count)

    def test_a_broken_yt_dlp_is_reported_unknown_rather_than_crashing_startup(self):
        with patch("app.engines.subprocess.run", side_effect=OSError("not found")):
            self.assertEqual("", engines.ytdlp_version())
            self.assertIsNone(engines.ytdlp_age_days())


if __name__ == "__main__":
    unittest.main()
