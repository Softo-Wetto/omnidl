import unittest

from app.matching import (Candidate, choose, clean_title, consensus_duration, decide_match,
                          is_music_video, reference_duration,
                          same_recording, version_markers)


def candidate(**changes):
    data = {
        "source": "youtube",
        "url": "https://example.test/a",
        "title": "Midnight Run",
        "artist": "Nova",
        "duration": 181,
        "official": False,
    }
    data.update(changes)
    return Candidate(**data)


class MatchingTests(unittest.TestCase):
    def test_accepts_exact_official_candidate(self):
        decision = decide_match(
            "Nova", "Midnight Run", 180,
            candidate(source="youtube_music", official=True, duration=182),
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(100, decision.score)

    def test_rejects_wrong_artist_even_when_duration_matches(self):
        decision = decide_match(
            "Nova", "Midnight Run", 180,
            candidate(artist="Different Artist", duration=180),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("artist similarity below 80%", decision.reason)

    def test_rejects_duration_over_ten_seconds(self):
        """20s let radio edits and alternate cuts through as the album track; 10s doesn't."""
        decision = decide_match(
            "Nova", "Midnight Run", 180, candidate(duration=195),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("duration differs by more than 10 seconds", decision.reason)

    def test_unknown_duration_is_rejected_for_review(self):
        decision = decide_match(
            "Nova", "Midnight Run", 0,
            candidate(source="youtube_music", official=True, duration=180),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("match score below 90", decision.reason)


def c(source, title, artist, duration, official=None):
    return Candidate(source, f"https://example.test/{source}/{title}/{duration}", title, artist,
                     duration, source == "youtube_music" if official is None else official)


class CreditsAndDecorationsTests(unittest.TestCase):
    """Real cases where the exact track lost because of text that doesn't name the song."""

    def test_a_featured_artist_in_the_title_is_not_a_title_mismatch(self):
        """Payphone: the exact 232s "(feat. Wiz Khalifa)" track scored 0.48 on title and a
        shorter 223s alternate edit was verified instead."""
        choice = choose("Maroon 5", "Payphone", 231, [
            c("youtube_music", "Payphone", "Maroon 5", 223),
            c("youtube_music", "Payphone (feat. Wiz Khalifa)", "Maroon 5", 232),
        ])
        self.assertEqual(232, choice.eligible[0].candidate.duration)
        self.assertTrue(choice.eligible[0].accepted)

    def test_the_artist_is_read_from_the_title_when_the_uploader_is_someone_else(self):
        """One Dance: every correct copy was uploaded by a lyrics channel, so reading only the
        uploader scored them all as the wrong artist and a 53s-longer outlier won."""
        choice = choose("Drake", "One Dance", 174, [
            c("youtube_music", "One Dance", "Drake", 227),
            c("youtube", "Drake - One Dance (Lyrics) ft. Wizkid & Kyla", "Billion Stars", 175),
        ])
        self.assertEqual(175, choice.eligible[0].candidate.duration)
        self.assertEqual(1, len(choice.eligible))        # 53s off is a different version

    def test_clean_title_handles_the_upload_title_shapes_seen_in_the_wild(self):
        cases = {
            ("Drake - One Dance (Lyrics) ft. Wizkid & Kyla", "Drake"): "One Dance",
            ("One Dance (feat. WizKid & Kyla) - Drake (Official Audio)", "Drake"): "One Dance",
            ("Justin Bieber, Nicki Minaj – Beauty And A Beat", "Justin Bieber"): "Beauty And A Beat",
            ('Tems "Higher" (Live Performance) | Open Mic', "Tems"): "Higher (Live Performance)",
        }
        for (title, artist), expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(expected, clean_title(title, artist)[0])

    def test_a_title_that_merely_contains_a_dash_keeps_its_parts(self):
        self.assertEqual("Sunflower - Spider-Man: Into the Spider-Verse",
                         clean_title("Sunflower - Spider-Man: Into the Spider-Verse", "Post Malone")[0])

    def test_a_multi_artist_credit_matches_its_lead_artist(self):
        d = decide_match("Katy Perry, Snoop Dogg", "California Gurls", 234,
                         c("youtube_music", "California Gurls", "Katy Perry", 234))
        self.assertTrue(d.accepted)


class VersionTests(unittest.TestCase):
    def test_versions_the_request_didnt_ask_for_are_never_eligible(self):
        for title in ("Midnight Run (Sped Up)", "Midnight Run (Live at Wembley)",
                      "Midnight Run (Wideboys Radio Mix)", "Midnight Run - Cover by Someone",
                      "Midnight Run [Nightcore]", "Midnight Run (Acoustic)"):
            with self.subTest(title=title):
                choice = choose("Nova", "Midnight Run", 180, [c("youtube_music", title, "Nova", 180)])
                self.assertEqual([], choice.eligible)
                self.assertTrue(choice.decisions[0].reason.startswith("different version"))

    def test_a_version_the_request_names_is_still_its_own_match(self):
        choice = choose("SZA", "The Weekend - Funk Wav Remix", 170,
                        [c("youtube_music", "The Weekend (Funk Wav Remix)", "SZA", 171)])
        self.assertTrue(choice.eligible and choice.eligible[0].accepted)

    def test_words_that_are_part_of_real_titles_are_not_versions(self):
        self.assertEqual(frozenset(),
                         version_markers("Live Your Life (feat. Rihanna)") - version_markers("Live Your Life"))
        self.assertEqual(frozenset(), version_markers("Cover Me Up") - version_markers("Cover Me Up"))
        self.assertEqual(frozenset(), version_markers("Teenage Dream (Official Music Video)"))
        self.assertEqual(frozenset(), version_markers("Teenage Dream (Cover Art)"))
        self.assertEqual(frozenset(), version_markers("Katy Perry - Teenage Dream [Album Cover]"))
        self.assertIn("cover", version_markers("Teenage Dream (Piano Cover)"))


class ConsensusTests(unittest.TestCase):
    """A typed song has no known length; the uploads of it vote on one instead."""

    def test_the_length_most_copies_agree_on_wins_over_an_exact_title_outlier(self):
        """Beauty And A Beat: a 145s edit with a bare title outranked the real 228s track."""
        choice = choose("Justin Bieber", "Beauty And A Beat", 0, [
            c("youtube_music", "Beauty And A Beat (feat. Nicki Minaj)", "Justin Bieber", 228),
            c("youtube_music", "Beauty And A Beat", "Justin Bieber", 145),
            c("youtube", "Justin Bieber - Beauty And A Beat ft. Nicki Minaj", "Justin Bieber", 229),
            c("youtube", "Justin Bieber - Beauty And A Beat (Official Music Video)", "Justin Bieber", 293),
        ])
        self.assertTrue(choice.anchored)
        self.assertEqual(228, choice.expected_duration)
        self.assertEqual(228, choice.eligible[0].candidate.duration)
        self.assertNotIn(145, [d.candidate.duration for d in choice.eligible])

    def test_one_upload_agreeing_with_itself_is_not_a_consensus(self):
        decisions = [decide_match("Drake", "One Dance", 0, c("youtube", "Drake - One Dance", "Drake", 227)),
                     decide_match("Drake", "One Dance", 0, c("youtube", "Drake - One Dance", "X", 175))]
        self.assertEqual(0, consensus_duration(decisions))

    def test_youtube_musics_own_entry_can_stand_alone_but_is_outvoted_by_agreeing_uploads(self):
        official = c("youtube_music", "One Dance", "Drake", 227)
        alone = [decide_match("Drake", "One Dance", 0, official)]
        self.assertEqual(227, consensus_duration(alone))
        crowd = alone + [decide_match("Drake", "One Dance", 0, c("youtube", "Drake - One Dance", "Drake", d))
                         for d in (174, 174, 175, 173)]
        self.assertEqual(174, consensus_duration(crowd))

    def test_previews_and_other_versions_do_not_vote(self):
        decisions = [decide_match("Nova", "Midnight Run", 0, cand) for cand in (
            c("soundcloud", "Midnight Run", "Nova", 30), c("soundcloud", "Midnight Run", "Nova", 30),
            c("youtube", "Nova - Midnight Run (Sped Up)", "Nova", 140),
            c("youtube", "Nova - Midnight Run (Sped Up)", "Nova", 141))]
        self.assertEqual(0, consensus_duration(decisions))


class SameAnswerEverywhereTests(unittest.TestCase):
    def test_a_playlist_track_and_the_same_song_typed_pick_the_same_recording(self):
        candidates = [
            c("youtube_music", "One Dance", "Drake", 227),
            c("youtube", "Drake - One Dance (Lyrics) ft. Wizkid & Kyla", "Billion Stars", 175),
            c("youtube", "One Dance (feat. WizKid & Kyla) - Drake (Official Audio)", "Dymnd", 177),
            c("youtube", "Drake - One Dance", "DynamicVibes", 175),
            c("soundcloud", "One Dance - Drake (Sped Up)", "USNAVYGEEK", 141),
        ]
        playlist = choose("Drake", "One Dance", 174, candidates).eligible[0].candidate
        typed = choose("Drake", "One Dance", 0, candidates).eligible[0].candidate
        self.assertEqual(playlist.url, typed.url)


class SettingsSwitchTests(unittest.TestCase):
    """Both matching switches in Settings used to do nothing."""

    def test_prefer_youtube_music_off_removes_the_official_bonus(self):
        cand = c("youtube_music", "Midnight Run", "Nova", 181)
        self.assertEqual(105, decide_match("Nova", "Midnight Run", 180, cand).raw_score)
        self.assertEqual(100, decide_match("Nova", "Midnight Run", 180, cand, prefer_official=False).raw_score)

    def test_match_length_off_ignores_the_known_duration(self):
        candidates = [c("youtube_music", "Midnight Run", "Nova", 260)]
        self.assertEqual([], choose("Nova", "Midnight Run", 180, candidates).eligible)
        self.assertEqual(1, len(choose("Nova", "Midnight Run", 180, candidates, use_duration=False).eligible))


class StandInTests(unittest.TestCase):
    """Which candidate may replace the chosen one when it won't download."""

    def _d(self, duration, verified):
        d = decide_match("Nova", "Midnight Run", 180 if verified else 0,
                         c("youtube", "Midnight Run", "Nova", duration))
        self.assertEqual(verified, d.accepted)
        return d

    def test_a_verified_choice_can_only_be_replaced_by_a_verified_one(self):
        self.assertTrue(same_recording(self._d(181, True), self._d(180, True)))
        self.assertFalse(same_recording(self._d(181, False), self._d(180, True)))

    def test_an_unverified_choice_can_only_be_replaced_by_the_same_length(self):
        self.assertTrue(same_recording(self._d(186, False), self._d(180, False)))
        self.assertFalse(same_recording(self._d(145, False), self._d(180, False)))
        self.assertFalse(same_recording(self._d(0, False), self._d(180, False)))


class WrongVersionTests(unittest.TestCase):
    """The wrong-version hardening of 16 Oct 2026 (offline benchmark of 120 real songs)."""

    def test_music_videos_are_recognised_unless_one_was_asked_for(self):
        self.assertTrue(is_music_video("Drake - One Dance (Official Music Video)"))
        self.assertTrue(is_music_video("Shakira - Waka Waka (Video Oficial)"))
        self.assertFalse(is_music_video("One Dance"))
        self.assertFalse(is_music_video("Song (Official Music Video)", "Song (Official Music Video)"))

    def test_a_typed_song_skips_the_music_video_for_the_album_length(self):
        choice = choose("Drake", "One Dance", 0, [
            c("youtube", "Drake - One Dance (Official Music Video)", "Drake", 255),
            c("youtube_music", "One Dance", "Drake", 174),
            c("youtube", "Drake - One Dance (Lyrics)", "Lyric Channel", 175),
        ])
        self.assertEqual(174, choice.eligible[0].candidate.duration)
        self.assertNotIn(255, [d.candidate.duration for d in choice.eligible])

    def test_a_remastered_spotify_title_matches_the_plain_upload(self):
        decision = decide_match("Queen", "Bohemian Rhapsody - Remastered 2011", 355,
                                c("youtube_music", "Bohemian Rhapsody", "Queen", 355))
        self.assertTrue(decision.accepted, decision.reason)

    def test_the_artists_own_upload_sets_the_length_when_nothing_agrees(self):
        decisions = [decide_match("Nova", "Midnight Run", 0, c("youtube", "Midnight Run", "Nova", 201)),
                     decide_match("Nova", "Midnight Run", 0, c("youtube", "Nova - Midnight Run (sped up)", "Fan", 150))]
        self.assertEqual((201, "the artist's upload"), reference_duration(decisions, "Nova", "Midnight Run"))

    def test_the_duration_window_is_ten_seconds(self):
        near = decide_match("Nova", "Midnight Run", 180, candidate(source="youtube_music", official=True, duration=189))
        far = decide_match("Nova", "Midnight Run", 180, candidate(source="youtube_music", official=True, duration=191))
        self.assertTrue(near.accepted, near.reason)
        self.assertFalse(far.accepted)


if __name__ == "__main__":
    unittest.main()
