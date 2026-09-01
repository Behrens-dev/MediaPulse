/* MediaPulse UI */
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

function fmtDuration(ms) {
  if (!ms) return "—";
  const m = Math.round(ms / 60000);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtBitrate(kbps) {
  if (!kbps) return "—";
  return kbps >= 1000 ? (kbps / 1000).toFixed(1) + " Mbps" : kbps + " kbps";
}

function channelName(ch) {
  return { 1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1" }[ch] || (ch ? String(ch) + "ch" : "?");
}

function itemTitle(row) {
  if (row.media_type === "episode")
    return `${row.grandparent_title} — ${row.parent_title ? row.parent_title + " · " : ""}${row.title}`;
  if (row.media_type === "track")
    return `${row.grandparent_title} — ${row.title}`;
  return row.title + (row.year ? ` (${row.year})` : "");
}

/* ---------- navigation ---------- */

let currentPage = "activity";
$$("nav button").forEach((b) =>
  b.addEventListener("click", () => showPage(b.dataset.page)));

const PAGE_TITLES = {
  activity: "Activity",
  history: "Watch History",
  libraries: "Libraries",
  notifications: "Notifications",
  users: "Users",
  backup: "Backup & Restore",
  settings: "Settings",
};

function showPage(page) {
  currentPage = page;
  $("#page-title").textContent = PAGE_TITLES[page] || "MediaPulse";
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== "page-" + page));
  if (page === "activity") loadActivity();
  if (page === "history") { loadHistoryUsers(); loadHistoryStats(); loadHistory(); }
  if (page === "libraries") { loadLibraries(); loadAutoSync(); }
  if (page === "notifications") loadNotifySettings();
  if (page === "users") loadUsers();
  if (page === "settings") loadSettings();
}

/* read a chosen image file as base64; resolves null when nothing selected */
function readImage(inputEl) {
  return new Promise((resolve, reject) => {
    const f = inputEl.files && inputEl.files[0];
    if (!f) return resolve(null);
    if (f.size > 5 * 1024 * 1024) return reject(new Error("Image too large — keep it under 5 MB"));
    const r = new FileReader();
    r.onload = () => resolve({ b64: r.result.split(",")[1], mime: f.type || "image/jpeg" });
    r.onerror = () => reject(new Error("Could not read the image file"));
    r.readAsDataURL(f);
  });
}

/* ---------- status badge ---------- */

async function refreshStatus() {
  try {
    const s = await api.get("/api/status");
    const b = $("#server-badge");
    if (!s.configured) { b.textContent = "⚙ not configured"; b.className = "badge"; }
    else if (s.plex_ok) { b.textContent = `● ${s.server.name} v${s.server.version}`; b.className = "badge up"; }
    else { b.textContent = "● Plex unreachable"; b.className = "badge down"; b.title = s.plex_error; }
  } catch { /* ignore */ }
}

/* ---------- activity ---------- */

async function loadActivity() {
  try {
    const a = await api.get("/api/activity");
    $("#activity-summary").textContent =
      a.stream_count === 0 ? "Nothing is playing right now"
        : `${a.stream_count} stream${a.stream_count > 1 ? "s" : ""} · ${fmtBitrate(a.total_bandwidth_kbps)} total`;
    const list = $("#activity-list");
    if (!a.sessions.length) {
      list.innerHTML = `<div class="empty">📺 No active streams${a.error ? `<br><span class="small">${esc(a.error)}</span>` : ""}</div>`;
      return;
    }
    list.innerHTML = a.sessions.map((s) => {
      const stateCls = s.state === "playing" ? "play" : s.state === "paused" ? "pause" : "buffer";
      const decision = s.stream_decision === "transcode"
        ? `<span class="pill tc">transcode${s.transcode_speed ? ` ${s.transcode_speed}x` : ""}</span>`
        : `<span class="pill dp">direct play</span>`;
      const poster = s.thumb ? `<img class="poster" src="/api/pximg?path=${encodeURIComponent(s.thumb)}" loading="lazy" onerror="this.style.visibility='hidden'">` : "";
      return `<div class="card">
        ${poster}
        <div class="body">
          <div class="title">${esc(itemTitle(s))}</div>
          <div class="sub">${esc(s.user)} · ${esc(s.player)} (${esc(s.platform)})${s.location ? " · " + esc(s.location.toUpperCase()) : ""}</div>
          <div>
            <span class="pill ${stateCls}">${esc(s.state)}</span>
            ${decision}
            <span class="pill">${fmtBitrate(s.bitrate_kbps)}</span>
            ${s.quality ? `<span class="pill">${esc(s.quality)}${/^\d+$/.test(s.quality) ? "p" : ""}</span>` : ""}
          </div>
          <div class="progress"><div style="width:${s.progress_pct}%"></div></div>
          <div class="sub" style="margin-top:4px">${fmtDuration(s.view_offset_ms)} / ${fmtDuration(s.duration_ms)} (${s.progress_pct}%)</div>
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    $("#activity-list").innerHTML = `<div class="empty">⚠ ${esc(e.message)}</div>`;
  }
}

/* ---------- history ---------- */

const hState = { offset: 0, limit: 50 };

async function loadHistoryUsers() {
  const users = await api.get("/api/history/users");
  const sel = $("#h-user");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All users</option>` +
    users.map((u) => `<option value="${esc(u.user)}">${esc(u.user)} (${u.plays})</option>`).join("");
  sel.value = cur;
}

async function loadHistoryStats() {
  const days = Number($("#h-days").value) || 30;
  const s = await api.get(`/api/history/stats?days=${days || 30}`);
  $("#history-stats").innerHTML = `
    <div class="stat"><b>${s.plays || 0}</b><span>plays</span></div>
    <div class="stat"><b>${s.users || 0}</b><span>users</span></div>
    <div class="stat"><b>${s.hours || 0}</b><span>hours</span></div>
    <div class="stat"><b>${s.transcodes || 0}</b><span>transcodes</span></div>`;
}

async function loadHistory() {
  const q = new URLSearchParams({
    search: $("#h-search").value,
    user: $("#h-user").value,
    media_type: $("#h-type").value,
    days: $("#h-days").value,
    offset: hState.offset,
    limit: hState.limit,
  });
  const data = await api.get("/api/history?" + q);
  const rows = data.rows;
  $("#history-table").innerHTML = rows.length ? `
    <table>
      <thead><tr><th>When</th><th>User</th><th>Title</th><th>Player</th><th>Stream</th><th>Progress</th><th>State</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td>${fmtDate(r.started_at)}</td>
          <td>${esc(r.user)}</td>
          <td>${esc(itemTitle(r))}<div class="sub">${esc(r.media_type)}</div></td>
          <td>${esc(r.player)}<div class="sub">${esc(r.platform)}</div></td>
          <td>${r.stream_decision === "transcode" ? '<span class="pill tc">transcode</span>' : '<span class="pill dp">direct</span>'}
              <div class="sub">${fmtBitrate(r.bitrate_kbps)}</div></td>
          <td>${Math.round(r.max_progress_pct || 0)}%</td>
          <td>${esc(r.stopped_at ? "stopped" : r.state)}</td>
        </tr>`).join("")}
      </tbody>
    </table>` : `<div class="empty">No history yet — it starts recording as soon as someone presses play.</div>`;

  const pages = Math.ceil(data.total / hState.limit);
  const page = hState.offset / hState.limit + 1;
  $("#history-pager").innerHTML = pages > 1 ? `
    <button class="btn ghost" ${page <= 1 ? "disabled" : ""} onclick="hPage(-1)">← Prev</button>
    <span>Page ${page} of ${pages} (${data.total} plays)</span>
    <button class="btn ghost" ${page >= pages ? "disabled" : ""} onclick="hPage(1)">Next →</button>` : "";
}

window.hPage = (dir) => { hState.offset += dir * hState.limit; loadHistory(); };
["h-search", "h-user", "h-type", "h-days"].forEach((id) => {
  $("#" + id).addEventListener(id === "h-search" ? "input" : "change", () => {
    hState.offset = 0;
    clearTimeout(window._hT);
    window._hT = setTimeout(() => { loadHistory(); loadHistoryStats(); }, 300);
  });
});

/* ---------- libraries ---------- */

let currentLib = null;
const mState = { offset: 0, limit: 50 };

async function loadLibraries() {
  $("#library-detail").classList.add("hidden");
  $("#library-cards").classList.remove("hidden");
  $("#library-list-head").classList.remove("hidden");
  const cards = $("#library-cards");
  cards.innerHTML = `<div class="empty">Loading libraries…</div>`;
  try {
    const libs = await api.get("/api/libraries");
    cards.innerHTML = libs.map((l) => {
      const counts = Object.entries(l.counts)
        .map(([k, v]) => `<div class="stat"><b>${v.toLocaleString()}</b><span>${k}</span></div>`).join("");
      const icon = { movie: "🎬", show: "📺", artist: "🎵", photo: "🖼" }[l.type] || "📁";
      const syncInfo = l.synced_items
        ? `${l.synced_items.toLocaleString()} files synced · ${fmtBytes(l.synced_size_bytes)}`
        : "media info not synced yet";
      return `<div class="card clickable" onclick="openLibrary('${l.key}', '${esc(l.title)}', '${l.type}')">
        <div class="body">
          <div class="title">${icon} ${esc(l.title)}</div>
          <div class="stat-row" style="margin:10px 0">${counts}</div>
          <div class="sub">Lifetime plays: <b>${(l.plex_view_count + l.tracked_plays).toLocaleString()}</b>
            (Plex: ${l.plex_view_count.toLocaleString()}, MediaPulse-tracked: ${l.tracked_plays.toLocaleString()})</div>
          <div class="sub">${syncInfo}${l.syncing ? " · <b>syncing now…</b>" : ""}</div>
        </div>
      </div>`;
    }).join("") || `<div class="empty">No libraries found</div>`;
  } catch (e) {
    cards.innerHTML = `<div class="empty">⚠ ${esc(e.message)}</div>`;
  }
}

async function loadAutoSync() {
  const s = await api.get("/api/settings");
  $("#autosync-interval").value = String(s.auto_sync_interval_min || 0);
}

$("#autosync-save").addEventListener("click", async () => {
  await api.post("/api/settings", { auto_sync_interval_min: Number($("#autosync-interval").value) });
  toast("Auto-sync schedule saved");
});

window.openLibrary = async (key, title, type) => {
  currentLib = { key, title, type };
  mState.offset = 0;
  $("#library-cards").classList.add("hidden");
  $("#library-list-head").classList.add("hidden");
  $("#library-detail").classList.remove("hidden");
  $("#lib-title").textContent = title;
  await refreshSyncStatus();
  await loadAudioSummary();
  await loadMedia();
};

$("#lib-back-btn").addEventListener("click", loadLibraries);

$("#lib-sync-btn").addEventListener("click", async () => {
  await api.post(`/api/libraries/${currentLib.key}/sync`);
  toast("Media info sync started");
  pollSync();
});

async function refreshSyncStatus() {
  const s = await api.get(`/api/libraries/${currentLib.key}/sync-status`);
  const el = $("#lib-sync-status");
  if (s.state === "never") el.textContent = "Media info has never been synced — click “Sync media info” to scan this library’s files and audio tracks.";
  else if (s.state === "listing") el.textContent = "Sync: listing items…";
  else if (s.state === "syncing") el.textContent = `Sync: ${s.done} / ${s.total} items…`;
  else if (s.state === "error") el.textContent = `Sync failed: ${s.error}`;
  else el.textContent = `Last synced ${fmtDate(s.updated_at)} · ${s.total} items`;
  return s;
}

async function pollSync() {
  const s = await refreshSyncStatus();
  if (s.state === "listing" || s.state === "syncing") {
    setTimeout(pollSync, 2000);
  } else {
    await loadAudioSummary();
    await loadMedia();
  }
}

async function loadAudioSummary() {
  const s = await api.get(`/api/libraries/${currentLib.key}/audio-summary`);
  const box = $("#lib-audio-summary");
  if (!s.total_items) { box.innerHTML = ""; return; }
  const codecRows = s.by_codec.map((c) => `<div class="row"><span>${esc((c.codec || "?").toUpperCase())}</span><span>${c.items}</span></div>`).join("");
  const chRows = s.by_channels.map((c) => `<div class="row"><span>${channelName(c.channels)}</span><span>${c.items}</span></div>`).join("");
  const contRows = s.by_container.map((c) => `<div class="row"><span>${esc(c.container || "?")}</span><span>${c.items}</span></div>`).join("");
  const resRows = s.by_resolution.map((c) => `<div class="row"><span>${esc(c.video_resolution || "?")}${/^\d+$/.test(c.video_resolution) ? "p" : ""}</span><span>${c.items}</span></div>`).join("");
  box.innerHTML = `
    <div class="sum-box ${s.missing_stereo ? "warn" : ""}"><h4>Audio health</h4>
      <div class="row"><span>Items missing a stereo track</span><span>${s.missing_stereo}</span></div>
      <div class="row"><span>Items with multiple audio tracks</span><span>${s.multi_track}</span></div>
      <div class="row"><span>Total items</span><span>${s.total_items}</span></div>
    </div>
    <div class="sum-box"><h4>Audio codecs (items having ≥1 track)</h4>${codecRows}</div>
    <div class="sum-box"><h4>Channel layouts</h4>${chRows}</div>
    <div class="sum-box"><h4>Containers</h4>${contRows}</div>
    <div class="sum-box"><h4>Resolutions</h4>${resRows}</div>`;

  // populate filter dropdowns from actual library contents
  fillSelect("#m-container", s.by_container.map((c) => c.container), "Any container");
  fillSelect("#m-resolution", s.by_resolution.map((c) => c.video_resolution), "Any resolution");
  fillSelect("#m-acodec", s.by_codec.map((c) => c.codec), "Any audio codec");
}

function fillSelect(sel, values, label) {
  const el = $(sel);
  const cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` +
    [...new Set(values.filter(Boolean))].map((v) => `<option value="${esc(v)}">${esc(String(v).toUpperCase())}</option>`).join("");
  el.value = cur;
}

async function loadMedia() {
  const q = new URLSearchParams({
    search: $("#m-search").value,
    container: $("#m-container").value,
    resolution: $("#m-resolution").value,
    video_codec: $("#m-vcodec").value,
    audio_codec: $("#m-acodec").value,
    audio_channels: $("#m-achannels").value,
    missing_stereo: $("#m-missing-stereo").checked,
    offset: mState.offset,
    limit: mState.limit,
  });
  const data = await api.get(`/api/libraries/${currentLib.key}/media?` + q);

  // populate video codec dropdown lazily from page results
  const vcodecs = [...new Set(data.rows.map((r) => r.video_codec).filter(Boolean))];
  if ($("#m-vcodec").options.length <= 1 && vcodecs.length) fillSelect("#m-vcodec", vcodecs, "Any video codec");

  $("#media-table").innerHTML = data.rows.length ? `
    <table>
      <thead><tr><th>Title</th><th>Container</th><th>Video</th><th>Audio tracks</th><th>Size</th><th>Added</th></tr></thead>
      <tbody>${data.rows.map((r) => {
        const title = r.media_type === "episode"
          ? `${esc(r.grandparent_title)} <span class="sub">${esc(r.parent_title)} · ${esc(r.title)}</span>`
          : r.media_type === "track"
            ? `${esc(r.grandparent_title)} <span class="sub">${esc(r.title)}</span>`
            : `${esc(r.title)}${r.year ? ` <span class="sub">(${r.year})</span>` : ""}`;
        const chips = (r.audio_tracks || []).map((t) => {
          const cls = t.channels <= 2 ? "stereo" : "surround";
          const lang = t.language && t.language !== "English" ? ` · ${esc(t.language)}` : "";
          return `<span class="audio-chip ${cls}">${esc((t.codec || "?").toUpperCase())} ${channelName(t.channels)}${lang}</span>`;
        }).join("") || '<span class="audio-chip">no audio info</span>';
        return `<tr>
          <td>${title}</td>
          <td>${esc(r.container || "—")}</td>
          <td>${esc(r.video_codec || "—")}${r.video_resolution ? ` <span class="sub">${esc(r.video_resolution)}${/^\d+$/.test(r.video_resolution) ? "p" : ""}</span>` : ""}</td>
          <td>${chips}</td>
          <td>${fmtBytes(r.file_size)}</td>
          <td>${fmtDate(r.added_at)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>` : `<div class="empty">No items match — or media info hasn't been synced yet.</div>`;

  const pages = Math.ceil(data.total / mState.limit);
  const page = mState.offset / mState.limit + 1;
  $("#media-pager").innerHTML = pages > 1 ? `
    <button class="btn ghost" ${page <= 1 ? "disabled" : ""} onclick="mPage(-1)">← Prev</button>
    <span>Page ${page} of ${pages} (${data.total} items)</span>
    <button class="btn ghost" ${page >= pages ? "disabled" : ""} onclick="mPage(1)">Next →</button>` : "";
}

window.mPage = (dir) => { mState.offset += dir * mState.limit; loadMedia(); };
["m-search", "m-container", "m-resolution", "m-vcodec", "m-acodec", "m-achannels", "m-missing-stereo"].forEach((id) => {
  $("#" + id).addEventListener(id === "m-search" ? "input" : "change", () => {
    mState.offset = 0;
    clearTimeout(window._mT);
    window._mT = setTimeout(loadMedia, 300);
  });
});

/* ---------- notifications ---------- */

$$(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab-body").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
    if (b.dataset.tab === "sent-log") loadSentLog();
  }));

function showPreview(subject, html) {
  $("#modal-subject").textContent = subject;
  $("#modal-frame").srcdoc = html;
  $("#modal").classList.remove("hidden");
}
$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").classList.add("hidden"); });

/* newsletter */
$("#nl-preview").addEventListener("click", async () => {
  try {
    const r = await api.post("/api/notify/newsletter/preview",
      { days_back: Number($("#nl-days").value), note: $("#nl-note").value });
    showPreview(r.subject, r.html);
  } catch (e) { toast(e.message, true); }
});
$("#nl-send").addEventListener("click", async () => {
  if (!confirm("Send the newsletter to all recipients now?")) return;
  const r = await api.post("/api/notify/newsletter/send",
    { days_back: Number($("#nl-days").value), note: $("#nl-note").value });
  r.ok ? toast(`Newsletter sent to ${r.recipients.join(", ")}`) : toast(r.error, true);
});
$("#nl-save-sched").addEventListener("click", async () => {
  await api.post("/api/settings", {
    newsletter_enabled: $("#nl-enabled").checked,
    newsletter_day: Number($("#nl-sched-day").value),
    newsletter_hour: Number($("#nl-sched-hour").value),
    newsletter_days_back: Number($("#nl-sched-days").value),
  });
  toast("Newsletter schedule saved");
});

/* recommendations */
let recPicks = [];

$("#rec-query").addEventListener("input", () => {
  clearTimeout(window._recT);
  const q = $("#rec-query").value.trim();
  if (q.length < 2) { $("#rec-results").innerHTML = ""; return; }
  window._recT = setTimeout(async () => {
    try {
      const results = await api.get("/api/search?q=" + encodeURIComponent(q));
      $("#rec-results").innerHTML = results.map((r, i) => {
        const label = r.type === "episode" ? `${r.grandparent_title} — ${r.title}` : r.title;
        return `<div class="rec-result" data-i="${i}">
          <b>${esc(label)}</b> ${r.year ? `(${r.year})` : ""} <span class="sub">· ${esc(r.type)}</span>
        </div>`;
      }).join("") || `<div class="rec-result">No results</div>`;
      window._recResults = results;
    } catch (e) { toast(e.message, true); }
  }, 350);
});

$("#rec-results").addEventListener("click", (e) => {
  const el = e.target.closest(".rec-result");
  if (!el || el.dataset.i === undefined) return;
  const r = window._recResults[Number(el.dataset.i)];
  const label = r.type === "episode" ? `${r.grandparent_title} — ${r.title}` : r.title;
  recPicks.push({ title: label, year: r.year, type: r.type, summary: r.summary, thumb: r.thumb || "", note: "" });
  $("#rec-results").innerHTML = "";
  $("#rec-query").value = "";
  renderPicks();
});

function renderPicks() {
  $("#rec-count").textContent = recPicks.length ? `(${recPicks.length})` : "";
  $("#rec-picks").innerHTML = recPicks.map((p, i) => `
    <div class="rec-pick">
      <div class="head">
        <span><b>${esc(p.title)}</b> ${p.year ? `(${p.year})` : ""} <span class="sub">· ${esc(p.type)}</span></span>
        <button class="btn ghost danger" onclick="removePick(${i})">✕</button>
      </div>
      <input placeholder="Why do you recommend it? (shows in the email)" value="${esc(p.note)}"
             oninput="recPicks[${i}].note = this.value">
    </div>`).join("") || `<div class="muted">Search above and click a result to add it.</div>`;
}
window.removePick = (i) => { recPicks.splice(i, 1); renderPicks(); };
window.recPicks = recPicks;

function recPayload() {
  return {
    heading: $("#rec-heading").value,
    intro: $("#rec-intro").value,
    items: recPicks,
  };
}
$("#rec-preview").addEventListener("click", async () => {
  if (!recPicks.length) return toast("Add at least one pick first", true);
  const r = await api.post("/api/notify/recommend/preview", recPayload());
  showPreview(r.subject, r.html);
});
$("#rec-send").addEventListener("click", async () => {
  if (!recPicks.length) return toast("Add at least one pick first", true);
  if (!confirm("Send recommendations to all recipients?")) return;
  const r = await api.post("/api/notify/recommend/send", recPayload());
  r.ok ? toast(`Sent to ${r.recipients.join(", ")}`) : toast(r.error, true);
});

/* maintenance */
async function mwPayload() {
  const fmt = (v) => v ? new Date(v).toLocaleString([], { weekday: "short", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
  const img = await readImage($("#mw-image"));
  return {
    start: fmt($("#mw-start").value), end: fmt($("#mw-end").value), message: $("#mw-msg").value,
    image_b64: img ? img.b64 : "", image_mime: img ? img.mime : "",
  };
}
$("#mw-image").addEventListener("change", () => {
  const f = $("#mw-image").files[0];
  $("#mw-image-name").textContent = f ? `${f.name} (${(f.size / 1024).toFixed(0)} KB)` : "";
});
$("#mw-preview").addEventListener("click", async () => {
  try {
    const p = await mwPayload();
    if (!p.start || !p.end) return toast("Pick a start and end time", true);
    const r = await api.post("/api/notify/maintenance/preview", p);
    showPreview(r.subject, r.html);
  } catch (e) { toast(e.message, true); }
});
$("#mw-send").addEventListener("click", async () => {
  try {
    const p = await mwPayload();
    if (!p.start || !p.end) return toast("Pick a start and end time", true);
    if (!confirm("Send the maintenance notice to all recipients?")) return;
    const r = await api.post("/api/notify/maintenance/send", p);
    r.ok ? toast(`Sent to ${r.recipients.join(", ")}`) : toast(r.error, true);
  } catch (e) { toast(e.message, true); }
});

/* outage alerts */
async function ogPayload() {
  const img = await readImage($("#og-image"));
  return {
    message: $("#og-msg").value, eta: $("#og-eta").value,
    image_b64: img ? img.b64 : "", image_mime: img ? img.mime : "",
  };
}
$("#og-image").addEventListener("change", () => {
  const f = $("#og-image").files[0];
  $("#og-image-name").textContent = f ? `${f.name} (${(f.size / 1024).toFixed(0)} KB)` : "";
});
$("#og-preview").addEventListener("click", async () => {
  try {
    const r = await api.post("/api/notify/outage/preview", await ogPayload());
    showPreview(r.subject, r.html);
  } catch (e) { toast(e.message, true); }
});
$("#og-send").addEventListener("click", async () => {
  try {
    if (!confirm("Send the outage notice to all recipients now?")) return;
    const r = await api.post("/api/notify/outage/send", await ogPayload());
    r.ok ? toast(`Sent to ${r.recipients.join(", ")}`) : toast(r.error, true);
  } catch (e) { toast(e.message, true); }
});
$("#og-auto-preview").addEventListener("click", async () => {
  try {
    const r = await api.post("/api/notify/outage/preview",
      { message: $("#og-auto-msg").value, auto: true });
    showPreview(r.subject + " (automatic)", r.html);
  } catch (e) { toast(e.message, true); }
});
$("#og-save-auto").addEventListener("click", async () => {
  await api.post("/api/settings", {
    outage_auto_enabled: $("#og-auto-enabled").checked,
    outage_auto_delay_min: Number($("#og-auto-delay").value),
    outage_auto_message: $("#og-auto-msg").value,
  });
  toast("Automatic outage notice settings saved");
});

/* email settings */
async function loadNotifySettings() {
  const s = await api.get("/api/settings");
  $("#smtp-host").value = s.smtp_host || "";
  $("#smtp-port").value = s.smtp_port || 587;
  $("#smtp-user").value = s.smtp_username || "";
  $("#smtp-pass").value = s.smtp_password || "";
  $("#smtp-security").value = s.smtp_security || "starttls";
  $("#smtp-from").value = s.smtp_from || "";
  $("#smtp-from-name").value = s.smtp_from_name || "MediaPulse";
  $("#recipients").value = (s.recipients || []).join(", ");
  $("#alert-recipients").value = (s.alert_recipients || []).join(", ");
  $("#alert-enabled").checked = s.alert_server_down !== false;
  $("#nl-enabled").checked = !!s.newsletter_enabled;
  $("#nl-sched-day").value = s.newsletter_day || 1;
  $("#nl-sched-hour").value = s.newsletter_hour ?? 9;
  $("#nl-sched-days").value = s.newsletter_days_back || 30;
  $("#og-auto-enabled").checked = !!s.outage_auto_enabled;
  $("#og-auto-delay").value = String(s.outage_auto_delay_min || 15);
  $("#og-auto-msg").value = s.outage_auto_message || "";
}

function splitEmails(v) {
  return v.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
}

$("#email-save").addEventListener("click", async () => {
  await api.post("/api/settings", {
    smtp_host: $("#smtp-host").value.trim(),
    smtp_port: Number($("#smtp-port").value),
    smtp_username: $("#smtp-user").value.trim(),
    smtp_password: $("#smtp-pass").value,
    smtp_security: $("#smtp-security").value,
    smtp_from: $("#smtp-from").value.trim(),
    smtp_from_name: $("#smtp-from-name").value.trim(),
    recipients: splitEmails($("#recipients").value),
    alert_recipients: splitEmails($("#alert-recipients").value),
    alert_server_down: $("#alert-enabled").checked,
  });
  const m = $("#email-msg");
  m.textContent = "Saved ✓"; m.className = "msg ok";
});

$("#email-test").addEventListener("click", async () => {
  const m = $("#email-msg");
  m.textContent = "Sending test…"; m.className = "msg";
  const r = await api.post("/api/settings/test-email", {});
  if (r.ok) { m.textContent = `Test sent to ${r.recipients.join(", ")} ✓`; m.className = "msg ok"; }
  else { m.textContent = r.error; m.className = "msg err"; }
});

/* sent log */
async function loadSentLog() {
  const log = await api.get("/api/notify/log");
  $("#sent-log-table").innerHTML = log.length ? `
    <table>
      <thead><tr><th>When</th><th>Type</th><th>Subject</th><th>Recipients</th><th>Status</th></tr></thead>
      <tbody>${log.map((r) => `
        <tr>
          <td>${fmtDate(r.sent_at)}</td>
          <td>${esc(r.kind)}</td>
          <td>${esc(r.subject)}</td>
          <td>${esc(r.recipients)}</td>
          <td>${r.ok ? '<span class="pill dp">sent</span>' : `<span class="pill tc" title="${esc(r.error)}">failed</span>`}</td>
        </tr>`).join("")}</tbody>
    </table>` : `<div class="empty">Nothing sent yet.</div>`;
}

/* ---------- users ---------- */

async function loadUsers() {
  const box = $("#users-table");
  box.innerHTML = `<div class="empty">Loading users from plex.tv…</div>`;
  try {
    const users = await api.get("/api/users");
    box.innerHTML = users.length ? `
      <table>
        <thead><tr><th>Username</th><th>Alias (real name)</th><th>Email</th><th>Access</th><th>Plays</th><th>Last watched</th></tr></thead>
        <tbody>${users.map((u) => `
          <tr>
            <td><b>${esc(u.username)}</b></td>
            <td><input class="alias-input" data-id="${esc(u.id)}" placeholder="Add a name…" value="${esc(u.alias)}"></td>
            <td>${esc(u.email || "—")}</td>
            <td>${esc(u.access)}</td>
            <td>${u.plays || 0}</td>
            <td>${u.last_play ? fmtDate(u.last_play) : "—"}</td>
          </tr>`).join("")}</tbody>
      </table>` : `<div class="empty">No users found.</div>`;
    $$(".alias-input").forEach((el) =>
      el.addEventListener("change", async () => {
        await api.post("/api/users/alias", { user_id: el.dataset.id, alias: el.value });
        toast("Alias saved");
      }));
  } catch (e) {
    box.innerHTML = `<div class="empty">⚠ ${esc(e.message)}<br>
      <span class="small">The Users page asks plex.tv who has access to your server, so it needs
      internet access and your account-owner token.</span></div>`;
  }
}

$("#users-refresh").addEventListener("click", loadUsers);

/* ---------- settings ---------- */

async function loadSettings() {
  const s = await api.get("/api/settings");
  $("#plex-url").value = s.plex_url || "";
  $("#plex-token").value = s.plex_token || "";
  $("#server-name").value = s.server_display_name || "";
}

$("#plex-test").addEventListener("click", async () => {
  const m = $("#settings-msg");
  m.textContent = "Testing…"; m.className = "msg";
  const r = await api.post("/api/settings/test-plex", {
    plex_url: $("#plex-url").value.trim(),
    plex_token: $("#plex-token").value.trim(),
  });
  if (r.ok) { m.textContent = `Connected to ${r.name} (v${r.version}) ✓`; m.className = "msg ok"; }
  else { m.textContent = r.error; m.className = "msg err"; }
});

$("#plex-save").addEventListener("click", async () => {
  await api.post("/api/settings", {
    plex_url: $("#plex-url").value.trim(),
    plex_token: $("#plex-token").value.trim(),
    server_display_name: $("#server-name").value.trim(),
  });
  const m = $("#settings-msg");
  m.textContent = "Saved ✓"; m.className = "msg ok";
  refreshStatus();
});

/* ---------- backup & restore ---------- */

$("#backup-download").addEventListener("click", () => {
  window.location.href = "/api/backup";
});

$("#restore-btn").addEventListener("click", async () => {
  const m = $("#restore-msg");
  m.className = "msg";
  m.textContent = "";
  const f = $("#restore-file").files && $("#restore-file").files[0];
  if (!f) {
    m.className = "msg err";
    m.textContent = "Choose a backup file first.";
    return;
  }
  if (!confirm("Restore this backup? It replaces the watch history and sent log on " +
               "this instance and overwrites the settings included in the file.")) return;
  try {
    let data;
    try { data = JSON.parse(await f.text()); }
    catch { throw new Error("That file isn't valid JSON — is it really a MediaPulse backup?"); }
    m.textContent = "Restoring…";
    const r = await api.post("/api/restore", data);
    m.className = "msg ok";
    m.textContent = `Restored ${r.restored.settings} settings, ` +
      `${r.restored.history.toLocaleString()} history entries, and ` +
      `${r.restored.sent_log.toLocaleString()} sent-log entries. ` +
      "Reload the page to see everything.";
    toast("Backup restored");
    refreshStatus();
  } catch (e) {
    m.className = "msg err";
    m.textContent = e.message;
  }
});

/* ---------- boot ---------- */

refreshStatus();
setInterval(refreshStatus, 30000);
loadActivity();
setInterval(() => { if (currentPage === "activity") loadActivity(); }, 10000);
renderPicks();
