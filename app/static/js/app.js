"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  settings: {},
  meta: { engines: {}, formats: [], browsers: [], spotify_methods: ["embed", "spotdl"] },
  jobs: new Map(),
  order: [],
  viewingId: null,
};

const ENGINE_LABEL = { spotify: "Spotify", youtube: "YouTube", soundcloud: "SoundCloud" };
const ENGINE_TOOL = { spotify: "spotdl", youtube: "yt-dlp", soundcloud: "scdl" };
const FINISHED = ["done", "error", "cancelled"];

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
function writeLive(jobId, data) { if (jobId === state.viewingId) logView.feed(data); }

/* ---------------- engine detection (mirrors backend) ---------------- */
function detectEngine(text) {
  const t = text.trim().toLowerCase();
  if (!t) return null;
  if (t.includes("open.spotify.com") || t.startsWith("spotify:")) return "spotify";
  if (t.includes("soundcloud.com")) return "soundcloud";
  return "youtube"; // links to YT or any plain-text search go to yt-dlp
}

function updateChip() {
  const override = $("#engine").value;
  const text = $("#input").value;
  const chip = $("#detected");
  const hint = $("#hint");
  const engine = override === "auto" ? detectEngine(text) : override;
  if (!engine) {
    chip.className = "chip";
    chip.textContent = "—";
    hint.textContent = "";
    return;
  }
  chip.className = "chip " + engine;
  const method = state.settings.spotify_method || "embed";
  const tool = engine === "spotify" ? (method === "embed" ? "embed → yt-dlp" : "spotdl") : ENGINE_TOOL[engine];
  chip.textContent = `${ENGINE_LABEL[engine]} · ${tool}`;
  if (engine === "spotify" && method === "embed") {
    hint.textContent = "Via public embed — no Spotify API, no Premium, no login needed.";
  } else if (engine === "spotify" && !(state.settings.spotify_client_id && state.settings.spotify_client_secret)) {
    hint.textContent = "⚠ spotDL method needs a Premium-owned app's Client ID/Secret (Settings).";
  } else if (engine === "soundcloud" && override !== "auto" && !text.toLowerCase().includes("soundcloud.com")) {
    hint.textContent = "scdl needs a SoundCloud URL (it can't search by text).";
  } else {
    hint.textContent = "";
  }
}

/* ---------------- toasts ---------------- */
function toast(message, kind = "info", ms = 3200) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 250);
  }, ms);
}

/* ---------------- queue rendering ---------------- */
function fmtTime(t) {
  return t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function renderSummary() {
  const counts = { running: 0, queued: 0, done: 0, error: 0, cancelled: 0 };
  for (const id of state.order) {
    const j = state.jobs.get(id);
    if (j) counts[j.status] = (counts[j.status] || 0) + 1;
  }
  const parts = [];
  if (counts.running) parts.push(`${counts.running} running`);
  if (counts.queued) parts.push(`${counts.queued} queued`);
  if (counts.done) parts.push(`${counts.done} done`);
  if (counts.error) parts.push(`${counts.error} failed`);
  if (counts.cancelled) parts.push(`${counts.cancelled} cancelled`);
  $("#queue-summary").textContent = parts.join(" · ");
  $("#queue-empty").classList.toggle("hidden", state.order.length > 0);
}

function makeButton(label, title, handler) {
  const b = document.createElement("button");
  b.textContent = label;
  if (title) b.title = title;
  b.onclick = (e) => { e.stopPropagation(); handler(); };
  return b;
}

function renderQueue() {
  const ul = $("#queue");
  ul.innerHTML = "";
  for (const id of state.order) {
    const job = state.jobs.get(id);
    if (!job) continue;
    const li = document.createElement("li");
    li.className = "job" + (id === state.viewingId ? " active" : "");
    li.dataset.id = id;

    const top = document.createElement("div");
    top.className = "job-top";
    const input = document.createElement("span");
    input.className = "job-input";
    input.textContent = job.input;
    input.title = job.input;
    const badge = document.createElement("span");
    badge.className = "badge " + job.status;
    badge.textContent = job.status;
    top.append(input, badge);

    const metaRow = document.createElement("div");
    metaRow.className = "job-meta";
    const tool = document.createElement("span");
    tool.className = "tool-tag";
    const dot = document.createElement("span");
    dot.className = "eng-dot " + job.engine;
    tool.append(dot, document.createTextNode(
      `${job.engine_label} · ${job.tool}` + (job.started ? ` · ${fmtTime(job.started)}` : "")
    ));

    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.status === "running" || job.status === "queued") {
      actions.append(makeButton("Cancel", "Cancel", () => api(`/api/jobs/${id}/cancel`, "POST")));
    } else {
      if (job.status === "error" || job.status === "cancelled") {
        actions.append(makeButton("↻ Retry", "Run again", () => retryJob(job)));
      }
      actions.append(makeButton("✕", "Remove from history", () => api(`/api/jobs/${id}`, "DELETE")));
    }
    metaRow.append(tool, actions);
    li.append(top, metaRow);

    if (job.status === "running") {
      const prog = document.createElement("div");
      prog.className = "job-progress";
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("div");
      fill.className = "fill";
      fill.style.width = (job.progress || 0) + "%";
      bar.append(fill);
      const pct = document.createElement("span");
      pct.className = "pct";
      pct.textContent = job.progress != null ? job.progress + "%" : "";
      prog.append(bar, pct);
      li.append(prog);
      const lbl = document.createElement("div");
      lbl.className = "job-label";
      lbl.textContent = job.label || "";
      li.append(lbl);
    }

    li.onclick = () => viewJob(id);
    ul.append(li);
  }
  renderSummary();
}

function updateJobProgress(id) {
  const li = document.querySelector(`.job[data-id="${id}"]`);
  const j = state.jobs.get(id);
  if (!li || !j) return;
  const fill = li.querySelector(".fill");
  if (!fill) { renderQueue(); return; }
  fill.style.width = (j.progress || 0) + "%";
  const pct = li.querySelector(".pct");
  if (pct) pct.textContent = j.progress != null ? j.progress + "%" : "";
  const lbl = li.querySelector(".job-label");
  if (lbl) lbl.textContent = j.label || "";
}

async function viewJob(id) {
  state.viewingId = id;
  renderQueue();
  termClear(false);
  try {
    const res = await fetch(`/api/jobs/${id}/output`);
    if (res.ok) {
      const data = await res.json();
      if (data.output) termWrite(data.output);
      else $("#term-empty").classList.remove("hidden");
    }
  } catch (_) {}
}

function retryJob(job) {
  const payload = { input: job.input, engine_override: job.engine, format: $("#format").value };
  api("/api/download", "POST", payload);
  toast(`Re-queued: ${job.input}`, "info");
}

/* ---------------- websocket ---------------- */
let ws = null;
function setConn(online) {
  const c = $("#conn");
  c.className = "conn " + (online ? "on" : "off");
  c.innerHTML = "<i></i>" + (online ? "online" : "offline");
}
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
}

function upsertJob(job) {
  if (!state.jobs.has(job.id)) state.order.push(job.id);
  state.jobs.set(job.id, job);
}

function handleMessage(msg) {
  switch (msg.type) {
    case "snapshot":
      state.jobs.clear();
      state.order = [];
      for (const job of msg.jobs) upsertJob(job);
      state.viewingId = msg.active_id;
      termClear(!msg.output);
      if (msg.output) termWrite(msg.output);
      renderQueue();
      break;
    case "job_added":
      upsertJob(msg.job);
      renderQueue();
      break;
    case "status": {
      const prev = state.jobs.get(msg.job.id);
      const wasActive = prev && !FINISHED.includes(prev.status);
      upsertJob(msg.job);
      if (msg.job.status === "running") {
        state.viewingId = msg.job.id;
        termClear(false);
      } else if (wasActive && FINISHED.includes(msg.job.status)) {
        notifyFinished(msg.job);
      }
      renderQueue();
      break;
    }
    case "output":
      if (msg.key) { if (msg.job_id === state.viewingId) logView.feedKeyed(msg.key, msg.data); }
      else writeLive(msg.job_id, msg.data);
      break;
    case "key_done":
      if (msg.job_id === state.viewingId) logView.releaseKey(msg.key);
      break;
    case "progress": {
      const j = state.jobs.get(msg.job_id);
      if (j) { j.progress = msg.progress; j.label = msg.label; updateJobProgress(msg.job_id); }
      break;
    }
    case "removed":
      state.jobs.delete(msg.job_id);
      state.order = state.order.filter((i) => i !== msg.job_id);
      if (state.viewingId === msg.job_id) { state.viewingId = null; termClear(true); }
      renderQueue();
      break;
  }
}

function notifyFinished(job) {
  const short = job.input.length > 48 ? job.input.slice(0, 48) + "…" : job.input;
  if (job.status === "done") toast(`✓ Finished: ${short}`, "success");
  else if (job.status === "error") toast(`✕ Failed: ${short}`, "error");
  else if (job.status === "cancelled") toast(`Cancelled: ${short}`, "info");
}

/* ---------------- api helpers ---------------- */
async function api(path, method = "GET", body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  return res.json().catch(() => ({}));
}

async function submitDownload() {
  const input = $("#input").value.trim();
  if (!input) return;
  const override = $("#engine").value;
  const payload = { input, format: $("#format").value };
  if (override !== "auto") payload.engine_override = override;
  const job = await api("/api/download", "POST", payload);
  if (job && job.engine_label) toast(`Queued · ${job.engine_label}`, "info");
  $("#input").value = "";
  updateChip();
  $("#input").focus();
}

/* ---------------- settings ---------------- */
function fillSelect(sel, items, current) {
  sel.innerHTML = "";
  for (const it of items) {
    const opt = document.createElement("option");
    opt.value = it;
    opt.textContent = it;
    if (it === current) opt.selected = true;
    sel.append(opt);
  }
}

function toggleSpotdlCreds() {
  $("#spotdl-creds").classList.toggle("hidden", $("#set-spotify-method").value !== "spotdl");
}

function loadSettingsForm() {
  const s = state.settings;
  fillSelect($("#set-spotify-method"), state.meta.spotify_methods || ["embed", "spotdl"], s.spotify_method);
  $("#set-client-id").value = s.spotify_client_id || "";
  $("#set-client-secret").value = s.spotify_client_secret || "";
  $("#set-output-dir").value = s.output_dir || "";
  $("#set-bitrate").value = s.bitrate || "";
  $("#set-threads").value = s.threads || 4;
  $("#set-concurrency").value = s.concurrency || 3;
  $("#set-template").value = s.spotify_template || "";
  $("#set-cookie-file").value = s.cookie_file || "";
  $("#set-prefer-ytmusic").checked = s.prefer_ytmusic !== false;
  $("#set-match-duration").checked = s.spotify_match_duration !== false;
  $("#set-sponsorblock").checked = !!s.sponsorblock;
  $("#set-skip-existing").checked = s.skip_existing !== false;
  fillSelect($("#set-format"), state.meta.formats, s.audio_format);
  fillSelect($("#set-browser"), state.meta.browsers, s.cookies_from_browser);
  toggleSpotdlCreds();
}

async function saveSettings() {
  const payload = {
    spotify_method: $("#set-spotify-method").value,
    spotify_client_id: $("#set-client-id").value.trim(),
    spotify_client_secret: $("#set-client-secret").value.trim(),
    output_dir: $("#set-output-dir").value.trim(),
    audio_format: $("#set-format").value,
    bitrate: $("#set-bitrate").value.trim(),
    threads: $("#set-threads").value,
    concurrency: $("#set-concurrency").value,
    spotify_template: $("#set-template").value.trim(),
    cookie_file: $("#set-cookie-file").value.trim(),
    cookies_from_browser: $("#set-browser").value,
    prefer_ytmusic: $("#set-prefer-ytmusic").checked,
    spotify_match_duration: $("#set-match-duration").checked,
    sponsorblock: $("#set-sponsorblock").checked,
    skip_existing: $("#set-skip-existing").checked,
  };
  state.settings = await api("/api/settings", "POST", payload);
  fillSelect($("#format"), state.meta.formats, state.settings.audio_format);
  $("#settings-status").textContent = "Saved ✓";
  setTimeout(() => { $("#settings-status").textContent = ""; }, 2000);
  updateChip();
  toast("Settings saved", "success");
}

function openSettings() { loadSettingsForm(); $("#settings-modal").classList.remove("hidden"); }
function closeSettings() { $("#settings-modal").classList.add("hidden"); }

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

/* ---------------- wire up ---------------- */
function bind() {
  $("#download").onclick = submitDownload;
  $("#input").addEventListener("input", updateChip);
  $("#input").addEventListener("keydown", (e) => { if (e.key === "Enter") submitDownload(); });
  $("#engine").addEventListener("change", updateChip);
  $("#open-settings").onclick = openSettings;
  $("#close-settings").onclick = closeSettings;
  $("#save-settings").onclick = saveSettings;
  $("#theme-toggle").onclick = toggleTheme;
  $("#set-spotify-method").addEventListener("change", toggleSpotdlCreds);
  $("#open-folder").onclick = async () => { await api("/api/open-folder", "POST"); };
  $("#clear-term").onclick = () => termClear(true);
  $("#jump-latest").onclick = () => logView.jump();
  $("#copy-log").onclick = async () => {
    try { await navigator.clipboard.writeText(logView.copy()); toast("Log copied", "success"); }
    catch (_) { toast("Copy failed", "error"); }
  };
  $("#cancel-all").onclick = async () => {
    let n = 0;
    for (const id of [...state.order]) {
      const job = state.jobs.get(id);
      if (job && (job.status === "running" || job.status === "queued")) { await api(`/api/jobs/${id}/cancel`, "POST"); n++; }
    }
    if (n) toast(`Cancelled ${n} job(s)`, "info");
  };
  $("#clear-done").onclick = async () => {
    let n = 0;
    for (const id of [...state.order]) {
      const job = state.jobs.get(id);
      if (job && FINISHED.includes(job.status)) { await api(`/api/jobs/${id}`, "DELETE"); n++; }
    }
    if (n) toast(`Cleared ${n} finished job(s)`, "info");
  };
  $("#settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") closeSettings(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });
}

async function init() {
  applyTheme(document.documentElement.classList.contains("light") ? "light" : "dark");
  state.meta = await api("/api/meta");
  state.settings = await api("/api/settings");
  fillSelect($("#format"), state.meta.formats, state.settings.audio_format);
  bind();
  updateChip();
  connect();
  $("#input").focus();
}

init();
