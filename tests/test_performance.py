import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.main import STATIC_DIR, NoCacheStaticFiles, dashboard


async def response_body(response) -> bytes:
    """Run a Starlette response as ASGI and return the bytes a browser receives."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await response(
        {"type": "http", "method": "GET", "path": "/dashboard", "headers": []},
        receive,
        send,
    )
    return b"".join(message.get("body", b"") for message in messages)


class AssetCachingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_versions_static_assets_while_html_remains_fresh(self):
        response = await dashboard()
        body = (await response_body(response)).decode("utf-8")

        self.assertEqual("no-cache, must-revalidate", response.headers["cache-control"])
        version = re.search(r'/static/css/style\.css\?v=([0-9a-f]+)"', body)
        self.assertIsNotNone(version)
        self.assertIn(f'/static/js/app.js?v={version.group(1)}"', body)

    async def test_current_version_static_assets_are_reused_without_revalidation(self):
        body = (await response_body(await dashboard())).decode("utf-8")
        version = re.search(r'/static/css/style\.css\?v=([0-9a-f]+)"', body)
        self.assertIsNotNone(version)
        static_files = NoCacheStaticFiles(directory=str(STATIC_DIR))
        response = await static_files.get_response(
            "css/style.css",
            {
                "type": "http",
                "method": "GET",
                "path": "/static/css/style.css",
                "headers": [],
                "query_string": f"v={version.group(1)}".encode("ascii"),
            },
        )

        self.assertEqual(
            "public, max-age=31536000, immutable",
            response.headers["cache-control"],
        )


class BrowserRuntimeTests(unittest.TestCase):
    def test_dashboard_startup_and_background_scheduling(self):
        test_file = Path(__file__).parent / "js" / "performance.test.cjs"
        completed = subprocess.run(
            ["node", "--test", str(test_file)],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )

    def test_page_entrance_motion_finishes_within_a_quarter_second(self):
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        if not edge.exists():
            self.skipTest("Microsoft Edge is required for computed-style checks")

        project = Path(__file__).parents[1]
        css = (project / "app/static/css/style.css").read_text(encoding="utf-8")
        css += (project / "app/static/css/site.css").read_text(encoding="utf-8")
        fixture = f"""<!doctype html>
<html class="js"><head><style>{css}</style></head><body>
<div id="top" class="topbar"></div>
<div id="smart" class="smartbar"></div>
<div id="terminal" class="terminal-wrap"></div>
<div id="queue" class="queue-wrap"></div>
<section class="block"><div id="landing" class="card in"></div></section>
<script>
const seconds = (value) => parseFloat(value) || 0;
const dashboard = ["top", "smart", "terminal", "queue"].map((id) => {{
  const style = getComputedStyle(document.getElementById(id));
  return seconds(style.animationDuration) + seconds(style.animationDelay);
}});
const landing = seconds(getComputedStyle(document.getElementById("landing")).transitionDuration);
document.body.dataset.timings = JSON.stringify({{ dashboard, landing }});
</script></body></html>"""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            directory = Path(directory)
            page = directory / "timings.html"
            page.write_text(fixture, encoding="utf-8")
            # Read the result over the DevTools protocol. Edge's --dump-dom used to be enough,
            # but current Edge prints nothing with it (even for a one-line page), which left
            # this test failing on every run without ever measuring anything.
            value = _evaluate_in_edge(edge, page.as_uri(), "document.body.dataset.timings",
                                      directory / "profile")
        self.assertIsNotNone(value, "Edge did not report the computed timings")
        timings = json.loads(value)
        self.assertLessEqual(max(timings["dashboard"]), 0.25)
        self.assertLessEqual(timings["landing"], 0.30)


def _evaluate_in_edge(edge: Path, url: str, expression: str, profile: Path):
    """Open `url` in headless Edge and return `expression` evaluated there, or None."""
    import asyncio
    import socket
    import urllib.request

    import websockets

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
                             f"--remote-debugging-port={port}", f"--user-data-dir={profile}", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def run():
        ws_url = None
        for _ in range(60):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                pages = [t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith("file:")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except OSError:
                pass
            await asyncio.sleep(0.25)
        if not ws_url:
            return None
        async with websockets.connect(ws_url, max_size=10_000_000) as ws:
            for attempt in range(40):
                await ws.send(json.dumps({"id": attempt + 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expression, "returnByValue": True}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == attempt + 1:
                        break
                value = ((msg.get("result") or {}).get("result") or {}).get("value")
                if value:
                    return value
                await asyncio.sleep(0.25)
        return None

    try:
        return asyncio.run(run())
    finally:
        # Edge's renderer and GPU helpers outlive the main process unless the tree is stopped.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    unittest.main()
