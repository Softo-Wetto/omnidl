// The dashboard races and error paths reproduced in the Codex audit (docs/audits/2026-10-15),
// driven directly against app/static/js/core.js.
const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const core = require(path.join(__dirname, "..", "..", "app", "static", "js", "core.js"));

function reply(status, body, { json = true } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (!json) throw new SyntaxError("Unexpected token <");
      return body;
    },
  };
}

// ---- D05: every failure is a failure ----------------------------------------------------

test("network failure, HTTP errors, HTML pages and malformed replies are never success", async () => {
  const offline = await core.requestJson(async () => { throw new TypeError("Failed to fetch"); }, "/x");
  assert.equal(offline.ok, false);
  assert.equal(offline.offline, true);

  for (const status of [401, 403, 429]) {
    const r = await core.requestJson(async () => reply(status, { error: "" }), "/x");
    assert.equal(r.ok, false);
    assert.ok(r.error.length > 0, `status ${status} has a message`);
  }
  const json500 = await core.requestJson(async () => reply(500, { error: "boom" }), "/x");
  assert.deepEqual([json500.ok, json500.error], [false, "boom"]);
  const html500 = await core.requestJson(async () => reply(500, null, { json: false }), "/x");
  assert.equal(html500.ok, false);
  assert.match(html500.error, /HTTP 500/);
  const htmlOk = await core.requestJson(async () => reply(200, null, { json: false }), "/x");
  assert.equal(htmlOk.ok, false, "a 200 HTML page (e.g. a login page) isn't a reply");
  const locked = await core.requestJson(async () => reply(403, { error: "locked", needs_unlock: true }), "/x");
  assert.equal(locked.needsUnlock, true);
  const field = await core.requestJson(async () => reply(400, { error: "bad", field: "output_dir" }), "/x");
  assert.equal(field.field, "output_dir");
  const good = await core.requestJson(async () => reply(200, { id: "j1", status: "queued" }), "/x");
  assert.equal(good.ok, true);
  assert.ok(core.isJob(good.data));
});

test("a request that never answers times out instead of hanging Retry", async () => {
  const never = (_path, opts) => new Promise((_resolve, reject) => {
    opts.signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
  const r = await core.requestJson(never, "/api/meta", { timeoutMs: 20 });
  assert.equal(r.ok, false);
  assert.match(r.error, /too long/);
});

test("shape checks reject replies that would corrupt state", () => {
  assert.equal(core.isJob({}), false);
  assert.equal(core.isSettings({ error: "x" }), false);
  assert.equal(core.isSettings({ audio_format: "opus" }), true);
  assert.equal(core.isMeta({ formats: null }), false, "startup crashed on items is not iterable");
  assert.equal(core.isMeta({ formats: ["opus"], video_qualities: ["1080p"] }), true);
});

// ---- D06: duplicate submissions -------------------------------------------------------------

test("a second submit while the first is pending is ignored, and later ones go through", async () => {
  const guard = core.createSubmitGuard();
  let calls = 0;
  let release;
  const slow = () => new Promise((resolve) => { calls += 1; release = resolve; });
  const first = guard.run("download", slow);
  const second = await guard.run("download", slow);
  assert.deepEqual(second, { skipped: true });
  assert.equal(calls, 1);
  release("ok");
  assert.equal(await first, "ok");
  await guard.run("download", async () => { calls += 1; });
  assert.equal(calls, 2, "a deliberate submit after completion works");
});

test("re-sending the same input after a lost reply reuses its token; new input gets a new one", () => {
  const tokens = core.createTokenKeeper();
  const a = tokens.forInput("drake - one dance");
  assert.equal(tokens.forInput("drake - one dance"), a);
  assert.notEqual(tokens.forInput("other song"), a);
  tokens.done();
  assert.notEqual(tokens.forInput("other song"), a);
});

// ---- D07: logs never mix --------------------------------------------------------------------

test("a slow log for A never lands in B, including A -> B -> A", () => {
  const sync = core.createLogSync();
  const a1 = sync.begin("A");
  const b = sync.begin("B");
  assert.equal(sync.settle(a1, { output: "A_STALE_LOG", end: 11 }), null);
  const shown = sync.settle(b, { output: "B_SELECTED_LOG", end: 14 });
  assert.equal(shown.output, "B_SELECTED_LOG");

  const a2 = sync.begin("A");
  assert.equal(sync.settle(b, { output: "late B", end: 6 }), null);
  assert.equal(sync.settle(a2, { output: "A log", end: 5 }).output, "A log");
  assert.equal(sync.live("B", { type: "output", data: "x", end: 7 }), "ignore");
});

test("live lines that arrive while the snapshot loads are shown once, in order", () => {
  const sync = core.createLogSync();
  const t = sync.begin("A");
  // "one\n" (end 4) is already in the snapshot; "tw" overlaps it; "three\n" is new.
  sync.live("A", { type: "output", data: "one\n", end: 4 });
  sync.live("A", { type: "output", data: "two\n", end: 8 });
  sync.live("A", { type: "output", data: "three\n", end: 14 });
  sync.live("A", { type: "output", data: "[1/2] live", key: "t1" });      // transient line
  const settled = sync.settle(t, { output: "one\ntw", end: 6 });
  assert.equal(settled.output, "one\ntw");
  assert.deepEqual(settled.pending.map((m) => m.data), ["o\n", "three\n", "[1/2] live"]);
  assert.equal(sync.live("A", { type: "output", data: "four\n", end: 19 }), "apply");
});

test("a failed log load still shows the live output it held back", () => {
  const sync = core.createLogSync();
  const t = sync.begin("A");
  sync.live("A", { type: "output", data: "x", end: 1 });
  assert.deepEqual(sync.fail(t).map((m) => m.data), ["x"]);
});

// ---- D04 / UI 5: bulk draft --------------------------------------------------------------

const preview = (...lines) => ({
  entries: lines.map((line) => {
    if (line.startsWith("http")) return { raw: line, kind: "url", url: line };
    const [artist, title] = line.split(" - ");
    return { raw: line, kind: "search", artist, title };
  }),
});

test("Download is unavailable until the current text is parsed", () => {
  const draft = core.createBulkDraft();
  let rev = draft.setText("Old artist - Old song");
  draft.applyPreview(rev, preview("Old artist - Old song"));
  assert.equal(draft.ready(), true);
  rev = draft.setText("New artist - New song");               // debounce not fired yet
  assert.equal(draft.ready(), false, "the old preview must not be downloadable");
  draft.applyPreview(rev, preview("New artist - New song"));
  assert.deepEqual(draft.entries(), [{ kind: "search", artist: "New artist", title: "New song" }]);
});

test("a reply for older text is discarded, whatever order replies arrive in", () => {
  const draft = core.createBulkDraft();
  const first = draft.setText("First - Song");
  const second = draft.setText("Second - Song");
  assert.equal(draft.applyPreview(second, preview("Second - Song")), true);
  assert.equal(draft.applyPreview(first, preview("First - Song")), false);
  assert.equal(draft.rows[0].artist, "Second");
});

test("clearing the text during a request leaves nothing to download", () => {
  const draft = core.createBulkDraft();
  const pending = draft.setText("A - B");
  const cleared = draft.setText("");
  draft.applyPreview(cleared, { entries: [] });
  assert.equal(draft.applyPreview(pending, preview("A - B")), false);
  assert.equal(draft.entries().length, 0);
});

test("edits and unticked rows survive a re-parse and a service change", () => {
  const draft = core.createBulkDraft();
  let rev = draft.setText("Artst - Song\nOther - Tune");
  draft.applyPreview(rev, preview("Artst - Song", "Other - Tune"));
  draft.editRow(0, "artist", "Artist");
  draft.includeRow(1, false);
  draft.setService("soundcloud");
  assert.equal(draft.ready(), true, "changing the service doesn't need a re-parse");
  rev = draft.setText("Artst - Song\nOther - Tune\nThird - One");
  draft.applyPreview(rev, preview("Artst - Song", "Other - Tune", "Third - One"));
  assert.equal(draft.rows[0].artist, "Artist");
  assert.equal(draft.rows[1].include, false);
  assert.deepEqual(draft.counts(), { selected: 2, total: 3 });
  assert.equal(draft.service, "soundcloud");
});

test("a draft is restored after closing, and removing a row removes its line", () => {
  const draft = core.createBulkDraft();
  const rev = draft.setText("A - 1\nB - 2");
  draft.applyPreview(rev, preview("A - 1", "B - 2"));
  draft.editRow(1, "title", "Two");
  const restored = core.createBulkDraft(JSON.parse(JSON.stringify(draft.serialize())));
  assert.equal(restored.ready(), false);
  const r2 = restored.revision;
  restored.applyPreview(r2, preview("A - 1", "B - 2"));
  assert.equal(restored.rows[1].title, "Two");
  const r3 = restored.removeRow(0);
  assert.equal(restored.text, "B - 2");
  restored.applyPreview(r3, preview("B - 2"));
  assert.equal(restored.rows[0].title, "Two");
});

test("held-back links are kept for later, everything queued is cleared", () => {
  const draft = core.createBulkDraft();
  draft.setText("A - 1\nhttps://soundcloud.com/a/1\nhttps://soundcloud.com/a/2");
  draft.keepOnly(["https://soundcloud.com/a/2"]);
  assert.equal(draft.text, "https://soundcloud.com/a/2");
  assert.equal(draft.ready(), false);
});

// ---- D09 / UI 3 / UI 7: job cards ------------------------------------------------------------

const NOW = Date.UTC(2026, 9, 16, 12, 0, 0);
const ctx = { local: true, remote: false, now: NOW };

test("a partly failed playlist says so and offers its retry and its files", () => {
  const job = { id: "j", status: "done", input: "spotify:x", list_name: "Road trip", kind: "spotify",
                counts: { downloaded: 8, review: 0, skipped: 2, failed: 3, total: 13 },
                has_files: true, file_count: 10, can_retry_failed: true, formats: ["opus"] };
  const card = core.describeJob(job, { ...ctx, remote: true });
  assert.equal(card.status, "Completed with issues");
  assert.equal(card.title, "Road trip");
  assert.equal(card.outcome, "8 saved · 2 already had · 3 not found — 13 tracks");
  assert.equal(card.saveLabel, "Save available files (10)");
  assert.equal(card.canRetryFailed, true);
  assert.deepEqual(card.formats, ["OPUS"]);
  assert.match(card.destination, /home PC/);
});

test("all-success, all-skipped, failed with cookies, and saved-but-unverified", () => {
  const ok = core.describeJob({ status: "done", counts: { downloaded: 2, review: 0, skipped: 0, failed: 0, total: 2 } }, ctx);
  assert.equal(ok.status, "Completed");
  const skipped = core.describeJob({ status: "done", counts: { downloaded: 0, review: 0, skipped: 2, failed: 0, total: 2 } }, ctx);
  assert.equal(skipped.status, "Already in library");
  const failed = core.describeJob({ status: "error", note: "YouTube bot check - cookies needed" }, ctx);
  assert.equal(failed.status, "Failed");
  assert.equal(failed.cookieTrouble, true);
  assert.equal(failed.canRetry, true);
  const review = core.describeJob({ status: "done", counts: { downloaded: 1, review: 1, skipped: 0, failed: 0, total: 1 } }, ctx);
  assert.equal(review.status, "Completed with issues");
  assert.match(review.outcome, /1 to check/);
});

test("hosted cards count down to the server's real expiry", () => {
  const hosted = { local: false, remote: false, now: NOW };
  const fresh = core.describeJob({ status: "done", has_files: true, file_count: 1, expires: NOW / 1000 + 42 * 60 }, hosted);
  assert.equal(fresh.destination, "Save to this device · within 42 min");
  const gone = core.describeJob({ status: "done", has_files: false, expires: NOW / 1000 - 5 }, hosted);
  assert.match(gone.destination, /Expired/);
});

test("older jobs show a date, today's only a time", () => {
  const today = core.fmtWhen(NOW / 1000 - 3600, NOW);
  const older = core.fmtWhen(NOW / 1000 - 5 * 86400, NOW);
  assert.ok(!/Oct/.test(today));
  assert.match(older, /Oct|10/);
});

test("history filters by outcome and text", () => {
  const jobs = [
    { status: "done", input: "a", counts: { downloaded: 1, review: 0, skipped: 0, failed: 1, total: 2 } },
    { status: "done", input: "b" },
    { status: "error", input: "c" },
  ];
  assert.deepEqual(jobs.filter((j) => core.matchesHistory(j, "issues", "")).map((j) => j.input), ["a"]);
  assert.deepEqual(jobs.filter((j) => core.matchesHistory(j, "done", "")).map((j) => j.input), ["b"]);
  assert.deepEqual(jobs.filter((j) => core.matchesHistory(j, "all", "C")).map((j) => j.input), ["c"]);
});

// ---- D14: library filter ---------------------------------------------------------------------

test("a filter that matches nothing never claims the library is clean", () => {
  const report = { summary: { total_files: 3 }, duplicate_groups: [],
                   tracks: [{ path: "Nova - Song.mp3", display_artist: "Nova", issues: ["missing_album"] }] };
  assert.equal(core.libraryView(report, "zzz").empty, "no-match");
  assert.equal(core.libraryView(report, "zzz").totalIssues, 1);
  assert.equal(core.libraryView(report, "nova").empty, null);
  assert.equal(core.libraryView({ summary: { total_files: 3 }, tracks: [{ issues: [] }] }, "").empty, "clean");
  assert.equal(core.libraryView({ summary: { total_files: 0 }, tracks: [] }, "").empty, "no-files");
});
