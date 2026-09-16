import unittest

from app.bulk_input import parse_bulk, summarise
from app.candidate_search import SERVICE_SOURCES
from app.matching import Candidate, decide_match, is_preview_clip

# The paste this feature was built for: a running order, where the text before " - " names
# the moment in the day and has nothing to do with the song.
RUNNING_ORDER = """Bridesmaid entrance song - Con Gai Mien Tay by Luong Khanh Vy
Groomsman entrance song - Mr Strong Man by George Lam
Groom & Bride Entrance song - Shanghai Bund by Frances Yip"""


class LabelledListTests(unittest.TestCase):
    def test_the_label_is_dropped_and_the_artist_is_found(self):
        entries = parse_bulk(RUNNING_ORDER)
        self.assertEqual(
            [("Luong Khanh Vy", "Con Gai Mien Tay"),
             ("George Lam", "Mr Strong Man"),
             ("Frances Yip", "Shanghai Bund")],
            [(e.artist, e.title) for e in entries],
        )

    def test_the_dropped_label_is_reported_so_the_ui_can_show_it(self):
        """A silently discarded label is indistinguishable from a parsing bug."""
        self.assertEqual("Bridesmaid entrance song", parse_bulk(RUNNING_ORDER)[0].label)

    def test_label_stripping_can_be_forced_off(self):
        entry = parse_bulk(RUNNING_ORDER, strip_labels=False)[0]
        self.assertEqual("Bridesmaid entrance song - Con Gai Mien Tay", entry.title)


class PlainListTests(unittest.TestCase):
    """A normal "Artist - Title" list must not have its artist mistaken for a label."""

    def test_artist_dash_title_is_preserved(self):
        entries = parse_bulk("Katy Perry - Teenage Dream\nBruno Mars - Treasure")
        self.assertEqual([("Katy Perry", "Teenage Dream"), ("Bruno Mars", "Treasure")],
                         [(e.artist, e.title) for e in entries])

    def test_a_mixed_list_does_not_eat_artists(self):
        """One labelled line among plain ones must not turn every artist into a label."""
        entries = parse_bulk("Katy Perry - Teenage Dream\n"
                             "Bruno Mars - Treasure\n"
                             "Tame Impala - Let It Happen\n"
                             "First dance - Shanghai Bund by Frances Yip")
        self.assertEqual("Katy Perry", entries[0].artist)
        self.assertEqual("Tame Impala", entries[2].artist)
        # ...and the labelled line is still cleaned: each line is judged on its own. This
        # used to be decided for the whole paste, so it kept "First dance - " in its title.
        self.assertEqual(("Shanghai Bund", "First dance"), (entries[3].title, entries[3].label))

    def test_a_line_parses_the_same_whatever_is_pasted_with_it(self):
        line = "First dance - Shanghai Bund by Frances Yip"
        alone = parse_bulk(line)[0]
        among_plain = parse_bulk("Katy Perry - Teenage Dream\nBruno Mars - Treasure\n" + line)[2]
        self.assertEqual((alone.artist, alone.title, alone.label),
                         (among_plain.artist, among_plain.title, among_plain.label))

    def test_a_version_suffix_is_part_of_the_title_not_a_label(self):
        """Stripping these searched for songs literally called "Single Edit" and "Live"."""
        cases = {
            "Black Betty - Single Edit by Spiderbait": "Black Betty - Single Edit",
            "Mr Brightside - Live by The Killers": "Mr Brightside - Live",
            "Bohemian Rhapsody - Remastered 2011 by Queen": "Bohemian Rhapsody - Remastered 2011",
        }
        for line, title in cases.items():
            with self.subTest(line=line):
                entry = parse_bulk(line)[0]
                self.assertEqual((title, ""), (entry.title, entry.label))

    def test_a_label_in_front_of_a_suffixed_title_is_still_removed(self):
        entry = parse_bulk("First dance - Black Betty - Single Edit by Spiderbait")[0]
        self.assertEqual(("Black Betty - Single Edit", "First dance"), (entry.title, entry.label))

    def test_autocorrected_dashes_split_like_hyphens(self):
        """Phones and Word turn " - " into an en or em dash; those lines weren't split at all."""
        for line in ("Katy Perry – Teenage Dream", "Katy Perry — Teenage Dream"):
            with self.subTest(line=line):
                entry = parse_bulk(line)[0]
                self.assertEqual(("Katy Perry", "Teenage Dream"), (entry.artist, entry.title))

    def test_a_hyphen_inside_a_name_is_not_a_separator(self):
        entry = parse_bulk("Jay-Z - Empire State Of Mind")[0]
        self.assertEqual(("Jay-Z", "Empire State Of Mind"), (entry.artist, entry.title))

    def test_wrapping_quotes_are_removed_but_apostrophes_kept(self):
        entries = parse_bulk('"Shanghai Bund" by Frances Yip\n'
                             "“Mr Strong Man” by George Lam\n"
                             "Nothin' On You by B.o.B")
        self.assertEqual(["Shanghai Bund", "Mr Strong Man", "Nothin' On You"], [e.title for e in entries])

    def test_by_is_split_on_the_last_occurrence(self):
        """"Stand By Me" contains "by"; only the final one separates the artist."""
        entry = parse_bulk("Stand By Me by Ben E. King")[0]
        self.assertEqual(("Ben E. King", "Stand By Me"), (entry.artist, entry.title))

    def test_a_bare_phrase_is_searched_verbatim(self):
        entry = parse_bulk("some obscure b-side")[0]
        self.assertEqual("", entry.artist)
        self.assertEqual("some obscure b-side", entry.query)


class ListHygieneTests(unittest.TestCase):
    def test_numbering_bullets_blanks_and_comments_are_handled(self):
        entries = parse_bulk("1. Katy Perry - Teenage Dream\n"
                             "- Bruno Mars - Treasure\n"
                             "• MKTO - Classic\n"
                             "\n"
                             "# not a song\n")
        self.assertEqual(3, len(entries))
        self.assertEqual(["Katy Perry", "Bruno Mars", "MKTO"], [e.artist for e in entries])

    def test_links_are_kept_whole_rather_than_parsed_as_songs(self):
        """A pasted playlist link must stay a link so it still resolves as a playlist."""
        url = "https://open.spotify.com/playlist/abc123"
        entry = parse_bulk(f"Katy Perry - Teenage Dream\n{url}")[1]
        self.assertEqual("url", entry.kind)
        self.assertEqual(url, entry.url)

    def test_spotify_uri_scheme_also_counts_as_a_link(self):
        self.assertEqual("url", parse_bulk("spotify:track:abc")[0].kind)

    def test_empty_input_yields_nothing(self):
        for text in ("", "   ", "\n\n", "# only a comment"):
            with self.subTest(text=text):
                self.assertEqual([], parse_bulk(text))

    def test_summary_counts_each_kind(self):
        s = summarise(parse_bulk(RUNNING_ORDER + "\nhttps://youtu.be/abc"))
        self.assertEqual({"total": 4, "searches": 3, "urls": 1, "labelled": 3}, s)


def _decision(duration, title="Mr Strong Man", source="soundcloud"):
    return decide_match("George Lam", "Mr Strong Man", 0,
                        Candidate(source=source, url="u", title=title, artist="George Lam",
                                  duration=duration))


class PreviewClipTests(unittest.TestCase):
    """A 30s preview matches artist and title perfectly, so score alone always picks it."""

    def test_a_short_clip_loses_to_a_full_length_alternative(self):
        decisions = [_decision(30), _decision(259, source="youtube_music")]
        preview = is_preview_clip(decisions, 0)
        self.assertTrue(preview(decisions[0]))
        self.assertFalse(preview(decisions[1]))

    def test_a_genuinely_short_song_is_still_downloaded(self):
        """With no long alternative the track really is short — refusing it downloads nothing."""
        decisions = [_decision(30), _decision(40)]
        preview = is_preview_clip(decisions, 0)
        self.assertFalse(any(preview(d) for d in decisions))

    def test_the_guard_stands_down_when_the_real_duration_is_known(self):
        """Spotify tracks carry a duration, and the existing window already rejects previews."""
        decisions = [_decision(30), _decision(259)]
        self.assertFalse(is_preview_clip(decisions, 259)(decisions[0]))

    def test_candidates_without_a_duration_are_not_assumed_to_be_previews(self):
        decisions = [_decision(0), _decision(259)]
        self.assertFalse(is_preview_clip(decisions, 0)(decisions[0]))


class ServiceSelectionTests(unittest.TestCase):
    def test_auto_covers_every_searchable_source(self):
        self.assertEqual(("youtube_music", "youtube", "soundcloud"), SERVICE_SOURCES["auto"])

    def test_a_deliberate_choice_is_not_widened(self):
        self.assertEqual(("soundcloud",), SERVICE_SOURCES["soundcloud"])
        self.assertNotIn("soundcloud", SERVICE_SOURCES["youtube"])

    def test_spotify_is_not_offered_as_a_search_source(self):
        """It has no usable public search (429 QUOTA_EXCEEDED) and serves no audio."""
        self.assertNotIn("spotify", SERVICE_SOURCES)


if __name__ == "__main__":
    unittest.main()
