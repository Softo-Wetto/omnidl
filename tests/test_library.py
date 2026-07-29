import tempfile
import subprocess
import unittest
import wave
from pathlib import Path

from app.library import (
    LibraryIndex,
    LibraryTrack,
    find_duplicate_groups,
    quality_score,
    repair_missing_tags,
    scan_library,
)


def track(
    relative_path: str,
    *,
    artist: str = "Nova",
    title: str = "Midnight Run",
    duration: float = 180,
    bitrate: int = 0,
    sample_rate: int = 44_100,
    codec: str = "mp3",
    size: int = 1_000,
) -> LibraryTrack:
    return LibraryTrack(
        path=Path(relative_path),
        relative_path=relative_path,
        title=title,
        artist=artist,
        album="Night Drive",
        duration=duration,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=2,
        codec=codec,
        size=size,
        has_artwork=True,
        issues=(),
    )


class LibraryScannerTests(unittest.TestCase):
    def test_scans_nested_audio_and_reports_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "nested" / "Nova - Midnight Run.wav"
            path.parent.mkdir()
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(44_100)
                audio.writeframes(b"\0\0\0\0" * 4_410)

            report = scan_library(root)

            self.assertEqual(1, report["summary"]["total_files"])
            self.assertEqual("nested/Nova - Midnight Run.wav", report["tracks"][0]["path"])
            self.assertIn("missing_artist", report["tracks"][0]["issues"])
            self.assertIn("missing_title", report["tracks"][0]["issues"])
            self.assertTrue(report["tracks"][0]["repairable"])

    def test_groups_only_same_identity_with_compatible_duration(self):
        originals = [
            track("a.opus", codec="opus", bitrate=160_000, duration=180),
            track("b.mp3", codec="mp3", bitrate=192_000, duration=182),
            track("extended.mp3", codec="mp3", bitrate=320_000, duration=240),
        ]

        groups = find_duplicate_groups(originals)

        self.assertEqual(1, len(groups))
        self.assertEqual({"a.opus", "b.mp3"}, {item.relative_path for item in groups[0]})

    def test_quality_prefers_lossless_then_efficient_lossy_codecs(self):
        flac = track("song.flac", codec="flac", bitrate=900_000)
        opus = track("song.opus", codec="opus", bitrate=160_000)
        mp3 = track("song.mp3", codec="mp3", bitrate=192_000)

        self.assertGreater(quality_score(flac), quality_score(opus))
        self.assertGreater(quality_score(opus), quality_score(mp3))

    def test_index_finds_cross_format_duplicate_but_rejects_wrong_duration(self):
        existing = track("archive/Nova - Midnight Run.mp3", duration=180)
        index = LibraryIndex([existing])

        self.assertEqual(existing, index.find("Nova", "Midnight Run", 183))
        self.assertIsNone(index.find("Nova", "Midnight Run", 240))

    def test_safe_repair_rejects_ambiguous_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Midnight Run.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(44_100)
                audio.writeframes(b"\0\0\0\0" * 4_410)

            with self.assertRaisesRegex(ValueError, "Artist - Title"):
                repair_missing_tags(root, path.name)
    def test_safe_repair_rejects_path_outside_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "outside"):
                repair_missing_tags(root, "../other.mp3")

    def test_safe_repair_fills_missing_id3_values_without_overwriting_artist(self):
        from mutagen.id3 import ID3, TIT2, TPE1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.wav"
            path = root / "Filename Artist - Midnight Run.mp3"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(44_100)
                audio.writeframes(b"\0\0\0\0" * 4_410)
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(source), str(path)],
                check=True,
            )
            tags = ID3()
            tags.add(TPE1(encoding=3, text=["Existing Artist"]))
            tags.save(path)

            result = repair_missing_tags(root, path.name)
            saved = ID3(path)

            self.assertEqual(["title"], result["changed"])
            self.assertEqual(["Existing Artist"], saved.getall("TPE1")[0].text)
            self.assertEqual(["Midnight Run"], saved.getall("TIT2")[0].text)

if __name__ == "__main__":
    unittest.main()
