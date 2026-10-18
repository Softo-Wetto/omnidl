"""Browser checks: failed requests, reordered responses, edits during saves, keyboard focus,
and layouts at phone-to-desktop widths (both Codex reviews, docs/audits/).

Runs an isolated copy of OmniDL (its own temporary config, history and library; no tunnel) and
drives headless Edge over the DevTools protocol. Every endpoint that could download, write tags
or touch the PC is intercepted and answered with a fake reply — nothing real is downloaded or
changed, and your settings and library are never used.

    python tests/browser/checks.py [--shots FOLDER]

Exits 0 only if every check passes. Part of `python tests/run_all.py`.
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import socket
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

SRC = Path(__file__).resolve().parents[2]
HERE = Path(tempfile.mkdtemp(prefix="omnidl-browser-checks-"))
BOX = HERE / "sandbox"
SHOTS = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else HERE / "shots"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
DEBUG_PORT = _free_port()
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
EDGE = next((path for path in EDGE_CANDIDATES if Path(path).exists()), None)
BASE = f"http://127.0.0.1:{PORT}"

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ---------------------------------------------------------------------------------------------
def build_sandbox():
    if BOX.exists():
        shutil.rmtree(BOX, ignore_errors=True)
    (BOX / "music").mkdir(parents=True)
    shutil.copytree(SRC / "app", BOX / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (BOX / "afile.txt").write_text("not a folder")
    (BOX / "config.json").write_text(json.dumps({"output_dir": str(BOX / "music"), "audio_format": "opus"}))
    now = time.time()
    music = BOX / "music"
    for name in ("Drake - One Dance.opus", "Nova - Road 1.opus", "Nova - Road 2.mp3"):
        (music / name).write_bytes(b"x")

    def rec(id_, **kw):
        base = {"id": id_, "input": id_, "kind": "search", "engine": "youtube", "engine_label": "Song search",
                "tool": "all services", "status": "done", "code": 0, "created": now - 600, "started": now - 590,
                "finished": now - 500, "progress": 100, "label": "", "media_type": "audio",
                "audio_format": "opus", "output": "", "produced": [], "failed_tracks": [], "counts": None,
                "list_name": "", "bulk_service": "auto", "tracks": None, "session": "", "report_path": None,
                "out_dir": None, "review_count": 0, "note": ""}
        base.update(kw)
        return base

    history = [
        rec("jobD", input="Old cancelled thing", status="cancelled", created=now - 3 * 86400,
            started=now - 3 * 86400, finished=now - 3 * 86400 + 60, output="D_LOG\r\n"),
        rec("jobC", input="https://www.youtube.com/watch?v=x", kind="command", status="error", code=1,
            note="YouTube bot check - cookies needed", output="C_LOG\r\n"),
        rec("jobB", input="https://open.spotify.com/playlist/x", kind="spotify", engine="spotify",
            engine_label="Spotify", list_name="Road trip", output="B_SELECTED_LOG\r\n",
            produced=[str(music / "Nova - Road 1.opus"), str(music / "Nova - Road 2.mp3")],
            counts={"downloaded": 8, "review": 0, "skipped": 2, "failed": 3, "total": 13},
            failed_tracks=[{"artist": "Nova", "title": f"Miss {i}", "artists": ["Nova"], "duration": 0} for i in range(3)]),
        rec("jobA", input="Drake - One Dance", output="A_LOG line 1\r\nA_LOG line 2\r\n",
            produced=[str(music / "Drake - One Dance.opus")],
            counts={"downloaded": 1, "review": 0, "skipped": 0, "failed": 0, "total": 1}),
    ]
    (BOX / "history.json").write_text(json.dumps(history))


def start_server():
    env = dict(os.environ, OMNIDL_NO_TUNNEL="1", PYTHONUTF8="1", OMNIDL_LOCAL="1")
    log = open(HERE / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", str(PORT)], cwd=BOX, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/api/instance", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    raise SystemExit("server didn't start")


def start_edge():
    profile = HERE / "profile"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}",
                             f"--user-data-dir={profile}", "--no-first-run", "--disable-extensions",
                             "--window-size=1440,900", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1).read())
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("edge didn't start")


# ---------------------------------------------------------------------------------------------
DANGEROUS = ("/api/download", "/api/bulk/download", "/retry", "/api/library/repair",
             "/api/maintenance", "/api/open-folder", "/api/instance/shutdown", "/cancel")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.pending = {}
        self.rules = []          # (predicate, queue)
        self.seen = []           # every intercepted (method, url)
        self.console = []

    async def run(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg:
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == "Fetch.requestPaused":
                asyncio.create_task(self.on_paused(msg["params"]))
            elif msg.get("method") == "Runtime.exceptionThrown":
                self.console.append(("exception", json.dumps(msg["params"]["exceptionDetails"])[:400]))
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                self.console.append(("console.error", str(msg["params"]["args"])[:300]))

    async def send(self, method, **params):
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        msg = await asyncio.wait_for(fut, 30)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def eval(self, expr):
        # Statements that declare names run in their own block so repeated evals don't collide.
        # Promises are NOT awaited: an action that sends a request we're holding would deadlock.
        if "const " in expr and not expr.lstrip().startswith("(") and not expr.lstrip().startswith("["):
            expr = "{" + expr + "}"
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=False, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"eval failed: {expr[:80]} -> {r['exceptionDetails'].get('exception', {}).get('description')}")
        return r["result"].get("value")

    async def on_paused(self, p):
        url, method = p["request"]["url"], p["request"]["method"]
        self.seen.append((method, url))
        for pred, queue in self.rules:
            if pred(method, url):
                await queue.put(p)
                return
        path = url.split(str(PORT), 1)[-1]
        if method != "GET" and any(d in path for d in DANGEROUS):
            # Never let a download/tag write/process action reach even the sandbox server.
            await self.fulfill(p, 200, {"id": "fake", "status": "queued", "engine_label": "Song search",
                                        "ok": True, "jobs": [], "tracks": 0, "links": 0})
            return
        await self.send("Fetch.continueRequest", requestId=p["requestId"])

    def hold(self, pred):
        queue = asyncio.Queue()
        self.rules.append((pred, queue))
        return queue

    def release_rules(self):
        self.rules.clear()

    async def fulfill(self, p, status, body, content_type="application/json"):
        raw = body if isinstance(body, (bytes, str)) and content_type != "application/json" else json.dumps(body)
        if isinstance(raw, str):
            raw = raw.encode()
        await self.send("Fetch.fulfillRequest", requestId=p["requestId"], responseCode=status,
                        responseHeaders=[{"name": "Content-Type", "value": content_type}],
                        body=base64.b64encode(raw).decode())

    async def proceed(self, p):
        await self.send("Fetch.continueRequest", requestId=p["requestId"])

    async def fail(self, p, reason="InternetDisconnected"):
        await self.send("Fetch.failRequest", requestId=p["requestId"], errorReason=reason)

    async def key(self, key, code=None, vk=None, text=None, shift=False):
        params = {"type": "keyDown", "key": key, "code": code or key, "windowsVirtualKeyCode": vk or 0,
                  "modifiers": 8 if shift else 0}
        if text:
            params["text"] = text
        await self.send("Input.dispatchKeyEvent", **params)
        params = {k: v for k, v in params.items() if k != "text"}
        params["type"] = "keyUp"
        await self.send("Input.dispatchKeyEvent", **params)

    async def tab(self, shift=False):
        await self.key("Tab", "Tab", 9, shift=shift)

    async def enter(self):
        await self.key("Enter", "Enter", 13, text="\r")

    async def escape(self):
        await self.key("Escape", "Escape", 27)


async def settle(cdp, ms=150):
    await asyncio.sleep(ms / 1000)


async def load(cdp, width=1440, height=900, mobile=False):
    await cdp.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                   deviceScaleFactor=1, mobile=mobile)
    await cdp.send("Page.navigate", url=BASE + "/dashboard")
    for _ in range(300):
        await asyncio.sleep(0.1)
        try:
            if await cdp.eval("typeof state !== 'undefined' && state.loaded && state.connected"):
                break
        except RuntimeError:
            pass
    else:
        raise RuntimeError("dashboard didn't finish loading in 30s")
    await settle(cdp, 300)


async def active_id(cdp):
    return await cdp.eval("document.activeElement && (document.activeElement.id || document.activeElement.className)")


def is_path(fragment, method=None):
    return lambda m, u: fragment in u and (method is None or m == method)


# ---------------------------------------------------------------------------------------------
async def failed_requests(cdp):
    await load(cdp)
    # D05: settings save that fails (HTML 500) changes nothing.
    q = cdp.hold(is_path("/api/settings", "POST"))
    await cdp.eval("openSettings(); document.querySelector('#set-format').value = 'mp3'; "
                   "document.querySelector('#set-format').dispatchEvent(new Event('change', {bubbles:true}))")
    await cdp.eval("document.querySelector('#save-settings').click()")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.fulfill(p, 500, "<html><body>Internal Server Error</body></html>", "text/html")
    await settle(cdp, 300)
    status = await cdp.eval("document.querySelector('#settings-status').textContent")
    fmt = await cdp.eval("state.settings.audio_format")
    check("D05 failed settings save says 'Not saved' and keeps the last good settings",
          status == "Not saved" and fmt == "opus", f"status={status!r} fmt={fmt!r}")
    # Offline save: same.
    await cdp.eval("document.querySelector('#save-settings').click()")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.fail(p)
    await settle(cdp, 300)
    status = await cdp.eval("document.querySelector('#settings-status').textContent")
    toasts = await cdp.eval("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ')")
    check("D05 offline settings save is reported, never 'Settings saved'",
          status == "Not saved" and "Settings saved" not in toasts and "Can't reach" in toasts, f"{status!r} {toasts!r}")
    cdp.release_rules()
    # D11 end-to-end against the sandbox server: a folder under a file is refused inline.
    bad = str(BOX / "afile.txt" / "sub").replace("\\", "\\\\")
    await cdp.eval(f"document.querySelector('#set-output-dir').value = '{bad}'; document.querySelector('#save-settings').click()")
    await settle(cdp, 800)
    err = await cdp.eval("document.querySelector('#output-dir-error').hidden ? '' : document.querySelector('#output-dir-error').textContent")
    invalid = await cdp.eval("document.querySelector('#set-output-dir').getAttribute('aria-invalid')")
    saved = json.loads((BOX / "config.json").read_text())["output_dir"]
    check("D11 an output folder under a file is refused next to the field and not saved",
          bool(err) and invalid == "true" and saved == str(BOX / "music"), f"err={err!r} saved={saved!r}")
    await cdp.escape()

    # D05: Download with an HTML 500, a 429 and offline keeps the input; a good reply clears it.
    for label, respond in (
        ("HTML 500", lambda p: cdp.fulfill(p, 500, "<html>oops</html>", "text/html")),
        ("429", lambda p: cdp.fulfill(p, 429, {"error": "Too many active downloads (max 3)."})),
        ("offline", lambda p: cdp.fail(p)),
        ("malformed 200", lambda p: cdp.fulfill(p, 200, "<html>login</html>", "text/html")),
    ):
        q = cdp.hold(is_path("/api/download", "POST"))
        await cdp.eval("document.querySelector('#input').value = 'Nova - Midnight Run'; updateChip(); submitDownload()")
        p = await asyncio.wait_for(q.get(), 5)
        await respond(p)
        await settle(cdp, 250)
        value = await cdp.eval("document.querySelector('#input').value")
        check(f"D05 Download with {label} keeps what was typed", value == "Nova - Midnight Run", value)
        cdp.release_rules()
    q = cdp.hold(is_path("/api/download", "POST"))
    await cdp.eval("submitDownload()")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.fulfill(p, 200, {"id": "new1", "status": "queued", "engine_label": "Song search"})
    await settle(cdp, 250)
    check("D05 a confirmed job clears the input", await cdp.eval("document.querySelector('#input').value") == "")
    cdp.release_rules()

    # D06: double click and repeated Enter send one request.
    q = cdp.hold(is_path("/api/download", "POST"))
    await cdp.eval("document.querySelector('#input').value = 'Nova - Twice'; document.querySelector('#download').click(); "
                   "document.querySelector('#download').click()")
    await cdp.send("Runtime.evaluate", expression="document.querySelector('#input').focus()")
    await cdp.enter()
    await cdp.enter()
    await settle(cdp, 400)
    held = q.qsize()
    disabled = await cdp.eval("document.querySelector('#download').disabled")
    check("D06 double click + repeated Enter send exactly one request, button disabled meanwhile",
          held == 1 and disabled, f"requests={held} disabled={disabled}")
    first = await q.get()
    body = json.loads(first["request"].get("postData") or "{}")
    await cdp.fail(first)                      # lost reply: did it get through?
    await settle(cdp, 250)
    await cdp.eval("submitDownload()")
    again = await asyncio.wait_for(q.get(), 5)
    body2 = json.loads(again["request"].get("postData") or "{}")
    check("D06 re-sending after a lost reply reuses the submission token",
          body.get("client_token") and body.get("client_token") == body2.get("client_token"))
    await cdp.fulfill(again, 200, {"id": "new2", "status": "queued", "engine_label": "Song search"})
    await settle(cdp, 200)
    cdp.release_rules()

    # D05: Cancel all counts only confirmed cancellations.
    await cdp.eval("""handleMessage({type:'job_added', job:{id:'r1', status:'running', input:'one', kind:'search', engine:'youtube', engine_label:'Song search', tool:'all services', created: Date.now()/1000}});
                      handleMessage({type:'job_added', job:{id:'r2', status:'queued', input:'two', kind:'search', engine:'youtube', engine_label:'Song search', tool:'all services', created: Date.now()/1000}});""")
    q = cdp.hold(is_path("/cancel", "POST"))
    await cdp.eval("document.querySelector('#cancel-all').click()")
    await settle(cdp, 200)
    armed = await cdp.eval("document.querySelector('#cancel-all').textContent")
    check("UI7 Cancel all asks for a second press first", q.qsize() == 0 and "Confirm" in armed, armed)
    await cdp.eval("document.querySelector('#cancel-all').click()")
    p1 = await asyncio.wait_for(q.get(), 5)
    p2 = await asyncio.wait_for(q.get(), 5)
    await cdp.fulfill(p1, 200, {"ok": True})
    await cdp.fulfill(p2, 500, {"error": "boom"})
    await settle(cdp, 300)
    toasts = await cdp.eval("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ')")
    check("D05 Cancel all reports 1 cancelled and 1 failure, not 2 cancelled",
          "Cancelled 1 download" in toasts and "couldn't be cancelled" in toasts, toasts)
    cdp.release_rules()

    # D05: startup with a failing /api/meta shows Retry and recovers.
    q = cdp.hold(is_path("/api/meta", "GET"))
    await cdp.send("Page.navigate", url=BASE + "/dashboard")
    p = await asyncio.wait_for(q.get(), 10)
    await cdp.fulfill(p, 500, "<html>bad gateway</html>", "text/html")
    await settle(cdp, 800)
    banner = await cdp.eval("document.querySelector('#conn-banner').hidden ? '' : document.querySelector('#conn-banner').textContent")
    retry = await cdp.eval("!document.querySelector('#conn-banner-retry').hidden")
    formats = await cdp.eval("document.querySelectorAll('#format option').length")
    connected = await cdp.eval("state.connected")
    check("D05 a failed startup request shows Retry, keeps working controls and still connects",
          "Couldn't load" in banner and retry and formats >= 6 and connected, f"{banner!r} {retry} {formats} {connected}")
    cdp.release_rules()
    while not q.empty():                    # the reconnect's own startup attempt, held above
        await cdp.proceed(await q.get())
    await settle(cdp, 500)
    await cdp.eval("document.querySelector('#conn-banner-retry').click()")
    await settle(cdp, 800)
    check("D05 Retry recovers", await cdp.eval("state.loaded && document.querySelector('#conn-banner').hidden"))
    errors = [c for c in cdp.console if c[0] == "exception"]
    check("no uncaught exceptions during failure checks", not errors, str(errors[:2]))


async def reordered(cdp):
    await load(cdp)
    # D04: Download is disabled until the new text is parsed; an old reply can't win.
    await cdp.eval("openBulk()")
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'Old artist - Old song'; t.dispatchEvent(new Event('input'))")
    await settle(cdp, 800)
    ready_old = await cdp.eval("!document.querySelector('#do-bulk').disabled")
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'New artist - New song'; t.dispatchEvent(new Event('input'))")
    disabled_now = await cdp.eval("document.querySelector('#do-bulk').disabled")
    sent = cdp.hold(is_path("/api/bulk/download", "POST"))
    await cdp.eval("document.querySelector('#do-bulk').click()")
    await settle(cdp, 100)
    check("D04 Download is disabled before the new text is parsed (old list can't be sent)",
          ready_old and disabled_now and sent.qsize() == 0, f"{ready_old} {disabled_now} {sent.qsize()}")
    await settle(cdp, 800)
    await cdp.eval("document.querySelector('#do-bulk').click()")
    p = await asyncio.wait_for(sent.get(), 5)
    entries = json.loads(p["request"]["postData"])["entries"]
    await cdp.fulfill(p, 200, {"jobs": [{"id": "b1", "status": "queued"}], "tracks": 1, "links": 0,
                               "links_held_back": 0, "held_back_urls": []})
    check("D04 the list sent is the current one", entries == [{"kind": "search", "artist": "New artist", "title": "New song"}], str(entries))
    cdp.release_rules()
    await settle(cdp, 300)

    # Reversed preview replies: the second text wins.
    await cdp.eval("openBulk()")
    q = cdp.hold(is_path("/api/bulk/preview", "POST"))
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'First - A'; t.dispatchEvent(new Event('input'))")
    first = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'Second - B'; t.dispatchEvent(new Event('input'))")
    second = await asyncio.wait_for(q.get(), 5)
    await cdp.proceed(second)
    await settle(cdp, 300)
    await cdp.proceed(first)
    await settle(cdp, 400)
    shown = await cdp.eval("[...document.querySelectorAll('#bulk-preview .bulk-artist')].map(i => i.value).join(',')")
    check("D04 a late reply for older text doesn't replace the current preview", shown == "Second", shown)
    # Edits survive a service change and a re-parse.
    await cdp.eval("const a = document.querySelector('.bulk-artist'); a.value = 'Second Edited'; a.dispatchEvent(new Event('input'))")
    await cdp.eval("const s = document.querySelector('#bulk-service'); s.value = 'soundcloud'; s.dispatchEvent(new Event('change'))")
    await settle(cdp, 300)
    q2 = q.qsize()
    kept = await cdp.eval("document.querySelector('.bulk-artist').value")
    check("D04 changing the service keeps row edits and doesn't re-parse", kept == "Second Edited" and q2 == 0, f"{kept} {q2}")
    cdp.release_rules()
    # Clearing during a request.
    q = cdp.hold(is_path("/api/bulk/preview", "POST"))
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'Third - C'; t.dispatchEvent(new Event('input'))")
    pending = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = ''; t.dispatchEvent(new Event('input'))")
    await settle(cdp, 400)
    await cdp.proceed(pending)
    await settle(cdp, 400)
    rows = await cdp.eval("document.querySelectorAll('#bulk-preview .bulk-row').length")
    disabled = await cdp.eval("document.querySelector('#do-bulk').disabled")
    check("D04 clearing the text during a request leaves nothing to download", rows == 0 and disabled, f"{rows} {disabled}")
    cdp.release_rules()
    # Draft survives closing and a reload.
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'Keep - Me'; t.dispatchEvent(new Event('input'))")
    await settle(cdp, 700)
    await cdp.escape()
    closed = await cdp.eval("document.querySelector('#bulk-modal').classList.contains('hidden')")
    await load(cdp)
    await cdp.eval("openBulk()")
    await settle(cdp, 700)
    text = await cdp.eval("document.querySelector('#bulk-text').value")
    check("UI5 Escape closes Bulk and the draft survives a reload", closed and text == "Keep - Me", f"{closed} {text!r}")
    await cdp.eval("clearBulk(); closeBulk()")

    # D07: logs never mix (A -> B with A answering last, then A -> B -> A).
    q = cdp.hold(lambda m, u: "/api/jobs/" in u and u.endswith("/output"))
    await cdp.eval("viewJob('jobA')")
    a = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("viewJob('jobB')")
    b = await asyncio.wait_for(q.get(), 5)
    await cdp.proceed(b)
    await settle(cdp, 200)
    await cdp.proceed(a)
    await settle(cdp, 300)
    log = await cdp.eval("logView.copy()")
    check("D07 a slow log for A never lands in B", "B_SELECTED_LOG" in log and "A_LOG" not in log, log)
    await cdp.eval("viewJob('jobA')")
    a1 = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("viewJob('jobB')")
    b1 = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("viewJob('jobA')")
    a2 = await asyncio.wait_for(q.get(), 5)
    await cdp.proceed(a2)
    await settle(cdp, 150)
    await cdp.proceed(b1)
    await cdp.proceed(a1)
    await settle(cdp, 300)
    log = await cdp.eval("logView.copy()")
    check("D07 A -> B -> A shows A's log once", log.count("A_LOG line 1") == 1 and "B_SELECTED" not in log, log)
    # Live output while the snapshot loads: shown once, in order.
    await cdp.eval("viewJob('jobB')")
    snap = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("handleMessage({type:'output', job_id:'jobB', data:'B_SELECTED_LOG\\r\\n', end:16}); "
                   "handleMessage({type:'output', job_id:'jobB', data:'LIVE_AFTER\\r\\n', end:28}); "
                   "handleMessage({type:'output', job_id:'jobA', data:'NOT_B\\r\\n', end:99})")
    await cdp.fulfill(snap, 200, {"id": "jobB", "output": "B_SELECTED_LOG\r\n", "status": "done", "end": 16})
    await settle(cdp, 300)
    log = await cdp.eval("logView.copy()")
    check("D07 live lines during the snapshot are merged once and in order",
          log.count("B_SELECTED_LOG") == 1 and log.index("B_SELECTED_LOG") < log.index("LIVE_AFTER") and "NOT_B" not in log, log)
    # HTTP failure for the log.
    await cdp.eval("viewJob('jobC')")
    c = await asyncio.wait_for(q.get(), 5)
    await cdp.fulfill(c, 500, {"error": "boom"})
    await settle(cdp, 300)
    log = await cdp.eval("logView.copy()")
    check("D07 a failed log load says so", "Couldn't load this job's log" in log, log)
    cdp.release_rules()


async def keyboard(cdp):
    await load(cdp)
    await cdp.eval("document.querySelector('#open-settings').focus()")
    await cdp.enter()
    await settle(cdp, 300)
    inside = await cdp.eval("document.querySelector('#settings-modal .modal-box').contains(document.activeElement)")
    role = await cdp.eval("document.querySelector('#settings-modal .modal-box').getAttribute('role')")
    check("D13 Enter opens Settings as a dialog with focus inside", inside and role == "dialog", f"{inside} {role}")
    escaped = False
    for i in range(60):
        await cdp.tab(shift=(i % 7 == 6))
        if not await cdp.eval("document.querySelector('#settings-modal .modal-box').contains(document.activeElement)"):
            escaped = True
            break
    check("D13 Tab and Shift+Tab stay inside the dialog", not escaped)
    await cdp.escape()
    await settle(cdp, 200)
    check("D13 Escape closes Settings and focus returns to its button",
          await cdp.eval("document.querySelector('#settings-modal').classList.contains('hidden')")
          and await active_id(cdp) == "open-settings", await active_id(cdp))
    for opener, modal in (("#open-bulk", "#bulk-modal"), ("#open-library", "#library-modal")):
        await cdp.eval(f"document.querySelector('{opener}').focus()")
        await cdp.enter()
        await settle(cdp, 400)
        opened = await cdp.eval(f"!document.querySelector('{modal}').classList.contains('hidden')")
        await cdp.escape()
        await settle(cdp, 200)
        closed = await cdp.eval(f"document.querySelector('{modal}').classList.contains('hidden')")
        check(f"D13 {modal} opens from the keyboard and Escape closes it",
              opened and closed and await active_id(cdp) == opener[1:], f"{opened} {closed} {await active_id(cdp)}")
    inert = await cdp.eval("document.querySelector('#workspace').inert")
    check("D13 the page behind is interactive again after closing", inert is False)
    # Queue rows: reachable and operable with Enter and Space, selection exposed.
    q = cdp.hold(lambda m, u: u.endswith("/output"))
    await cdp.eval("document.querySelector('.job[data-id=\"jobC\"] .job-open').focus()")
    await cdp.enter()
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.proceed(p)
    await settle(cdp, 300)
    pressed = await cdp.eval("document.querySelector('.job[data-id=\"jobC\"] .job-open').getAttribute('aria-pressed')")
    check("D13 Enter on a queue row shows that job and marks it selected",
          await cdp.eval("state.viewingId") == "jobC" and pressed == "true", pressed)
    await cdp.eval("document.querySelector('.job[data-id=\"jobD\"] .job-open').focus()")
    await cdp.key(" ", "Space", 32, text=" ")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.proceed(p)
    await settle(cdp, 300)
    check("D13 Space works too", await cdp.eval("state.viewingId") == "jobD")
    cdp.release_rules()
    labels = await cdp.eval("""(() => {
        const input = document.querySelector('#input');
        const label = document.querySelector('label[for=input]');
        const pressed = [...document.querySelectorAll('#media-toggle .mt-btn')].map(b => b.getAttribute('aria-pressed'));
        return {label: label && label.textContent, pressed, live: document.querySelector('#toasts').getAttribute('aria-live')};
    })()""")
    check("D13 input label, Audio/Video state and live toasts are exposed",
          labels["label"] and labels["pressed"] == ["true", "false"] and labels["live"] == "polite", str(labels))
    await cdp.eval("document.querySelector('[data-media=video]').click()")
    pressed = await cdp.eval("[...document.querySelectorAll('#media-toggle .mt-btn')].map(b => b.getAttribute('aria-pressed'))")
    check("D13 aria-pressed follows the Audio/Video choice", pressed == ["false", "true"], str(pressed))
    await cdp.eval("document.querySelector('[data-media=audio]').click()")
    focus_style = await cdp.eval("(() => { document.querySelector('#download').focus(); "
                                 "return getComputedStyle(document.querySelector('#download')).outlineStyle; })()")
    check("UI4 keyboard focus is visible", focus_style not in ("none", ""), focus_style)


async def results_and_library(cdp):
    await load(cdp)
    card = await cdp.eval("""(() => {
        const li = document.querySelector('.job[data-id="jobB"]');
        return {badge: li.querySelector('.badge').textContent, title: li.querySelector('.job-input').textContent,
                facts: (li.querySelector('.job-facts')||{}).textContent, buttons: [...li.querySelectorAll('button')].map(b => b.textContent)};
    })()""")
    check("D09/UI3 a partly failed playlist shows 'Completed with issues', its name, counts and Retry failed",
          card["badge"] == "Completed with issues" and card["title"] == "Road trip"
          and "3 not found" in (card["facts"] or "") and any("Retry 3 failed" in b for b in card["buttons"]), str(card))
    await cdp.eval("state.meta.remote = true; renderQueue()")
    saves = await cdp.eval("[...document.querySelectorAll('.save-btn')].map(b => b.textContent)")
    check("D08 Save is offered with the file count for a partial result (remote)",
          any("Save available files (2)" in s for s in saves), str(saves))
    help_btn = await cdp.eval("!!document.querySelector('.job[data-id=\"jobC\"] .btn-help')")
    check("UI3 a cookie/bot-check failure offers 'How to fix'", help_btn)
    when = await cdp.eval("document.querySelector('.job[data-id=\"jobD\"] .tool-tag').textContent")
    check("UI7 older jobs show a date", any(m in when for m in ("Oct", "/")), when)
    await cdp.eval("state.meta.remote = false; renderQueue()")
    await cdp.eval("const f = document.querySelector('#history-filter'); f.value = 'issues'; f.dispatchEvent(new Event('change'))")
    shown = await cdp.eval("[...document.querySelectorAll('#queue .job')].map(li => li.dataset.id)")
    check("UI7 history can be filtered by outcome", shown == ["jobB"], str(shown))
    await cdp.eval("const f = document.querySelector('#history-filter'); f.value = 'all'; f.dispatchEvent(new Event('change'))")

    # D14 with a mocked scan containing problems.
    report = {"root": str(BOX / "music"), "order": "artist-title",
              "summary": {"total_files": 3, "total_size": 3, "files_with_issues": 1, "repairable_files": 1,
                          "duplicate_groups": 0, "duplicate_files": 0, "reclaimable_size": 0},
              "tracks": [{"path": "Song - Nova.mp3", "title": "", "artist": "", "album": "", "display_title": "Nova",
                          "display_artist": "Song", "duration": 180, "bitrate": 0, "sample_rate": 0, "channels": 0,
                          "codec": "mp3", "size": 1, "has_artwork": False,
                          "issues": ["missing_artist", "missing_title"], "repairable": True, "quality_score": 60}],
              "duplicate_groups": []}
    q = cdp.hold(is_path("/api/library/scan", "POST"))
    await cdp.eval("openLibrary()")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.fulfill(p, 200, report)
    await settle(cdp, 300)
    await cdp.eval("const s = document.querySelector('#library-search'); s.value = 'zzz'; s.dispatchEvent(new Event('input'))")
    title = await cdp.eval("document.querySelector('#library-empty-title').textContent")
    clear = await cdp.eval("!document.querySelector('#library-clear-filter').hidden")
    check("D14 a filter with no matches says so (not 'clean') and offers Clear filter",
          title == "No matching results." and clear, title)
    await cdp.eval("document.querySelector('#library-clear-filter').click()")
    count = await cdp.eval("document.querySelectorAll('#library-issues .issue-card').length")
    check("D14 Clear filter brings the problems back", count == 1)
    # D02: the repair shows what it will write; switching the order swaps it; nothing is written until Save.
    prev = cdp.hold(is_path("/api/library/repair/preview", "POST"))
    write = cdp.hold(is_path("/api/library/repair", "POST"))
    await cdp.eval("document.querySelector('.library-repair').click()")
    p = await asyncio.wait_for(prev.get(), 5)
    await cdp.fulfill(p, 200, {"path": "Song - Nova.mp3", "filename": "Song - Nova.mp3", "order": "artist-title",
                               "current": {"artist": "", "title": ""}, "proposed": {"artist": "Song", "title": "Nova"},
                               "problem": ""})
    await settle(cdp, 200)
    values = await cdp.eval("[...document.querySelectorAll('.repair-field input')].map(i => i.value)")
    check("D02 repair shows the proposed tags before writing anything", values == ["Song", "Nova"] and write.qsize() == 0, str(values))
    await cdp.eval("const s = document.querySelector('.repair-order select'); s.value = 'title-artist'; s.dispatchEvent(new Event('change'))")
    p = await asyncio.wait_for(prev.get(), 5)
    order = json.loads(p["request"]["postData"])["order"]
    await cdp.fulfill(p, 200, {"path": "Song - Nova.mp3", "filename": "Song - Nova.mp3", "order": "title-artist",
                               "current": {"artist": "", "title": ""}, "proposed": {"artist": "Nova", "title": "Song"},
                               "problem": ""})
    await settle(cdp, 200)
    values = await cdp.eval("[...document.querySelectorAll('.repair-field input')].map(i => i.value)")
    check("D02 choosing 'Title - Artist' swaps the proposal", order == "title-artist" and values == ["Nova", "Song"], str(values))
    await cdp.eval("[...document.querySelectorAll('.repair-actions button')].find(b => b.textContent === 'Save tags').click()")
    p = await asyncio.wait_for(write.get(), 5)
    sent = json.loads(p["request"]["postData"])
    await cdp.fulfill(p, 200, {"ok": True, "changed": ["artist", "title"], "path": "Song - Nova.mp3"})
    check("D02 Save sends exactly the confirmed values and order",
          sent == {"path": "Song - Nova.mp3", "order": "title-artist", "artist": "Nova", "title": "Song"}, str(sent))
    await settle(cdp, 300)
    cdp.release_rules()
    await cdp.escape()


async def layouts(cdp):
    SHOTS.mkdir(exist_ok=True)
    for width, height in ((320, 740), (360, 780), (390, 844), (768, 1024), (820, 1180), (1024, 768), (1440, 900)):
        mobile = width < 800
        await load(cdp, width, height, mobile)
        await cdp.eval("""handleMessage({type:'job_added', job:{id:'live1', status:'running', input:'https://open.spotify.com/playlist/y',
            kind:'spotify', list_name:'Friday mix', engine:'spotify', engine_label:'Spotify', tool:'embed → yt-dlp', progress: 42,
            label:'Nova - Midnight Run', created: Date.now()/1000, started: Date.now()/1000}}); renderQueue();""")
        await settle(cdp, 300)
        m = await cdp.eval("""(() => {
            const visibleAction = () => [...document.querySelectorAll('.queue-wrap .job-actions button')]
                .find((b) => b.getBoundingClientRect().height > 0);
            const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect();
                return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height),
                        shown: getComputedStyle(e).display !== 'none' && !e.hidden && b.width > 0}; };
            return {iw: innerWidth, sw: document.documentElement.scrollWidth, input: r('#input'), engine: r('#engine'),
                    format: r('#format'), media: r('#media-toggle'), download: r('#download'), bulk: r('#open-bulk'),
                    now: r('#now-card'), queue: r('.queue-wrap'), term: r('#terminal-wrap'), splitter: r('#splitter'),
                    actionFont: parseFloat(getComputedStyle(visibleAction()).fontSize),
                    actionH: Math.round(visibleAction().getBoundingClientRect().height)};
        })()""")
        tag = f"{width}px"
        problems = []
        if m["sw"] > m["iw"]:
            problems.append(f"horizontal scroll {m['sw']}>{m['iw']}")
        for name in ("input", "engine", "format", "media", "download", "bulk"):
            box = m[name]
            if box["x"] < 0 or box["x"] + box["w"] > m["iw"] + 1:
                problems.append(f"{name} off-screen {box}")
        if m["input"]["w"] < (200 if width < 360 else 240):
            problems.append(f"input only {m['input']['w']}px")
        if m["engine"]["w"] < 110:
            problems.append(f"source menu only {m['engine']['w']}px")
        if m["format"]["w"] < 70:
            problems.append(f"format menu only {m['format']['w']}px")
        if m["actionFont"] < 12 or m["actionH"] < 30:
            problems.append(f"small actions {m['actionFont']}px/{m['actionH']}px")
        if width <= 760:
            if not m["now"]["shown"]:
                problems.append("no current-download card")
            elif not (m["now"]["y"] < m["queue"]["y"] < m["term"]["y"]):
                problems.append(f"order now={m['now']['y']} queue={m['queue']['y']} term={m['term']['y']}")
            if m["now"]["shown"] and m["now"]["y"] + 80 > height:
                problems.append(f"current download starts below the first screen (y={m['now']['y']})")
        if width > 860 and not m["splitter"]["shown"]:
            problems.append("no splitter")
        check(f"UI1/UI2 layout at {tag}", not problems, "; ".join(problems))
        shot = await cdp.send("Page.captureScreenshot", format="png")
        (SHOTS / f"w{width}.png").write_bytes(base64.b64decode(shot["data"]))
    # Splitter resizes from the keyboard.
    await load(cdp, 1440, 900)
    before = await cdp.eval("Math.round(document.querySelector('.queue-wrap').getBoundingClientRect().width)")
    await cdp.eval("document.querySelector('#splitter').focus()")
    for _ in range(3):
        await cdp.key("ArrowLeft", "ArrowLeft", 37)
    after = await cdp.eval("Math.round(document.querySelector('.queue-wrap').getBoundingClientRect().width)")
    check("UI1 the log/downloads split resizes (keyboard)", after == before + 60, f"{before} -> {after}")
    await cdp.eval("localStorage.removeItem('omnidl-queue-w')")


async def review_1016(cdp):
    """The 16 Oct 2026 review: edits during saves, focus during updates, export feedback."""
    await load(cdp)
    # R07: Bulk is locked while sending, and the sent list is the one cleared.
    await cdp.eval("openBulk()")
    await cdp.eval("const t = document.querySelector('#bulk-text'); t.value = 'Artist - A'; t.dispatchEvent(new Event('input'))")
    await settle(cdp, 800)
    q = cdp.hold(is_path("/api/bulk/download", "POST"))
    await cdp.eval("document.querySelector('#do-bulk').click()")
    p = await asyncio.wait_for(q.get(), 5)
    locked = await cdp.eval("({text: document.querySelector('#bulk-text').readOnly, "
                            "rows: [...document.querySelectorAll('#bulk-preview input')].every(i => i.disabled), "
                            "service: document.querySelector('#bulk-service').disabled})")
    await cdp.send("Runtime.evaluate", expression="document.querySelector('#bulk-text').focus()")
    await cdp.send("Input.insertText", text="\nArtist - B")
    typed = await cdp.eval("document.querySelector('#bulk-text').value")
    await cdp.fulfill(p, 200, {"jobs": [{"id": "b1", "status": "queued"}], "tracks": 1, "links": 0,
                               "links_held_back": 0, "held_back_urls": []})
    await settle(cdp, 300)
    check("R07 Bulk is locked while sending (no edit can be wiped by the reply)",
          all(locked.values()) and typed == "Artist - A", f"{locked} {typed!r}")
    check("R07 the list that was sent is cleared afterwards and the dialog closes",
          await cdp.eval("document.querySelector('#bulk-modal').classList.contains('hidden') && bulkDraft.text === ''"))
    cdp.release_rules()

    # R07: Settings keeps an edit made while it was saving.
    q = cdp.hold(is_path("/api/settings", "POST"))
    await cdp.eval("openSettings(); document.querySelector('#save-settings').click()")
    p = await asyncio.wait_for(q.get(), 5)
    await cdp.eval("const c = document.querySelector('#set-concurrency'); c.value = '7'; "
                   "c.dispatchEvent(new Event('input', {bubbles: true}))")
    reply = await cdp.eval("({...state.settings, concurrency: 3})")
    await cdp.fulfill(p, 200, reply)
    await settle(cdp, 300)
    after = await cdp.eval("({value: document.querySelector('#set-concurrency').value, dirty: state.settingsDirty, "
                           "status: document.querySelector('#settings-status').textContent})")
    check("R07 an edit made while Settings was saving stays on screen and stays unsaved",
          after["value"] == "7" and after["dirty"] and "newer" in after["status"], str(after))
    cdp.release_rules()
    await cdp.escape()

    # R09: focus survives progress updates and job selection.
    for width, height, scope in ((390, 844, "#now-card"), (1440, 900, "#active-queue")):
        await load(cdp, width, height, width < 800)
        await cdp.eval("""handleMessage({type:'job_added', job:{id:'live1', status:'running', input:'Friday mix',
            kind:'spotify', list_name:'Friday mix', engine:'spotify', engine_label:'Spotify', tool:'embed', progress: 10,
            label:'Nova - Song', created: Date.now()/1000, started: Date.now()/1000}});""")
        await settle(cdp, 200)
        await cdp.eval(f"document.querySelector('{scope} [data-action=cancel]').focus()")
        for n in range(1, 6):
            counts = '{downloaded: %d, review: 0, skipped: 0, failed: 0, temporary: 0, total: 10}' % n
            await cdp.eval(f"handleMessage({{type:'progress', job_id:'live1', progress:{n * 10}, label:'Song {n}', counts:{counts}}})")
        await cdp.eval("renderQueue()")                       # the once-a-minute refresh
        focused = await cdp.eval("(() => { const a = document.activeElement; return {tag: a.tagName, "
                                 "action: a.dataset.action, connected: a.isConnected}; })()")
        check(f"R09 a focused Cancel keeps focus through progress updates ({width}px)",
              focused == {"tag": "BUTTON", "action": "cancel", "connected": True}, str(focused))
    q = cdp.hold(lambda m, u: u.endswith("/output"))
    await cdp.eval("document.querySelector('.job[data-id=\"jobA\"] .job-open').focus()")
    await cdp.enter()
    await cdp.proceed(await asyncio.wait_for(q.get(), 5))
    await settle(cdp, 300)
    selection = await cdp.eval("(() => { const a = document.activeElement; return {connected: a.isConnected, "
                               "pressed: a.getAttribute('aria-pressed'), job: a.closest('[data-id]')?.dataset.id}; })()")
    check("R09 selecting a job's log keeps focus on that job",
          selection == {"connected": True, "pressed": "true", "job": "jobA"}, str(selection))
    cdp.release_rules()
    await cdp.eval("handleMessage({type:'removed', job_id:'jobA'})")
    check("R09 removing the focused job leaves focus somewhere sensible",
          await cdp.eval("document.activeElement !== document.body && document.activeElement.isConnected"))

    # UI: on a phone the current job isn't repeated right under its card.
    await load(cdp, 390, 844, True)
    await cdp.eval("""handleMessage({type:'job_added', job:{id:'live2', status:'running', input:'Solo',
        kind:'search', engine:'youtube', engine_label:'Song search', tool:'all services', progress: 5,
        created: Date.now()/1000, started: Date.now()/1000}});""")
    await settle(cdp, 200)
    dup = await cdp.eval("({card: !document.querySelector('#now-card').hidden, "
                         "listed: getComputedStyle(document.querySelector('#active-section')).display})")
    check("UI the phone card isn't repeated in 'In progress' below it", dup == {"card": True, "listed": "none"}, str(dup))

    # R12: the summary wraps instead of clipping at 320 px.
    await load(cdp, 320, 740, True)
    await cdp.eval("""for (const [id, status] of [['s1','running'],['s2','queued'],['s3','error']])
        handleMessage({type:'job_added', job:{id, status, input:id, kind:'search', engine:'youtube',
        engine_label:'Song search', tool:'all services', created: Date.now()/1000}}); renderQueue();""")
    clipped = await cdp.eval("[...document.querySelectorAll('#queue-summary .sum-chip')].filter(c => "
                             "c.getBoundingClientRect().right > innerWidth + 1).length")
    chips = await cdp.eval("document.querySelectorAll('#queue-summary .sum-chip').length")
    check("R12 every queue-summary count is visible at 320 px", chips >= 4 and clipped == 0, f"{chips} chips, {clipped} clipped")

    # UI: Save checks first, stays on the page, and explains a failure.
    await load(cdp)
    await cdp.eval("state.meta.remote = true; renderQueue()")
    q = cdp.hold(is_path("/api/jobs/jobB/files", "GET"))
    await cdp.eval("document.querySelector('.job[data-id=\"jobB\"] [data-action=save]').click()")
    p = await asyncio.wait_for(q.get(), 5)
    preparing = await cdp.eval("document.querySelector('.job[data-id=\"jobB\"] [data-action=save]').textContent")
    await cdp.fulfill(p, 404, {"error": "Its files are no longer available."})
    await settle(cdp, 300)
    stayed = await cdp.eval("location.pathname")
    toasts = await cdp.eval("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ')")
    check("UI Save shows 'Preparing…', and a missing file is a message on the dashboard",
          preparing == "Preparing…" and stayed == "/dashboard" and "no longer available" in toasts,
          f"{preparing!r} {stayed} {toasts!r}")
    cdp.release_rules()

    # The quality chip says what a download is saved as, and opens the setting.
    await load(cdp)
    await cdp.eval("const f = document.querySelector('#format'); f.value = 'mp3'; f.dispatchEvent(new Event('change'))")
    chip = await cdp.eval("document.querySelector('#quality-chip').textContent")
    await cdp.eval("document.querySelector('#quality-chip').click()")
    await settle(cdp, 300)
    focused = await cdp.eval("document.activeElement.id")
    labels = await cdp.eval("[...document.querySelectorAll('#set-bitrate option')].map(o => o.textContent)")
    check("UI the quality chip shows format and size, and opens the quality setting",
          chip.startswith("MP3 · ") and "MB/min" in chip and focused == "set-bitrate" and len(labels) == 4,
          f"{chip!r} {focused} {labels}")
    await cdp.escape()


async def main():
    build_sandbox()
    server = start_server()
    edge, ws_url = start_edge()
    try:
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            cdp = CDP(ws)
            reader = asyncio.create_task(cdp.run())
            await cdp.send("Runtime.enable")
            await cdp.send("Page.enable")
            await cdp.send("Fetch.enable", patterns=[{"urlPattern": "*/api/*", "requestStage": "Request"}])
            for section in (failed_requests, reordered, keyboard, results_and_library, layouts, review_1016):
                try:
                    await section(cdp)
                except Exception as exc:  # noqa: BLE001
                    check(f"{section.__name__} ran to completion", False, repr(exc))
                cdp.release_rules()
            reader.cancel()
    finally:
        edge.kill()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge.pid)], capture_output=True)
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} browser checks passed")
    for name, _ok, detail in failed:
        print(f"  FAIL {name}: {detail}")
    shutil.rmtree(BOX, ignore_errors=True)
    return 1 if failed or not RESULTS else 0


if __name__ == "__main__":
    if EDGE is None:
        print("No Edge or Chrome found - browser checks skipped.")
        sys.exit(0)
    sys.exit(asyncio.run(main()))
