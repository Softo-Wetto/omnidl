"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  settings: {},
  meta: { engines: {}, formats: [], video_qualities: [], video_containers: [] },
  jobs: new Map(),
  order: [],
  viewingId: null,
  pendingInput: null,        // input held back by the access gate, retried after unlock
  mediaType: "audio",        // "audio" | "video" (per-download choice)
  lastAudioFormat: null,     // remembered selection when toggling back to audio
  lastVideoQuality: null,    // remembered selection when toggling back to video
  libraryReport: null,
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
  } else {
    fillSelect(fmt, state.meta.formats, state.lastAudioFormat || state.settings.audio_format);
    fmt.title = "Audio format";
  }
}

function setMediaType(type, { persist = true } = {}) {
  state.mediaType = type === "video" ? "video" : "audio";
  for (const btn of document.querySelectorAll("#media-toggle .mt-btn"))
    btn.classList.toggle("active", btn.dataset.media === state.mediaType);
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
    chip.className = "chip";
    chip.textContent = "—";
    hint.textContent = "";
    return;
  }
  chip.className = "chip " + engine;
  const wantsVideo = state.mediaType === "video" && videoSupported(engine);
  let tool = engine === "spotify" ? "embed → yt-dlp" : ENGINE_TOOL[engine];
  if (wantsVideo) tool += " · video";
  chip.textContent = `${ENGINE_LABEL[engine]} · ${tool}`;
  if (gateBlocks(text)) {
    hint.innerHTML = '🔒 YouTube &amp; Spotify need the access passphrase — ' +
                     '<a href="#" id="hint-unlock" style="color:var(--accent)">unlock</a>. ' +
                     'SoundCloud &amp; direct links work without it.';
    const a = $("#hint-unlock");
    if (a) a.onclick = (e) => { e.preventDefault(); state.pendingInput = text.trim(); openUnlock(); };
  } else if (wantsVideo) {
    hint.textContent = `Full video → ${$("#format").value} (${state.settings.video_container || "mp4"}).`;
  } else if (engine === "spotify") {
    hint.textContent = "Via public embed — no Spotify API, no Premium, no login needed.";
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

function makeButton(label, title, handler, cls) {
  const b = document.createElement("button");
  b.textContent = label;
  if (title) b.title = title;
  if (cls) b.className = cls;
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
      actions.append(makeButton("Cancel", "Cancel", () => api(`/api/jobs/${id}/cancel`, "POST"), "btn-danger"));
    } else {
      if (job.status === "done" && job.has_files) {
        actions.append(makeButton("⤓ Save", "Download to this device", () => {
          window.location.href = `/api/jobs/${id}/file`;
        }, "save-btn"));
      }
      if (job.status === "error" || job.status === "cancelled") {
        actions.append(makeButton("↻ Retry", "Run again", () => retryJob(job), "btn-retry"));
      }
      actions.append(makeButton("✕", "Remove from history", () => api(`/api/jobs/${id}`, "DELETE"), "btn-remove"));
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

function downloadPayload(input, override) {
  const payload = { input };
  if (override && override !== "auto") payload.engine_override = override;
  const engine = (override && override !== "auto") ? override : detectEngine(input);
  if (state.mediaType === "video" && videoSupported(engine)) {
    payload.media_type = "video";
    payload.video_quality = $("#format").value;
  } else {
    payload.format = $("#format").value;
  }
  return payload;
}

function retryJob(job) {
  // Reproduce the original job's mode (audio vs video + format/quality), not the
  // current toggle state.
  const payload = { input: job.input, engine_override: job.engine };
  if (job.media_type === "video" && videoSupported(job.engine)) {
    payload.media_type = "video";
    if (job.video_quality) payload.video_quality = job.video_quality;
  } else if (job.audio_format) {
    payload.format = job.audio_format;
  }
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
  // Must match the page scheme: a ws:// socket on an https:// page is blocked as mixed
  // content, which would leave the dashboard permanently "offline" with no live output.
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${scheme}//${location.host}/ws`);
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
        // With several jobs running at once, only auto-focus if the user isn't already
        // watching another live job (don't yank the terminal away from them).
        const cur = state.jobs.get(state.viewingId);
        if (!cur || FINISHED.includes(cur.status)) {
          state.viewingId = msg.job.id;
          termClear(false);
        }
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
  if (job.status === "done") {
    // Hosted mode delivers the file through the browser, so the job isn't really "done"
    // for the user until they hit Save — say so, and note that it won't wait forever.
    if (!state.meta.local && job.has_files) {
      toast(`✓ Ready: ${short} — click ⤓ Save to download it`, "success", 7000);
    } else {
      toast(`✓ Finished: ${short}`, "success");
    }
  }
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

/* ---------------- access gate (hosted, YouTube/Spotify only) ---------------- */
// Mirrors engines.needs_youtube_account() so the UI can warn before submitting.
function needsUnlock(text) {
  const t = (text || "").trim().toLowerCase();
  if (!t) return false;
  if (t.includes("youtube.com") || t.includes("youtu.be")) return true;
  if (t.includes("open.spotify.com") || t.startsWith("spotify:")) return true;
  if (t.includes("soundcloud.com")) return false;
  return !/^https?:\/\//.test(t);          // bare search -> YouTube search
}

function gateBlocks(text) {
  return state.meta.gated && !state.meta.unlocked && needsUnlock(text);
}

function openUnlock() {
  $("#unlock-error").textContent = "";
  $("#unlock-input").value = "";
  $("#unlock-modal").classList.remove("hidden");
  $("#unlock-input").focus();
}
function closeUnlock() { $("#unlock-modal").classList.add("hidden"); }

async function doUnlock() {
  const pass = $("#unlock-input").value;
  if (!pass) return;
  const r = await api("/api/unlock", "POST", { passphrase: pass });
  if (r && r.ok) {
    state.meta.unlocked = true;
    closeUnlock();
    toast("Unlocked — YouTube & Spotify enabled", "success");
    updateChip();
    if (state.pendingInput) { $("#input").value = state.pendingInput; state.pendingInput = null; submitDownload(); }
  } else {
    $("#unlock-error").textContent = (r && r.error) || "Unlock failed.";
  }
}

async function submitDownload() {
  const input = $("#input").value.trim();
  if (!input) return;
  // Prompt before spending a request we know will be refused.
  if (gateBlocks(input)) { state.pendingInput = input; openUnlock(); return; }
  const job = await api("/api/download", "POST", downloadPayload(input, $("#engine").value));
  // Server rejections (rate limits, bad input) must be surfaced — otherwise the box just
  // clears and it looks like the download silently vanished.
  if (job && job.needs_unlock) { state.pendingInput = input; openUnlock(); return; }
  if (job && job.error) { toast(job.error, "error", 6000); return; }
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

function loadSettingsForm() {
  const s = state.settings;
  if (state.meta.local) {
    $("#output-dir-field").hidden = false;
    $("#set-output-dir").value = s.output_dir || "";
    $("#cookie-file-field").hidden = false;
    $("#set-cookie-file").value = s.cookie_file || "";
  }
  $("#set-bitrate").value = s.bitrate || "";
  const maxc = state.meta.max_concurrency || 4;
  $("#set-concurrency").max = maxc;
  $("#set-concurrency").value = Math.min(s.concurrency || 3, maxc);
  $("#set-prefer-ytmusic").checked = s.prefer_ytmusic !== false;
  $("#set-match-duration").checked = s.spotify_match_duration !== false;
  $("#set-sponsorblock").checked = !!s.sponsorblock;
  $("#set-skip-existing").checked = s.skip_existing !== false;
  fillSelect($("#set-format"), state.meta.formats, s.audio_format);
  fillSelect($("#set-video-container"), state.meta.video_containers, s.video_container || "mp4");
}

async function saveSettings() {
  const payload = {
    audio_format: $("#set-format").value,
    video_container: $("#set-video-container").value,
    bitrate: $("#set-bitrate").value.trim(),
    concurrency: $("#set-concurrency").value,
    prefer_ytmusic: $("#set-prefer-ytmusic").checked,
    spotify_match_duration: $("#set-match-duration").checked,
    sponsorblock: $("#set-sponsorblock").checked,
    skip_existing: $("#set-skip-existing").checked,
  };
  if (state.meta.local) {
    payload.output_dir = $("#set-output-dir").value.trim();
    payload.cookie_file = $("#set-cookie-file").value.trim();
  }
  state.settings = await api("/api/settings", "POST", payload);
  state.lastAudioFormat = state.settings.audio_format;
  refreshFormatOptions();
  $("#settings-status").textContent = "Saved ✓";
  setTimeout(() => { $("#settings-status").textContent = ""; }, 2000);
  updateChip();
  toast("Settings saved", "success");
  if (state.settings.cookie_warning) toast("⚠ " + state.settings.cookie_warning, "error", 6000);
}

function openSettings() { loadSettingsForm(); $("#settings-modal").classList.remove("hidden"); }
function closeSettings() { $("#settings-modal").classList.add("hidden"); }

/* ---------------- music library ---------------- */
const ISSUE_LABELS = {
  unreadable: "Unreadable",
  missing_title: "Missing title",
  missing_artist: "Missing artist",
  missing_album: "Missing album",
  missing_artwork: "Missing artwork",
  low_bitrate: "Low bitrate",
};

function libraryNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

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

function libraryMatches(item, term) {
  if (!term) return true;
  const files = item.files || [];
  const values = [
    item.artist, item.title, item.display_artist, item.display_title, item.path,
    ...files.flatMap((file) => [
      file.artist, file.title, file.display_artist, file.display_title, file.path,
    ]),
  ];
  return values.some((value) => String(value || "").toLowerCase().includes(term));
}

function renderLibrarySummary(summary) {
  const host = $("#library-summary");
  const stats = [
    ["Tracks", summary.total_files],
    ["Library size", formatBytes(summary.total_size)],
    ["Need attention", summary.files_with_issues],
    ["Safe repairs", summary.repairable_files],
    ["Duplicate groups", summary.duplicate_groups],
    ["Potential savings", formatBytes(summary.reclaimable_size)],
  ];
  host.replaceChildren(...stats.map(([label, value]) => {
    const card = libraryNode("div", "library-stat");
    card.append(libraryNode("strong", "", value), libraryNode("span", "", label));
    return card;
  }));
  host.classList.remove("hidden");
}

function duplicateCard(group) {
  const card = libraryNode("article", "library-card duplicate-card");
  const head = libraryNode("div", "library-card-head");
  const identity = libraryNode("div", "library-identity");
  identity.append(
    libraryNode("strong", "", group.title || "Unknown title"),
    libraryNode("span", "", group.artist || "Unknown artist"),
  );
  head.append(identity, libraryNode("span", "library-saving", `${formatBytes(group.reclaimable_size)} duplicated`));
  card.append(head);

  const files = libraryNode("div", "duplicate-files");
  for (const file of group.files || []) {
    const row = libraryNode("div", `duplicate-file${file.recommended ? " recommended" : ""}`);
    const details = libraryNode("div", "duplicate-file-main");
    const title = libraryNode("div", "library-path", file.path);
    if (file.recommended) title.append(libraryNode("span", "recommended-badge", "Best copy"));
    details.append(title, libraryNode("span", "library-quality", qualityLabel(file)));
    row.append(details, libraryNode("span", "quality-score", `${file.quality_score}/100`));
    files.append(row);
  }
  card.append(files);
  return card;
}

function issueCard(track) {
  const card = libraryNode("article", "library-card issue-card");
  const head = libraryNode("div", "library-card-head");
  const identity = libraryNode("div", "library-identity");
  identity.append(
    libraryNode("strong", "", track.display_title || "Unknown title"),
    libraryNode("span", "", track.display_artist || "Unknown artist"),
  );
  head.append(identity, libraryNode("span", "quality-score", `${track.quality_score}/100`));
  card.append(head, libraryNode("div", "library-path", track.path));

  const footer = libraryNode("div", "library-card-footer");
  const issues = libraryNode("div", "issue-badges");
  for (const issue of track.issues || []) {
    issues.append(libraryNode("span", `issue-badge issue-${issue}`, ISSUE_LABELS[issue] || issue));
  }
  footer.append(issues);
  if (track.repairable) {
    const repair = libraryNode("button", "ghost library-repair", "Fill artist/title");
    repair.onclick = async () => {
      repair.disabled = true;
      repair.textContent = "Repairing...";
      const result = await api("/api/library/repair", "POST", { path: track.path });
      if (result.error || result.detail) {
        toast(result.error || result.detail, "error");
        repair.disabled = false;
        repair.textContent = "Fill artist/title";
        return;
      }
      const changed = (result.changed || []).join(" and ");
      toast(changed ? `Updated ${changed}` : "Tags were already complete", "success");
      await scanLibrary();
    };
    footer.append(repair);
  }
  card.append(footer, libraryNode("div", "library-quality", qualityLabel(track)));
  return card;
}

function renderLibrary() {
  const report = state.libraryReport;
  if (!report) return;
  const term = $("#library-search").value.trim().toLowerCase();
  const duplicates = (report.duplicate_groups || []).filter((item) => libraryMatches(item, term));
  const issues = (report.tracks || []).filter(
    (item) => (item.issues || []).length && libraryMatches(item, term),
  );

  $("#library-duplicates").replaceChildren(...duplicates.map(duplicateCard));
  $("#library-issues").replaceChildren(...issues.map(issueCard));
  $("#library-duplicate-count").textContent = String(duplicates.length);
  $("#library-issue-count").textContent = String(issues.length);
  $("#library-duplicates-section").classList.toggle("hidden", duplicates.length === 0);
  $("#library-issues-section").classList.toggle("hidden", issues.length === 0);
  $("#library-empty").classList.toggle("hidden", duplicates.length > 0 || issues.length > 0);
}

async function scanLibrary() {
  const button = $("#scan-library");
  button.disabled = true;
  button.textContent = "Scanning...";
  $("#library-status").textContent = "Reading audio files and metadata...";
  try {
    const report = await api("/api/library/scan", "POST");
    if (report.error || report.detail) throw new Error(report.error || report.detail);
    state.libraryReport = report;
    renderLibrarySummary(report.summary);
    renderLibrary();
    $("#library-status").textContent = `Scanned ${report.root}`;
    toast(`Library scan complete: ${report.summary.total_files} tracks`, "success");
  } catch (error) {
    $("#library-status").textContent = error.message || "Library scan failed.";
    toast(error.message || "Library scan failed", "error");
  } finally {
    button.disabled = false;
    button.textContent = "Scan again";
  }
}

function openLibrary() {
  $("#library-modal").classList.remove("hidden");
  if (!state.libraryReport) scanLibrary();
}

function closeLibrary() {
  $("#library-modal").classList.add("hidden");
}
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
  $("#close-library").onclick = closeLibrary;
  $("#scan-library").onclick = scanLibrary;
  $("#library-search").addEventListener("input", renderLibrary);
  $("#theme-toggle").onclick = toggleTheme;
  if (state.meta.local) {
    const libraryButton = $("#open-library");
    libraryButton.hidden = false;
    libraryButton.onclick = openLibrary;
    const fb = $("#open-folder");
    fb.hidden = false;
    fb.onclick = async () => {
      const r = await api("/api/open-folder", "POST");
      if (r && r.error) toast(r.error, "error");
    };
  } else {
    // Hosted: there's no server folder to open, so explain how files reach the visitor.
    const hint = $("#hosted-hint");
    if (hint) hint.hidden = false;
  }
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
  $("#do-unlock").onclick = doUnlock;
  $("#close-unlock").onclick = closeUnlock;
  $("#unlock-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doUnlock(); });
  $("#unlock-modal").addEventListener("click", (e) => { if (e.target.id === "unlock-modal") closeUnlock(); });
  $("#settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") closeSettings(); });
  $("#library-modal").addEventListener("click", (e) => { if (e.target.id === "library-modal") closeLibrary(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeSettings();
      closeUnlock();
      closeLibrary();
    }
  });
}

async function init() {
  applyTheme(document.documentElement.classList.contains("light") ? "light" : "dark");
  state.meta = await api("/api/meta");
  state.settings = await api("/api/settings");
  let savedMedia = "audio";
  try { savedMedia = localStorage.getItem("omnidl-media") || "audio"; } catch (_) {}
  const urlMedia = new URLSearchParams(location.search).get("media");  // ?media=video deep-link
  if (urlMedia === "video" || urlMedia === "audio") savedMedia = urlMedia;
  setMediaType(savedMedia, { persist: false });
  bind();
  // A stale yt-dlp doesn't degrade downloads, it stops them dead (YouTube breaks old
  // releases). The .exe bundles yt-dlp at build time and never self-updates, so say so.
  if (state.meta.ytdlp_age_days != null && state.meta.ytdlp_age_days > 30) {
    const how = state.meta.local
      ? "Update with: pip install -U yt-dlp  (then rebuild the app)"
      : "The server updates daily — check the health report.";
    toast(`⚠ yt-dlp is ${state.meta.ytdlp_age_days} days old (${state.meta.ytdlp_version}). ` +
          `YouTube downloads may fail. ${how}`, "error", 15000);
  }
  updateChip();
  connect();
  $("#input").focus();
}

init();
