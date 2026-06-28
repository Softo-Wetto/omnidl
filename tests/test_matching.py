import unittest

from app.matching import Candidate, decide_match


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

    def test_rejects_duration_over_twenty_seconds(self):
        decision = decide_match(
            "Nova", "Midnight Run", 180, candidate(duration=205),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("duration differs by more than 20 seconds", decision.reason)

    def test_unknown_duration_is_rejected_for_review(self):
        decision = decide_match(
            "Nova", "Midnight Run", 0,
            candidate(source="youtube_music", official=True, duration=180),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual("match score below 90", decision.reason)


if __name__ == "__main__":
    unittest.main()
