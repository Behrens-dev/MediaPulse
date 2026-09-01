/* MediaForge UI */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: "DELETE" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
};

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtBytes(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 ? 0 : 1) + " " + u[i];
}

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function channelName(ch) {
  return { 1: "1.0 mono", 2: "2.0 stereo", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1" }[ch] || (ch ? ch + "ch" : "?");
}

const KIND_LABELS = {
  downmix: "Downmix audio",
  embed_sub: "Embed subtitles",
  remove_audio: "Remove audio",
  audio_sync: "Audio sync",
  sub_sync: "Subtitle sync",
};

/* ---------- navigation ---------- */

let currentPage = "convert";
$$("nav button").forEach((b) =>
  b.addEventListener("click", () => showPage(b.dataset.page)));

const PAGE_TITLES = { convert: "Convert", jobs: "Jobs", settings: "Settings" };

function showPage(page) {
  currentPage = page;
  $("#page-title").textContent = PAGE_TITLES[page] || "MediaForge";
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== "page-" + page));
  if (page === "jobs") loadJobs();
  if (page === "settings") loadSettings();
}

/* ---------- status badges ---------- */

async function refreshStatus() {
  try {
    const s = await api.get("/api/status");
    const b = $("#server-badge");
    if (!s.configured) { b.textContent = "⚙ Plex not configured"; b.className = "badge"; }
    else if (s.plex_ok) { b.textContent = "● " + (s.server.name || "Plex up"); b.className = "badge up"; }
    else { b.textContent = "● Plex unreachable"; b.className = "badge down"; }
    $("#mode-badge").textContent = s.mode === "ssh" ? "⚡ runs on Plex server" : "⚡ runs in container";
    const running = s.jobs.running || 0, queued = s.jobs.queued || 0;
    $("#topbar-info").textContent =
      running || queued ? `${running} running · ${queued} queued` : "";
  } catch { /* ignore */ }
}
refreshStatus();
setInterval(refreshStatus, 15000);

/* =========================================================
   CONVERT page
   ========================================================= */

const state = {
  item: null,        // selected leaf item {rating_key, title, thumb, files}
  fileIdx: 0,
  probe: null,       // ffprobe result for the selected file
  tab: "downmix",
};

/* ---------- search ---------- */

let searchTimer = null;
$("#search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("#search-input").value.trim();
  if (q.length < 2) { $("#search-results").classList.add("hidden"); return; }
  searchTimer = setTimeout(() => runSearch(q), 300);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".rec-search")) $("#search-results").classList.add("hidden");
});

async function runSearch(q) {
  try {
    const results = await api.get("/api/search?q=" + encodeURIComponent(q));
    const box = $("#search-results");
    if (!results.length) {
      box.innerHTML = `<div class="rec-result muted">No matches</div>`;
    } else {
      box.innerHTML = results.map((r, i) => {
        const sub = { movie: "Movie", show: "Show", season: "Season", episode: "Episode" }[r.type] || r.type;
        const line = r.type === "episode"
          ? `${esc(r.grandparent_title)} — ${esc(r.title)}`
          : `${esc(r.title)}${r.year ? " (" + r.year + ")" : ""}`;
        const img = r.thumb ? `<img src="/api/poster?thumb=${encodeURIComponent(r.thumb)}&w=64&h=96" loading="lazy">` : "<img>";
        return `<div class="rec-result" data-i="${i}">${img}<div>${line}<div class="sub muted small">${sub}</div></div></div>`;
      }).join("");
      $$("#search-results .rec-result").forEach((el) => {
        el.addEventListener("click", () => {
          box.classList.add("hidden");
          pickSearchResult(results[+el.dataset.i]);
        });
      });
    }
    box.classList.remove("hidden");
  } catch (e) { toast(e.message, true); }
}

async function pickSearchResult(r) {
  try {
    const item = await api.get("/api/item/" + r.rating_key);
    $("#convert-empty").classList.add("hidden");
    $("#item-panel").classList.remove("hidden");
    $("#item-poster").src = item.thumb
      ? "/api/poster?thumb=" + encodeURIComponent(item.thumb) + "&w=184&h=276" : "";
    $("#item-title").textContent = item.title;

    if (item.episodes && item.episodes.length) {
      const sel = $("#episode-select");
      sel.innerHTML = item.episodes.map((e) =>
        `<option value="${esc(e.rating_key)}">${esc(e.title)}</option>`).join("");
      $("#episode-picker").classList.remove("hidden");
      sel.onchange = () => loadLeaf(sel.value);
      await loadLeaf(item.episodes[0].rating_key);
    } else {
      $("#episode-picker").classList.add("hidden");
      applyLeaf(item);
    }
  } catch (e) { toast(e.message, true); }
}

async function loadLeaf(ratingKey) {
  try {
    const item = await api.get("/api/item/" + ratingKey);
    applyLeaf(item);
  } catch (e) { toast(e.message, true); }
}

function applyLeaf(item) {
  state.item = item;
  state.fileIdx = 0;
  state.probe = null;
  if ($("#episode-picker").classList.contains("hidden")) {
    $("#item-title").textContent = item.title;
  }
  const parts = item.files || [];
  if (parts.length > 1) {
    const sel = $("#part-select");
    sel.innerHTML = parts.map((f, i) =>
      `<option value="${i}">${esc(f.path)} (${fmtBytes(f.size)})</option>`).join("");
    sel.onchange = () => { state.fileIdx = +sel.value; onFileChosen(); };
    $("#part-picker").classList.remove("hidden");
  } else {
    $("#part-picker").classList.add("hidden");
  }
  onFileChosen();
}

function currentFile() {
  return (state.item && state.item.files && state.item.files[state.fileIdx]) || null;
}

async function onFileChosen() {
  const f = currentFile();
  state.probe = null;
  $("#tracks-box").classList.add("hidden");
  $("#actions-box").classList.add("hidden");
  $("#queue-msg").textContent = "";
  if (!f || !f.path) {
    $("#item-file").textContent = "No file found for this item.";
    $("#probe-status").textContent = "";
    return;
  }
  $("#item-file").textContent = f.path + (f.size ? `  ·  ${fmtBytes(f.size)}` : "");
  $("#probe-status").textContent = "Reading the file with ffprobe…";
  try {
    const p = await api.post("/api/probe", { path: f.path });
    if (!p.ok) {
      $("#probe-status").innerHTML =
        `<span style="color:var(--red)">${esc(p.error)}</span> — showing Plex's metadata instead.`;
      state.probe = null;
      renderTracks(plexFallbackProbe(f), "(from Plex metadata — file not probed)");
    } else {
      state.probe = p;
      $("#probe-status").textContent = "";
      renderTracks(p, `(ffprobe, ${p.mode === "ssh" ? "on the Plex server" : "in the container"})`);
    }
    renderActions();
    updateOutPreview();
  } catch (e) {
    $("#probe-status").innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
  }
}

function plexFallbackProbe(f) {
  return {
    audio: (f.audio || []).map((a, i) => ({
      a_index: i, codec: a.codec, channels: a.channels || 0,
      language: a.language, title: a.title, layout: "", default: false,
    })),
    subs: (f.subs || []).map((s, i) => ({
      s_index: i, codec: s.codec, language: s.language, title: s.title, default: false,
    })),
  };
}

function tracksData() {
  return state.probe || plexFallbackProbe(currentFile() || {});
}

function renderTracks(p, sourceNote) {
  $("#probe-source").textContent = sourceNote || "";
  const audioRows = (p.audio || []).map((a) => {
    const cls = a.channels >= 6 ? "surround" : (a.channels === 2 ? "stereo" : "");
    return `<tr>
      <td>a:${a.a_index}</td>
      <td><span class="audio-chip ${cls}">${esc((a.codec || "?").toUpperCase())} ${channelName(a.channels)}</span></td>
      <td>${esc(a.language || "—")}</td>
      <td>${esc(a.title || "—")}${a.default ? ' <span class="muted small">(default)</span>' : ""}</td>
    </tr>`;
  }).join("");
  const subRows = (p.subs || []).map((s) => `<tr>
      <td>s:${s.s_index}</td>
      <td>${esc((s.codec || "?").toUpperCase())}</td>
      <td>${esc(s.language || "—")}</td>
      <td>${esc(s.title || "—")}${s.default ? ' <span class="muted small">(default)</span>' : ""}</td>
    </tr>`).join("");
  $("#tracks-tables").innerHTML = `
    <table style="margin-bottom:10px">
      <thead><tr><th>#</th><th>Audio</th><th>Language</th><th>Name</th></tr></thead>
      <tbody>${audioRows || '<tr><td colspan="4" class="muted">No audio streams</td></tr>'}</tbody>
    </table>
    <table>
      <thead><tr><th>#</th><th>Subtitle</th><th>Language</th><th>Name</th></tr></thead>
      <tbody>${subRows || '<tr><td colspan="4" class="muted">No embedded subtitles</td></tr>'}</tbody>
    </table>`;
  $("#tracks-box").classList.remove("hidden");
}

/* ---------- action tabs ---------- */

$$("#action-tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    $$("#action-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-body").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + state.tab));
    updateOutPreview();
  }));

function renderActions() {
  const t = tracksData();
  const has = (n) => (t.audio || []).some((a) => a.channels === n);

  // downmix preset availability
  const avail = { "71_add": has(8), "51_add": has(6), "71_only": has(8) };
  $$("#downmix-opts .opt").forEach((el) => {
    const ok = avail[el.dataset.preset];
    el.classList.toggle("disabled", !ok);
    const input = el.querySelector("input");
    input.disabled = !ok;
    if (!ok) input.checked = false;
  });
  const firstOk = $$("#downmix-opts .opt input").find((i) => !i.disabled);
  if (firstOk && !$$("#downmix-opts .opt input").some((i) => i.checked)) firstOk.checked = true;
  syncOptSelection("#downmix-opts");

  // remove-audio list
  $("#remove-list").innerHTML = (t.audio || []).map((a) => {
    const cls = a.channels >= 6 ? "surround" : (a.channels === 2 ? "stereo" : "");
    return `<label class="opt selected" data-a="${a.a_index}">
      <input type="checkbox" checked data-a="${a.a_index}">
      <span><b><span class="audio-chip ${cls}">${esc((a.codec || "?").toUpperCase())} ${channelName(a.channels)}</span>
      ${esc(a.language || "")}</b>
      <span class="sub">${esc(a.title || "a:" + a.a_index)}</span></span>
    </label>`;
  }).join("") || '<div class="muted">No audio streams found.</div>';
  $$("#remove-list input").forEach((i) =>
    i.addEventListener("change", () =>
      i.closest(".opt").classList.toggle("selected", i.checked)));

  $("#actions-box").classList.remove("hidden");
}

function syncOptSelection(scope) {
  $$(scope + " .opt").forEach((el) => {
    const i = el.querySelector("input");
    el.classList.toggle("selected", i.checked);
  });
}
$$("#downmix-opts input").forEach((i) =>
  i.addEventListener("change", () => syncOptSelection("#downmix-opts")));
$$("#tab-audio_sync input[name=as-mode]").forEach((i) =>
  i.addEventListener("change", () => syncOptSelection("#tab-audio_sync")));
syncOptSelection("#tab-audio_sync");

/* ---------- output preview ---------- */

function sanitizeSuffix(s) {
  return (s || "").replace(/[^A-Za-z0-9 ._()\[\]-]/g, "").trim();
}

function updateOutPreview() {
  const f = currentFile();
  const el = $("#out-preview");
  if (!f || !f.path) { el.textContent = ""; return; }
  const suffix = sanitizeSuffix($("#suffix").value);
  const path = f.path;
  const slash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const dot = path.lastIndexOf(".");
  const base = dot > slash ? path.slice(slash + 1, dot) : path.slice(slash + 1);
  const ext = dot > slash ? path.slice(dot) : "";
  if (!suffix) {
    el.textContent = "Enter a suffix — e.g. " + base + "_encoded" + ext;
    el.className = "out-preview bad";
  } else {
    el.textContent = "→ " + base + suffix + ext;
    el.className = "out-preview";
  }
}
$("#suffix").addEventListener("input", updateOutPreview);

/* ---------- queue job ---------- */

function readSubFile() {
  return new Promise((resolve, reject) => {
    const f = $("#sub-file").files && $("#sub-file").files[0];
    if (!f) return resolve(null);
    if (f.size > 10 * 1024 * 1024) return reject(new Error("Subtitle file too large — keep it under 10 MB"));
    const r = new FileReader();
    r.onload = () => resolve({ b64: r.result.split(",")[1], name: f.name });
    r.onerror = () => reject(new Error("Could not read the subtitle file"));
    r.readAsDataURL(f);
  });
}

$("#queue-btn").addEventListener("click", async () => {
  const f = currentFile();
  const msg = $("#queue-msg");
  msg.className = "msg";
  msg.textContent = "";
  if (!f || !f.path) return toast("Pick a file first", true);
  const suffix = sanitizeSuffix($("#suffix").value);
  if (!suffix) {
    msg.className = "msg err";
    msg.textContent = "A filename suffix is required (e.g. _encoded) so the original file is never overwritten.";
    return;
  }

  const kind = state.tab;
  const options = {};
  try {
    if (kind === "downmix") {
      const sel = $$("#downmix-opts input").find((i) => i.checked);
      if (!sel) throw new Error("Pick a downmix operation — none of them fit this file's audio tracks.");
      options.preset = sel.value;
    } else if (kind === "embed_sub") {
      const up = await readSubFile();
      if (up) { options.sub_b64 = up.b64; options.sub_name = up.name; }
      else if ($("#sub-path").value.trim()) options.sub_path = $("#sub-path").value.trim();
      else throw new Error("Choose a subtitle file to upload, or enter its path on the server.");
      options.label = $("#sub-label").value.trim();
      options.language = $("#sub-lang").value.trim() || "eng";
      options.make_default = $("#sub-default").checked;
    } else if (kind === "remove_audio") {
      const keep = $$("#remove-list input").filter((i) => i.checked).map((i) => +i.dataset.a);
      const total = $$("#remove-list input").length;
      if (!keep.length) throw new Error("Keep at least one audio track.");
      if (keep.length === total) throw new Error("Uncheck the track(s) you want removed.");
      options.keep = keep;
    } else if (kind === "audio_sync") {
      options.mode = $$("input[name=as-mode]").find((i) => i.checked).value;
      if (options.mode === "offset") {
        options.offset_ms = parseFloat($("#as-offset").value);
        if (!options.offset_ms) throw new Error("Enter a non-zero offset in milliseconds.");
      }
    } else if (kind === "sub_sync") {
      options.offset_ms = parseFloat($("#ss-offset").value);
      if (!options.offset_ms) throw new Error("Enter a non-zero offset in milliseconds.");
    }
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
    return;
  }

  const btn = $("#queue-btn");
  btn.disabled = true;
  try {
    const res = await api.post("/api/jobs", {
      kind, options, suffix,
      path: f.path,
      title: state.item.title,
      rating_key: state.item.rating_key,
    });
    msg.className = "msg ok";
    msg.textContent = `Queued job #${res.id} → ${res.output}`;
    toast(`Job #${res.id} queued (${KIND_LABELS[kind]})`);
    refreshStatus();
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

/* =========================================================
   JOBS page
   ========================================================= */

let jobsTimer = null;

async function loadJobs() {
  clearTimeout(jobsTimer);
  if (currentPage !== "jobs") return;
  try {
    const rows = await api.get("/api/jobs?limit=200");
    const counts = { queued: 0, running: 0, done: 0, error: 0, canceled: 0 };
    rows.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
    $("#jobs-stats").innerHTML = ["running", "queued", "done", "error"].map((k) =>
      `<div class="stat"><b>${counts[k] || 0}</b><span>${k}</span></div>`).join("");

    if (!rows.length) {
      $("#jobs-table").innerHTML = `<div class="empty">No jobs yet — queue one from the Convert page.</div>`;
    } else {
      $("#jobs-table").innerHTML = `<table>
        <thead><tr><th>#</th><th>Media</th><th>Action</th><th>Status</th><th>Output</th><th>When</th><th></th></tr></thead>
        <tbody>${rows.map(jobRow).join("")}</tbody></table>`;
      $$("#jobs-table [data-act]").forEach((b) =>
        b.addEventListener("click", () => jobAction(b.dataset.act, +b.dataset.id)));
    }
  } catch (e) { toast(e.message, true); }
  jobsTimer = setTimeout(loadJobs, 2500);
}

function jobRow(r) {
  const prog = r.status === "running"
    ? `<div class="progress"><div style="width:${r.progress || 0}%"></div></div>
       <span class="sub">${(r.progress || 0).toFixed(0)}%</span>`
    : (r.status === "error" ? `<div class="sub" style="color:var(--red)">${esc((r.error || "").split("\n")[0])}</div>` : "");
  const outName = (r.output_path || "").split(/[\\/]/).pop();
  const actions = [
    `<button class="btn ghost small" data-act="log" data-id="${r.id}">Log</button>`,
    (r.status === "queued" || r.status === "running")
      ? `<button class="btn ghost small danger" data-act="cancel" data-id="${r.id}">Cancel</button>` : "",
    (r.status === "error" || r.status === "canceled")
      ? `<button class="btn ghost small" data-act="retry" data-id="${r.id}">Retry</button>` : "",
    (r.status !== "running")
      ? `<button class="btn ghost small" data-act="delete" data-id="${r.id}">✕</button>` : "",
  ].join("");
  return `<tr>
    <td>${r.id}</td>
    <td>${esc(r.title)}<div class="sub">${esc((r.plex_path || "").split(/[\\/]/).pop())}</div></td>
    <td>${KIND_LABELS[r.kind] || r.kind}<div class="sub">${r.mode === "ssh" ? "on Plex server" : "in container"}</div></td>
    <td><span class="pill ${r.status}">${r.status}</span>${prog}</td>
    <td class="sub">${esc(outName)}</td>
    <td class="sub">${fmtDate(r.created_at)}</td>
    <td style="white-space:nowrap">${actions}</td>
  </tr>`;
}

async function jobAction(act, id) {
  try {
    if (act === "log") return showJobLog(id);
    if (act === "cancel") await api.post(`/api/jobs/${id}/cancel`);
    if (act === "retry") await api.post(`/api/jobs/${id}/retry`);
    if (act === "delete") await api.del(`/api/jobs/${id}`);
    loadJobs();
  } catch (e) { toast(e.message, true); }
}

let logTimer = null;

async function showJobLog(id) {
  try {
    const j = await api.get("/api/jobs/" + id);
    $("#modal-title").textContent = `Job #${j.id} — ${j.title} (${j.status})`;
    $("#modal-log").textContent =
      (j.error ? "ERROR: " + j.error + "\n\n" : "") + (j.log || "(no output yet)");
    $("#modal").classList.remove("hidden");
    clearTimeout(logTimer);
    if (j.status === "running" || j.status === "queued") {
      logTimer = setTimeout(() => {
        if (!$("#modal").classList.contains("hidden")) showJobLog(id);
      }, 2500);
    }
  } catch (e) { toast(e.message, true); }
}

$("#modal-close").addEventListener("click", () => {
  $("#modal").classList.add("hidden");
  clearTimeout(logTimer);
});
$("#modal").addEventListener("click", (e) => {
  if (e.target === $("#modal")) { $("#modal").classList.add("hidden"); clearTimeout(logTimer); }
});
$("#jobs-refresh").addEventListener("click", loadJobs);

/* =========================================================
   SETTINGS page
   ========================================================= */

const mapState = { local: [], ssh: [] };

function activeMode() {
  const sel = $$("input[name=exec-mode]").find((i) => i.checked);
  return sel ? sel.value : "local";
}

async function loadSettings() {
  try {
    const s = await api.get("/api/settings");
    $("#plex-url").value = s.plex_url || "";
    $("#plex-token").value = s.plex_token || "";
    $$("input[name=exec-mode]").forEach((i) => { i.checked = i.value === (s.exec_mode || "local"); });
    $("#ssh-host").value = s.ssh_host || "";
    $("#ssh-port").value = s.ssh_port || 22;
    $("#ssh-username").value = s.ssh_username || "";
    $("#ssh-os").value = s.ssh_os || "linux";
    $("#ssh-auth").value = s.ssh_auth || "password";
    // secrets are never echoed back into the form; blank means "unchanged"
    $("#ssh-password").value = "";
    $("#ssh-key").value = "";
    $("#ssh-key-passphrase").value = "";
    mapState.local = s.path_maps_local || [];
    mapState.ssh = s.path_maps_ssh || [];
    onModeChange();
  } catch (e) { toast(e.message, true); }
}

function onModeChange() {
  const mode = activeMode();
  syncOptSelection("#page-settings .opts");
  $("#ssh-box").classList.toggle("hidden", mode !== "ssh");
  $("#maps-mode-label").textContent =
    mode === "ssh" ? "— as seen over SSH on the Plex server" : "— as mounted in the MediaForge container";
  onAuthChange();
  renderMaps();
}

function onAuthChange() {
  const auth = $("#ssh-auth").value;
  $("#ssh-pass-row").classList.toggle("hidden", auth !== "password");
  $("#ssh-key-row").classList.toggle("hidden", auth !== "key");
  $("#ssh-phrase-row").classList.toggle("hidden", auth !== "key");
}

$$("input[name=exec-mode]").forEach((i) => i.addEventListener("change", onModeChange));
$("#ssh-auth").addEventListener("change", onAuthChange);

function renderMaps() {
  const mode = activeMode();
  const rows = mapState[mode];
  $("#map-rows").innerHTML = rows.map((m, i) => `
    <div class="map-row">
      <input placeholder="Plex path prefix, e.g. Z:\\Media or /mnt/pool/media" value="${esc(m.plex || "")}" data-i="${i}" data-k="plex">
      <span class="arrow">→</span>
      <input placeholder="${mode === "ssh" ? "path on the Plex server" : "path in this container, e.g. /media"}" value="${esc(m.local || "")}" data-i="${i}" data-k="local">
      <button class="btn ghost small danger" data-i="${i}">✕</button>
    </div>`).join("") || '<div class="muted small">No mappings — paths are used exactly as Plex reports them.</div>';
  $$("#map-rows input").forEach((inp) =>
    inp.addEventListener("input", () => { mapState[mode][+inp.dataset.i][inp.dataset.k] = inp.value; }));
  $$("#map-rows button").forEach((b) =>
    b.addEventListener("click", () => { mapState[mode].splice(+b.dataset.i, 1); renderMaps(); }));
}

$("#map-add").addEventListener("click", () => {
  mapState[activeMode()].push({ plex: "", local: "" });
  renderMaps();
});

function gatherSettings() {
  const out = {
    plex_url: $("#plex-url").value.trim(),
    plex_token: $("#plex-token").value.trim(),
    exec_mode: activeMode(),
    ssh_host: $("#ssh-host").value.trim(),
    ssh_port: parseInt($("#ssh-port").value, 10) || 22,
    ssh_username: $("#ssh-username").value.trim(),
    ssh_os: $("#ssh-os").value,
    ssh_auth: $("#ssh-auth").value,
    path_maps_local: mapState.local.filter((m) => m.plex && m.local),
    path_maps_ssh: mapState.ssh.filter((m) => m.plex && m.local),
  };
  // only send secrets the user actually typed (blank = keep the saved one)
  if ($("#ssh-password").value) out.ssh_password = $("#ssh-password").value;
  if ($("#ssh-key").value.trim()) out.ssh_key = $("#ssh-key").value.trim();
  if ($("#ssh-key-passphrase").value) out.ssh_key_passphrase = $("#ssh-key-passphrase").value;
  return out;
}

$("#settings-save").addEventListener("click", async () => {
  const m = $("#exec-msg");
  m.className = "msg";
  m.textContent = "";
  try {
    await api.post("/api/settings", gatherSettings());
    m.className = "msg ok";
    m.textContent = "Saved.";
    toast("Settings saved");
    refreshStatus();
  } catch (e) {
    m.className = "msg err";
    m.textContent = e.message;
  }
});

$("#plex-test").addEventListener("click", async () => {
  const m = $("#plex-msg");
  m.className = "msg";
  m.textContent = "Testing…";
  try {
    const r = await api.post("/api/settings/test-plex", {
      plex_url: $("#plex-url").value.trim(),
      plex_token: $("#plex-token").value.trim(),
    });
    m.className = "msg " + (r.ok ? "ok" : "err");
    m.textContent = r.ok ? `Connected to ${r.name} (v${r.version})` : r.error;
  } catch (e) { m.className = "msg err"; m.textContent = e.message; }
});

$("#exec-test").addEventListener("click", async () => {
  const m = $("#exec-msg");
  m.className = "msg";
  m.textContent = "Testing ffmpeg…";
  try {
    const s = gatherSettings();
    const r = await api.post("/api/settings/test-exec", {
      mode: s.exec_mode,
      ssh_host: s.ssh_host, ssh_port: s.ssh_port, ssh_username: s.ssh_username,
      ssh_os: s.ssh_os, ssh_auth: s.ssh_auth, ssh_password: s.ssh_password || "",
      ssh_key: s.ssh_key || "", ssh_key_passphrase: s.ssh_key_passphrase || "",
    });
    m.className = "msg " + (r.ok ? "ok" : "err");
    m.textContent = r.ok
      ? `${r.mode === "ssh" ? "On the Plex server" : "In the container"}:\n${r.ffmpeg}\n${r.ffprobe}`
      : r.error;
  } catch (e) { m.className = "msg err"; m.textContent = e.message; }
});
