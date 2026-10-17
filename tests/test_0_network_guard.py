"""The network guard (network_guard.py) is in force for the whole test run."""
import asyncio
import socket
import unittest

import network_guard  # noqa: F401  — installs the guard


class NetworkGuardTests(unittest.TestCase):
    def test_the_internet_is_out_of_reach(self):
        with self.assertRaises(RuntimeError):
            socket.create_connection(("example.com", 443), timeout=1)

    def test_a_real_download_cant_start(self):
        async def start():
            await asyncio.create_subprocess_exec("yt-dlp", "https://www.youtube.com/watch?v=x")
        with self.assertRaises(RuntimeError):
            asyncio.run(start())
