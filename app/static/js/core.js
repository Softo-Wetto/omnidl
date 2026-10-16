"use strict";

// The parts of the dashboard that have to be right under bad networks and fast clicking:
// request handling, submission guards, the bulk draft, merging a log snapshot with live
// output, and the wording of job cards. No DOM here, so tests/js/core.test.cjs can drive
// every race directly.
(function (root, factory) {
  const core = factory();
  if (typeof module === "object" && module.exports) module.exports = core;
  else root.OmniCore = core;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  /* ---------------- requests ---------------- */

  function httpMessage(status) {
    if (status === 401 || status === 403) return "Not allowed — your sign-in may have expired. Reload the page.";
    if (status === 404) return "Not found — it may have been removed.";
    if (status === 409) return "Busy — try again in a moment.";
    if (status === 429) return "Too many requests — wait a moment and try again.";
    if (status >= 500) return `OmniDL hit an error (HTTP ${status}). Try again.`;
    return `Request failed (HTTP ${status}).`;
  }

  // One shape for every outcome: { ok, status, data, error, offline, needsUnlock, field }.
  // A network failure, an HTTP error, an HTML error page and a malformed reply are all
  // failures; only a 2xx JSON object/array is a success. Callers act on `ok`, never on the
  // mere absence of an `error` key (which made an HTML 500 look like a queued download).
  // timeoutMs: give up on a request that never answers (a stalled tunnel), so Retry starts a
  // fresh attempt instead of waiting on the stuck one. 0 = wait as long as it takes.
  async function requestJson(fetchImpl, path, { method = "GET", body, offlineMessage, timeoutMs = 0 } = {}) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let timer = null;
    let timedOut = false;
    if (timeoutMs > 0 && typeof AbortController !== "undefined") {
      const controller = new AbortController();
      opts.signal = controller.signal;
      timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    }
    let res;
    try {
      res = await fetchImpl(path, opts);
    } catch (_) {
      if (timedOut) {
        return { ok: false, offline: true, status: 0, data: null,
                 error: "OmniDL took too long to answer. Try again." };
      }
      return { ok: false, offline: true, status: 0, data: null,
               error: offlineMessage || "Can't reach OmniDL — is it still running?" };
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    const isObject = data !== null && typeof data === "object";
    if (!res.ok) {
      const detail = isObject ? (data.error || (typeof data.detail === "string" ? data.detail : "")) : "";
      return {
        ok: false, offline: false, status: res.status, data: isObject ? data : null,
        error: detail || httpMessage(res.status),
        needsUnlock: !!(isObject && data.needs_unlock),
        field: isObject && typeof data.field === "string" ? data.field : "",
      };
    }
    if (!isObject) {
      return { ok: false, offline: false, status: res.status, data: null,
               error: "OmniDL sent an unexpected reply. Try again." };
    }
    return { ok: true, offline: false, status: res.status, data, error: "" };
  }

  const isJob = (d) => !!d && typeof d.id === "string" && typeof d.status === "string";
  const isSettings = (d) => !!d && typeof d.audio_format === "string";
  const isMeta = (d) => !!d && Array.isArray(d.formats) && Array.isArray(d.video_qualities);

  /* ---------------- submissions ---------------- */

  // At most one submission per key in flight: a double click or a held Enter used to queue
  // the same download twice before the first response arrived.
  function createSubmitGuard() {
    const busy = new Set();
    return {
      busy: (key) => busy.has(key),
      async run(key, fn) {
        if (busy.has(key)) return { skipped: true };
        busy.add(key);
        try { return await fn(); } finally { busy.delete(key); }
      },
    };
  }

  function newToken() {
    const bytes = new Uint8Array(12);
    const c = typeof crypto !== "undefined" ? crypto : null;
    if (c && c.getRandomValues) c.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // The server remembers a reply by this token for a few minutes. Re-sending the same input
  // after a lost reply reuses the token, so it can't queue twice; anything new gets a fresh one.
  function createTokenKeeper() {
    let last = null;
    return {
      forInput(input) {
        if (!last || last.input !== input) last = { input, token: newToken() };
        return last.token;
      },
      done() { last = null; },
    };
  }

  /* ---------------- live log ---------------- */

  // Loading a job's log is a fetch; live output keeps streaming meanwhile. Live lines that
  // arrive while the snapshot loads are held back, then only the part the snapshot doesn't
  // already contain is shown (`end` = characters the job had logged after that line). Any
  // reply for a job that's no longer selected is dropped — A's log never lands in B.
  function createLogSync() {
    let current = null;
    let counter = 0;
    return {
      begin(id) {
        current = { id, token: ++counter, loading: true, buffer: [] };
        return current.token;
      },
      isCurrent(token) { return !!current && current.token === token; },
      viewing() { return current ? current.id : null; },
      live(id, msg) {
        if (!current || current.id !== id) return "ignore";
        if (current.loading) { current.buffer.push(msg); return "buffer"; }
        return "apply";
      },
      settle(token, snapshot) {
        if (!current || current.token !== token) return null;
        current.loading = false;
        const end = Number(snapshot && snapshot.end) || 0;
        const pending = [];
        for (const msg of current.buffer) {
          if (msg.type !== "output" || msg.end == null) { pending.push(msg); continue; }
          if (msg.end <= end) continue;                       // already in the snapshot
          const start = msg.end - (msg.data || "").length;
          pending.push(start < end ? { ...msg, data: msg.data.slice(end - start) } : msg);
        }
        current.buffer = [];
        return { output: (snapshot && snapshot.output) || "", pending };
      },
      fail(token) {
        if (!current || current.token !== token) return null;
        current.loading = false;
        const pending = current.buffer;
        current.buffer = [];
        return pending;
      },
    };
  }

  /* ---------------- bulk draft ---------------- */

  // What the user is building in Bulk, kept apart from the preview the server returns:
  //  - every text change bumps `revision`; a preview applies only to the revision it was
  //    asked for, so a slow reply for old text can't replace the current list;
  //  - Download is allowed only when the rows describe the current text (`ready`);
  //  - artist/title edits and unticked rows are keyed by their line, so they survive a
  //    re-parse, changing the search service, and closing the dialog.
  function lineKeys(entries) {
    const seen = {};
    return entries.map((e) => {
      const raw = String(e.raw || e.url || `${e.artist} - ${e.title}`);
      seen[raw] = (seen[raw] || 0) + 1;
      return `${raw}\u0000${seen[raw]}`;
    });
  }

  function createBulkDraft(saved) {
    const d = {
      text: "", service: "auto", name: "", strip: true,
      revision: 0, parsed: 0, rows: [], edits: {}, excluded: {},
      capped: false, maxLines: 0, error: "",
    };
    if (saved && typeof saved === "object") {
      d.text = typeof saved.text === "string" ? saved.text : "";
      d.service = ["auto", "youtube", "soundcloud"].includes(saved.service) ? saved.service : "auto";
      d.name = typeof saved.name === "string" ? saved.name : "";
      d.strip = saved.strip !== false;
      d.edits = saved.edits && typeof saved.edits === "object" ? saved.edits : {};
      d.excluded = saved.excluded && typeof saved.excluded === "object" ? saved.excluded : {};
      if (d.text.trim()) d.revision = 1;     // restored text still has to be parsed
    }

    function change() { d.revision += 1; d.error = ""; return d.revision; }

    return {
      get text() { return d.text; },
      get service() { return d.service; },
      get name() { return d.name; },
      get strip() { return d.strip; },
      get revision() { return d.revision; },
      get rows() { return d.rows; },
      get capped() { return d.capped; },
      get maxLines() { return d.maxLines; },
      get error() { return d.error; },
      setText(text) { if (text === d.text) return d.revision; d.text = text; return change(); },
      setStrip(on) { d.strip = !!on; return change(); },
      // The service doesn't change how lines are parsed, so it never discards the rows.
      setService(service) { d.service = service; },
      setName(name) { d.name = name; },
      needsParse() { return d.parsed !== d.revision; },
      ready() { return d.parsed === d.revision && !d.error; },
      applyPreview(revision, data) {
        if (revision !== d.revision) return false;
        const entries = (data && data.entries) || [];
        const keys = lineKeys(entries);
        d.rows = entries.map((e, i) => {
          const edit = d.edits[keys[i]] || {};
          return {
            key: keys[i], raw: e.raw || "", kind: e.kind, url: e.url || "", label: e.label || "",
            artist: edit.artist !== undefined ? edit.artist : (e.artist || ""),
            title: edit.title !== undefined ? edit.title : (e.title || ""),
            include: !d.excluded[keys[i]],
          };
        });
        d.capped = !!(data && data.capped);
        d.maxLines = (data && data.max_lines) || 0;
        d.parsed = revision;
        d.error = "";
        return true;
      },
      previewFailed(revision, message) {
        if (revision !== d.revision) return false;
        d.error = message || "Couldn't read the list.";
        return true;
      },
      editRow(index, field, value) {
        const row = d.rows[index];
        if (!row || (field !== "artist" && field !== "title")) return;
        row[field] = value;
        d.edits[row.key] = { artist: row.artist, title: row.title };
      },
      includeRow(index, on) {
        const row = d.rows[index];
        if (!row) return;
        row.include = !!on;
        if (row.include) delete d.excluded[row.key]; else d.excluded[row.key] = true;
      },
      // Removing a row removes its line from the text, so the text stays the source of truth.
      removeRow(index) {
        const row = d.rows[index];
        if (!row) return d.revision;
        const lines = d.text.split(/\r?\n/);
        const at = lines.findIndex((line) => line.trim() === row.raw.trim());
        if (at < 0) return d.revision;
        lines.splice(at, 1);
        delete d.edits[row.key];
        delete d.excluded[row.key];
        d.text = lines.join("\n");
        return change();
      },
      selected() {
        return d.rows.filter((r) => r.include && (r.kind === "url" ? r.url : (r.artist.trim() || r.title.trim())));
      },
      counts() {
        const usable = d.rows.filter((r) => (r.kind === "url" ? r.url : (r.artist.trim() || r.title.trim())));
        return { selected: this.selected().length, total: usable.length };
      },
      entries() {
        return this.selected().map((r) => (r.kind === "url"
          ? { kind: "url", url: r.url }
          : { kind: "search", artist: r.artist.trim(), title: r.title.trim() }));
      },
      // After a submit: keep only what wasn't queued (links held back by the limit).
      keepOnly(urls) {
        d.text = (urls || []).join("\n");
        d.edits = {};
        d.excluded = {};
        d.rows = [];
        return change();
      },
      clear() { return this.keepOnly([]); },
      serialize() {
        return { text: d.text, service: d.service, name: d.name, strip: d.strip,
                 edits: d.edits, excluded: d.excluded };
      },
    };
  }

  /* ---------------- job cards ---------------- */

  const FINISHED = ["done", "error", "cancelled"];

  function plural(n, word, many) { return `${n} ${n === 1 ? word : (many || word + "s")}`; }

  function fmtWhen(seconds, now) {
    if (!seconds) return "";
    const when = new Date(seconds * 1000);
    const today = new Date(now);
    const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
      && a.getDate() === b.getDate();
    if (sameDay(when, today)) return time;
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (sameDay(when, yesterday)) return `Yesterday ${time}`;
    const opts = { day: "numeric", month: "short" };
    if (when.getFullYear() !== today.getFullYear()) opts.year = "numeric";
    return `${when.toLocaleDateString([], opts)} ${time}`;
  }

  function fmtLeft(seconds) {
    const mins = Math.max(1, Math.round(seconds / 60));
    return mins >= 90 ? `${Math.round(mins / 60)} h` : `${mins} min`;
  }

  const COOKIE_RE = /cookie|bot check|sign in|confirm you|login|403/i;

  // Everything a job card says, in plain words. ctx = { local, remote, now (ms) }.
  function describeJob(job, ctx) {
    const counts = job.counts || null;
    // A playlist Spotify only partly handed over is unfinished business even if every track
    // it did list was saved.
    const issues = (!!counts && (counts.failed > 0 || counts.review > 0)) || !!job.incomplete_list;
    let status, tone;
    if (job.status === "queued") { status = "Queued"; tone = "queued"; }
    else if (job.status === "running") { status = "Downloading"; tone = "running"; }
    else if (job.status === "done") {
      if (issues) { status = "Completed with issues"; tone = "issues"; }
      else if (counts && counts.total && counts.skipped === counts.total) { status = "Already in library"; tone = "done"; }
      else { status = "Completed"; tone = "done"; }
    } else if (job.status === "error") { status = "Failed"; tone = "error"; }
    else if (job.status === "cancelled") { status = "Cancelled"; tone = "cancelled"; }
    else { status = String(job.status || ""); tone = "queued"; }

    let outcome = "";
    if (counts) {
      const bits = [];
      if (counts.downloaded) bits.push(`${counts.downloaded} saved` + (counts.review ? ` (${counts.review} to check)` : ""));
      if (counts.skipped) bits.push(`${counts.skipped} already had`);
      if (counts.failed) bits.push(`${counts.failed} not found`);
      outcome = bits.join(" · ") + (counts.total ? ` — ${plural(counts.total, "track")}` : "");
    }

    const formats = (job.formats || []).map((f) => f.toUpperCase());
    let destination = "";
    const finished = FINISHED.includes(job.status);
    if (finished && job.has_files) {
      if (!ctx.local) {
        const left = job.expires ? job.expires - ctx.now / 1000 : null;
        destination = left != null && left <= 0 ? "Expired — the server deleted these files"
          : `Save to this device` + (left != null ? ` · within ${fmtLeft(left)}` : "");
      } else if (ctx.remote) {
        destination = "In your home PC's library — Save for a copy here";
      } else {
        destination = "In your music library";
      }
    } else if (finished && !ctx.local && job.expires && job.expires <= ctx.now / 1000) {
      destination = "Expired — the server deleted these files";
    }

    const title = job.list_name && job.kind !== "search" ? job.list_name : job.input;
    const fileCount = job.file_count || 0;
    const note = job.note ? job.note.charAt(0).toUpperCase() + job.note.slice(1) : "";
    return {
      status, tone, title, outcome, formats, destination, fileCount,
      // Why it failed — or, for a finished job, anything it couldn't do (e.g. a partial playlist).
      note: job.status === "running" || job.status === "queued" ? "" : note,
      noteIsError: job.status === "error",
      cookieTrouble: job.status === "error" && COOKIE_RE.test(job.note || ""),
      when: fmtWhen(job.finished || job.started || job.created, ctx.now),
      canSave: finished && !!job.has_files,
      saveLabel: job.status === "done" && !issues ? "Save" : `Save available files (${fileCount})`,
      canRetry: job.status === "error" || job.status === "cancelled",
      canRetryFailed: !!job.can_retry_failed && job.status === "done",
    };
  }

  // History filter: "all" | "done" | "issues" | "error" | "cancelled", plus free text.
  function matchesHistory(job, filter, term) {
    const d = describeJob(job, { local: true, remote: false, now: Date.now() });
    if (filter === "done" && d.tone !== "done") return false;
    if (filter === "issues" && d.tone !== "issues") return false;
    if (filter === "error" && job.status !== "error") return false;
    if (filter === "cancelled" && job.status !== "cancelled") return false;
    const t = (term || "").trim().toLowerCase();
    if (!t) return true;
    return [job.input, job.list_name, job.label, job.note].some((v) => String(v || "").toLowerCase().includes(t));
  }

  /* ---------------- library ---------------- */

  function libraryMatches(item, term) {
    if (!term) return true;
    const files = item.files || [];
    const values = [
      item.artist, item.title, item.display_artist, item.display_title, item.path,
      ...files.flatMap((f) => [f.artist, f.title, f.display_artist, f.display_title, f.path]),
    ];
    return values.some((v) => String(v || "").toLowerCase().includes(term));
  }

  // "Clean" only when the scan itself found nothing; a filter that matches nothing says so.
  function libraryView(report, rawTerm) {
    const term = (rawTerm || "").trim().toLowerCase();
    const allDuplicates = report.duplicate_groups || [];
    const allIssues = (report.tracks || []).filter((t) => (t.issues || []).length);
    const duplicates = allDuplicates.filter((g) => libraryMatches(g, term));
    const issues = allIssues.filter((t) => libraryMatches(t, term));
    let empty = null;
    if (!(report.summary && report.summary.total_files)) empty = "no-files";
    else if (!allDuplicates.length && !allIssues.length) empty = "clean";
    else if (!duplicates.length && !issues.length) empty = "no-match";
    return { duplicates, issues, totalDuplicates: allDuplicates.length, totalIssues: allIssues.length,
             filtered: !!term, empty };
  }

  return {
    requestJson, httpMessage, isJob, isSettings, isMeta,
    createSubmitGuard, createTokenKeeper, newToken,
    createLogSync, createBulkDraft,
    describeJob, matchesHistory, fmtWhen, libraryView, FINISHED,
  };
});
