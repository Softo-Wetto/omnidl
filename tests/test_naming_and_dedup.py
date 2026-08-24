import unittest

from pathlib import Path

from app.library import LibraryIndex, LibraryTrack, _artist_aliases, _normalise
from app.settings import DEFAULTS, NAMING_ARTISTS, NAMING_ORDERS, _coerce_pref
from app.spotify_resolver import Track


def _lib(relative_path, artist, title, duration=200):
    return LibraryTrack(
        path=Path(relative_path), relative_path=relative_path,
        title=title, artist=artist, album="",
        duration=duration, bitrate=320000, sample_rate=44100, channels=2,
        codec="mp3", size=8_000_000, has_artwork=True, issues=(),
    )


class FilenameConventionTests(unittest.TestCase):
    def setUp(self):
        self.track = Track("Justin Bieber", "Peaches",
                           artists=["Justin Bieber", "Daniel Caesar", "Giveon"])

    def test_all_artists_matches_the_spotdl_template(self):
        """spotify_template is "{artists} - {title}", so OmniDL's own naming must agree or
        every spotdl-built library gains a duplicate."""
        self.assertEqual("Justin Bieber, Daniel Caesar, Giveon - Peaches",
                         self.track.filename_for("artist-title", "all"))

    def test_primary_only_and_reversed_order(self):
        self.assertEqual("Justin Bieber - Peaches",
                         self.track.filename_for("artist-title", "primary"))
        self.assertEqual("Peaches - Justin Bieber, Daniel Caesar, Giveon",
                         self.track.filename_for("title-artist", "all"))

    def test_search_query_stays_primary_artist_regardless_of_naming(self):
        """Filenames credit everyone; the YouTube query must not, or match rates drop."""
        self.assertEqual("Justin Bieber - Peaches", self.track.query)

    def test_illegal_characters_are_replaced_not_dropped(self):
        t = Track("AC/DC", 'Who Made Who?', artists=["AC/DC"])
        name = t.filename_for()
        self.assertNotIn("/", name)
        self.assertNotIn("?", name)

    def test_missing_artist_falls_back_to_title_alone(self):
        self.assertEqual("Untitled", Track("", "Untitled").filename_for())

    def test_defaults_are_valid_and_junk_is_rejected(self):
        self.assertIn(DEFAULTS["naming_order"], NAMING_ORDERS)
        self.assertIn(DEFAULTS["naming_artists"], NAMING_ARTISTS)
        self.assertEqual(DEFAULTS["naming_order"], _coerce_pref("naming_order", "../etc"))
        self.assertEqual(DEFAULTS["naming_artists"], _coerce_pref("naming_artists", "wat"))


class CrossConventionDedupTests(unittest.TestCase):
    """The whole point of the index: one song, however it was named, is found once."""

    def test_a_spotdl_file_is_found_by_its_lead_artist(self):
        idx = LibraryIndex([_lib("Justin Bieber, Daniel Caesar, Giveon - Peaches.mp3",
                                 "Justin Bieber/Daniel Caesar/Giveon", "Peaches")])
        self.assertIsNotNone(idx.find("Justin Bieber", "Peaches", 200))

    def test_a_lead_artist_file_is_found_by_the_full_credit_list(self):
        idx = LibraryIndex([_lib("Justin Bieber - Peaches.opus", "Justin Bieber", "Peaches")])
        self.assertIsNotNone(
            idx.find("Justin Bieber, Daniel Caesar, Giveon", "Peaches", 200))

    def test_accents_do_not_split_the_same_artist(self):
        """Spotify returns GIVEON with a macron; spotdl tagged it plainly."""
        idx = LibraryIndex([_lib("x.mp3", "Giveon", "Heartbreak Anniversary")])
        self.assertIsNotNone(idx.find("GIVĒON", "Heartbreak Anniversary", 200))

    def test_separator_styles_are_interchangeable(self):
        for written in ("A/B", "A, B", "A feat. B", "A & B"):
            with self.subTest(written=written):
                idx = LibraryIndex([_lib("x.mp3", written, "Song")])
                self.assertIsNotNone(idx.find("A", "Song", 200))

    def test_a_different_song_is_not_a_false_positive(self):
        idx = LibraryIndex([_lib("x.mp3", "Justin Bieber", "Peaches")])
        self.assertIsNone(idx.find("Lil Nas X", "Old Town Road", 200))

    def test_duration_mismatch_still_rejects(self):
        """Same names but a wildly different length is a different recording."""
        idx = LibraryIndex([_lib("x.mp3", "A", "Song", duration=200)])
        self.assertIsNone(idx.find("A", "Song", 400))

    def test_untagged_file_is_matched_by_its_filename(self):
        item = _lib("Katy Perry, Snoop Dogg - California Gurls.mp3", "", "")
        self.assertIsNotNone(
            LibraryIndex([item]).find("Katy Perry", "California Gurls", 200))

    def test_aliases_of_a_blank_credit_are_empty_not_crashing(self):
        self.assertEqual([], _artist_aliases(""))
        self.assertEqual("", _normalise(None))


if __name__ == "__main__":
    unittest.main()
