import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.main import library_repair, library_scan


class LibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_returns_configured_library_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Nova - Midnight Run.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(44_100)
                audio.writeframes(b"\0\0\0\0" * 4_410)

            with patch("app.main.settings_mod.LOCAL_MODE", True), \
                 patch("app.main.settings_mod.load_settings", return_value={"output_dir": str(root)}):
                response = await library_scan()

            self.assertEqual(1, response["summary"]["total_files"])

    async def test_scan_is_not_available_in_hosted_mode(self):
        with patch("app.main.settings_mod.LOCAL_MODE", False):
            response = await library_scan()

        self.assertEqual(403, response.status_code)

    async def test_repair_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("app.main.settings_mod.LOCAL_MODE", True), \
             patch("app.main.settings_mod.load_settings", return_value={"output_dir": tmp}):
            response = await library_repair({"path": "../outside.mp3"})

        self.assertEqual(400, response.status_code)
        self.assertIn("outside", json.loads(response.body)["error"])


if __name__ == "__main__":
    unittest.main()
