"use strict";

const $ = (sel) => document.querySelector(sel);
const Core = window.OmniCore;

// Used until /api/meta answers (or if it never does), so every control still has options.
const DEFAULT_META = {
  engines: {}, formats: ["opus", "m4a", "mp3", "flac", "wav", "ogg"],
  video_qualities: ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"],
  video_containers: ["mp4", "mkv"], naming_orders: ["artist-title", "title-artist"],
  naming_artists: ["all", "primary"], bitrates: ["auto", "128k", "160k", "192k", "256k", "320k"],
  soundcloud_format_notes: {}, file_ttl: 3600, local: false, remote: false,
};

const state = {
  settings: { audio_format: "opus" },
  meta: { ...DEFAULT_META },
  loaded: false,             // meta + settings arrived and were valid
  startupError: "",
  jobs: new Map(),
  order: [],
  viewingId: null,
  pendingInput: null,        // input held back by the access gate, retried after unlock
  mediaType: "audio",        // "audio" | "video" (per-download choice)
  lastAudioFormat: null,     // remembered selection when toggling back to audio
  lastVideoQuality: null,    // remembered selection when toggling back to video
  libraryReport: null,
  historyFilter: "all",
  historySearch: "",
  settingsDirty: false,
  settingsRevision: 0,          // bumped on every edit, so a save can tell if more came after it
  bulkSubmitting: false,
  connected: false,
};

const ENGINE_LABEL = { spotify: "Spotify", youtube: "YouTube", soundcloud: "SoundCloud" };
const ENGINE_TOOL = { spotify: "spotdl", youtube: "yt-dlp", soundcloud: "scdl" };
const FINISHED = Core.FINISHED;
const ACTIVE = ["running", "queued"];

const submitGuard = Core.createSubmitGuard();
const downloadTokens = Core.createTokenKeeper();
const bulkTokens = Core.createTokenKeeper();
const logSync = Core.createLogSync();

/* ---------------- log view (custom, clean terminal) ---------------- */
const ANSI_RE = /\x1b\[[0-9;]*m/g;
const COLOR_CODES = { 30: "c30", 31: "c31", 32: "c32", 33: "c33", 34: "c34", 35: "c35", 36: "c36", 37: "c37" };

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Convert a line containing ANSI SGR codes into safe HTML with color spans.
function ansiToHtml(text) {
  let html = "";
  let last = 0;
  let open = false;
  const classes = new Set();
  const reopen = () => {
    if (open) { html += "</span>"; open = false; }
    if (classes.size) { html += `<span class="${[...classes].join(" ")}">`; open = true; }
  };
  text.replace(ANSI_RE, (match, offset) => {
    html += escapeHtml(text.slice(last, offset));
    last = offset + match.length;
    const codes = match.slice(2, -1).split(";").filter((x) => x !== "").map(Number);
    if (codes.length === 0 || codes.includes(0)) classes.clear();
    for (const c of codes) {
      if (c === 1) classes.add("b");
      else if (COLOR_CODES[c]) { for (const k of [...classes]) if (k.startsWith("c3")) classes.delete(k); classes.add(COLOR_CODES[c]); }
      else if (c >= 90 && c <= 97) { for (const k of [...classes]) if (k.startsWith("c3")) classes.delete(k); classes.add(COLOR_CODES[c - 60]); }
    }
    reopen();
    return match;
  });
  html += escapeHtml(text.slice(last));
  if (open) html += "</span>";
  return html;
}

const PROG_RE = /\d{1,3}(\.\d+)?%/;
const PROG_CTX = /(\[download\]|ETA|\bof\b|SponsorBlock)/i;

class LogView {
  constructor(host) {
    this.el = host;
    this.buffer = "";
    this.lines = [];      // { node, plain, prog }
    this.partial = null;  // node for the not-yet-newlined tail
    this.keyed = {};      // key -> node, for in-place per-track lines (parallel mode)
    this.stick = true;
    this.el.addEventListener("scroll", () => {
      this.stick = this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight < 24;
      $("#jump-latest").classList.toggle("hidden", this.stick);
    });
  }
  clear(showEmpty = true) {
    this.buffer = "";
    this.lines = [];
    this.partial = null;
    this.keyed = {};
    this.el.innerHTML = "";
    this.stick = true;
    $("#jump-latest").classList.add("hidden");
    $("#term-empty").classList.toggle("hidden", !showEmpty);
  }
  feed(data) {
    if (!data) return;
    $("#term-empty").classList.add("hidden");
    this.buffer += data;
    const parts = this.buffer.split("\n");
    this.buffer = parts.pop();
    for (const raw of parts) this._commit(raw);
    this._renderPartial();
    if (this.stick) this.el.scrollTop = this.el.scrollHeight;
  }
  feedKeyed(key, data) {
    // One in-place line per concurrent track. Updates the same node as progress changes.
    $("#term-empty").classList.add("hidden");
    const m = this._lineMeta(data.replace(/[\r\n]+$/, ""));
    let node = this.keyed[key];
    if (!node) {
      node = document.createElement("div");
      if (this.partial) this.el.insertBefore(node, this.partial);
      else this.el.appendChild(node);
      this.keyed[key] = node;
      this.lines.push({ node, plain: m.plain, prog: false });
    }
    node.className = m.cls;
    node.innerHTML = m.html;
    if (this.stick) this.el.scrollTop = this.el.scrollHeight;
  }
  releaseKey(key) { delete this.keyed[key]; }
  _lineMeta(raw) {
    // Drop a single trailing CR (from splitting "\r\n" lines); only treat an *internal*
    // bare CR as a carriage-return overwrite (e.g. progress bars). Without this, every
    // "\r\n"-terminated line would slice to empty and render blank.
    const trimmed = raw.replace(/\r$/, "");
    const line = trimmed.includes("\r") ? trimmed.slice(trimmed.lastIndexOf("\r") + 1) : trimmed;
    const plain = line.replace(ANSI_RE, "");
    const prog = PROG_RE.test(plain) && PROG_CTX.test(plain);
    let cls = "ln";
    if (plain.startsWith("$ ")) cls += " cmd";
    else if (/^\[\d+\/\d+\]/.test(plain.trim())) cls += " track";
    else if (prog) cls += " prog";
    return { html: ansiToHtml(line) || "&nbsp;", plain, prog, cls };
  }
  _commit(raw) {
    const m = this._lineMeta(raw);
    const last = this.lines[this.lines.length - 1];
    if (m.prog && last && last.prog) {       // collapse consecutive progress lines
      last.node.innerHTML = m.html;
      last.node.className = m.cls;
      last.plain = m.plain;
      return;
    }
    const node = document.createElement("div");
    node.className = m.cls;
    node.innerHTML = m.html;
    if (this.partial) this.el.insertBefore(node, this.partial);
    else this.el.appendChild(node);
    this.lines.push({ node, plain: m.plain, prog: m.prog });
    if (this.lines.length > 3000) { const old = this.lines.shift(); old.node.remove(); }
  }
  _renderPartial() {
    if (!this.buffer) {
      if (this.partial) { this.partial.remove(); this.partial = null; }
      return;
    }
    if (!this.partial) {
      this.partial = document.createElement("div");
      this.partial.className = "ln";
      this.el.appendChild(this.partial);
    }
    const m = this._lineMeta(this.buffer);
    this.partial.innerHTML = m.html;
    this.partial.className = m.cls;
  }
  jump() { this.stick = true; this.el.scrollTop = this.el.scrollHeight; $("#jump-latest").classList.add("hidden"); }
  copy() { return this.lines.map((l) => l.plain).join("\n") + (this.buffer ? "\n" + this.buffer.replace(ANSI_RE, "") : ""); }
}

const logView = new LogView($("#log"));
function termWrite(data) { logView.feed(data); }
function termClear(showEmpty = true) { logView.clear(showEmpty); }

function applyLive(msg) {
  if (msg.type === "key_done") { logView.releaseKey(msg.key); return; }
  if (msg.key) logView.feedKeyed(msg.key, msg.data);
  else logView.feed(msg.data);
}

/* ---------------- engine detection (mirrors backend) ---------------- */
function detectEngine(text) {
  const t = text.trim().toLowerCase();
  if (!t) return null;
  if (t.includes("open.spotify.com") || t.startsWith("spotify:")) return "spotify";
  if (t.includes("soundcloud.com")) return "soundcloud";
  return "youtube"; // links to YT or any plain-text search go to yt-dlp
}

/* ---------------- audio / video mode ---------------- */
function videoSupported(engine) {
  // Video applies only to YouTube / generic links (yt-dlp). Spotify & SoundCloud are audio.
  return engine === "youtube";
}

function refreshFormatOptions() {
  const fmt = $("#format");
  if (state.mediaType === "video") {
    fillSelect(fmt, state.meta.video_qualities, state.lastVideoQuality || state.settings.video_quality || "1080p");
    fmt.title = "Video quality";
    fmt.setAttribute("aria-label", "Video quality");
  } else {
    fillSelect(fmt, state.meta.formats, state.lastAudioFormat || state.settings.audio_format);
    fmt.title = "Audio format";
    fmt.setAttribute("aria-label", "Audio format");
  }
}

function setMediaType(type, { persist = true } = {}) {
  state.mediaType = type === "video" ? "video" : "audio";
  for (const btn of document.querySelectorAll("#media-toggle .mt-btn")) {
    const on = btn.dataset.media === state.mediaType;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  }
  refreshFormatOptions();
  if (persist) { try { localStorage.setItem("omnidl-media", state.mediaType); } catch (_) {} }
}

function updateMediaToggle(engine) {
  // Disable Video for Spotify/SoundCloud; an empty box (engine === null) stays enabled.
  const canVideo = engine ? videoSupported(engine) : true;
  const toggle = $("#media-toggle");
  toggle.classList.toggle("disabled", !canVideo);
  toggle.querySelector('[data-media="video"]').disabled = !canVideo;
  if (!canVideo && state.mediaType === "video") setMediaType("audio", { persist: false });
}

function updateChip() {
  const override = $("#engine").value;
  const text = $("#input").value;
  const chip = $("#detected");
  const hint = $("#hint");
  const engine = override === "auto" ? detectEngine(text) : override;
  updateMediaToggle(engine);
  if (!engine) {
    // Nothing typed: no engine to name, so a short tip instead of an empty "—" chip.
    chip.hidden = true;
    hint.textContent = "Spotify, YouTube and SoundCloud links, or a song name. ☰ Bulk takes a whole list.";
    return;
  }
  chip.hidden = false;
  const wantsVideo = state.mediaType === "video" && videoSupported(engine);
  // Typed text in audio mode is matched as a song across services (the same matcher as
  // playlists and Bulk), not handed to one tool — say so instead of "YouTube · yt-dlp".
  const songSearch = !isLink(text) && state.mediaType !== "video";
  if (songSearch) {
    const where = { auto: "all services", youtube: "YouTube", soundcloud: "SoundCloud" }[textService()];
    chip.className = "chip " + (textService() === "auto" ? "youtube" : textService());
    chip.textContent = `Song search · ${where}`;
  } else {
    chip.className = "chip " + engine;
    let tool = engine === "spotify" ? "embed → yt-dlp" : ENGINE_TOOL[engine];
    if (wantsVideo) tool += " · video";
    chip.textContent = `${ENGINE_LABEL[engine]} · ${tool}`;
  }
  const scNote = engine === "soundcloud" && isLink(text)
    ? (state.meta.soundcloud_format_notes || {})[audioFormat()] : "";
  if (gateBlocks(text)) {
    hint.innerHTML = '🔒 YouTube &amp; Spotify need the access passphrase — ' +
                     '<a href="#" id="hint-unlock" style="color:var(--accent)">unlock</a>. ' +
                     'SoundCloud &amp; direct links work without it.';
    const a = $("#hint-unlock");
    if (a) a.onclick = (e) => { e.preventDefault(); state.pendingInput = text.trim(); openUnlock(); };
  } else if (wantsVideo) {
    hint.textContent = `Full video → ${$("#format").value} (${state.settings.video_container || "mp4"}).`;
  } else if (scNote) {
    hint.textContent = scNote;
  } else if (songSearch && override === "spotify") {
    hint.textContent = "Spotify can't be searched by text, so this searches all services.";
  } else if (songSearch) {
    hint.textContent = "Finds the studio version — the same match a playlist or Bulk list would get.";
  } else if (engine === "spotify") {
    hint.textContent = "Via public embed — no Spotify API, no Premium, no login needed.";
  } else {
    hint.textContent = "";
  }
}

/* ---------------- toasts ---------------- */
const recentToasts = new Map();

function toast(message, kind = "info", ms = 3200) {
  // The same message twice in a few seconds (e.g. a connection problem reported by both the
  // request helper and the button that used it) shows once.
  const now = Date.now();
  if (now - (recentToasts.get(message) || 0) < 4000) return;
  recentToasts.set(message, now);
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 250);
  }, ms);
}

/* ---------------- banner (connection / startup) ---------------- */
function showBanner(text, { retry = false, kind = "warn" } = {}) {
  const b = $("#conn-banner");
  $("#conn-banner-text").textContent = text;
  $("#conn-banner-retry").hidden = !retry;
  b.className = "conn-banner " + kind;
  b.hidden = false;
}
function hideBanner() { $("#conn-banner").hidden = true; }
function refreshBanner() {
  if (state.startupError) {
    showBanner(`Couldn't load OmniDL's settings — ${state.startupError}`, { retry: true, kind: "error" });
  } else if (!state.connected && state.disconnectedFor) {
    showBanner(state.meta.remote
      ? "Can't reach your home PC — it may be asleep or offline. Reconnecting…"
      : "Lost the connection to OmniDL — reconnecting… (is it still running?)");
  } else {
    hideBanner();
  }
}

/* ---------------- queue rendering ---------------- */
function jobCtx() { return { local: !!state.meta.local, remote: !!state.meta.remote, now: Date.now() }; }

function makeButton(label, title, handler, cls, action) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  if (title) {
    b.title = title;
    // Symbol-only buttons ("✕") are named by what they do; worded ones by their words.
    if ([...label.trim()].length <= 2) b.setAttribute("aria-label", title);
  }
  if (cls) b.className = cls;
  // What the button does, so focus can find "the same button" after a card is redrawn.
  if (action) b.dataset.action = action;
  b.onclick = (e) => { e.stopPropagation(); handler(b); };
  return b;
}

function trunc(text, n) { return text.length > n ? text.slice(0, n) + "…" : text; }

function jobActions(job, card) {
  const out = [];
  const id = job.id;
  if (ACTIVE.includes(job.status)) {
    out.push(makeButton("Cancel", "Cancel this download", () => cancelJob(id), "btn-danger", "cancel"));
    return out;
  }
  if (card.canSave && canSave()) {
    const tip = state.meta.local ? "Download a copy to this device (it's also in your PC's library)"
                                 : "Download to this device";
    out.push(makeButton(`⤓ ${card.saveLabel}`, tip, (b) => saveJob(job, b), "save-btn", "save"));
  }
  if (job.has_report) {
    // Tracks not downloaded or saved for review. Opens from any device.
    const n = job.review_count || 0;
    out.push(makeButton(n ? `⚠ Review ${n}` : "⚠ Review",
                        "Open the review report: tracks to check or that weren't found",
                        () => window.open(`/api/jobs/${id}/report`, "_blank", "noopener"), "review-btn", "review"));
  }
  if (card.canRetryFailed) {
    out.push(makeButton(`↻ Retry ${job.counts ? job.counts.failed : ""} failed`.replace("  ", " "),
                        "Search again for just the tracks that weren't found", () => retryJob(job, true),
                        "btn-retry", "retry-failed"));
  }
  if (card.canRetry) out.push(makeButton("↻ Retry", "Run again", () => retryJob(job), "btn-retry", "retry"));
  if (card.cookieTrouble) out.push(makeButton("How to fix", "What to do about a YouTube block", showCookieHelp, "btn-help", "help"));
  out.push(makeButton("✕", "Remove from history", () => removeJob(id), "btn-remove", "remove"));
  return out;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function progressNode(job) {
  const prog = el("div", "job-progress");
  const bar = el("div", "bar");
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", "Progress");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");
  bar.setAttribute("aria-valuenow", String(job.progress || 0));
  const fill = el("div", "fill");
  fill.style.width = (job.progress || 0) + "%";
  bar.append(fill);
  prog.append(bar, el("span", "pct", job.progress != null ? job.progress + "%" : ""));
  return prog;
}

function setProgress(root, job) {
  const fill = root.querySelector(".fill");
  if (!fill) return false;
  fill.style.width = (job.progress || 0) + "%";
  root.querySelector(".bar").setAttribute("aria-valuenow", String(job.progress || 0));
  const pct = root.querySelector(".pct");
  if (pct) pct.textContent = job.progress != null ? job.progress + "%" : "";
  return true;
}

function renderJob(job) {
  const card = Core.describeJob(job, jobCtx());
  const li = el("li", `job tone-${card.tone}` + (job.id === state.viewingId ? " active" : ""));
  li.dataset.id = job.id;

  const top = el("div", "job-top");
  // The title is the control for "show this job's log" — a real button, so it works from
  // the keyboard and says whether it's the one shown.
  const open = el("button", "job-open");
  open.type = "button";
  open.dataset.action = "open";
  open.setAttribute("aria-pressed", String(job.id === state.viewingId));
  open.title = `Show the log for ${card.title}`;
  open.append(el("span", "job-input", card.title));
  open.onclick = (e) => { e.stopPropagation(); viewJob(job.id); };
  top.append(open, el("span", `badge ${card.tone}`, card.status));
  li.append(top);

  const facts = [];
  if (card.outcome) facts.push(card.outcome);
  if (card.formats.length) facts.push(card.formats.join(", "));
  if (facts.length) li.append(el("div", "job-facts", facts.join(" · ")));
  if (card.destination) li.append(el("div", "job-dest", card.destination));
  if (card.note) {
    const note = el("div", card.noteIsError ? "job-note" : "job-note info", card.note);
    note.title = card.note;
    li.append(note);
  }
  if (job.status === "running") {
    li.append(progressNode(job));
    li.append(el("div", "job-label", job.label || ""));
  }

  const metaRow = el("div", "job-meta");
  const tool = el("span", "tool-tag");
  tool.append(el("span", "eng-dot " + job.engine),
              document.createTextNode(`${job.engine_label} · ${job.tool}` + (card.when ? ` · ${card.when}` : "")));
  const actions = el("div", "job-actions");
  actions.append(...jobActions(job, card));
  metaRow.append(tool, actions);
  li.append(metaRow);
  li.onclick = () => viewJob(job.id);
  return li;
}

/* Cards are redrawn as jobs change. Redrawing removes the focused button, which dropped
   keyboard focus to the page (and confused screen readers) every progress tick. Before a
   redraw, remember which job and which action had focus; afterwards, put focus back on the
   same action of the same job — or, if that job is gone, the nearest sensible place. */
function focusMemo() {
  const active = document.activeElement;
  if (!active || active === document.body) return null;
  const host = active.closest("#active-queue, #queue, #now-card");
  if (!host) return null;
  const item = active.closest("[data-id]");
  return { host: host.id, jobId: item ? item.dataset.id : host.dataset.jobId || null,
           action: active.dataset.action || "open" };
}

function restoreFocus(memo) {
  if (!memo) return;
  const current = document.activeElement;
  if (current && current !== document.body && document.contains(current)) return;
  const sameJob = (sel) => document.querySelector(sel);
  let target = null;
  if (memo.host === "now-card") {
    target = $("#now-card").querySelector(`[data-action="${memo.action}"]`);
  }
  if (!target && memo.jobId) {
    target = sameJob(`#${memo.host} .job[data-id="${memo.jobId}"] [data-action="${memo.action}"]`)
      || sameJob(`.job[data-id="${memo.jobId}"] [data-action="${memo.action}"]`)   // moved to history
      || sameJob(`.job[data-id="${memo.jobId}"] .job-open`);
  }
  if (!target) target = document.querySelector(`#${memo.host} .job-open`) || document.querySelector(".job-open");
  if (!target) {
    const heading = $("#queue-summary");
    heading.tabIndex = -1;
    target = heading;
  }
  target.focus({ preventScroll: true });
}

function renderSummary(jobs) {
  const counts = { running: 0, queued: 0 };
  let issues = 0;
  for (const j of jobs) {
    counts[j.status] = (counts[j.status] || 0) + 1;
    if (Core.describeJob(j, jobCtx()).tone === "issues") issues += 1;
  }
  // Separate chips that wrap, so on a narrow phone nothing (least of all "failed") is cut off.
  const chips = [
    [counts.running, "downloading", "running"], [counts.queued, "queued", "queued"],
    [counts.done, "done", "done"], [issues, "with issues", "issues"], [counts.error, "failed", "error"],
  ].filter(([n]) => n).map(([n, word, tone]) => el("span", `sum-chip ${tone}`, `${n} ${word}`));
  $("#queue-summary").replaceChildren(...chips);
}

function renderQueue() {
  const memo = focusMemo();
  const jobs = state.order.map((id) => state.jobs.get(id)).filter(Boolean);
  const active = jobs.filter((j) => ACTIVE.includes(j.status))
    .sort((a, b) => (a.status === b.status ? 0 : a.status === "running" ? -1 : 1));
  const history = jobs.filter((j) => FINISHED.includes(j.status)).reverse();   // newest first
  const shown = history.filter((j) => Core.matchesHistory(j, state.historyFilter, state.historySearch));

  $("#active-queue").replaceChildren(...active.map(renderJob));
  $("#queue").replaceChildren(...shown.map(renderJob));
  $("#active-section").hidden = !active.length;
  $("#history-section").hidden = !history.length;
  $("#history-none").hidden = !(history.length && !shown.length);
  $("#queue-empty").classList.toggle("hidden", jobs.length > 0);
  $("#queue-empty").hidden = jobs.length > 0;
  renderSummary(jobs);
  $("#clear-done").title = state.meta.local
    ? "Clears finished downloads from this list. Your music files stay in your library."
    : "Clears finished downloads — and deletes their files from the server, so Save stops working for them.";
  renderNowCard();
  restoreFocus(memo);
}

// One job changed: redraw just its card (its counts, its buttons), not the whole history.
function refreshJob(id) {
  const job = state.jobs.get(id);
  const old = document.querySelector(`.job[data-id="${id}"]`);
  const list = job && old ? old.parentElement : null;
  const belongs = list && (list.id === "active-queue") === ACTIVE.includes(job.status);
  if (!belongs) { renderQueue(); return; }
  const memo = focusMemo();
  const fresh = renderJob(job);
  if (old.classList.contains("shown-above")) fresh.classList.add("shown-above");
  old.replaceWith(fresh);
  renderSummary(state.order.map((jid) => state.jobs.get(jid)).filter(Boolean));
  renderNowCard();
  restoreFocus(memo);
}

// Phones: the job you care about right now, with its actions, above everything else.
function nowJob() {
  const jobs = state.order.map((id) => state.jobs.get(id)).filter(Boolean);
  const running = jobs.filter((j) => j.status === "running");
  if (running.length) return { job: running[running.length - 1], more: jobs.filter((j) => ACTIVE.includes(j.status)).length - 1 };
  const queued = jobs.filter((j) => j.status === "queued");
  if (queued.length) return { job: queued[0], more: queued.length - 1 };
  const last = jobs.reverse().find((j) => FINISHED.includes(j.status));
  if (last && Date.now() / 1000 - (last.finished || 0) < 15 * 60) return { job: last, more: 0 };
  return null;
}

function nowCardKey(job, card) {
  return JSON.stringify([job.id, job.status, card.status, card.outcome, card.destination, card.note,
                         job.has_report, job.review_count, card.canSave, card.canRetry, card.canRetryFailed]);
}

function renderNowCard() {
  const host = $("#now-card");
  const pick = nowJob();
  // On a phone the card already shows this job, so its copy in "In progress" is hidden
  // (and the section too if that was its only job) — no more two of the same card.
  for (const li of document.querySelectorAll("#active-queue .job")) {
    li.classList.toggle("shown-above", !!pick && li.dataset.id === pick.job.id);
  }
  $("#active-section").classList.toggle("only-shown-above",
    !!pick && document.querySelectorAll("#active-queue .job:not(.shown-above)").length === 0);
  if (!pick) { host.hidden = true; host.replaceChildren(); host.dataset.key = ""; return; }
  const { job, more } = pick;
  const card = Core.describeJob(job, jobCtx());
  const key = nowCardKey(job, card) + more;
  if (host.dataset.key === key && !host.hidden) {
    // Same job, same state: only the numbers moved. Update them in place so a focused
    // Cancel button stays focused.
    setProgress(host, job);
    const label = host.querySelector(".now-label");
    if (label) label.textContent = job.label || "";
    return;
  }
  const memo = focusMemo();
  const head = el("div", "now-head");
  head.append(el("strong", "now-title", card.title), el("span", `badge ${card.tone}`, card.status));
  const parts = [head];
  if (job.status === "running") {
    parts.push(progressNode(job));
    parts.push(el("div", "now-label", job.label || ""));
  }
  const facts = [card.outcome, card.destination].filter(Boolean).join(" · ");
  if (facts) parts.push(el("div", "now-facts", facts));
  if (card.note) parts.push(el("div", card.noteIsError ? "job-note" : "job-note info", card.note));
  const actions = el("div", "job-actions now-actions");
  actions.append(...jobActions(job, card).filter((b) => !b.classList.contains("btn-remove")));
  const logBtn = makeButton("Show log", "Show this job's live output", () => {
    setTermCollapsed(false);
    viewJob(job.id);
    $("#terminal-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
  }, "ghost", "log");
  actions.append(logBtn);
  parts.push(actions);
  if (more > 0) parts.push(el("div", "now-more", `+${more} more in the queue below`));
  host.replaceChildren(...parts);
  host.dataset.key = key;
  host.dataset.jobId = job.id;
  host.hidden = false;
  restoreFocus(memo);
}

function updateJobProgress(id) {
  const j = state.jobs.get(id);
  if (!j) return;
  const nodes = [...document.querySelectorAll(`.job[data-id="${id}"]`)];
  if (!nodes.length || nodes.some((li) => !li.querySelector(".fill"))) { refreshJob(id); return; }
  for (const li of nodes) {
    setProgress(li, j);
    const lbl = li.querySelector(".job-label");
    if (lbl) lbl.textContent = j.label || "";
  }
  renderNowCard();
}

// Selecting a job only moves the highlight; the cards (and the focused title) stay put.
function markViewing(id) {
  for (const li of document.querySelectorAll(".job[data-id]")) {
    const on = li.dataset.id === id;
    li.classList.toggle("active", on);
    const open = li.querySelector(".job-open");
    if (open) open.setAttribute("aria-pressed", String(on));
  }
}

// Save: check what's there first, so a moved or expired file is a message on the dashboard
// instead of the page navigating to an error; then download without leaving the page.
async function saveJob(job, btn) {
  await submitGuard.run(`save:${job.id}`, async () => {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Preparing…";
    const r = await api(`/api/jobs/${job.id}/files`, "GET");
    if (!r.ok) {
      btn.disabled = false;
      btn.textContent = label;
      if (!r.offline) toast(`${r.error} Press Save to try again.`, "error", 8000);
      return;
    }
    const link = document.createElement("a");
    link.href = `/api/jobs/${job.id}/file`;
    link.download = "";
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    const what = r.data.zip ? `a zip of ${r.data.count} files` : "the file";
    toast(`Downloading ${what} (${formatBytes(r.data.bytes)}) — see your browser's downloads.`, "success", 7000);
    btn.textContent = "Downloading…";
    setTimeout(() => {
      if (btn.isConnected) { btn.disabled = false; btn.textContent = label; }
    }, 4000);
  });
}

// Show one job's log. A slow reply for a job you've since moved away from is dropped, and
// live lines that arrive while it loads are merged without gaps or repeats (see OmniCore).
async function viewJob(id) {
  const token = logSync.begin(id);
  state.viewingId = id;
  markViewing(id);
  termClear(false);
  const r = await api(`/api/jobs/${id}/output`, "GET", undefined, { quiet: true });
  if (!logSync.isCurrent(token)) return;
  if (!r.ok) {
    const pending = logSync.fail(token) || [];
    termWrite(`\x1b[31m✗ Couldn't load this job's log: ${r.error}\x1b[0m\r\n`);
    pending.forEach(applyLive);
    return;
  }
  const settled = logSync.settle(token, r.data);
  if (!settled) return;
  if (settled.output) termWrite(settled.output);
  settled.pending.forEach(applyLive);
  if (!settled.output && !settled.pending.length) $("#term-empty").classList.remove("hidden");
}

function downloadPayload(input, override) {
  const payload = { input };
  if (override && override !== "auto") payload.engine_override = override;
  const engine = (override && override !== "auto") ? override : detectEngine(input);
  if (state.mediaType === "video" && videoSupported(engine)) {
    payload.media_type = "video";
    payload.video_quality = $("#format").value;
  } else {
    // Said explicitly: the server routes typed text by mode (audio = song match).
    payload.media_type = "audio";
    payload.format = audioFormat();
  }
  return payload;
}

async function retryJob(job, failedOnly = false) {
  // Done on the server, which knows what the job really was. Re-sending the display text
  // re-ran a pasted list as a YouTube search for "Wedding songs (3 tracks)".
  await submitGuard.run(`retry:${job.id}:${failedOnly}`, async () => {
    const r = await api(`/api/jobs/${job.id}/retry${failedOnly ? "?failed_only=true" : ""}`, "POST");
    if (r.needsUnlock) { openUnlock(); return; }
    if (!r.ok) { if (!r.offline) toast(r.error, "error", 6000); return; }
    const title = Core.describeJob(job, jobCtx()).title;
    toast(failedOnly ? `Retrying the failed tracks of ${trunc(title, 40)}` : `Re-queued: ${trunc(title, 48)}`, "info");
  });
}

async function cancelJob(id) {
  const r = await api(`/api/jobs/${id}/cancel`, "POST");
  if (!r.ok || !r.data.ok) { if (!r.offline) toast(r.error || "That download couldn't be cancelled.", "error"); }
}

async function removeJob(id) {
  const r = await api(`/api/jobs/${id}`, "DELETE");
  if (!r.ok || !r.data.ok) { if (!r.offline) toast(r.error || "That job couldn't be removed.", "error"); }
}

function showCookieHelp() {
  if (state.meta.local && !state.meta.remote) {
    toast("YouTube is asking OmniDL to sign in. In Settings → YouTube account, point Cookies file at a " +
          "fresh cookies.txt exported while signed in to youtube.com, then press Retry.", "info", 12000);
    openSettings();
    const field = $("#set-cookie-file");
    if (field) field.focus();
  } else if (state.meta.local) {
    toast("YouTube is asking your home PC to sign in. On the PC, set a fresh cookies.txt in Settings → " +
          "YouTube account, then press Retry here.", "info", 12000);
  } else {
    toast("YouTube is blocking the server right now. Try again later, or search SoundCloud instead.", "info", 10000);
  }
}

// Two clicks for anything that can't be undone: the first arms the button and says what the
// second will do; it disarms itself after a few seconds.
function armOrRun(btn, armedLabel, explanation, run) {
  if (btn.dataset.armed === "1") {
    clearTimeout(Number(btn.dataset.timer));
    btn.dataset.armed = "";
    btn.textContent = btn.dataset.label;
    btn.classList.remove("armed");
    return run();
  }
  btn.dataset.label = btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = armedLabel;
  btn.classList.add("armed");
  toast(explanation, "info", 5000);
  btn.dataset.timer = String(setTimeout(() => {
    btn.dataset.armed = "";
    btn.textContent = btn.dataset.label;
    btn.classList.remove("armed");
  }, 5000));
  return null;
}

async function cancelAll() {
  const ids = state.order.filter((id) => ACTIVE.includes((state.jobs.get(id) || {}).status));
  if (!ids.length) return;
  const btn = $("#cancel-all");
  await armOrRun(btn, `Confirm: cancel ${ids.length}`,
    `Press again to cancel ${ids.length} download${ids.length === 1 ? "" : "s"}. Finished songs are kept.`,
    async () => {
      const results = await Promise.all(ids.map((id) => api(`/api/jobs/${id}/cancel`, "POST", undefined, { quiet: true })));
      const ok = results.filter((r) => r.ok && r.data.ok).length;
      const failed = results.length - ok;
      if (ok) toast(`Cancelled ${ok} download${ok === 1 ? "" : "s"}`, "info");
      if (failed) toast(`${failed} couldn't be cancelled — ${results.find((r) => !r.ok)?.error || "try again"}`, "error", 6000);
    });
}

async function clearHistory() {
  const ids = state.order.filter((id) => FINISHED.includes((state.jobs.get(id) || {}).status));
  if (!ids.length) return;
  const btn = $("#clear-done");
  const what = state.meta.local
    ? "Your music files stay in your library."
    : "Their files are deleted from the server, so Save stops working for them.";
  await armOrRun(btn, `Confirm: clear ${ids.length}`,
    `Press again to clear ${ids.length} finished download${ids.length === 1 ? "" : "s"} from the list. ${what}`,
    async () => {
      const results = await Promise.all(ids.map((id) => api(`/api/jobs/${id}`, "DELETE", undefined, { quiet: true })));
      const ok = results.filter((r) => r.ok && r.data.ok).length;
      const failed = results.length - ok;
      if (ok) toast(`Cleared ${ok} finished download${ok === 1 ? "" : "s"}`, "info");
      if (failed) toast(`${failed} couldn't be cleared — ${results.find((r) => !r.ok)?.error || "try again"}`, "error", 6000);
    });
}

/* ---------------- websocket ---------------- */
let ws = null;
let wsRetries = 0;
let bannerTimer = null;

function setConn(online) {
  state.connected = online;
  const c = $("#conn");
  c.className = "conn " + (online ? "on" : "off");
  c.innerHTML = "<i></i>" + (online ? "online" : "offline");
  clearTimeout(bannerTimer);
  if (online) {
    state.disconnectedFor = false;
    refreshBanner();
  } else {
    // A blip while reconnecting isn't worth a banner; a real outage is.
    bannerTimer = setTimeout(() => { state.disconnectedFor = true; refreshBanner(); }, 1500);
  }
}

function connect() {
  // Must match the page scheme: a ws:// socket on an https:// page is blocked as mixed
  // content, which would leave the dashboard permanently "offline" with no live output.
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${scheme}//${location.host}/ws`);
  ws.onopen = () => {
    wsRetries = 0;
    setConn(true);
    if (!state.loaded) loadStartup();
  };
  ws.onclose = () => {
    setConn(false);
    wsRetries += 1;
    setTimeout(connect, Math.min(10000, 1500 * wsRetries));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handleMessage(msg);
  };
}

function upsertJob(job) {
  if (!state.jobs.has(job.id)) state.order.push(job.id);
  state.jobs.set(job.id, job);
}

function handleMessage(msg) {
  switch (msg.type) {
    case "snapshot": {
      state.jobs.clear();
      state.order = [];
      for (const job of msg.jobs || []) upsertJob(job);
      // After a reconnect, stay on the job you were looking at if it still exists.
      const keep = state.viewingId && state.jobs.has(state.viewingId) && state.viewingId !== msg.active_id;
      if (keep) {
        viewJob(state.viewingId);
      } else {
        state.viewingId = msg.active_id;
        const token = logSync.begin(msg.active_id);
        logSync.settle(token, { output: msg.output, end: msg.end });
        termClear(!msg.output);
        if (msg.output) termWrite(msg.output);
      }
      renderQueue();
      break;
    }
    case "job_added":
      upsertJob(msg.job);
      renderQueue();
      break;
    case "status": {
      const prev = state.jobs.get(msg.job.id);
      const wasActive = prev && !FINISHED.includes(prev.status);
      upsertJob(msg.job);
      refreshJob(msg.job.id);              // just this card (moved between lists if needed)
      if (msg.job.status === "running" && (!prev || prev.status !== "running")) {
        // With several jobs running at once, only auto-focus if the user isn't already
        // watching another live job (don't yank the terminal away from them).
        const cur = state.jobs.get(state.viewingId);
        if (!cur || FINISHED.includes(cur.status)) viewJob(msg.job.id);
      } else if (wasActive && FINISHED.includes(msg.job.status)) {
        notifyFinished(msg.job);
      }
      break;
    }
    case "output":
    case "key_done":
      if (logSync.live(msg.job_id, msg) === "apply") applyLive(msg);
      break;
    case "progress": {
      const j = state.jobs.get(msg.job_id);
      if (!j) break;
      j.progress = msg.progress;
      j.label = msg.label;
      // A finished track updates the card's live tally ("57 saved · 2 not found").
      if (msg.counts) { j.counts = msg.counts; refreshJob(msg.job_id); } else updateJobProgress(msg.job_id);
      break;
    }
    case "removed":
      state.jobs.delete(msg.job_id);
      state.order = state.order.filter((i) => i !== msg.job_id);
      if (state.viewingId === msg.job_id) { state.viewingId = null; logSync.begin(null); termClear(true); }
      renderQueue();
      break;
  }
}

function notifyFinished(job) {
  const card = Core.describeJob(job, jobCtx());
  const short = trunc(card.title, 48);
  if (job.status === "done") {
    if (card.tone === "issues") {
      toast(`⚠ ${short}: ${card.outcome || "finished with issues"}`, "info", 9000);
    } else if (canSave() && job.has_files) {
      // Hosted mode delivers the file through the browser, so the job isn't really "done"
      // for the user until they hit Save — say so, and note that it won't wait forever.
      const where = state.meta.local ? "saved on your PC — click ⤓ Save for a copy here"
                                     : "click ⤓ Save to download it";
      toast(`✓ Ready: ${short} — ${where}`, "success", 7000);
    } else {
      toast(`✓ Finished: ${short}`, "success");
    }
  }
  else if (job.status === "error") toast(`✕ Failed: ${short}${card.note ? ` — ${card.note}` : ""}`, "error", 7000);
  else if (job.status === "cancelled") toast(`Cancelled: ${short}`, "info");
}

/* ---------------- api helpers ---------------- */
function offlineMessage() {
  // Through the tunnel a failed request is usually the PC being off/asleep, or the Cloudflare
  // login expiring (its redirect to the login page is cross-origin, which fetch reports as a
  // failure).
  return state.meta.remote
    ? "Can't reach your home PC — it may be off or asleep, or your Cloudflare login expired. Reload to sign in again."
    : "Can't reach OmniDL — is it still running?";
}

// Every request goes through OmniCore.requestJson, which turns network failures, HTTP errors,
// HTML error pages and malformed replies into { ok: false, error }. Nothing below treats a
// reply as success without checking `ok`.
// Requests that can't finish leave buttons busy forever, so each has a deadline: 30 s by
// default, none only for the few that legitimately take minutes (library scan, updating yt-dlp).
const DEFAULT_DEADLINE_MS = 30000;

async function api(path, method = "GET", body, { quiet = false, timeoutMs = DEFAULT_DEADLINE_MS } = {}) {
  const r = await Core.requestJson(fetch, path, { method, body, offlineMessage: offlineMessage(), timeoutMs });
  if (!r.ok && r.offline && !quiet) toast(r.error, "error", 8000);
  return r;
}

/* ---------------- access gate (hosted, YouTube/Spotify only) ---------------- */
// Mirrors engines.needs_youtube_account() so the UI can warn before submitting.
function isLink(text) {
  const t = (text || "").trim().toLowerCase();
  return /^https?:\/\//.test(t) || t.startsWith("spotify:");
}

// Where typed text is searched: the engine dropdown picks a service, Video mode always
// searches YouTube. Mirrors _TEXT_SERVICES / _needs_account on the server.
function textService() {
  if (state.mediaType === "video") return "youtube";
  return { youtube: "youtube", soundcloud: "soundcloud" }[$("#engine").value] || "auto";
}

function needsUnlock(text, service = textService()) {
  const t = (text || "").trim().toLowerCase();
  if (!t) return false;
  if (t.includes("youtube.com") || t.includes("youtu.be")) return true;
  if (t.includes("open.spotify.com") || t.startsWith("spotify:")) return true;
  if (t.includes("soundcloud.com")) return false;
  if (isLink(t)) return false;
  return service !== "soundcloud";          // a SoundCloud-only search never touches YouTube
}

function gateBlocks(text, service) {
  return state.meta.gated && !state.meta.unlocked && needsUnlock(text, service);
}

function openUnlock() {
  $("#unlock-error").textContent = "";
  $("#unlock-input").value = "";
  openModal("#unlock-modal", "#unlock-input");
}
function closeUnlock() { closeModal("#unlock-modal"); }

async function doUnlock() {
  const pass = $("#unlock-input").value;
  if (!pass) return;
  await submitGuard.run("unlock", async () => {
    const r = await api("/api/unlock", "POST", { passphrase: pass });
    if (r.ok && r.data.ok) {
      state.meta.unlocked = true;
      closeUnlock();
      toast("Unlocked — YouTube & Spotify enabled", "success");
      updateChip();
      if (state.pendingInput) { $("#input").value = state.pendingInput; state.pendingInput = null; submitDownload(); }
    } else {
      $("#unlock-error").textContent = r.error || "Unlock failed.";
    }
  });
}

async function submitDownload() {
  const input = $("#input").value.trim();
  if (!input) return;
  // Prompt before spending a request we know will be refused.
  if (gateBlocks(input)) { state.pendingInput = input; openUnlock(); return; }
  const btn = $("#download");
  // One submission at a time: a double click or a repeated Enter used to queue it twice.
  await submitGuard.run("download", async () => {
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    try {
      const payload = downloadPayload(input, $("#engine").value);
      // Same request again after a lost reply = same token, so the server won't queue it twice.
      payload.client_token = downloadTokens.forInput(JSON.stringify(payload));
      const r = await api("/api/download", "POST", payload);
      if (r.needsUnlock) { state.pendingInput = input; openUnlock(); return; }
      // Anything but a confirmed job keeps what was typed, so nothing silently vanishes.
      if (!r.ok) { if (!r.offline) toast(r.error, "error", 6000); return; }
      if (!Core.isJob(r.data)) {
        toast("OmniDL sent an unexpected reply — check the queue before trying again.", "error", 7000);
        return;
      }
      downloadTokens.done();
      toast(`Queued · ${r.data.engine_label}`, "info");
      if ($("#input").value.trim() === input) $("#input").value = "";
      updateChip();
    } finally {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
    }
  });
  $("#input").focus();
}

/* ---------------- settings ---------------- */
function fillSelect(sel, items, current, labels) {
  sel.innerHTML = "";
  const list = Array.isArray(items) ? [...items] : [];
  if (current && !list.includes(current)) list.push(current);
  for (const it of list) {
    const opt = document.createElement("option");
    opt.value = it;
    opt.textContent = (labels && labels[it]) || it;
    if (it === current) opt.selected = true;
    sel.append(opt);
  }
}

function fmtTTL(seconds) {
  const mins = Math.round((Number(seconds) || 0) / 60);
  if (mins >= 120) return `${Math.round(mins / 60)} hours`;
  if (mins >= 60) return mins === 60 ? "1 hour" : `${mins} minutes`;
  return `${mins} minutes`;
}

const BITRATE_LABELS = { auto: "Auto (192 kbps)", "128k": "128 kbps", "160k": "160 kbps",
                         "192k": "192 kbps", "256k": "256 kbps", "320k": "320 kbps" };

function loadSettingsForm() {
  const s = state.settings;
  const local = !!state.meta.local;
  $("#output-dir-field").hidden = !local;
  $("#cookie-file-field").hidden = !local;
  $("#hosted-destination").hidden = local;
  if (local) {
    $("#set-output-dir").value = s.output_dir || "";
    $("#set-cookie-file").value = s.cookie_file || "";
    $("#output-dir-scope").textContent = state.meta.remote
      ? "(a folder on your home PC, not this device)" : "(where files are saved on this PC)";
    $("#output-dir-where").textContent = state.meta.remote
      ? "Downloads go into this folder on your home PC. To get a copy on this device, use ⤓ Save on a finished download."
      : "Downloads go straight into this folder, and Skip already-downloaded checks it in every format.";
  } else {
    $("#hosted-destination").textContent =
      `Downloads are prepared on the server — use ⤓ Save to get them. They're deleted after ${fmtTTL(state.meta.file_ttl)}.`;
  }
  clearFieldError();
  const maxc = state.meta.max_concurrency || 4;
  $("#set-concurrency").max = maxc;
  $("#set-concurrency").value = Math.min(s.concurrency || 3, maxc);
  $("#set-strict").checked = s.strict_matching !== false;
  $("#set-prefer-ytmusic").checked = s.prefer_ytmusic !== false;
  $("#set-match-duration").checked = s.spotify_match_duration !== false;
  $("#set-sponsorblock").checked = !!s.sponsorblock;
  $("#set-skip-existing").checked = s.skip_existing !== false;
  fillSelect($("#set-format"), state.meta.formats, s.audio_format);
  fillSelect($("#set-bitrate"), state.meta.bitrates, s.bitrate || "auto", BITRATE_LABELS);
  fillSelect($("#set-video-container"), state.meta.video_containers, s.video_container || "mp4");
  fillSelect($("#set-naming-order"), state.meta.naming_orders, s.naming_order || "artist-title",
             { "artist-title": "Artist - Title", "title-artist": "Title - Artist" });
  fillSelect($("#set-naming-artists"), state.meta.naming_artists, s.naming_artists || "all",
             { all: "All credited artists", primary: "Lead artist only" });
  updateNamingPreview();
  updateFormatNotes();
  setSettingsDirty(false);
}

function setSettingsDirty(dirty) {
  state.settingsDirty = dirty;
  const status = $("#settings-status");
  status.className = "status-msg" + (dirty ? " warn" : "");
  status.textContent = dirty ? "Unsaved changes" : "";
}

function showFieldError(message) {
  const box = $("#output-dir-error");
  box.textContent = message;
  box.hidden = false;
  $("#set-output-dir").setAttribute("aria-invalid", "true");
  $("#set-output-dir").focus();
}
function clearFieldError() {
  $("#output-dir-error").hidden = true;
  $("#output-dir-error").textContent = "";
  $("#set-output-dir").removeAttribute("aria-invalid");
}

// Show the exact filename the current choice produces. Naming is a compatibility setting,
// not a cosmetic one - picking the convention that does not match an existing library
// silently re-downloads everything under a second name.
function updateNamingPreview() {
  const node = $("#naming-preview");
  if (!node) return;
  const order = ($("#set-naming-order") || {}).value || "artist-title";
  const mode = ($("#set-naming-artists") || {}).value || "all";
  const who = mode === "primary" ? "Justin Bieber" : "Justin Bieber, Daniel Caesar, Giveon";
  const ext = ($("#set-format") || {}).value || "opus";
  node.textContent = (order === "title-artist" ? `Peaches - ${who}` : `${who} - Peaches`) + "." + ext;
}

// What the chosen format really does for each source — the controls used to promise things
// the downloaders ignored.
function updateFormatNotes() {
  const fmt = $("#set-format").value;
  const notes = [];
  if (fmt === "opus") notes.push("Opus is YouTube's own audio, saved without re-encoding — the smallest file at full source quality.");
  if (fmt === "m4a") notes.push("M4A keeps YouTube's AAC stream when there is one, otherwise converts at the bitrate below.");
  if (fmt === "mp3") notes.push("MP3 is always converted, at the bitrate below.");
  if (fmt === "flac" || fmt === "wav") {
    notes.push(`${fmt.toUpperCase()} from YouTube or SoundCloud is made from compressed audio: a bigger file, no extra quality. The bitrate doesn't apply.`);
  } else {
    notes.push("Bitrate is only used when audio has to be converted; a stream that's already in the chosen format is kept as it is.");
  }
  const sc = (state.meta.soundcloud_format_notes || {})[fmt];
  if (sc) notes.push(sc);
  $("#format-notes").replaceChildren(...notes.map((n) => el("li", "", n)));
}

async function saveSettings() {
  const payload = {
    audio_format: $("#set-format").value,
    video_container: $("#set-video-container").value,
    naming_order: $("#set-naming-order").value,
    naming_artists: $("#set-naming-artists").value,
    bitrate: $("#set-bitrate").value,
    concurrency: $("#set-concurrency").value,
    strict_matching: $("#set-strict").checked,
    prefer_ytmusic: $("#set-prefer-ytmusic").checked,
    spotify_match_duration: $("#set-match-duration").checked,
    sponsorblock: $("#set-sponsorblock").checked,
    skip_existing: $("#set-skip-existing").checked,
  };
  if (state.meta.local) {
    payload.output_dir = $("#set-output-dir").value.trim();
    payload.cookie_file = $("#set-cookie-file").value.trim();
  }
  await submitGuard.run("settings", async () => {
    const btn = $("#save-settings");
    const status = $("#settings-status");
    const sentRevision = state.settingsRevision;
    btn.disabled = true;
    clearFieldError();
    status.className = "status-msg";
    status.textContent = "Saving…";
    try {
      const r = await api("/api/settings", "POST", payload);
      if (!r.ok || !Core.isSettings(r.data)) {
        // Nothing changes on failure: the form keeps your edits and the app keeps the last
        // settings that were really saved.
        const message = r.ok ? "OmniDL sent an unexpected reply — nothing was saved." : r.error;
        if (r.field === "output_dir") showFieldError(message);
        status.className = "status-msg error";
        status.textContent = "Not saved";
        if (!r.offline) toast(message, "error", 7000);
        return;
      }
      state.settings = r.data;
      state.lastAudioFormat = state.settings.audio_format;
      refreshFormatOptions();
      updateChip();
      if (state.settingsRevision !== sentRevision) {
        // Changed again while saving: keep those newer edits on screen (reloading the form
        // used to put the old values back and claim "Saved ✓").
        status.className = "status-msg warn";
        status.textContent = "Saved — newer changes not saved yet";
        toast("Saved what you'd set — press Save again for your newer changes.", "info", 6000);
        return;
      }
      loadSettingsForm();
      status.className = "status-msg";
      status.textContent = "Saved ✓";
      setTimeout(() => { if (status.textContent === "Saved ✓") status.textContent = ""; }, 2500);
      toast("Settings saved", "success");
      if (r.data.cookie_warning) toast("⚠ " + r.data.cookie_warning, "error", 6000);
    } finally {
      btn.disabled = false;
    }
  });
}

function openSettings() { loadSettingsForm(); openModal("#settings-modal"); }
function closeSettings() { closeModal("#settings-modal"); }

/* ---------------- dialogs ---------------- */
// One controller for every dialog: focus moves in, Tab stays inside, Escape closes the top
// one, focus returns to whatever opened it, and the page behind is inert.
const modalStack = [];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
                  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null || node === document.activeElement);
}

function setBackgroundInert(on) {
  for (const sel of [".topbar", "#conn-banner", ".smartbar", "#workspace"]) {
    const node = $(sel);
    if (node) node.inert = on;
  }
}

function openModal(id, focusSel) {
  const modal = $(id);
  if (!modal.classList.contains("hidden")) return;
  modalStack.push({ modal, restore: document.activeElement });
  modal.classList.remove("hidden");
  setBackgroundInert(true);
  for (const other of modalStack.slice(0, -1)) other.modal.inert = true;
  const box = modal.querySelector(".modal-box");
  const target = (focusSel && modal.querySelector(focusSel)) || focusables(box)[0] || box;
  if (target === box) box.tabIndex = -1;
  target.focus();
}

function closeModal(id) {
  const modal = $(id);
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  const at = modalStack.findIndex((m) => m.modal === modal);
  const entry = at >= 0 ? modalStack.splice(at, 1)[0] : null;
  const top = modalStack[modalStack.length - 1];
  if (top) top.modal.inert = false;
  else setBackgroundInert(false);
  const back = entry && entry.restore;
  if (back && document.contains(back) && back.offsetParent !== null) back.focus();
}

function topModal() { return modalStack.length ? modalStack[modalStack.length - 1].modal : null; }

function trapKeys(e) {
  const modal = topModal();
  if (!modal) return;
  if (e.key === "Escape") {
    e.preventDefault();
    closeModal("#" + modal.id);
    return;
  }
  if (e.key !== "Tab") return;
  const items = focusables(modal.querySelector(".modal-box"));
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
    e.preventDefault();
    first.focus();
  }
}

/* ---------------- bulk paste ---------------- */
// Parsing happens on the server so the preview and the download can never disagree. The
// draft (OmniCore.createBulkDraft) is what you're building: it survives closing the dialog
// and reloads, keeps your row edits across re-parses, and only allows Download once the rows
// describe the text that's actually in the box.
const BULK_KEY = "omnidl-bulk-draft";
let bulkTimer = null;
let bulkDraft = Core.createBulkDraft(loadBulkDraft());

function loadBulkDraft() {
  try { return JSON.parse(localStorage.getItem(BULK_KEY) || "null"); } catch (_) { return null; }
}
function saveBulkDraft() {
  try { localStorage.setItem(BULK_KEY, JSON.stringify(bulkDraft.serialize())); } catch (_) {}
}

function openBulk() {
  $("#bulk-text").value = bulkDraft.text;
  $("#bulk-service").value = bulkDraft.service;
  $("#bulk-name").value = bulkDraft.name;
  $("#bulk-strip").checked = bulkDraft.strip;
  renderBulk();
  if (bulkDraft.needsParse()) refreshBulkPreview();
  openModal("#bulk-modal", "#bulk-text");
}
function closeBulk() { closeModal("#bulk-modal"); }

function onBulkText() {
  bulkDraft.setText($("#bulk-text").value);
  saveBulkDraft();
  renderBulkState();
  clearTimeout(bulkTimer);
  bulkTimer = setTimeout(refreshBulkPreview, 250);
}

async function refreshBulkPreview() {
  clearTimeout(bulkTimer);
  const revision = bulkDraft.revision;
  const text = bulkDraft.text;
  if (!text.trim()) { bulkDraft.applyPreview(revision, { entries: [] }); renderBulk(); return; }
  const body = { text };
  // Checked = remove labels on the lines that have one (judged line by line); unchecked =
  // never. There is no "force": that would also strip real title parts like "- Live".
  if (!bulkDraft.strip) body.strip_labels = false;
  renderBulkState();
  const r = await api("/api/bulk/preview", "POST", body, { quiet: true, timeoutMs: 20000 });
  if (!r.ok) { if (bulkDraft.previewFailed(revision, r.error)) renderBulkState(); return; }
  if (bulkDraft.applyPreview(revision, r.data)) renderBulk();
}

function renderBulkState() {
  const counts = bulkDraft.counts();
  const hasText = !!bulkDraft.text.trim();
  const parsing = hasText && bulkDraft.needsParse() && !bulkDraft.error;
  const summary = $("#bulk-summary");
  if (bulkDraft.error) {
    summary.textContent = `Couldn't read the list: ${bulkDraft.error}`;
  } else if (!hasText) {
    summary.textContent = "Paste songs or links above.";
  } else if (parsing) {
    summary.textContent = "Reading your list…";
  } else {
    const songs = bulkDraft.selected().filter((r) => r.kind !== "url").length;
    const links = bulkDraft.selected().length - songs;
    const kinds = [songs && `${songs} song${songs === 1 ? "" : "s"}`, links && `${links} link${links === 1 ? "" : "s"}`]
      .filter(Boolean).join(" · ");
    summary.textContent = counts.total
      ? `${counts.selected} of ${counts.total} selected` + (kinds ? ` — ${kinds}` : "")
      : "Nothing to download in that text.";
  }
  const cap = $("#bulk-cap");
  cap.hidden = !(bulkDraft.ready() && bulkDraft.capped);
  if (!cap.hidden) {
    cap.textContent = `Only the first ${bulkDraft.maxLines} lines will be downloaded — that's the limit` +
      (state.meta.local ? "." : " here.") + " Put the rest in another list.";
  }
  const btn = $("#do-bulk");
  btn.disabled = state.bulkSubmitting || !bulkDraft.ready() || !counts.selected;
  btn.textContent = state.bulkSubmitting ? "Queueing…" : parsing ? "Reading…"
    : counts.selected ? `Download ${counts.selected}` : "Download";
}

function renderBulk() {
  const host = $("#bulk-preview");
  const rows = bulkDraft.rows.map((r, i) => {
    const row = el("div", "bulk-row" + (r.include ? "" : " excluded"));
    const name = r.kind === "url" ? r.url : [r.artist, r.title].filter(Boolean).join(" - ");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.className = "bulk-include";
    tick.checked = r.include;
    tick.setAttribute("aria-label", `Include ${name || "this line"}`);
    tick.onchange = () => { bulkDraft.includeRow(i, tick.checked); row.classList.toggle("excluded", !tick.checked); saveBulkDraft(); renderBulkState(); };
    row.append(tick);
    if (r.kind === "url") {
      row.append(el("span", "bulk-tag link", "link"));
      const u = el("span", "bulk-url", r.url);
      u.title = r.url;
      row.append(u);
    } else {
      // Editable, because a mis-parsed line should cost one keystroke to fix — not a wrong
      // file on disk. Built with DOM calls: these strings are pasted by the user.
      const field = (cls, key, placeholder) => {
        const input = document.createElement("input");
        input.className = cls;
        input.value = r[key];
        input.placeholder = placeholder;
        input.setAttribute("aria-label", `${placeholder} for line ${i + 1}`);
        input.oninput = () => { bulkDraft.editRow(i, key, input.value); saveBulkDraft(); renderBulkState(); };
        return input;
      };
      row.append(el("span", "bulk-tag", "song"), field("bulk-artist", "artist", "artist"),
                 el("span", "bulk-dash", "–"), field("bulk-title", "title", "title"));
      if (r.label) {
        const tag = el("span", "bulk-label", r.label);
        tag.title = "Ignored label";
        row.append(tag);
      }
    }
    row.append(makeButton("✕", "Remove this line from the list", () => {
      bulkDraft.removeRow(i);
      $("#bulk-text").value = bulkDraft.text;
      saveBulkDraft();
      refreshBulkPreview();
    }, "bulk-remove"));
    return row;
  });
  host.replaceChildren(...rows);
  if (state.bulkSubmitting) setBulkLocked(true);
  renderBulkState();
}

// While a list is being sent its controls are locked: an edit made then used to be wiped out
// by the reply ("clear the list you sent" cleared the new one too).
function setBulkLocked(locked) {
  $("#bulk-text").readOnly = locked;
  for (const id of ["#bulk-service", "#bulk-name", "#bulk-strip", "#clear-bulk"]) $(id).disabled = locked;
  for (const node of document.querySelectorAll("#bulk-preview input, #bulk-preview button")) node.disabled = locked;
  $("#bulk-preview").classList.toggle("locked", locked);
}

async function doBulkDownload() {
  if (!bulkDraft.ready()) return;
  const entries = bulkDraft.entries();
  if (!entries.length) return;
  await submitGuard.run("bulk", async () => {
    state.bulkSubmitting = true;
    const sentRevision = bulkDraft.revision;
    setBulkLocked(true);
    renderBulkState();
    $("#bulk-status").textContent = "Queueing…";
    try {
      const payload = { entries, service: bulkDraft.service, name: bulkDraft.name.trim(), format: audioFormat() };
      payload.client_token = bulkTokens.forInput(JSON.stringify(payload));
      const r = await api("/api/bulk/download", "POST", payload);
      if (r.needsUnlock) { $("#bulk-status").textContent = ""; closeBulk(); openUnlock(); return; }
      if (!r.ok || !Array.isArray(r.data.jobs)) {
        // The list stays exactly as it is.
        const message = r.ok ? "OmniDL sent an unexpected reply — check the queue before trying again." : r.error;
        $("#bulk-status").textContent = message;
        if (!r.offline) toast(message, "error", 6000);
        return;
      }
      bulkTokens.done();
      const res = r.data;
      const parts = [];
      if (res.tracks) parts.push(`${res.tracks} song${res.tracks === 1 ? "" : "s"}`);
      if (res.links) parts.push(`${res.links} link${res.links === 1 ? "" : "s"}`);
      if (parts.length) toast(`Queued · ${parts.join(" + ")}`, "success");
      const held = res.held_back_urls || [];
      if (bulkDraft.revision !== sentRevision) {
        // The list changed after it was sent (it can't while locked, but never risk it):
        // keep the newer list rather than clearing it.
        $("#bulk-status").textContent = "Queued the list you sent — your newer changes are still here.";
      } else if (held.length) {
        // Keep exactly the links that weren't queued, so nothing has to be pasted again.
        bulkDraft.keepOnly(held);
        $("#bulk-text").value = bulkDraft.text;
        saveBulkDraft();
        refreshBulkPreview();
        $("#bulk-status").textContent =
          `${held.length} link${held.length === 1 ? " was" : "s were"} held back by the download limit — ` +
          "still in your list. Download them once some finish.";
      } else {
        bulkDraft.clear();
        bulkDraft.setName("");
        $("#bulk-text").value = "";
        $("#bulk-name").value = "";
        saveBulkDraft();
        $("#bulk-status").textContent = "";
        renderBulk();
        closeBulk();
      }
    } finally {
      state.bulkSubmitting = false;
      setBulkLocked(false);
      if ($("#bulk-status").textContent === "Queueing…") $("#bulk-status").textContent = "";
      renderBulkState();
    }
  });
}

function clearBulk() {
  bulkDraft.clear();
  $("#bulk-text").value = "";
  saveBulkDraft();
  $("#bulk-status").textContent = "";
  renderBulk();
  $("#bulk-text").focus();
}

// The audio format to send. In Video mode the toolbar's format box holds a video quality
// ("1080p"), which used to be sent as the audio format — for Bulk, and for any engine that
// can't do video — and every track then failed to convert.
function audioFormat() {
  if (state.mediaType !== "video") return $("#format").value || state.settings.audio_format || "opus";
  return state.lastAudioFormat || state.settings.audio_format || "opus";
}

/* ---------------- music library ---------------- */
const ISSUE_LABELS = {
  unreadable: "Unreadable",
  missing_title: "Missing title",
  missing_artist: "Missing artist",
  missing_album: "Missing album",
  missing_artwork: "Missing artwork",
  low_bitrate: "Low bitrate",
};

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / (1024 ** index);
  return `${amount.toFixed(index === 0 || amount >= 10 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function qualityLabel(track) {
  const parts = [(track.codec || "unknown").toUpperCase()];
  if (track.bitrate) parts.push(`${Math.round(track.bitrate / 1000)} kbps`);
  if (track.sample_rate) parts.push(`${(track.sample_rate / 1000).toFixed(1)} kHz`);
  parts.push(formatDuration(track.duration));
  parts.push(formatBytes(track.size));
  return parts.join(" / ");
}

function renderLibrarySummary(summary) {
  const host = $("#library-summary");
  const stats = [
    ["Tracks", summary.total_files],
    ["Library size", formatBytes(summary.total_size)],
    ["Need attention", summary.files_with_issues],
    ["Fixable tags", summary.repairable_files],
    ["Duplicate groups", summary.duplicate_groups],
    ["Potential savings", formatBytes(summary.reclaimable_size)],
  ];
  host.replaceChildren(...stats.map(([label, value]) => {
    const card = el("div", "library-stat");
    card.append(el("strong", "", value), el("span", "", label));
    return card;
  }));
  host.classList.remove("hidden");
}

function duplicateCard(group) {
  const card = el("article", "library-card duplicate-card");
  const head = el("div", "library-card-head");
  const identity = el("div", "library-identity");
  identity.append(el("strong", "", group.title || "Unknown title"), el("span", "", group.artist || "Unknown artist"));
  head.append(identity, el("span", "library-saving", `${formatBytes(group.reclaimable_size)} duplicated`));
  card.append(head);

  const files = el("div", "duplicate-files");
  for (const file of group.files || []) {
    const row = el("div", `duplicate-file${file.recommended ? " recommended" : ""}`);
    const details = el("div", "duplicate-file-main");
    const title = el("div", "library-path", file.path);
    if (file.recommended) title.append(el("span", "recommended-badge", "Best copy"));
    details.append(title, el("span", "library-quality", qualityLabel(file)));
    row.append(details, el("span", "quality-score", `${file.quality_score}/100`));
    files.append(row);
  }
  card.append(files);
  return card;
}

// Repair shows exactly what it will write before writing it. The filename is read in the
// order you choose (not assumed from the download setting), a name that can't be read
// unambiguously isn't guessed at, and existing tags are never changed.
async function openRepair(card, track, trigger, order) {
  trigger.disabled = true;
  const r = await api("/api/library/repair/preview", "POST", { path: track.path, order: order || $("#library-order").value });
  trigger.disabled = false;
  if (!r.ok) { if (!r.offline) toast(r.error, "error"); return; }
  const p = r.data;
  const old = card.querySelector(".repair-editor");
  const editor = el("div", "repair-editor");
  editor.setAttribute("role", "group");
  editor.setAttribute("aria-label", `Tags to write for ${p.filename}`);
  editor.append(el("p", "repair-from", `From the filename “${p.filename}”`));
  if (p.problem) {
    editor.append(el("p", "repair-problem", `${p.problem.charAt(0).toUpperCase()}${p.problem.slice(1)}. Type the tags below.`));
  } else {
    editor.append(el("p", "repair-help", "Check these before saving. Only empty tags are written; existing ones are never changed."));
  }
  const orderLabel = el("label", "repair-order", "Filename is ");
  const orderSel = document.createElement("select");
  fillSelect(orderSel, ["artist-title", "title-artist"], p.order, { "artist-title": "Artist - Title", "title-artist": "Title - Artist" });
  orderSel.onchange = () => openRepair(card, track, trigger, orderSel.value);
  orderLabel.append(orderSel);
  editor.append(orderLabel);

  const inputs = {};
  for (const key of ["artist", "title"]) {
    const label = el("label", "repair-field", key === "artist" ? "Artist" : "Title");
    const input = document.createElement("input");
    input.type = "text";
    if (p.current[key]) {
      input.value = p.current[key];
      input.readOnly = true;
      label.append(el("span", "muted", " (already set — kept)"));
    } else {
      input.value = p.proposed[key] || "";
      input.required = true;
    }
    inputs[key] = input;
    label.append(input);
    editor.append(label);
  }
  const error = el("p", "field-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  const actions = el("div", "repair-actions");
  const save = makeButton("Save tags", "Write the empty tags shown above", async () => {
    const body = { path: track.path, order: orderSel.value };
    for (const key of ["artist", "title"]) {
      if (inputs[key].readOnly) continue;
      const value = inputs[key].value.trim();
      if (!value) {
        error.textContent = `Enter the ${key} first.`;
        error.hidden = false;
        inputs[key].focus();
        return;
      }
      body[key] = value;
    }
    save.disabled = true;
    const result = await api("/api/library/repair", "POST", body);
    save.disabled = false;
    if (!result.ok) { error.textContent = result.error; error.hidden = false; return; }
    const changed = (result.data.changed || []).join(" and ");
    toast(changed ? `Updated ${changed}` : "Tags were already complete", "success");
    await scanLibrary();
  }, "primary small");
  const cancel = makeButton("Cancel", "Close without writing anything", () => { editor.remove(); trigger.hidden = false; trigger.focus(); }, "ghost small");
  actions.append(save, cancel);
  editor.append(error, actions);
  if (old) old.replaceWith(editor); else card.append(editor);
  trigger.hidden = true;
  const first = Object.values(inputs).find((i) => !i.readOnly && !i.value) || save;
  first.focus();
}

function issueCard(track) {
  const card = el("article", "library-card issue-card");
  const head = el("div", "library-card-head");
  const identity = el("div", "library-identity");
  identity.append(el("strong", "", track.display_title || "Unknown title"), el("span", "", track.display_artist || "Unknown artist"));
  head.append(identity, el("span", "quality-score", `${track.quality_score}/100`));
  card.append(head, el("div", "library-path", track.path));

  const footer = el("div", "library-card-footer");
  const issues = el("div", "issue-badges");
  for (const issue of track.issues || []) issues.append(el("span", `issue-badge issue-${issue}`, ISSUE_LABELS[issue] || issue));
  footer.append(issues);
  if (track.repairable) {
    const repair = el("button", "ghost library-repair", "Fill artist/title…");
    repair.type = "button";
    repair.onclick = () => openRepair(card, track, repair);
    footer.append(repair);
  }
  card.append(footer, el("div", "library-quality", qualityLabel(track)));
  return card;
}

function renderLibrary() {
  const report = state.libraryReport;
  if (!report) return;
  const view = Core.libraryView(report, $("#library-search").value);
  $("#library-duplicates").replaceChildren(...view.duplicates.map(duplicateCard));
  $("#library-issues").replaceChildren(...view.issues.map(issueCard));
  // Filtered counts say "of" the real totals, so a filter never hides that problems exist.
  $("#library-duplicate-count").textContent = view.filtered
    ? `${view.duplicates.length} of ${view.totalDuplicates}` : String(view.totalDuplicates);
  $("#library-issue-count").textContent = view.filtered
    ? `${view.issues.length} of ${view.totalIssues}` : String(view.totalIssues);
  $("#library-duplicates-section").classList.toggle("hidden", view.duplicates.length === 0);
  $("#library-issues-section").classList.toggle("hidden", view.issues.length === 0);
  const empty = $("#library-empty");
  empty.classList.toggle("hidden", !view.empty);
  empty.classList.toggle("neutral", view.empty !== "clean");
  $("#library-clear-filter").hidden = view.empty !== "no-match";
  if (view.empty === "clean") {
    $("#library-empty-title").textContent = "Your library looks clean.";
    $("#library-empty-text").textContent = "No metadata problems or duplicate groups were found.";
  } else if (view.empty === "no-files") {
    $("#library-empty-title").textContent = "No audio files found.";
    $("#library-empty-text").textContent = `There's nothing to check in ${report.root} yet.`;
  } else if (view.empty === "no-match") {
    $("#library-empty-title").textContent = "No matching results.";
    $("#library-empty-text").textContent =
      `Nothing matches “${$("#library-search").value.trim()}”. The scan found ${view.totalIssues} ` +
      `file${view.totalIssues === 1 ? "" : "s"} needing attention and ${view.totalDuplicates} duplicate ` +
      `group${view.totalDuplicates === 1 ? "" : "s"}.`;
  }
}

async function scanLibrary() {
  const button = $("#scan-library");
  button.disabled = true;
  button.textContent = "Scanning...";
  $("#library-status").textContent = "Reading audio files and metadata...";
  try {
    const r = await api("/api/library/scan", "POST", { order: $("#library-order").value }, { timeoutMs: 0 });
    if (!r.ok) {
      $("#library-status").textContent = r.error;
      if (!state.libraryReport) {
        const empty = $("#library-empty");
        empty.classList.remove("hidden");
        empty.classList.add("neutral");
        $("#library-empty-title").textContent = "Couldn't scan the library.";
        $("#library-empty-text").textContent = r.error;
        $("#library-clear-filter").hidden = true;
      }
      if (!r.offline) toast(r.error, "error", 7000);
      return;
    }
    state.libraryReport = r.data;
    renderLibrarySummary(r.data.summary);
    renderLibrary();
    $("#library-status").textContent = `Scanned ${r.data.root}`;
    toast(`Library scan complete: ${r.data.summary.total_files} tracks`, "success");
  } finally {
    button.disabled = false;
    button.textContent = "Scan again";
  }
}

function openLibrary() {
  $("#library-order").value = state.settings.naming_order || "artist-title";
  openModal("#library-modal", "#library-search");
  if (!state.libraryReport) scanLibrary();
}

function closeLibrary() { closeModal("#library-modal"); }

/* ---------------- theme ---------------- */
function applyTheme(theme) {
  const light = theme === "light";
  document.documentElement.classList.toggle("light", light);
  $("#theme-toggle").textContent = light ? "🌙" : "☀";
  $("#theme-toggle").title = light ? "Switch to dark theme" : "Switch to light theme";
}
function toggleTheme() {
  const next = document.documentElement.classList.contains("light") ? "dark" : "light";
  try { localStorage.setItem("omnidl-theme", next); } catch (_) {}
  applyTheme(next);
}

/* ---------------- layout: phone log toggle, desktop splitter ---------------- */
const phoneQuery = window.matchMedia("(max-width: 760px)");

function setTermCollapsed(collapsed, persist = true) {
  $("#terminal-wrap").classList.toggle("collapsed", collapsed);
  const btn = $("#toggle-term");
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.textContent = collapsed ? "Show" : "Hide";
  btn.title = collapsed ? "Show the live output" : "Hide the live output";
  if (persist) { try { localStorage.setItem("omnidl-term", collapsed ? "collapsed" : "open"); } catch (_) {} }
}

function initLayout() {
  let saved = null;
  try { saved = localStorage.getItem("omnidl-term"); } catch (_) {}
  // On a phone the log starts folded away: the card above has the controls that matter.
  setTermCollapsed(saved ? saved === "collapsed" : phoneQuery.matches, false);

  const workspace = $("#workspace");
  const splitter = $("#splitter");
  let width = 340;
  try { width = Number(localStorage.getItem("omnidl-queue-w")) || 340; } catch (_) {}
  const apply = (w) => {
    width = Math.max(260, Math.min(640, Math.round(w)));
    workspace.style.setProperty("--queue-w", `${width}px`);
    splitter.setAttribute("aria-valuenow", String(width));
  };
  const persist = () => { try { localStorage.setItem("omnidl-queue-w", String(width)); } catch (_) {} };
  apply(width);
  splitter.addEventListener("pointerdown", (e) => {
    splitter.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startW = width;
    splitter.classList.add("dragging");
    splitter.onpointermove = (ev) => apply(startW + (startX - ev.clientX));
    splitter.onpointerup = () => {
      splitter.onpointermove = null;
      splitter.classList.remove("dragging");
      persist();
    };
  });
  splitter.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 60 : 20;
    if (e.key === "ArrowLeft") apply(width + step);
    else if (e.key === "ArrowRight") apply(width - step);
    else if (e.key === "Home") apply(640);
    else if (e.key === "End") apply(260);
    else return;
    e.preventDefault();
    persist();
  });
}

/* ---------------- wire up ---------------- */
// Save hands files to the device you're using. Hosted: always (that's the only way to get
// them). A home install: only when reached through the tunnel — at the PC itself the files
// are already in your library, and Save would just make a second copy in Downloads.
function canSave() {
  return !state.meta.local || !!state.meta.remote;
}

function showYtdlpStatus(version, age) {
  const node = $("#ytdlp-status");
  if (!node) return;
  node.textContent = version ? `${version}${age != null ? ` · ${age} day${age === 1 ? "" : "s"} old` : ""}` : "unknown";
  node.classList.toggle("stale", age != null && age > 21);
}

async function updateYtdlp() {
  const btn = $("#update-ytdlp");
  btn.disabled = true;
  btn.textContent = "Updating…";
  const r = await api("/api/maintenance/update-ytdlp", "POST", undefined, { timeoutMs: 0 });
  btn.disabled = false;
  btn.textContent = "Update yt-dlp";
  if (!r.ok) { if (!r.offline) toast(r.error, "error", 7000); return; }
  const res = r.data;
  showYtdlpStatus(res.after, res.age_days);
  state.meta.ytdlp_version = res.after;
  state.meta.ytdlp_age_days = res.age_days;
  toast(res.changed ? `yt-dlp updated to ${res.after} — restart OmniDL so searches use it too.`
                    : `yt-dlp is already the latest (${res.after}).`, "success", 7000);
}

function syncEnvironmentControls() {
  const local = !!state.meta.local;
  // Only a local, non-packaged install can update its own yt-dlp.
  $("#maintenance").hidden = !local || !!state.meta.frozen;
  showYtdlpStatus(state.meta.ytdlp_version, state.meta.ytdlp_age_days);
  const scope = $("#settings-scope");
  if (scope && local) {
    scope.innerHTML = "These settings are <strong>saved on your PC</strong> and apply wherever " +
                      "you use OmniDL from — the PC itself, your Mac or your phone.";
  }
  $("#open-library").hidden = !local;
  // Folder opens Explorer on the PC — invisible and confusing from another device.
  $("#open-folder").hidden = !local || !!state.meta.remote;
  $("#remote-badge").hidden = !state.meta.remote;
  $("#hosted-hint").hidden = local;
  $("#hosted-ttl").textContent = `Files are kept on the server for ${fmtTTL(state.meta.file_ttl)}, then deleted.`;
}

function bind() {
  $("#download").onclick = submitDownload;
  $("#input").addEventListener("input", updateChip);
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.repeat) { e.preventDefault(); submitDownload(); } });
  $("#engine").addEventListener("change", updateChip);
  for (const btn of document.querySelectorAll("#media-toggle .mt-btn"))
    btn.addEventListener("click", () => { if (!btn.disabled) { setMediaType(btn.dataset.media); updateChip(); } });
  $("#format").addEventListener("change", () => {
    if (state.mediaType === "video") state.lastVideoQuality = $("#format").value;
    else state.lastAudioFormat = $("#format").value;
    updateChip();
  });
  $("#open-settings").onclick = openSettings;
  $("#close-settings").onclick = closeSettings;
  $("#save-settings").onclick = saveSettings;
  $("#update-ytdlp").onclick = updateYtdlp;
  const edited = () => { state.settingsRevision += 1; setSettingsDirty(true); };
  $("#settings-modal .modal-body").addEventListener("input", edited);
  $("#settings-modal .modal-body").addEventListener("change", edited);
  for (const id of ["#set-naming-order", "#set-naming-artists", "#set-format"]) {
    $(id).addEventListener("change", updateNamingPreview);
  }
  $("#set-format").addEventListener("change", updateFormatNotes);
  $("#set-output-dir").addEventListener("input", clearFieldError);
  $("#close-library").onclick = closeLibrary;
  $("#scan-library").onclick = scanLibrary;
  $("#library-search").addEventListener("input", renderLibrary);
  $("#library-order").addEventListener("change", () => { if (state.libraryReport) scanLibrary(); });
  $("#library-clear-filter").onclick = () => { $("#library-search").value = ""; renderLibrary(); $("#library-search").focus(); };
  $("#theme-toggle").onclick = toggleTheme;
  $("#open-library").onclick = openLibrary;
  $("#open-folder").onclick = async () => {
    const r = await api("/api/open-folder", "POST");
    if (!r.ok && !r.offline) toast(r.error, "error");
  };
  $("#clear-term").onclick = () => termClear(true);
  $("#toggle-term").onclick = () => setTermCollapsed(!$("#terminal-wrap").classList.contains("collapsed"));
  $("#jump-latest").onclick = () => logView.jump();
  $("#copy-log").onclick = async () => {
    try { await navigator.clipboard.writeText(logView.copy()); toast("Log copied", "success"); }
    catch (_) { toast("Copy failed", "error"); }
  };
  $("#cancel-all").onclick = cancelAll;
  $("#clear-done").onclick = clearHistory;
  $("#history-filter").addEventListener("change", () => { state.historyFilter = $("#history-filter").value; renderQueue(); });
  $("#history-search").addEventListener("input", () => { state.historySearch = $("#history-search").value; renderQueue(); });
  $("#conn-banner-retry").onclick = () => loadStartup();
  $("#do-unlock").onclick = doUnlock;
  $("#close-unlock").onclick = closeUnlock;
  $("#open-bulk").onclick = openBulk;
  $("#close-bulk").onclick = closeBulk;
  $("#do-bulk").onclick = doBulkDownload;
  $("#clear-bulk").onclick = clearBulk;
  $("#bulk-text").addEventListener("input", onBulkText);
  // The service doesn't change how lines are read, so it never re-parses or drops edits.
  $("#bulk-service").addEventListener("change", () => { bulkDraft.setService($("#bulk-service").value); saveBulkDraft(); });
  $("#bulk-name").addEventListener("input", () => { bulkDraft.setName($("#bulk-name").value); saveBulkDraft(); });
  $("#bulk-strip").addEventListener("change", () => { bulkDraft.setStrip($("#bulk-strip").checked); saveBulkDraft(); refreshBulkPreview(); });
  $("#unlock-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doUnlock(); });
  for (const [modal, close] of [["#bulk-modal", closeBulk], ["#unlock-modal", closeUnlock],
                                ["#settings-modal", closeSettings], ["#library-modal", closeLibrary]]) {
    $(modal).addEventListener("click", (e) => { if (e.target === $(modal)) close(); });
  }
  document.addEventListener("keydown", trapKeys);
  // Keep relative times and hosted expiry countdowns current.
  setInterval(() => { if (state.order.length) renderQueue(); }, 60000);
}

let warnedStale = false;
function warnStaleYtdlp() {
  // A stale yt-dlp doesn't degrade downloads, it stops them dead (YouTube breaks old
  // releases). The .exe bundles yt-dlp at build time and never self-updates, so say so.
  if (warnedStale || state.meta.ytdlp_age_days == null || state.meta.ytdlp_age_days <= 21) return;
  warnedStale = true;
  const how = !state.meta.local ? "The server updates daily — check the health report."
    : state.meta.frozen ? "Rebuild the app with packaging\\build_exe.bat to update it."
    : "Settings → Maintenance → Update yt-dlp.";
  toast(`⚠ yt-dlp is ${state.meta.ytdlp_age_days} days old (${state.meta.ytdlp_version}). ` +
        `YouTube downloads may fail. ${how}`, "error", 15000);
}

let savedMedia = "audio";
let bound = false;
let loading = null;

async function loadMeta() {
  const r = await api("/api/meta", "GET", undefined, { quiet: true, timeoutMs: 15000 });
  if (!r.ok || !Core.isMeta(r.data)) throw new Error(r.ok ? "unexpected reply" : r.error);
  return r.data;
}
async function loadSettings() {
  const r = await api("/api/settings", "GET", undefined, { quiet: true, timeoutMs: 15000 });
  if (!r.ok || !Core.isSettings(r.data)) throw new Error(r.ok ? "unexpected reply" : r.error);
  return r.data;
}

// Startup, and Retry after it failed. Until it succeeds the page runs on DEFAULT_META, so
// every control still works and nothing half-loaded is applied.
function loadStartup() {
  if (loading) return loading;
  loading = OmniStartup.startDashboard({
    bind() { if (!bound) { bound = true; bind(); } },
    loadMeta,
    loadSettings,
    hydrate(meta, settings) {
      state.meta = { ...DEFAULT_META, ...meta };
      state.settings = settings;
      state.loaded = true;
      state.startupError = "";
      setMediaType(savedMedia, { persist: false });
      syncEnvironmentControls();
      if (!$("#settings-modal").classList.contains("hidden") && !state.settingsDirty) loadSettingsForm();
      updateChip();
      renderQueue();
      refreshBanner();
      warnStaleYtdlp();
    },
    onError(error) {
      state.startupError = error.message || "unknown error";
      refreshBanner();
    },
  }).finally(() => { loading = null; });
  return loading;
}

async function init() {
  applyTheme(document.documentElement.classList.contains("light") ? "light" : "dark");
  try { savedMedia = localStorage.getItem("omnidl-media") || "audio"; } catch (_) {}
  const urlMedia = new URLSearchParams(location.search).get("media");  // ?media=video deep-link
  if (urlMedia === "video" || urlMedia === "audio") savedMedia = urlMedia;
  setMediaType(savedMedia, { persist: false });
  initLayout();
  await loadStartup();
  updateChip();
  connect();
  $("#input").focus();
}

init();
