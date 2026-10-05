const state = {
  rooms: [],
  selected: new Set(),
  currentJobId: null,
  ws: null,
  concurrency: 1,
  service: "matrix",
};

const el = (id) => document.getElementById(id);

/* ---------- Toasts + busy buttons ---------- */
// Visible confirmation for saves etc. - the console may be collapsed or on
// another tab, so it isn't enough on its own.
function toast(message, level = "info") {
  const t = document.createElement("div");
  t.className = `toast ${level}`;
  t.textContent = message;
  el("toast-stack").appendChild(t);
  setTimeout(() => t.classList.add("leaving"), level === "error" ? 7000 : 3500);
  setTimeout(() => t.remove(), level === "error" ? 7400 : 3900);
}
function setBusy(btn, label) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = label;
  return () => { btn.disabled = false; btn.innerHTML = original; };
}

/* ---------- Server connection watchdog ---------- */
// A network-level fetch failure ("TypeError: Failed to fetch") means the local
// server is gone - almost always because its console window was closed. Show
// a clear banner and poll until it's back instead of cryptic per-action errors.
const rawFetch = window.fetch.bind(window);
let serverDown = false;
let serverPoll = null;
function setServerDown(down) {
  if (down === serverDown) return;
  serverDown = down;
  el("server-banner").hidden = !down;
  clearInterval(serverPoll);
  if (down) {
    serverPoll = setInterval(async () => {
      try {
        const r = await rawFetch("/api/health", { cache: "no-store" });
        if (r.ok) setServerDown(false);
      } catch (_) { /* still down */ }
    }, 3000);
  } else if (typeof logTo === "function") {
    logTo(GENERAL_TAB, "Reconnected to Matrix Facility Manager.", "success");
  }
}
// Presence: tells the server this window is open, so the packaged app can quit
// when the last window closes (it waits for running jobs to finish first).
const CLIENT_ID = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random()).slice(2);
function presenceBeat() {
  rawFetch(`/api/presence?id=${CLIENT_ID}`, { method: "POST" })
    .then(() => setServerDown(false))
    .catch(() => setServerDown(true));
}
presenceBeat();
setInterval(presenceBeat, 20000);
window.addEventListener("pagehide", () => navigator.sendBeacon(`/api/presence/bye?id=${CLIENT_ID}`));
window.addEventListener("beforeunload", (e) => {
  // Closing mid-job is allowed (the app finishes the job, then quits), but
  // warn so it's never an accident.
  if (document.querySelector("#job-indicator .dot.active")) e.preventDefault();
});

window.fetch = async (...args) => {
  try {
    const res = await rawFetch(...args);
    setServerDown(false);
    return res;
  } catch (e) {
    if (e instanceof TypeError) setServerDown(true);
    throw e;
  }
};

/* ---------- Theme ---------- */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("md-theme", theme);
}
applyTheme(localStorage.getItem("md-theme") || "light");
el("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(next);
});

/* ---------- Tabs ---------- */
document.querySelectorAll("#tabs .tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#tabs .tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`).classList.add("active");
    closePortPanel();  // shared by the Network and Fiber Test tabs
  });
});

/* ---------- Console tabs ---------- */
function escapeHtml(s) {
  // Quotes too: results are also used inside title="..." attributes.
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// One CSV cell. Text from rooms/NMS (device names...) that starts like a
// formula (= + - @) gets a leading ' so Excel shows it instead of running it.
// Numbers (e.g. -2.5 dBm) are left alone.
function csvCell(v) {
  let s = String(v ?? "");
  if (/^[=+\-@\t\r]./.test(s) && (typeof v !== "number" && Number.isNaN(Number(s)))) s = `'${s}`;
  return `"${s.replace(/"/g, '""')}"`;
}
// Only http(s) or same-site links are ever rendered (no javascript: / data: URLs).
const safeUrl = (url) => (/^(https?:\/\/|\/(?!\/))/i.test(String(url)) ? String(url) : "#");

const tabsBar = el("console-tabs");
const panelsEl = el("console-panels");
const ctabs = { active: null, tabs: {} };
let ctabSeq = 0;

function createConsoleTab(title, { closeable = true, jobId = null } = {}) {
  const id = "ctab" + ++ctabSeq;
  const btn = document.createElement("button");
  btn.className = "ctab";
  btn.dataset.tab = id;
  btn.innerHTML =
    `<span class="ctab-status"></span><span class="ctab-title">${escapeHtml(title)}</span>` +
    (closeable ? `<span class="ctab-close" title="Close tab">&times;</span>` : "");
  tabsBar.appendChild(btn);

  const panel = document.createElement("div");
  panel.className = "console";
  panel.dataset.console = id;
  panel.innerHTML = '<div class="console-empty">No output yet.</div>';
  panelsEl.appendChild(panel);

  ctabs.tabs[id] = { btn, panel, jobId, finished: false, closeable };
  btn.addEventListener("click", (e) => {
    if (e.target.classList.contains("ctab-close")) { closeConsoleTab(id); return; }
    setActiveConsoleTab(id);
  });
  setActiveConsoleTab(id);
  return id;
}

function setActiveConsoleTab(id) {
  ctabs.active = id;
  for (const [tid, t] of Object.entries(ctabs.tabs)) {
    t.btn.classList.toggle("active", tid === id);
    t.panel.classList.toggle("active", tid === id);
  }
  updateCancelButton();
}

function closeConsoleTab(id) {
  const t = ctabs.tabs[id];
  if (!t || !t.closeable) return;
  t.btn.remove();
  t.panel.remove();
  delete ctabs.tabs[id];
  if (ctabs.active === id) {
    const rest = Object.keys(ctabs.tabs);
    setActiveConsoleTab(rest[rest.length - 1] || null);
  }
}

function setConsoleTabStatus(id, status) {
  const t = ctabs.tabs[id];
  if (!t) return;
  const s = t.btn.querySelector(".ctab-status");
  s.className = "ctab-status " + (
    status === "running" ? "ctab-spin" :
    status === "success" ? "ctab-done" :
    status === "failed" ? "ctab-fail" : ""
  );
}

function logTo(id, message, level = "detail") {
  const t = ctabs.tabs[id];
  if (!t) return;
  const empty = t.panel.querySelector(".console-empty");
  if (empty) empty.remove();
  const line = document.createElement("div");
  line.className = "line";
  line.innerHTML = `<span class="ts">${new Date().toLocaleTimeString()}</span><span class="level-${level}">${escapeHtml(String(message))}</span>`;
  t.panel.appendChild(line);
  t.panel.scrollTop = t.panel.scrollHeight;
}

function logLine(message, level = "detail") {
  logTo(ctabs.active, message, level);
}

function logLinkTo(id, label, url) {
  const t = ctabs.tabs[id];
  if (!t) return;
  const empty = t.panel.querySelector(".console-empty");
  if (empty) empty.remove();
  const line = document.createElement("div");
  line.className = "line";
  line.innerHTML =
    `<span class="ts">${new Date().toLocaleTimeString()}</span>` +
    `<span class="level-info">${escapeHtml(label)} </span>` +
    `<a href="${escapeHtml(encodeURI(safeUrl(url)))}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:underline;">${escapeHtml(url)}</a>`;
  t.panel.appendChild(line);
  t.panel.scrollTop = t.panel.scrollHeight;
}

function updateCancelButton() {
  const t = ctabs.tabs[ctabs.active];
  el("cancel-job").disabled = !(t && t.jobId && !t.finished);
}

// Persistent general tab for non-job messages.
const GENERAL_TAB = createConsoleTab("General", { closeable: false });

el("clear-console").addEventListener("click", () => {
  const t = ctabs.tabs[ctabs.active];
  if (t) t.panel.innerHTML = '<div class="console-empty">No output yet.</div>';
});
el("copy-console").addEventListener("click", async () => {
  const t = ctabs.tabs[ctabs.active];
  if (!t) return;
  const text = (t.panel.innerText || "").trim();
  if (!text) { logLine("Nothing to copy.", "warning"); return; }
  const ok = await copyToClipboard(text);
  logLine(ok ? "Console copied to clipboard." : "Copy failed - select the text manually.", ok ? "info" : "warning");
});

/* ---------- Console resize + expand ---------- */
const dock = el("console-dock");
const SAVED_H = Number(localStorage.getItem("md-console-h"));
if (SAVED_H) dock.style.height = SAVED_H + "px";
let restoreH = null;
(function initResizer() {
  const handle = el("console-resizer");
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    const rect = dock.getBoundingClientRect();
    const newH = Math.max(120, Math.min(window.innerHeight - 160, rect.bottom - e.clientY));
    dock.style.height = newH + "px";
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = "";
    localStorage.setItem("md-console-h", String(Math.round(dock.getBoundingClientRect().height)));
  };
  handle.addEventListener("mousedown", (e) => { dragging = true; document.body.style.userSelect = "none"; e.preventDefault(); });
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
})();
el("console-expand").addEventListener("click", () => {
  if (restoreH === null) {
    restoreH = dock.getBoundingClientRect().height;
    dock.style.height = Math.max(120, window.innerHeight - 160) + "px";
    el("console-expand").textContent = "Restore";
  } else {
    dock.style.height = restoreH + "px";
    restoreH = null;
    el("console-expand").textContent = "Expand";
  }
});

/* ---------- Job indicator ---------- */
function setJobStatus(kind, text) {
  document.querySelector("#job-indicator .dot").className = "dot " + kind;
  el("job-status-text").textContent = text;
}
let runningJobs = 0;
function jobStarted() {
  runningJobs++;
  setJobStatus("active", runningJobs > 1 ? `Running (${runningJobs})` : "Running");
}
function jobEnded() {
  runningJobs = Math.max(0, runningJobs - 1);
  if (runningJobs) setJobStatus("active", runningJobs > 1 ? `Running (${runningJobs})` : "Running");
  else setJobStatus("done", "Completed");
}

/* ---------- Downloads ---------- */
function addDownload(name, url) {
  const list = el("downloads-list");
  const hint = list.querySelector(".empty-hint");
  if (hint) hint.remove();
  const a = document.createElement("a");
  a.className = "dl-item";
  a.href = url;
  a.setAttribute("download", name);
  a.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg><span>${escapeHtml(name)}</span><span class="dl-meta">${new Date().toLocaleTimeString()}</span>`;
  list.prepend(a);
}

/* ---------- Rooms ---------- */
function updateCounter() {
  el("room-counter").textContent = `${state.selected.size}/${state.rooms.length}`;
}
const demoUrls = {};
const appUrls = {};
// "OR 1" plus the configured name only when it adds information.
function roomName(room) {
  const base = `OR ${room.number}`;
  return room.name && room.name !== base ? `${base} \u00b7 ${room.name}` : base;
}
// Single-room pickers (Config, Tunnels) follow the most recently ticked room.
function followSidebarRoom(n) {
  for (const id of ["config-room", "tunnel-room"]) {
    const sel = el(id);
    if (sel && sel.querySelector(`option[value="${n}"]`)) sel.value = String(n);
  }
  syncConfigDeploy();
}
async function loadRooms() {
  state.rooms = await (await fetch("/api/rooms")).json();
  state.selected.clear();
  const container = el("rooms");
  container.innerHTML = "";
  const configRoom = el("config-room");
  configRoom.innerHTML = "";
  const tunnelRoom = el("tunnel-room");
  tunnelRoom.innerHTML = "";
  for (const room of state.rooms) {
    const chip = document.createElement("label");
    chip.className = "room-chip";
    chip.dataset.room = room.number;
    chip.innerHTML = `
      <input type="checkbox" data-room="${room.number}" />
      <span class="status" data-status="${room.number}"></span>
      <span class="rname">${escapeHtml(roomName(room))}</span>
      <span class="rid">${escapeHtml(room.room_id)}</span>
      <span class="room-links">
        <button type="button" class="room-link" data-open="nms" title="Open OR ${room.number}'s NMS Demonstrator (copies the login password)">NMS</button>
        <button type="button" class="room-link" data-open="app" title="Open OR ${room.number}'s web app">App</button>
      </span>
    `;
    container.appendChild(chip);
    demoUrls[room.number] = room.demonstrator_url;
    appUrls[room.number] = room.web_app_url;

    const opt = document.createElement("option");
    opt.value = room.number;
    opt.textContent = `${roomName(room)} \u00b7 ${room.room_id}`;
    configRoom.appendChild(opt);
    tunnelRoom.appendChild(opt.cloneNode(true));
  }
  state.configLoadedRoom = null;
  el("config-editor").value = "";
  el("config-status").textContent = "";
  syncConfigDeploy();
  updateCounter();
  // New site: cancel any running check and drop the old site's results.
  cancelLabStatus();
  labStatus.results = {};
  labStatus.changes = {};
  labStatus.since = {};
  el("ls-progress").textContent = "";
  el("ls-updated").textContent = "not checked yet";
  renderLabStatus();
}
// NMS / App buttons on each room chip. The chip is a <label>, so stop the
// click from also toggling the room's checkbox.
el("rooms").addEventListener("click", async (e) => {
  const btn = e.target.closest(".room-link");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const n = Number(btn.closest(".room-chip").dataset.room);
  setActiveConsoleTab(GENERAL_TAB);
  const url = (btn.dataset.open === "app" ? appUrls : demoUrls)[n];
  if (!url) {
    logTo(GENERAL_TAB, `No ${btn.dataset.open === "app" ? "web app" : "NMS"} address for OR ${n} - restart run_server.py to pick up the latest version.`, "warning");
    return;
  }
  if (btn.dataset.open === "app") {
    logLinkTo(GENERAL_TAB, `OR ${n} web app:`, appUrls[n]);
    window.open(safeUrl(appUrls[n]), "_blank", "noopener");
    return;
  }
  // Copy the password FIRST (while this page still has focus), then open the
  // demonstrator - otherwise the new tab steals focus and clipboard fails.
  await fetchAndCopyNmsPassword([n]);
  logLinkTo(GENERAL_TAB, `OR ${n} NMS Demonstrator:`, demoUrls[n]);
  window.open(safeUrl(demoUrls[n]), "_blank", "noopener");
});
el("rooms").addEventListener("change", (e) => {
  if (!e.target.matches("input[type=checkbox]")) return;
  const n = Number(e.target.dataset.room);
  const chip = e.target.closest(".room-chip");
  if (e.target.checked) { state.selected.add(n); chip.classList.add("checked"); followSidebarRoom(n); }
  else { state.selected.delete(n); chip.classList.remove("checked"); }
  updateCounter();
});
async function loadConnectionInfo() {
  const info = await (await fetch("/api/connection")).json();
  el("connection-info").querySelector(".conn-text").textContent =
    `${info.router_ip} \u00b7 ${info.ssh_username} \u00b7 ${info.same_physical_host ? "shared host" : "independent"}`;
}

/* ---------- Network: router port forwarding + switch layout ---------- */
let routerPorts = null;      // last /api/router-ports response
let routerLive = null;       // last live-router check for this site
const NAT_COLUMNS = [
  ["ssh", "SSH", 22], ["swu", "SWU", 8080], ["api", "API / Cert", 443], ["nms", "NMS", 8443],
];
const LIVE_LABEL = { ok: "forwarded", missing: "missing on router", conflict: "port used by something else" };
// What stops working for a room when its forward is missing.
const NAT_IMPACT = {
  ssh: "this app can't SSH in (Facility check, deploys, logs)", swu: "software updates can't be uploaded",
  api: "the Matrix API / certificate isn't reachable from outside", nms: "its NMS can't be checked or opened",
};
// Switch-style view: one row of "ports" per service, one cell per room,
// coloured by what the live router says. Problems are spelled out above it.
function renderRouterBox(data, cols) {
  const st = routerLive ? routerLive.status : {};
  const conflicts = (routerLive && routerLive.conflicts) || {};
  const problems = [];
  let total = 0, ok = 0;
  const rows = cols.map(([k, label, port]) => {
    const outs = data.rooms.map((r) => r.forwards[k] && r.forwards[k].outside_port).filter(Boolean);
    const range = outs.length > 1 ? `${Math.min(...outs)}\u2013${Math.max(...outs)}` : String(outs[0] || "");
    const source = (data.rooms.find((r) => r.forwards[k]) || { forwards: {} }).forwards[k];
    const cells = data.rooms.map((r) => {
      const f = r.forwards[k];
      if (!f) return `<span class="port fwd none"></span>`;
      total++;
      const live = st[String(f.outside_port)];
      if (live === "ok") ok++;
      const target = `${data.router_ip}:${f.outside_port} \u2192 ${r.room_id}:${f.inside_port}`;
      const c = conflicts[String(f.outside_port)];
      if (live === "missing") problems.push(`OR ${r.number} ${label}: port ${f.outside_port} isn't forwarded - ${NAT_IMPACT[k]}.`);
      if (live === "conflict") problems.push(`OR ${r.number} ${label}: port ${f.outside_port} goes to ${c ? `${c.inside_ip}:${c.inside_port}${c.label ? ` (${c.label})` : ""}` : "something else"} instead of ${r.room_id}:${f.inside_port}.`);
      const tip = `OR ${r.number} ${label}: ${target}${live ? ` - ${LIVE_LABEL[live]}` : " - not checked yet"}`;
      return `<span class="port fwd ${live || "unchecked"} ${f.source}" title="${escapeHtml(tip)}">${r.number}</span>`;
    }).join("");
    return `<div class="fwd-row"><span class="fwd-label"><b>${label}</b> <span class="vlan-meta">:${port} \u00b7 ${range}</span>` +
      `${source ? `<span class="fwd-src ${source.source}">${source.source === "qa" ? "QA test" : "facility"}</span>` : ""}</span>${cells}</div>`;
  });
  let summary;
  if (!routerLive) summary = `<span class="legend-hint">${total} planned forwards \u00b7 not compared with the live router yet - click <b>Check live router</b>.</span>`;
  else {
    const other = routerLive.other.length;
    summary = `<span>${ok} of ${total} forwards OK</span>` +
      `<span class="vlan-meta">${routerLive.rule_count} NAT rules on the router${other ? ` \u00b7 ${other} not part of this plan` : ""}</span>` +
      (problems.length ? `<span class="sum-issues">${problems.length} problem${problems.length === 1 ? "" : "s"}</span>` : `<span class="ok-text">No problems</span>`);
  }
  el("router-box").innerHTML =
    `<div class="switch-summary">${summary}</div>` +
    (problems.length ? `<ul class="switch-issues">${problems.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>` : "") +
    `<div class="switch-body"><div class="port-grid fwd-grid">${rows.join("")}</div></div>` +
    `<div class="net-legend fwd-legend"><span><span class="port fwd ok mini"></span> forwarded</span>` +
    `<span><span class="port fwd missing mini"></span> missing</span><span><span class="port fwd conflict mini"></span> used by something else</span>` +
    `<span><span class="port fwd unchecked mini"></span> not checked</span><span class="legend-hint">Hover a cell for the exact forward.</span></div>`;
}
function renderRouterPorts() {
  const data = routerPorts;
  if (!data) return;
  const cols = NAT_COLUMNS.filter(([k]) => data.rooms.some((r) => r.forwards[k]));
  const host = data.facility && data.facility.hostname ? `${data.facility.hostname} \u00b7 ` : "";
  el("router-ports-router").textContent = `${host}${data.router_ip} \u00b7 ${data.wan_interface}`;
  el("router-ports-hint").innerHTML =
    `Each room is reached through a port on <code>${escapeHtml(data.router_ip)}</code> (outside port = base + room number). ` +
    (data.qa_port_forwards
      ? "<b>API</b> is the facility forward every site gets; <b>SSH / SWU / NMS</b> are the extra QA test access on this lab's router."
      : "This lab only has the <b>facility</b> (API) forward, no QA test access.") +
    " <b>Check live router</b> compares the plan with the router's real config (read-only). If a forward is missing, that room can't be reached for it.";
  renderRouterBox(data, cols);
  el("router-ports-thead").innerHTML =
    `<tr><th>Room</th><th>Room IP</th>${cols.map(([, label, port]) => `<th>${label} <span class="th-sub">:${port}</span></th>`).join("")}</tr>`;
  const st = routerLive ? routerLive.status : {};
  el("router-ports-tbody").innerHTML = data.rooms.map((r) => `
    <tr>
      <td>${escapeHtml(roomName(r))}</td>
      <td class="mono">${escapeHtml(r.room_id)}</td>
      ${cols.map(([k]) => {
        const f = r.forwards[k];
        if (!f) return "<td></td>";
        const live = st[String(f.outside_port)];
        const tip = `${data.router_ip}:${f.outside_port} \u2192 ${r.room_id}:${f.inside_port}` +
          (f.source === "qa" ? " (QA test access)" : " (facility)") + (live ? ` - ${LIVE_LABEL[live]}` : "");
        return `<td><span class="port-chip ${f.source}${live ? " " + live : ""}" title="${escapeHtml(tip)}">${f.outside_port}</span></td>`;
      }).join("")}
    </tr>`).join("");
  const counts = Object.values(st).reduce((a, v) => ((a[v] = (a[v] || 0) + 1), a), {});
  el("router-ports-legend").innerHTML =
    `<span class="port-chip facility">facility</span>` +
    (data.qa_port_forwards ? `<span class="port-chip qa">QA test</span>` : "") +
    (routerLive
      ? `<span class="sep"></span><span class="port-chip ok">${counts.ok || 0} forwarded</span>` +
        `<span class="port-chip missing">${counts.missing || 0} missing</span>` +
        `<span class="port-chip conflict">${counts.conflict || 0} conflicting</span>`
      : `<span class="sep"></span><span class="legend-hint">Not compared with the live router yet.</span>`);
  const other = routerLive ? routerLive.other : [];
  el("router-other").hidden = !other.length;
  el("router-other-count").textContent = String(other.length);
  el("router-other-tbody").innerHTML = other.map((o) =>
    `<tr><td><span class="port-chip">${o.outside_port}</span></td><td>${escapeHtml(o.label || "")}</td><td class="mono">${escapeHtml(o.inside_ip)}:${o.inside_port}</td><td>${o.proto}</td></tr>`).join("");
  renderNatPicker(data, cols);
  renderSwitches(data.facility);
}

/* Fix router config: pick which planned forwards to add; the Cisco block is
   built from the picks and can be edited by hand before copying/applying. */
const nat = { sel: new Set(), touched: false, edited: false, cols: [] };
const natKey = (n, k) => `${n}:${k}`;
function natPick(data, cols, mode) {
  const st = routerLive ? routerLive.status : {};
  const out = new Set();
  for (const r of data.rooms) for (const [k] of cols) {
    const f = r.forwards[k];
    if (f && (mode === "all" || (mode === "missing" && st[String(f.outside_port)] === "missing"))) out.add(natKey(r.number, k));
  }
  return out;
}
function natScript(data, cols) {
  const lines = [];
  for (const r of data.rooms) for (const [k] of cols) {
    const f = r.forwards[k];
    if (f && nat.sel.has(natKey(r.number, k))) {
      lines.push(`ip nat inside source static tcp ${r.room_id} ${f.inside_port} interface ${data.wan_interface} ${f.outside_port}`);
    }
  }
  if (lines.length) return `configure terminal\n${lines.join("\n")}\nend\nwrite memory\n`;
  return routerLive && !nat.touched
    ? "! Nothing missing: every planned forward is already on the router.\n! Pick rules above to add some anyway.\n"
    : "! No rules picked. Click squares above (or All planned / Only missing).\n";
}
function renderNatPicker(data, cols) {
  nat.cols = cols;
  // Until the user picks: everything missing once the live router is known, else the whole plan.
  if (!nat.touched) nat.sel = natPick(data, cols, routerLive ? "missing" : "all");
  const st = routerLive ? routerLive.status : {};
  el("nat-pick-missing").disabled = !routerLive;
  el("nat-picker").innerHTML = cols.map(([k, label, port]) => {
    const cells = data.rooms.map((r) => {
      const f = r.forwards[k];
      if (!f) return `<span class="port fwd none"></span>`;
      const live = st[String(f.outside_port)];
      const on = nat.sel.has(natKey(r.number, k));
      const tip = `OR ${r.number} ${label}: ${data.router_ip}:${f.outside_port} \u2192 ${r.room_id}:${f.inside_port}` +
        `${live ? ` - ${LIVE_LABEL[live]}` : ""}\nClick to ${on ? "leave it out" : "add it"}`;
      return `<button type="button" class="port fwd pick ${on ? "on" : "off"}${live && live !== "ok" ? ` live-${live}` : ""}" ` +
        `data-nat="${natKey(r.number, k)}" title="${escapeHtml(tip)}">${r.number}</button>`;
    }).join("");
    return `<div class="fwd-row"><button type="button" class="fwd-label nat-row" data-nat-row="${k}" title="Add or leave out every ${label} rule">` +
      `<b>${label}</b> <span class="vlan-meta">:${port}</span></button>${cells}` +
      `<span class="nat-row-btns"><button type="button" class="ghost xs" data-nat-row="${k}" data-nat-set="all" title="Add every ${label} rule">All</button>` +
      `<button type="button" class="ghost xs" data-nat-row="${k}" data-nat-set="none" title="Leave out every ${label} rule">None</button></span></div>`;
  }).join("");
  const n = nat.sel.size;
  el("nat-count").textContent = routerLive && !nat.touched && !n ? "nothing missing" : `${n} selected`;
  if (!nat.edited) el("router-ports-script").value = natScript(data, cols);
  el("nat-reset").hidden = !nat.edited;
}
function natChange(fn) {
  if (nat.edited && !confirm("Replace your edited commands with the rules you pick?")) return;
  fn();
  nat.touched = true;
  nat.edited = false;
  renderRouterPorts();
}
el("nat-picker").addEventListener("click", (e) => {
  const cell = e.target.closest("[data-nat]");
  const row = e.target.closest("[data-nat-row]");
  if (cell) natChange(() => (nat.sel.has(cell.dataset.nat) ? nat.sel.delete(cell.dataset.nat) : nat.sel.add(cell.dataset.nat)));
  else if (row) {
    const keys = routerPorts.rooms.filter((r) => r.forwards[row.dataset.natRow]).map((r) => natKey(r.number, row.dataset.natRow));
    // Row "All" / "None" buttons set the row; clicking the service name toggles it.
    const on = row.dataset.natSet ? row.dataset.natSet === "all" : !keys.every((key) => nat.sel.has(key));
    natChange(() => keys.forEach((key) => (on ? nat.sel.add(key) : nat.sel.delete(key))));
  }
});
document.querySelectorAll("[data-nat-pick]").forEach((b) => b.addEventListener("click", () => {
  natChange(() => { nat.sel = natPick(routerPorts, nat.cols, b.dataset.natPick); });
}));
el("router-ports-script").addEventListener("input", () => {
  nat.edited = true;
  el("nat-reset").hidden = false;
});
el("nat-reset").addEventListener("click", () => {
  nat.edited = false;
  renderRouterPorts();
});

// Port colours follow the facility planner: one hue per room VLAN.
const ROOM_HUES = ["#f59e0b", "#8b5cf6", "#06b6d4", "#22c55e", "#ec4899", "#3b82f6"];
let switchLive = null;       // last /api/switches/live response for this site
let switchView = "switch";   // "switch" | "room"
let netModel = null;         // last buildNetwork() result (used by the port panel)
const KIND_LABEL = { ENCODER: "Encoder", DECODER: "Decoder", PC: "PC", EXTRON: "Extron", ROUTER: "Router", SWITCH: "Switch", OTHER: "Device" };

function ipToInt(ip) { return ip.split(".").reduce((a, o) => (a << 8) + Number(o), 0) >>> 0; }
function ipInNet(ip, net, prefix) {
  if (!ip || !net || prefix == null) return true;
  const mask = prefix === 0 ? 0 : (~0 << (32 - prefix)) >>> 0;
  return (ipToInt(ip) & mask) === (ipToInt(net) & mask);
}
function vlanTitle(v) { return v.room ? `OR ${v.room}` : v.name.replace(/_/g, " "); }
function speedText(s) { return s === "1000" ? "1G" : s === "100" ? "100M" : s || ""; }

// What NMS calls a device: an encoder's source names / a decoder's display
// names (e.g. "OR1 Display 1 - mna-240"), else "Decoder MNA240".
function nmsNames(dev) {
  const io = dev.kind === "ENCODER" ? dev.inputs : dev.kind === "DECODER" ? dev.outputs : [...(dev.inputs || []), ...(dev.outputs || [])];
  // Unnamed connectors keep NMS's default id-like names (dviOut0, hdmiIn2, dpOut0): skip those.
  const names = [...new Set((io || []).map((x) => x.label).filter((n) => n && !/^[a-z]+(In|Out)\d+$/.test(n)))];
  const kind = `${KIND_LABEL[dev.kind] || dev.kind} ${dev.model}`;
  return names.length
    ? { name: names.join(", "), meta: [dev.model, dev.serial].filter(Boolean).join(" · ") }
    : { name: kind, meta: dev.serial || "" };
}

// NMS devices from the Facility check, indexed by MAC.
function nmsIndex() {
  const byMac = {}, byId = {};
  for (const [room, r] of Object.entries(labStatus.results || {})) {
    for (const d of (r && r.devices) || []) {
      if (byId[d.id]) continue;
      const ifaces = d.interfaces || [];
      const dev = { ...d, room: Number(room), real: ifaces.filter((i) => i.ip && i.ip !== "255.255.255.255") };
      byId[d.id] = dev;
      for (const i of ifaces) byMac[i.mac] = { dev, iface: i.name };
    }
  }
  return { byMac, devices: Object.values(byId) };
}

// One model for both views: switches with port owners, and devices (one
// entry per physical device, with every port it's plugged into) + problems.
function buildNetwork(fac) {
  const nms = nmsIndex();
  const liveBy = {};
  for (const s of (switchLive && switchLive.switches) || []) liveBy[s.name] = s;
  const entities = {};
  const switches = fac.switches.map((sw, idx) => {
    const owner = {};
    let roomIdx = 0;
    const vlans = sw.vlans.map((v) => {
      const vv = { ...v, color: v.kind === "room" ? ROOM_HUES[roomIdx++ % ROOM_HUES.length] : null };
      v.ports.forEach((p) => (owner[p] = vv));
      return vv;
    });
    const live = liveBy[sw.name];
    return { idx, sw, live, lp: live && live.ports ? live.ports : null, owner, vlans, issues: [], portIssues: {} };
  });
  const portIssue = (S, n, msg) => (S.portIssues[n] = S.portIssues[n] || []).push(msg);
  const problem = (e, l, msg) => { e.issues.push(msg); if (l) portIssue(l.S, l.port, `${e.name}: ${msg}`); };

  for (const S of switches) {
    if (!S.lp) continue;
    for (const [ns, st] of Object.entries(S.lp)) {
      const n = Number(ns), v = S.owner[n];
      if (v && (v.kind === "uplink" || v.kind === "backbone") && !st.link) {
        S.issues.push(`${vlanTitle(v)} (port ${n}) has no link`);
        portIssue(S, n, `${vlanTitle(v)} has no link`);
      }
      if (!st.link || st.neighbor) continue;
      if (!v && st.devices.length) portIssue(S, n, "Something is plugged into a port that isn't assigned to any room");
      for (const d of st.devices) {
        const hit = nms.byMac[d.mac];
        let key, base;
        if (hit) {
          key = `nms:${hit.dev.id}`;
          base = { ...nmsNames(hit.dev), kind: hit.dev.kind, serial: hit.dev.serial, nms: hit.dev };
        } else if (d.label) {
          key = `lbl:${d.label}`;
          base = { name: d.label, kind: /PC$/.test(d.label) ? "PC" : /Extron/.test(d.label) ? "EXTRON" : /Router/.test(d.label) ? "ROUTER" : "OTHER" };
        } else {
          key = `mac:${d.mac}`;
          base = { name: d.vendor ? `${d.vendor} device` : "Unknown device", kind: "OTHER" };
          // Barco gear that no room's NMS lists: not adopted (or a spare).
          if (d.vendor === "Barco" && nms.devices.length) base.notes = ["Not registered in any room's NMS"];
        }
        const e = entities[key] || (entities[key] = { notes: [], ...base, key, links: [], issues: [] });
        e.links.push({ S, port: n, speed: st.speed, ip: d.ip, mac: d.mac, iface: hit ? hit.iface : "", vlan: v });
      }
    }
  }
  for (const e of Object.values(entities)) {
    const l0 = e.links.find((l) => l.vlan && l.vlan.room);
    e.room = l0 ? l0.vlan.room : (e.nms ? e.nms.room : null);
    for (const l of e.links) {
      if (l.vlan && l.vlan.kind === "room" && l.ip && !ipInNet(l.ip, l.vlan.ip, l.vlan.prefix)) {
        problem(e, l, `IP ${l.ip} doesn't belong to ${vlanTitle(l.vlan)} (${l.vlan.ip}/${l.vlan.prefix}) - plugged into the wrong room's port?`);
      }
      if (e.nms && l.speed && !/G$/.test(l.speed)) problem(e, l, `Port ${l.port} is running at ${speedText(l.speed)} (expected 10G)`);
    }
    if (e.nms && e.nms.real.length > 1) {
      const seen = new Set(e.links.map((l) => l.mac)).size;
      if (seen < e.nms.real.length) problem(e, null, `Only ${seen} of its ${e.nms.real.length} network ports is connected`);
    }
  }
  // NMS devices that no switch reports (only for rooms whose switch was read).
  const roomRead = {};
  for (const S of switches) for (const v of S.vlans) if (v.kind === "room") roomRead[v.room] = !!S.lp;
  for (const dev of nms.devices) {
    if (!dev.real.length || !roomRead[dev.room] || entities[`nms:${dev.id}`]) continue;
    entities[`nms:${dev.id}`] = {
      key: `nms:${dev.id}`, ...nmsNames(dev), kind: dev.kind, serial: dev.serial,
      nms: dev, room: dev.room, links: [], missing: true, notes: [],
      issues: ["Not seen on any switch port - unplugged, powered off or link down?"],
    };
  }
  // Planned devices (each room's PC from the lab profile, Extrons from the
  // facility config) vs what the switches see. Only for switches that were read.
  const seenIps = new Set(Object.values(entities).flatMap((e) => e.links.map((l) => l.ip)).filter(Boolean));
  const planned = [
    ...state.rooms.map((rm) => ({ ip: rm.room_id, name: `${rm.name} PC`, kind: "PC" })),
    ...Object.entries(fac.rooms || {}).flatMap(([n, meta]) => (meta.extron || []).map((ip) => ({ ip, name: `OR ${n} Extron`, kind: "EXTRON" }))),
  ];
  const expected = [];
  for (const p of planned) {
    const home = switches.flatMap((S) => S.vlans.filter((v) => v.kind === "room" && v.ip && ipInNet(p.ip, v.ip, v.prefix)).map((v) => ({ S, v })))[0];
    if (!home || !home.S.lp) continue;
    const seen = seenIps.has(p.ip);
    expected.push({ ...p, room: home.v.room, seen });
    if (!seen) {
      // A missing room PC is a real problem (the room can't work without it).
      // An Extron is optional and the planner's list can be stale, so it's a note.
      const msg = p.kind === "PC"
        ? `Planned at ${p.ip} but not seen on any switch port - unplugged, powered off or idle?`
        : `Planned at ${p.ip} but not seen on any switch port - not installed, unplugged or powered off? (Check the planner if this room has no Extron.)`;
      entities[`exp:${p.ip}`] = {
        key: `exp:${p.ip}`, name: p.name, kind: p.kind, meta: p.ip, room: home.v.room, links: [], missing: true,
        issues: p.kind === "PC" ? [msg] : [], notes: p.kind === "PC" ? [] : [msg],
      };
    }
  }
  netModel = { switches, entities: Object.values(entities), expected, haveNms: nms.devices.length > 0 };
  return netModel;
}

// Per room: planned PC/Extron and NMS encoders/decoders vs what the switches see.
function expectedRow(m, room) {
  const items = m.expected.filter((x) => x.room === room).map((x) =>
    `<span class="exp ${x.seen ? "ok" : x.kind === "PC" ? "bad" : "warn"}" title="${escapeHtml(x.ip)}">${escapeHtml(x.name.replace(/^OR \d+ /, ""))} ${x.seen ? "\u2713" : "\u2717"}</span>`);
  if (m.haveNms) {
    for (const kind of ["ENCODER", "DECODER"]) {
      const list = m.entities.filter((e) => e.room === room && e.kind === kind && e.nms);
      if (!list.length) continue;
      const seen = list.filter((e) => !e.missing).length;
      items.push(`<span class="exp ${seen === list.length ? "ok" : "bad"}" title="Seen on a switch port / listed in the room's NMS">${KIND_LABEL[kind]}s ${seen}/${list.length}</span>`);
    }
  }
  return items.length ? `<div class="expect-row"><span class="vlan-meta">Planned vs seen</span>${items.join("")}</div>` : "";
}

function portsText(e, withSwitch = false) {
  if (!e.links.length) return "-";
  const by = {};
  for (const l of e.links) (by[l.S.sw.name] = by[l.S.sw.name] || []).push(l.port);
  return Object.entries(by).map(([sw, ps]) => `${withSwitch ? `${sw.replace(/^.*-/, "")} ` : ""}${[...new Set(ps)].sort((a, b) => a - b).join(" + ")}`).join(", ");
}
function kindBadge(kind) {
  return `<span class="kind-badge k-${escapeHtml(String(kind || "other").toLowerCase())}">${KIND_LABEL[kind] || "Device"}</span>`;
}
function entityRows(list, withSwitch) {
  const order = { PC: 0, EXTRON: 1, ENCODER: 2, DECODER: 3, ROUTER: 4, OTHER: 5 };
  return list
    .sort((a, b) => (b.issues.length > 0) - (a.issues.length > 0) || (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || a.name.localeCompare(b.name))
    .map((e) => {
      const ips = [...new Set(e.links.map((l) => l.ip).filter(Boolean))].join(", ") || (e.nms ? e.nms.real.map((i) => i.ip).join(", ") : "");
      const speeds = [...new Set(e.links.map((l) => speedText(l.speed)).filter(Boolean))].join(" / ");
      const status = e.issues.length || e.notes.length
        ? [...e.issues.map((i) => `<span class="issue-text">${escapeHtml(i)}</span>`),
           ...e.notes.map((i) => `<span class="note-text">${escapeHtml(i)}</span>`)].join("<br>")
        : '<span class="ok-text">OK</span>';
      return `<tr class="${e.issues.length ? "has-issue" : ""}"><td>${kindBadge(e.kind)} ${escapeHtml(e.name)}` +
        `${e.meta ? ` <span class="vlan-meta mono">${escapeHtml(e.meta)}</span>` : ""}</td>` +
        `<td class="mono">${escapeHtml(portsText(e, withSwitch))}</td><td class="mono">${escapeHtml(ips)}</td>` +
        `<td>${escapeHtml(speeds)}</td><td>${status}</td></tr>`;
    }).join("");
}
const ENTITY_HEAD = "<thead><tr><th>Device</th><th>Ports</th><th>IP</th><th>Speed</th><th>Status</th></tr></thead>";

function renderSwitches(fac) {
  const box = el("switches");
  if (!fac || !fac.switches.length) {
    el("switches-badge").textContent = "";
    el("switches-check").hidden = true;
    el("switch-view").hidden = true;
    box.innerHTML = `<div class="empty-state"><span>No switch layout for this lab yet. Import its facility config to see each switch's ports, VLANs and rooms.</span>` +
      `<button class="action sm" id="switches-import">Import facility config</button></div>`;
    el("switches-import").addEventListener("click", () => { openSettingsTab(); el("facility-choose").click(); });
    return;
  }
  el("switches-check").hidden = false;
  el("switch-view").hidden = false;
  el("switches-badge").textContent = `${fac.site_code} \u00b7 version ${fac.version}`;
  document.querySelectorAll("#switch-view .seg").forEach((b) => b.classList.toggle("active", b.dataset.view === switchView));
  const m = buildNetwork(fac);
  const nmsHint = switchLive && !m.haveNms
    ? `<p class="hint">Encoders and decoders get their names from the <b>Facility</b> check. Run it (Facility &rarr; Check All Rooms) to name them here.</p>` : "";
  box.innerHTML = nmsHint + (switchView === "room" ? renderRoomView(m) : renderSwitchView(m));
}

function renderSwitchView(m) {
  return m.switches.map((S) => {
    const { sw, lp, owner, vlans } = S;
    const port = (n) => {
      const v = owner[n];
      const st = lp && lp[String(n)];
      const style = v && v.color ? ` style="--port:${v.color}"` : "";
      let cls = `port ${v ? v.kind : "free"}`;
      if (lp) cls += st && st.link ? " up" : " down";
      if (S.portIssues[n]) cls += " issue";
      const tip = `Port ${n}: ${v ? vlanTitle(v) : "not assigned"}${lp ? (st && st.link ? ` \u00b7 link up ${speedText(st.speed)}` : " \u00b7 no link") : ""} - click for details`;
      return `<button type="button" class="${cls}"${style} data-sw="${S.idx}" data-port="${n}" title="${escapeHtml(tip)}">${n}</button>`;
    };
    const front = Array.from({ length: 48 }, (_, i) => i + 1);
    const liveExtra = lp ? Object.keys(lp).map(Number).filter((n) => n > 48 && lp[n].link) : [];
    const extra = [...new Set([...Object.keys(owner).map(Number).filter((n) => n > 48), ...liveExtra])].sort((a, b) => a - b);
    const mine = m.entities.filter((e) => e.links.some((l) => l.S === S) || (e.missing && vlans.some((v) => v.room === e.room)));
    const issueCount = S.issues.length + mine.reduce((a, e) => a + e.issues.length, 0);
    let summary;
    if (S.live && S.live.error) summary = `<span class="issue-text">Couldn't read this switch: ${escapeHtml(S.live.error)}</span>`;
    else if (!lp) summary = `<span class="legend-hint">Not checked yet - click <b>Check switches</b>.</span>`;
    else {
      const parts = vlans.filter((v) => v.kind === "room").map((v) => {
        const n = mine.filter((e) => e.room === v.room && !e.missing).length;
        return `<span class="sum-room"><span class="swatch room" style="--port:${v.color}"></span>OR ${v.room}: ${n} device${n === 1 ? "" : "s"}</span>`;
      });
      parts.push(issueCount ? `<span class="sum-issues">${issueCount} problem${issueCount === 1 ? "" : "s"}</span>` : `<span class="ok-text">No problems</span>`);
      // Planned devices that aren't seen but aren't problems (e.g. an Extron): amber, so they're visible at a glance.
      const unseen = mine.filter((e) => e.key.startsWith("exp:") && !e.issues.length);
      if (unseen.length) {
        const text = unseen.length <= 2 ? unseen.map((e) => `${e.name} not seen`).join(" \u00b7 ") : `${unseen.length} planned devices not seen`;
        parts.push(`<span class="sum-notes" title="${escapeHtml(unseen.map((e) => e.notes[0] || e.name).join("\n"))}">${escapeHtml(text)}</span>`);
      }
      summary = parts.join("");
    }
    const legend = vlans.map((v) => {
      const range = v.ports.length > 1 ? `${v.ports[0]}\u2013${v.ports[v.ports.length - 1]}` : `${v.ports[0] || ""}`;
      const swatch = v.color ? ` style="--port:${v.color}"` : "";
      const upCount = lp ? v.ports.filter((p) => lp[String(p)] && lp[String(p)].link).length : null;
      return `<div class="vlan-row"><span class="swatch ${v.kind}"${swatch}></span>` +
        `<span class="vlan-name">${escapeHtml(vlanTitle(v))}</span>` +
        `<span class="vlan-meta">${v.tag ? `VLAN ${v.tag}` : ""}</span>` +
        `<span class="vlan-meta mono">${v.ip ? `${escapeHtml(v.ip)}/${v.prefix}` : ""}</span>` +
        `<span class="vlan-meta">port${v.ports.length > 1 ? "s" : ""} ${range}${upCount !== null && v.ports.length > 1 ? ` \u00b7 ${upCount} in use` : ""}</span></div>`;
    }).join("");
    const switchIssues = S.issues.map((i) => `<li>${escapeHtml(i)}</li>`).join("");
    const devTable = lp ? `<details class="net-other switch-devices">
        <summary class="subgroup-label">Devices <span class="badge subtle">${mine.filter((e) => !e.missing).length}</span></summary>
        <div class="table-wrap"><table class="data-table net-table">${ENTITY_HEAD}<tbody>${entityRows(mine, false) || '<tr><td colspan="5" class="legend-hint">Nothing plugged in.</td></tr>'}</tbody></table></div>
      </details>` : "";
    return `<div class="switch">
      <div class="switch-head"><strong>${escapeHtml(sw.name || `Switch ${sw.number}`)}</strong><span class="vlan-meta">${escapeHtml(sw.model || "")}</span></div>
      <div class="switch-summary">${summary}</div>
      ${switchIssues ? `<ul class="switch-issues">${switchIssues}</ul>` : ""}
      <div class="switch-body">
        <div class="port-grid">
          <div class="port-row">${front.filter((n) => n % 2).map(port).join("")}</div>
          <div class="port-row">${front.filter((n) => !(n % 2)).map(port).join("")}</div>
        </div>
        ${extra.length ? `<div class="port-grid uplinks"><div class="port-row">${extra.map(port).join("")}</div><span class="vlan-meta">QSFP</span></div>` : ""}
      </div>
      <details class="net-other vlan-fold"><summary class="subgroup-label">VLANs &amp; port ranges</summary><div class="vlan-list">${legend}</div></details>
      ${devTable}
    </div>`;
  }).join("");
}

function renderRoomView(m) {
  if (!switchLive) return `<p class="legend-hint">Click <b>Check switches</b> to see what's plugged in for each room.</p>`;
  const rooms = [];
  for (const S of m.switches) for (const v of S.vlans) if (v.kind === "room") rooms.push({ v, S });
  rooms.sort((a, b) => a.v.room - b.v.room);
  return rooms.map(({ v, S }) => {
    const list = m.entities.filter((e) => e.room === v.room);
    const issues = list.reduce((a, e) => a + e.issues.length, 0);
    const counts = {};
    for (const e of list) if (!e.missing) counts[e.kind] = (counts[e.kind] || 0) + 1;
    const NOUN = { PC: ["PC", "PCs"], EXTRON: ["Extron", "Extrons"], ENCODER: ["encoder", "encoders"], DECODER: ["decoder", "decoders"], ROUTER: ["router", "routers"] };
    const order = ["PC", "EXTRON", "ENCODER", "DECODER", "ROUTER", "OTHER"];
    const mix = order.filter((k) => counts[k]).map((k) => {
      const [one, many] = NOUN[k] || ["other device", "other devices"];
      return `${counts[k]} ${counts[k] === 1 ? one : many}`;
    }).join(" \u00b7 ");
    const range = v.ports.length > 1 ? `${v.ports[0]}\u2013${v.ports[v.ports.length - 1]}` : `${v.ports[0]}`;
    const err = S.live && S.live.error ? `<p class="hint warn-text">Couldn't read ${escapeHtml(S.sw.name)}: ${escapeHtml(S.live.error)}</p>` : "";
    return `<div class="switch room-card">
      <div class="switch-head"><span class="swatch room" style="--port:${v.color}"></span><strong>OR ${v.room}</strong>` +
      `<span class="vlan-meta">${escapeHtml(S.sw.name)} \u00b7 ports ${range} \u00b7 VLAN ${v.tag} \u00b7 ${escapeHtml(v.ip)}/${v.prefix}</span>` +
      `<span class="room-card-sum">${mix || "nothing plugged in"}${issues ? ` \u00b7 <span class="sum-issues">${issues} problem${issues === 1 ? "" : "s"}</span>` : ""}</span></div>
      ${err}
      ${S.lp ? expectedRow(m, v.room) : ""}
      ${list.length ? `<div class="table-wrap"><table class="data-table net-table">${ENTITY_HEAD}<tbody>${entityRows(list, true)}</tbody></table></div>` : ""}
    </div>`;
  }).join("");
}

/* Port detail panel (click a port in the switch view). */
function openPortPanel(swIdx, n) {
  const S = netModel && netModel.switches[swIdx];
  if (!S) return;
  const v = S.owner[n];
  const st = S.lp && S.lp[String(n)];
  const here = netModel.entities.filter((e) => e.links.some((l) => l.S === S && l.port === n));
  const rows = [
    ["Switch", S.sw.name],
    ["VLAN", v ? `${vlanTitle(v)}${v.tag ? ` \u00b7 VLAN ${v.tag}` : ""}${v.tagged ? " (tagged)" : ""}` : "Not assigned to any VLAN"],
    ["Link", !S.lp ? "Not checked yet" : st && st.link ? `Up \u00b7 ${speedText(st.speed)}` : "No link"],
  ];
  if (st && st.neighbor) rows.push(["Connected to", st.neighbor]);
  let html = `<div class="settings-grid pp-grid">${rows.map(([k, val]) => settingCell(k, val)).join("")}</div>`;
  for (const e of here) {
    const l = e.links.find((x) => x.S === S && x.port === n);
    const others = e.links.filter((x) => x !== l).map((x) => `${x.S === S ? "" : x.S.sw.name + " "}port ${x.port}${x.iface ? ` (${x.iface})` : ""}`);
    html += `<div class="pp-device">
      <div>${kindBadge(e.kind)} <strong>${escapeHtml(e.name)}</strong></div>
      ${e.nms ? `<div class="vlan-meta">${escapeHtml(e.nms.model)}${e.serial ? ` · serial <span class="mono">${escapeHtml(e.serial)}</span>` : ""}</div>` : ""}
      <div class="vlan-meta mono">${escapeHtml(l.ip || "no IP learned")} \u00b7 ${escapeHtml(l.mac)}${l.iface ? ` \u00b7 ${escapeHtml(l.iface)}` : ""}</div>
      ${others.length ? `<div class="vlan-meta">Also on ${escapeHtml(others.join(", "))}</div>` : ""}
      ${e.issues.map((i) => `<div class="issue-text">${escapeHtml(i)}</div>`).join("")}
      ${e.notes.map((i) => `<div class="note-text">${escapeHtml(i)}</div>`).join("")}
    </div>`;
  }
  if (st && st.link && !here.length && !st.neighbor) html += `<p class="legend-hint">Link is up but the switch hasn't learned a device on it yet.</p>`;
  const portOnly = (S.portIssues[n] || []).filter((i) => !here.some((e) => i.startsWith(`${e.name}:`)));
  html += portOnly.map((i) => `<div class="issue-text">${escapeHtml(i)}</div>`).join("");
  el("pp-title").textContent = `Port ${n}`;
  el("pp-body").innerHTML = html;
  el("port-panel").hidden = false;
  document.querySelectorAll(".port.selected").forEach((p) => p.classList.remove("selected"));
  const btn = document.querySelector(`.port[data-sw="${swIdx}"][data-port="${n}"]`);
  if (btn) btn.classList.add("selected");
}
function closePortPanel() {
  el("port-panel").hidden = true;
  document.querySelectorAll(".port.selected").forEach((p) => p.classList.remove("selected"));
}
el("switches").addEventListener("click", (e) => {
  const p = e.target.closest(".port[data-port]");
  if (p) openPortPanel(Number(p.dataset.sw), Number(p.dataset.port));
});
el("pp-close").addEventListener("click", closePortPanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !el("port-panel").hidden) closePortPanel(); });
document.querySelectorAll("#switch-view .seg").forEach((b) => b.addEventListener("click", () => {
  switchView = b.dataset.view;
  closePortPanel();
  renderSwitches(routerPorts && routerPorts.facility);
}));

async function checkSwitches({ quiet = false } = {}) {
  const btn = el("switches-check");
  if (btn.disabled || btn.hidden) return;
  // The Fiber Test is already logged in to every switch and fills switchLive when it's done.
  if (fiberBusy) { el("switches-status").textContent = "The Fiber Test is reading the switches\u2026"; return; }
  const done = setBusy(btn, "Reading switches\u2026");
  try {
    const res = await fetch("/api/switches/live", { method: "POST" });
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(d.detail || res.status);
    switchLive = d;
    el("switches-status").textContent = `Checked ${new Date(d.checked_at * 1000).toLocaleTimeString()}`;
    renderSwitches(routerPorts && routerPorts.facility);
    const bad = d.switches.filter((s) => s.error);
    if (bad.length) logTo(GENERAL_TAB, `Switch check: ${bad.map((s) => `${s.name}: ${s.error}`).join("; ")}`, "warning");
    const problems = netModel ? netModel.switches.reduce((a, S) => a + S.issues.length, 0) + netModel.entities.reduce((a, e) => a + e.issues.length, 0) : 0;
    logTo(GENERAL_TAB, `Switch check: ${problems ? `${problems} problem${problems === 1 ? "" : "s"} found` : "no problems found"}.`, problems ? "warning" : "success");
  } catch (e) {
    if (!quiet) toast(`Switch check failed: ${e.message || e}`, "error");
    logTo(GENERAL_TAB, `Switch check failed: ${e.message || e}`, quiet ? "warning" : "error");
  } finally {
    done();
  }
}
el("switches-check").addEventListener("click", () => checkSwitches());

async function loadRouterPorts() {
  routerPorts = await (await fetch("/api/router-ports")).json();
  routerLive = null;
  switchLive = null;
  fiberLive = fiberPrev = null;
  fiberNms = {};
  fiberRun++;
  Object.assign(nat, { sel: new Set(), touched: false, edited: false });
  el("router-live-status").textContent = "";
  el("switches-status").textContent = "";
  el("fiber-updated").textContent = "not tested yet";
  el("fiber-progress").textContent = "";
  renderRouterPorts();
  renderFiber();
}
async function checkLiveRouter({ quiet = false } = {}) {
  const btn = el("router-live-check");
  if (btn.disabled) return;
  const done = setBusy(btn, "Checking\u2026");
  el("router-live-status").textContent = "";
  try {
    const res = await fetch("/api/router/live-nat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        router_admin_username: el("router-admin-username").value.trim() || null,
        router_admin_password: el("router-admin-password").value || null,
        router_enable_password: el("router-enable-password").value || null,
      }),
    });
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(d.detail || res.status);
    routerLive = d;
    el("router-live-status").textContent = `Checked ${new Date(d.checked_at * 1000).toLocaleTimeString()}`;
    renderRouterPorts();
  } catch (e) {
    if (!quiet) toast(`Router check failed: ${e.message || e}`, "error");
    logTo(GENERAL_TAB, `Router check failed: ${e.message || e}`, quiet ? "warning" : "error");
  } finally {
    done();
  }
}
el("router-live-check").addEventListener("click", () => checkLiveRouter());
// Compare with the live router the first time the tab is opened per site,
// when a router login is saved.
document.querySelector('#tabs .tab[data-tab="router-ports"]').addEventListener("click", () => {
  // Router first: the switch check reuses its NAT rules to find each switch.
  if (!routerLive && hasCred("router-admin-password", "router_admin_password")) {
    checkLiveRouter({ quiet: true }).then(() => { if (routerLive && !switchLive) checkSwitches({ quiet: true }); });
  }
});
el("router-ports-refresh").addEventListener("click", () => {
  loadRouterPorts().catch((e) => logLine(`Failed to load router ports: ${e}`, "error"));
});

/* ---------- Fiber Test: both ends of every fiber link ---------- */
// Switch end: the Network tab's switch read plus the optics/error commands.
// Device end: each room's NMS (SFP light + receive errors per device port),
// matched to switch ports by MAC, so each link gets its loss in both
// directions (device Tx - switch Rx, switch Tx - device Rx), like the lab's
// mg2diagnostics --fiber-test. All read-only. The previous test is kept in
// this browser (per site, no secrets), so a re-test shows how the light
// changed and whether errors are still going up.
let fiberLive = null;        // last /api/switches/live {fiber: true} response
let fiberNms = {};           // last /api/fiber/room response per room number
let fiberPrev = null;        // the test before it: { at, ports: {"<switch>|<port>" | "dev|<id>|<port>": {rx, err}} }
let fiberRows = [];          // last rendered switch-port rows (Export CSV)
let fiberLinks = [];         // last rendered device links (By room view)
let fiberView = "switch";    // "switch" | "room"
// One test at a time (each logs in to every switch and room); a site change
// bumps fiberRun so a test still running for the old site is dropped.
let fiberBusy = false;
let fiberRun = 0;
const fiberKey = () => `md-fiber-last:${el("site-select").value || "default"}`;
// The lab's fiber limits (extreme_autov2.check_attenuation): Tx -4.5..-1.5 dBm,
// Rx -5..-1.5 dBm. Below -22 dBm there's no light (link down), not a cabling reading.
const FIBER_RANGE = { tx: [-4.5, -1.5], rx: [-5, -1.5], dark: -22 };
const FIBER_MAX_LOSS = 3;  // dB, one direction of a link (sender's Tx - receiver's Rx)
const outOfRange = (v, [lo, hi]) => v != null && v > FIBER_RANGE.dark && (v < lo || v > hi);
const rangeText = ([lo, hi]) => `${lo} to ${hi} dBm`;
// Error counters that point at the cable/optic. "Lost" (drops) is congestion, not the fiber.
const FIBER_ERRORS = {
  rx: { crc: "CRC", align: "alignment", frag: "fragment", jabber: "jabber", under: "undersize", over: "oversize" },
  tx: { errors: "transmit", coll: "collision", late_coll: "late collision", parity: "parity" },
};
const FIBER_LEVEL = { ok: 0, idle: 0, warn: 1, bad: 2 };
// Copper modules can report DDMI too (a 1000BASE-T SFP's temperature, a QSFP
// direct-attach cable's vendor with "0 nm"), so judge by media type, not by
// whether DDMI answered: 1000T / 10GT = copper SFP, CR / CR4 = direct-attach cable.
const isCopper = (o) => /(^|[^A-Z])(\d+G?T|CR\d*|CU)(_|$)/i.test(o.media || "") || /^0\s*nm$/i.test(o.wavelength || "");
const isOptical = (o) => !isCopper(o) && (o.ddmi || /[_+](SR|LR|ER|ZR|SX|LX|BX|ZX|FX)/i.test(o.media || ""));
const dbm = (v) => (v == null ? "no light" : `${v > 0 ? "+" : ""}${v.toFixed(2)} dBm`);

function fiberLoadPrev() {
  try { return JSON.parse(localStorage.getItem(fiberKey())) || null; } catch { return null; }
}
function fiberSnapshot(d, nms) {
  const ports = {};
  for (const s of d.switches) {
    for (const [n, p] of Object.entries(s.ports || {})) {
      if (!p.optic || !p.optic.present) continue;
      const rx = p.optic.ddm && p.optic.ddm.rx_power;
      ports[`${s.name}|${n}`] = { rx: rx ? rx.value : null, err: p.errors || null };
    }
  }
  for (const r of Object.values(nms)) {
    for (const dev of r.devices || []) {
      for (const i of dev.interfaces) if (i.rx != null || i.tx != null) ports[`dev|${dev.id}|${i.name}`] = { rx: i.rx, err: i.rx_errors };
    }
  }
  return { at: d.checked_at, ports };
}

// What an out-of-range DDMI reading means, in plain words.
function fiberDdmIssue(key, m) {
  const high = /high/i.test(m.status), kind = /alarm/i.test(m.status) ? "alarm" : "warning";
  const limit = high ? m[`high_${kind === "alarm" ? "alarm" : "warn"}`] : m[`low_${kind === "alarm" ? "alarm" : "warn"}`];
  const lim = limit == null ? "" : `, ${kind} ${high ? "above" : "below"} ${limit}`;
  switch (key) {
    case "rx_power":
      return high
        ? `Receiving too much light (${dbm(m.value)}${lim}) - the receiver is overloaded. A long-reach optic on a short link? Use matching optics or add an attenuator.`
        : `Weak light received (${dbm(m.value)}${lim}) - dirty connector, tight bend or damaged fiber. Clean both ends and test again.`;
    case "tx_power":
      return `Sending ${high ? "too much" : "too little"} light (${dbm(m.value)}${lim}) - this SFP's laser is failing; replace the module.`;
    case "temp":
      return `Module ${high ? "running hot" : "too cold"} (${m.value} \u00b0C${lim})${high ? " - check the switch's airflow and fans" : ""}.`;
    case "tx_bias":
      return `Laser bias current ${high ? "high" : "low"} (${m.value} mA${lim}) - the module is wearing out; plan to replace it.`;
    default:
      return `Supply voltage out of range (${m.value} V${lim}) - reseat the module.`;
  }
}

// One port's verdict: level (ok / idle / warn / bad), problems, notes, Rx change.
function fiberCheckPort(S, n, p, prev) {
  const o = p.optic, ddm = o.ddm || {}, rx = ddm.rx_power, issues = [], notes = [];
  let level = "ok";
  const bump = (l) => { if (FIBER_LEVEL[l] > FIBER_LEVEL[level]) level = l; };
  const v = S && S.owner[n];
  const planned = v && (v.kind === "uplink" || v.kind === "backbone");
  const dark = rx && (rx.value == null || /low alarm/i.test(rx.status));
  if (!p.link) {
    if (dark || !rx) {
      if (planned) { issues.push(`${vlanTitle(v)} port has no ${rx ? "light and no " : ""}link - fiber unplugged or broken, or the far end is off`); bump("bad"); }
      else { notes.push(rx ? "No light - nothing plugged in, or the far end is off/unplugged" : "No link"); level = "idle"; }
    } else {
      issues.push(`Light is arriving (${dbm(rx.value)}) but there's no link - the far end runs a different speed or optic type (e.g. 1G vs 10G, SR vs LR)?`);
      bump("bad");
    }
  } else {
    for (const key of ["rx_power", "tx_power", "temp", "tx_bias", "voltage"]) {
      const m = ddm[key];
      if (!m || !/alarm|warn/i.test(m.status)) continue;
      issues.push(fiberDdmIssue(key, m));
      bump(/alarm/i.test(m.status) ? "bad" : "warn");
    }
    // The lab's own (tighter) limits, when the module's limits didn't already flag it.
    const tx = ddm.tx_power;
    if (rx && !/alarm|warn/i.test(rx.status) && outOfRange(rx.value, FIBER_RANGE.rx)) {
      issues.push(`Switch Rx ${dbm(rx.value)} is outside the lab's range (${rangeText(FIBER_RANGE.rx)}) - check the cabling: clean both connectors, reseat, and test again.`);
      bump("warn");
    }
    if (tx && !/alarm|warn/i.test(tx.status) && outOfRange(tx.value, FIBER_RANGE.tx)) {
      issues.push(`Switch Tx ${dbm(tx.value)} is outside the lab's range (${rangeText(FIBER_RANGE.tx)}) - bad switch SFP?`);
      bump("warn");
    }
  }
  // Errors: compare with the previous test when the counters didn't reset (switch reboot / clear).
  const before = prev && prev.err;
  for (const dir of ["rx", "tx"]) {
    for (const [k, label] of Object.entries(FIBER_ERRORS[dir])) {
      const c = p.errors && p.errors[dir] ? p.errors[dir][k] || 0 : 0;
      const b = before && before[dir] ? before[dir][k] : null;
      if (b != null && c > b) { issues.push(`${c - b} new ${label} error${c - b === 1 ? "" : "s"} since the last test - the link is corrupting traffic right now.`); bump("bad"); }
      else if (b != null && c && c === b) notes.push(`${c} ${label} error${c === 1 ? "" : "s"} from before (not increasing)`);
      else if (c) { issues.push(`${c} ${label} error${c === 1 ? "" : "s"} since the counters were last cleared - test again to see whether they're still going up.`); bump("warn"); }
    }
  }
  const rxDelta = prev && prev.rx != null && rx && rx.value != null ? rx.value - prev.rx : null;
  return { level, issues, notes, rxDelta };
}

function fiberErrorsText(p) {
  if (!p.errors) return "-";
  const parts = [];
  for (const dir of ["rx", "tx"]) {
    for (const [k, label] of Object.entries(FIBER_ERRORS[dir])) {
      const c = p.errors[dir] && p.errors[dir][k];
      if (c) parts.push(`${c} ${label}`);
    }
  }
  return parts.join(", ") || "0";
}

// A DDMI reading as text, with the module's limits (port panel, table tooltips).
const ddmCls = (m) => (/alarm/i.test(m.status) ? "issue-text" : /warn/i.test(m.status) ? "note-text" : "");
function ddmLimits(m) {
  if (m.low_alarm == null || m.high_alarm == null) return "";
  return `warning below ${m.low_warn ?? m.low_alarm} or above ${m.high_warn ?? m.high_alarm}, alarm below ${m.low_alarm} or above ${m.high_alarm}`;
}
function rxChange(delta) {
  return delta != null && Math.abs(delta) >= 1 ? `${delta > 0 ? "\u25b2" : "\u25bc"} ${Math.abs(delta).toFixed(1)} dB since the last test` : "";
}
function ddmCell(m, delta = null) {
  if (!m) return '<span class="legend-hint">-</span>';
  const change = rxChange(delta);
  return `<span class="mono ${ddmCls(m)}" title="${escapeHtml(`${m.status}: ${ddmLimits(m)}`)}">${m.value == null ? "no light" : m.value.toFixed(2)}</span>` +
    (change ? ` <span class="vlan-meta">${escapeHtml(change.replace(" since the last test", ""))}</span>` : "");
}

// What's on the other end of a port: LLDP neighbour, else the devices the
// Network model names on it (NMS / lab profile), else its VLAN.
function fiberFarEnd(S, n, p) {
  if (p.neighbor) return { name: p.neighbor, kind: "SWITCH" };
  const here = netModel ? netModel.entities.filter((e) => e.links.some((l) => l.S === S && l.port === n)) : [];
  if (here.length) return { name: here.map((e) => e.name).join(", "), kind: here[0].kind };
  const v = S && S.owner[n];
  return { name: v ? vlanTitle(v) : "", kind: null, vlanOnly: true };
}

// The device end of one link: the lab's limits on its SFP, NMS alerts, receive errors.
function fiberCheckDevice(L, prev) {
  const issues = [], notes = [];
  let level = "ok";
  const bump = (l) => { if (FIBER_LEVEL[l] > FIBER_LEVEL[level]) level = l; };
  if (outOfRange(L.devRx, FIBER_RANGE.rx)) {
    issues.push(`Device Rx ${dbm(L.devRx)} on ${L.iface} is outside the lab's range (${rangeText(FIBER_RANGE.rx)}) - check the cabling: clean both connectors, reseat, and test again.`);
    bump("warn");
  }
  if (outOfRange(L.devTx, FIBER_RANGE.tx)) {
    issues.push(`Device Tx ${dbm(L.devTx)} on ${L.iface} is outside the lab's range (${rangeText(FIBER_RANGE.tx)}) - bad SFP in the device?`);
    bump("warn");
  }
  // A short patch loses well under 3 dB; more in one direction = that strand of the pair.
  for (const [loss, other, dir] of [[L.lossToSw, L.lossToDev, "device \u2192 switch"], [L.lossToDev, L.lossToSw, "switch \u2192 device"]]) {
    if (loss == null || loss <= FIBER_MAX_LOSS) continue;
    issues.push(`High loss ${dir}: ${loss.toFixed(1)} dB${other != null ? ` (the other direction ${other.toFixed(1)} dB)` : ""} - that strand of the fiber pair is dirty, bent or damaged.`);
    bump("warn");
  }
  for (const a of L.alerts || []) { issues.push(`The device reports on ${L.iface}: ${a}`); bump("warn"); }
  const c = L.rxErrors || 0, b = prev ? prev.err : null;
  const errs = (x) => `${x} receive error${x === 1 ? "" : "s"}`;
  if (b != null && c > b) { issues.push(`${errs(c - b)} on the device's ${L.iface} since the last test - the link is corrupting traffic right now.`); bump("bad"); }
  else if (b != null && c && c === b) notes.push(`Device ${L.iface}: ${errs(c)} from before (not increasing)`);
  else if (c) { issues.push(`Device ${L.iface}: ${errs(c)} since it started - test again to see whether they're still going up.`); bump("warn"); }
  if (!L.r) notes.push("Switch end not found (no switch port has learned this MAC)");
  const rxDelta = prev && prev.rx != null && L.devRx != null ? L.devRx - prev.rx : null;
  return { level, issues, notes, rxDelta };
}

// Device links from each room's NMS, matched to the checked switch ports by MAC.
// Device issues are added to their switch port's row too.
function fiberBuildLinks(rowsBy) {
  const at = {};
  for (const s of (fiberLive && fiberLive.switches) || []) {
    for (const [n, p] of Object.entries(s.ports || {})) for (const d of p.devices || []) at[d.mac] = (rowsBy[s.name] || {})[Number(n)];
  }
  const nms = nmsIndex();
  const reading = (r, key) => (r && r.p.optic.ddm && r.p.optic.ddm[key] ? r.p.optic.ddm[key].value : null);
  const links = [];
  for (const [room, res] of Object.entries(fiberNms)) {
    for (const dev of res.devices || []) {
      for (const i of dev.interfaces) {
        if (i.rx == null && i.tx == null) continue;  // port without an SFP (e.g. an unused nexxis1)
        const r = at[i.mac] || null, hit = nms.byMac[i.mac];
        const swRx = reading(r, "rx_power"), swTx = reading(r, "tx_power");
        const L = {
          room: Number(room), devId: dev.id, name: hit ? nmsNames(hit.dev).name : dev.id, kind: hit ? hit.dev.kind : "OTHER",
          iface: i.name, ip: i.ip, mac: i.mac, devRx: i.rx, devTx: i.tx, rxErrors: i.rx_errors, alerts: i.alerts,
          vendor: i.vendor, temp: i.temp, r, swRx, swTx,
          lossToSw: i.tx != null && swRx != null ? i.tx - swRx : null,
          lossToDev: swTx != null && i.rx != null ? swTx - i.rx : null,
        };
        Object.assign(L, fiberCheckDevice(L, fiberPrev && fiberPrev.ports[`dev|${dev.id}|${i.name}`]));
        if (r) {
          (r.links = r.links || []).push(L);
          r.issues.push(...L.issues);
          r.notes.push(...L.notes.filter((x) => !x.startsWith("Switch end")));
          if (FIBER_LEVEL[L.level] > FIBER_LEVEL[r.level]) r.level = L.level;
        }
        links.push(L);
      }
    }
  }
  return links;
}

// Checked ports per switch, by port number ({S, rows: {n: row}}), from the last render (port panel).
let fiberModel = [];

const FIBER_ORDER = { bad: 0, warn: 1, ok: 2, idle: 3 };
function fiberStatusCell(r) {
  if (!r.issues.length && !r.notes.length) return '<span class="ok-text">OK</span>';
  return [...r.issues.map((i) => `<span class="${r.level === "bad" ? "issue-text" : "note-text"}">${escapeHtml(i)}</span>`),
    ...r.notes.map((i) => `<span class="legend-hint">${escapeHtml(i)}</span>`)].join("<br>");
}
function fiberFarCell(r) {
  if (!r.far.name) return '<span class="legend-hint">-</span>';
  return `${r.far.kind ? `${kindBadge(r.far.kind)} ` : ""}<span class="${r.far.vlanOnly ? "legend-hint" : ""}">${escapeHtml(r.far.name)}</span>`;
}
const dash = '<span class="legend-hint">-</span>';
// A device-end reading: amber when outside the lab's range, change since the last test.
function devCell(v, range, delta = null) {
  if (v == null) return dash;
  const change = rxChange(delta);
  return `<span class="mono ${outOfRange(v, range) ? "note-text" : ""}" title="Lab range ${escapeHtml(rangeText(range))}">${v.toFixed(2)}</span>` +
    (change ? ` <span class="vlan-meta">${escapeHtml(change.replace(" since the last test", ""))}</span>` : "");
}
const lossCell = (v) => (v == null ? dash : `<span class="mono">${v.toFixed(2)}</span>`);
const FIBER_DB = ' <span class="th-sub">dBm</span>';
const FIBER_HEAD = `<thead><tr><th>Port</th><th>Connected to</th><th>Speed</th><th>Switch Rx${FIBER_DB}</th><th>Device Rx${FIBER_DB}</th>` +
  `<th>Loss to switch <span class="th-sub">dB</span></th><th>Loss to device <span class="th-sub">dB</span></th><th>Errors</th><th>Status</th></tr></thead>`;
function fiberTableRows(list) {
  return list.slice().sort((a, b) => FIBER_ORDER[a.level] - FIBER_ORDER[b.level] || a.n - b.n).map((r) => {
    const ddm = r.p.optic.ddm || {}, L = (r.links || [])[0];
    return `<tr class="${r.level === "bad" ? "has-issue" : ""}"><td class="mono">${r.n}</td><td class="fib-name">${fiberFarCell(r)}</td>` +
      `<td>${r.p.link ? escapeHtml(speedText(r.p.speed)) : '<span class="legend-hint">no link</span>'}</td>` +
      `<td>${ddmCell(ddm.rx_power, r.rxDelta)}</td><td>${L ? devCell(L.devRx, FIBER_RANGE.rx, L.rxDelta) : dash}</td>` +
      `<td>${lossCell(L && L.lossToSw)}</td><td>${lossCell(L && L.lossToDev)}</td>` +
      `<td class="mono">${escapeHtml(fiberErrorsText(r.p))}</td><td>${fiberStatusCell(r)}</td></tr>`;
  }).join("");
}

// By room: every device port with an SFP, both ends side by side (the lab's --fiber-test view).
const FIBER_ROOM_HEAD = `<thead><tr><th>Device</th><th>Port</th><th>Switch port</th><th>Switch Rx${FIBER_DB}</th><th>Device Rx${FIBER_DB}</th>` +
  `<th>Loss to switch <span class="th-sub">dB</span></th><th>Loss to device <span class="th-sub">dB</span></th><th>Errors</th><th>Status</th></tr></thead>`;
function renderFiberRooms(m) {
  if (!fiberLive) return `<p class="legend-hint">Click <b>Test Fiber</b> to see each room's fiber links.</p>`;
  const vlanOf = {};
  for (const S of m.switches) for (const v of S.vlans) if (v.kind === "room") vlanOf[Number(v.room)] = { v, S };
  return state.rooms.filter((rm) => fiberNms[rm.number]).sort((a, b) => a.number - b.number).map((rm) => {
    const res = fiberNms[rm.number], home = vlanOf[rm.number];
    const list = fiberLinks.filter((L) => L.room === rm.number);
    const status = (L) => L.r || L;
    const bad = list.filter((L) => status(L).level === "bad").length, warn = list.filter((L) => status(L).level === "warn").length;
    const sum = res.error ? "" : [`${list.length} fiber link${list.length === 1 ? "" : "s"}`,
      bad ? `<span class="sum-issues">${bad} problem${bad === 1 ? "" : "s"}</span>` : "",
      warn ? `<span class="sum-notes">${warn} warning${warn === 1 ? "" : "s"}</span>` : "",
      !bad && !warn ? '<span class="ok-text">No problems</span>' : ""].filter(Boolean).join(" \u00b7 ");
    const rows = list.slice().sort((a, b) => FIBER_ORDER[status(a).level] - FIBER_ORDER[status(b).level] || a.name.localeCompare(b.name) || a.iface.localeCompare(b.iface)).map((L) => {
      const st = status(L);
      const swErr = L.r ? fiberErrorsText(L.r.p) : "0";
      const errors = [swErr !== "0" && swErr !== "-" ? `switch ${swErr}` : "", L.rxErrors ? `device ${L.rxErrors}` : ""].filter(Boolean).join(" \u00b7 ") || "0";
      return `<tr class="${st.level === "bad" ? "has-issue" : ""}"><td class="fib-name" title="${escapeHtml(L.devId)}">${kindBadge(L.kind)} ${escapeHtml(L.name)}</td>` +
        `<td class="mono">${escapeHtml(L.iface)}</td><td class="mono">${L.r ? escapeHtml(`${L.r.sw.replace(/^.*-/, "")} ${L.r.n}`) : dash}</td>` +
        `<td>${L.r ? ddmCell(L.r.p.optic.ddm && L.r.p.optic.ddm.rx_power, L.r.rxDelta) : dash}</td><td>${devCell(L.devRx, FIBER_RANGE.rx, L.rxDelta)}</td>` +
        `<td>${lossCell(L.lossToSw)}</td><td>${lossCell(L.lossToDev)}</td><td class="mono">${escapeHtml(errors)}</td><td>${fiberStatusCell(st)}</td></tr>`;
    }).join("");
    const where = home ? `${home.S.sw.name} \u00b7 VLAN ${home.v.tag}` : "";
    return `<div class="switch room-card">
      <div class="switch-head">${home ? `<span class="swatch room" style="--port:${home.v.color}"></span>` : ""}<strong>${escapeHtml(roomName(rm))}</strong>` +
      `<span class="vlan-meta">${escapeHtml(where)}</span><span class="room-card-sum">${sum}</span></div>
      ${res.error ? `<p class="hint warn-text">Couldn't read the device end from OR ${rm.number}'s NMS: ${escapeHtml(res.error)}</p>` : ""}
      ${rows ? `<div class="table-wrap"><table class="data-table net-table">${FIBER_ROOM_HEAD}<tbody>${rows}</tbody></table></div>` : res.error ? "" : '<p class="legend-hint">No fiber ports reported by this room\'s NMS.</p>'}
    </div>`;
  }).join("") || `<p class="legend-hint">No room NMS was read. Enter the SSH and sudo passwords in the sidebar (the NMS login is read from each room) and test again.</p>`;
}

// Switch view like the Network tab: front panel coloured by result, problems
// listed under the summary, and the full module list folded away.
function renderFiberSwitch(S, s, rows) {
  const { sw, owner } = S;
  const port = (n) => {
    const r = rows[n];
    const st = s && s.ports && s.ports[String(n)];
    let cls = "port", tip = `Port ${n}: `;
    if (!s || s.error) { cls += " free"; tip += "not tested"; }
    else if (!r) { cls += " free"; tip += "no module"; }
    else {
      cls += ` fib ${r.level === "ok" && !r.optical ? "copper" : r.level}${st && st.link ? " up" : " down"}${r.level === "bad" ? " issue" : ""}`;
      tip += `${r.p.optic.media || "module"} \u00b7 ${st && st.link ? `link up ${speedText(st.speed)}` : "no link"}${r.far.name ? ` \u00b7 ${r.far.name}` : ""}`;
    }
    return `<button type="button" class="${cls}" data-fsw="${S.idx}" data-port="${n}" title="${escapeHtml(`${tip} - click for details`)}">${n}</button>`;
  };
  const front = Array.from({ length: 48 }, (_, i) => i + 1);
  const live = s && s.ports ? Object.keys(s.ports).map(Number).filter((n) => n > 48 && (s.ports[n].link || rows[n])) : [];
  const extra = [...new Set([...Object.keys(owner).map(Number).filter((n) => n > 48), ...live])].sort((a, b) => a - b);
  const list = Object.values(rows);
  const fiber = list.filter((r) => r.optical);
  const count = (l) => list.filter((r) => r.level === l).length;
  let summary;
  if (s && s.error) summary = `<span class="issue-text">Couldn't read this switch: ${escapeHtml(s.error)}</span>`;
  else if (!s) summary = `<span class="legend-hint">Not tested yet - click <b>Test Fiber</b>.</span>`;
  else {
    const parts = [`<span>${fiber.length} fiber port${fiber.length === 1 ? "" : "s"} \u00b7 ${fiber.filter((r) => r.p.link).length} linked</span>`];
    if (count("idle")) parts.push(`<span>${count("idle")} unused SFP${count("idle") === 1 ? "" : "s"}</span>`);
    if (count("bad")) parts.push(`<span class="sum-issues">${count("bad")} problem${count("bad") === 1 ? "" : "s"}</span>`);
    if (count("warn")) parts.push(`<span class="sum-notes">${count("warn")} warning${count("warn") === 1 ? "" : "s"}</span>`);
    if (!count("bad") && !count("warn")) parts.push('<span class="ok-text">No problems</span>');
    summary = parts.join("");
  }
  const where = (r) => `Port ${r.n}${r.far.name && !r.far.vlanOnly ? ` (${r.far.name})` : ""}`;
  const issueList = (level) => list.filter((r) => r.level === level).sort((a, b) => a.n - b.n)
    .flatMap((r) => r.issues.map((i) => `<li>${escapeHtml(`${where(r)}: ${i}`)}</li>`)).join("");
  const bad = issueList("bad"), warn = issueList("warn");
  const table = s && !s.error ? `<details class="net-other switch-devices">
      <summary class="subgroup-label">Modules <span class="badge subtle">${list.length}</span></summary>
      <div class="table-wrap"><table class="data-table net-table">${FIBER_HEAD}<tbody>${fiberTableRows(list) || '<tr><td colspan="9" class="legend-hint">No SFP modules in this switch.</td></tr>'}</tbody></table></div>
    </details>` : "";
  return `<div class="switch">
    <div class="switch-head"><strong>${escapeHtml(sw.name || `Switch ${sw.number}`)}</strong><span class="vlan-meta">${escapeHtml(sw.model || "")}</span></div>
    <div class="switch-summary">${summary}</div>
    ${bad ? `<ul class="switch-issues">${bad}</ul>` : ""}
    ${warn ? `<ul class="switch-issues notes">${warn}</ul>` : ""}
    <div class="switch-body">
      <div class="port-grid">
        <div class="port-row">${front.filter((n) => n % 2).map(port).join("")}</div>
        <div class="port-row">${front.filter((n) => !(n % 2)).map(port).join("")}</div>
      </div>
      ${extra.length ? `<div class="port-grid uplinks"><div class="port-row">${extra.map(port).join("")}</div><span class="vlan-meta">QSFP</span></div>` : ""}
    </div>
    ${table}
  </div>`;
}

function renderFiber() {
  const box = el("fiber-switches");
  const fac = routerPorts && routerPorts.facility;
  const have = !!(fac && fac.switches.length);
  el("fiber-check").disabled = !have || fiberBusy;
  el("fiber-legend").hidden = !have || fiberView === "room";
  el("fiber-view").hidden = !have;
  el("fiber-badge").textContent = have ? `${fac.site_code} \u00b7 version ${fac.version}` : "";
  document.querySelectorAll("#fiber-view .seg").forEach((b) => b.classList.toggle("active", b.dataset.view === fiberView));
  fiberModel = [];
  fiberRows = [];
  fiberLinks = [];
  if (!have) {
    el("fiber-summary").innerHTML = "";
    el("fiber-export").disabled = true;
    box.innerHTML = `<div class="empty-state"><span>No switch layout for this lab yet. Import its facility config to test the switches' fiber ports.</span>` +
      `<button class="action sm" id="fiber-import">Import facility config</button></div>`;
    el("fiber-import").addEventListener("click", () => { openSettingsTab(); el("facility-choose").click(); });
    return;
  }
  const m = buildNetwork(fac);
  const liveBy = {};
  for (const s of (fiberLive && fiberLive.switches) || []) liveBy[s.name] = s;
  const rows = [], rowsBy = {};
  for (const S of m.switches) {
    const s = liveBy[S.sw.name] || null;
    const byPort = {};
    if (s && !s.error) {
      for (const [ns, p] of Object.entries(s.ports || {})) {
        if (!p.optic || !p.optic.present) continue;
        const n = Number(ns), prev = fiberPrev && fiberPrev.ports[`${s.name}|${n}`];
        const r = { sw: s.name, n, p, optical: isOptical(p.optic), far: fiberFarEnd(S, n, p), ...fiberCheckPort(S, n, p, prev) };
        byPort[n] = r;
        rows.push(r);
      }
    }
    rowsBy[S.sw.name] = byPort;
    fiberModel[S.idx] = { S, s, rows: byPort };
  }
  fiberLinks = fiberBuildLinks(rowsBy);
  fiberRows = rows;
  box.innerHTML = fiberView === "room" ? renderFiberRooms(m) : fiberModel.map((F) => renderFiberSwitch(F.S, F.s, F.rows)).join("");
  renderFiberNotes();
  if (!fiberLive) {
    el("fiber-summary").innerHTML = "";
    el("fiber-export").disabled = true;
    return;
  }
  const fiberAll = rows.filter((r) => r.optical);
  const n = (l, list = rows) => list.filter((r) => r.level === l).length;
  const metric = (label, value, ok, title = "") =>
    `<div class="ls-metric ${ok ? "good" : "warn"}"${title ? ` title="${escapeHtml(title)}"` : ""}><span class="k">${label}</span><span class="v">${escapeHtml(value)}</span></div>`;
  const errPorts = rows.filter((r) => fiberErrorsText(r.p) !== "0" && fiberErrorsText(r.p) !== "-").length;
  const failed = fiberLive.switches.filter((s) => s.error).length;
  const roomsRead = Object.values(fiberNms).filter((x) => !x.error).length, roomsAll = Object.keys(fiberNms).length;
  const both = fiberLinks.filter((L) => L.r).length;
  el("fiber-summary").innerHTML = [
    metric("Fiber ports OK", `${fiberAll.filter((r) => r.level === "ok").length} / ${fiberAll.filter((r) => r.level !== "idle").length}`, !n("bad", fiberAll) && !n("warn", fiberAll), "Linked fiber ports with good light and no errors (unused ports not counted)"),
    metric("Links, both ends", `${both} / ${fiberLinks.length}`, both === fiberLinks.length && roomsRead === roomsAll,
      `Device fiber ports (from ${roomsRead} of ${roomsAll} rooms' NMS) matched to a switch port, so the loss is known in both directions`),
    metric("Problems", String(n("bad")), !n("bad")),
    metric("Warnings", String(n("warn")), !n("warn")),
    metric("Ports with errors", String(errPorts), !errPorts, "Ports whose error counters aren't zero (since the switch booted or the counters were cleared)"),
    metric("Unused SFPs", String(n("idle")), true, "SFP fitted but no light and no link"),
    failed ? metric("Switches not read", String(failed), false) : "",
  ].join("");
  el("fiber-export").disabled = !rows.length;
}

// What couldn't be tested, shown in the main card whichever view is open. A
// changed SSH key / NMS certificate is spelled out: the app refused on purpose,
// and the fingerprint should only be removed once someone knows why it changed.
function renderFiberNotes() {
  const box = el("fiber-notes");
  if (!fiberLive) { box.innerHTML = ""; return; }
  const failed = Object.values(fiberNms).filter((r) => r.error).sort((a, b) => a.number - b.number);
  const changed = failed.filter((r) => /host key changed|certificate changed/i.test(r.error));
  const other = failed.filter((r) => !changed.includes(r));
  const switches = fiberLive.switches.filter((s) => s.error);
  const items = [];
  if (changed.length) {
    items.push(`<li class="issue-text"><b>OR ${changed.map((r) => r.number).join(", ")}: SSH host key or NMS certificate changed</b>, so the app refused to connect ` +
      `and their device end wasn't tested. If ${changed.length === 1 ? "this room was" : "these rooms were"} re-imaged or replaced, remove the fingerprint ` +
      `(<b>Actions &rarr; Power &amp; Maintenance &rarr; Remove SSH Fingerprint</b>) and test again. If nobody knows why it changed, don't: something may be ` +
      `impersonating the room. Tell the team. <button class="action xs" id="fiber-select-changed">Select ${changed.length === 1 ? "it" : "them"} in Actions</button></li>`);
  }
  for (const r of other) items.push(`<li class="note-text">OR ${r.number}: device end not tested - ${escapeHtml(r.error)}</li>`);
  for (const s of switches) items.push(`<li class="issue-text">${escapeHtml(s.name)} not tested - ${escapeHtml(s.error)}</li>`);
  box.innerHTML = items.length ? `<ul class="switch-issues fiber-notes">${items.join("")}</ul>` : "";
  const btn = el("fiber-select-changed");
  if (btn) btn.addEventListener("click", () => {
    // Only selects the rooms and opens Actions; removing the fingerprint stays a deliberate click there.
    const want = new Set(changed.map((r) => r.number));
    document.querySelectorAll("#rooms input[type=checkbox]").forEach((cb) => {
      const on = want.has(Number(cb.dataset.room));
      cb.checked = on;
      cb.closest(".room-chip").classList.toggle("checked", on);
      if (on) state.selected.add(Number(cb.dataset.room)); else state.selected.delete(Number(cb.dataset.room));
    });
    updateCounter();
    document.querySelector('#tabs .tab[data-tab="actions"]').click();
    toast(`Selected OR ${[...want].join(", ")}. Remove SSH Fingerprint is under Power & Maintenance - only if you know why the key changed.`, "info");
  });
}

/* Port detail panel for the Fiber tab (same panel as the Network tab). */
function openFiberPortPanel(swIdx, n) {
  const F = fiberModel[swIdx];
  if (!F) return;
  const { S, s } = F, r = F.rows[n], st = s && s.ports && s.ports[String(n)];
  const v = S.owner[n];
  const rows = [
    ["Switch", S.sw.name],
    ["VLAN", v ? `${vlanTitle(v)}${v.tag ? ` \u00b7 VLAN ${v.tag}` : ""}` : "Not assigned to any VLAN"],
    ["Link", !s || s.error ? "Not tested yet" : st && st.link ? `Up \u00b7 ${speedText(st.speed)}` : "No link"],
  ];
  let html = "";
  if (!r) {
    html = `<div class="settings-grid pp-grid">${rows.map(([k, val]) => settingCell(k, val)).join("")}</div>`;
    if (s && !s.error) html += `<p class="legend-hint">No SFP module in this port.</p>`;
  } else {
    const o = r.p.optic, ddm = o.ddm || {};
    if (r.far.name) rows.push(["Connected to", r.far.name]);
    rows.push(["Module", [o.media, o.wavelength, r.optical ? "" : "copper / DAC, no light readings"].filter(Boolean).join(" \u00b7 ")]);
    if (o.vendor || o.part) rows.push(["Vendor / part", [o.vendor, o.part].filter(Boolean).join(" ")]);
    if (o.serial) rows.push(["Serial", o.serial]);
    html = `<div class="settings-grid pp-grid">${rows.map(([k, val]) => settingCell(k, val)).join("")}`;
    const reading = (label, key, unit, delta = null) => {
      const m = ddm[key];
      if (!m) return "";
      const value = m.value == null ? "no light" : `${m.value}${unit ? ` ${unit}` : ""}`;
      const extra = [m.status, rxChange(delta)].filter(Boolean).join(" \u00b7 ");
      return settingCell(label, `${value} \u00b7 ${extra}`, ddmCls(m));
    };
    html += reading("Rx light", "rx_power", "dBm", r.rxDelta) + reading("Tx light", "tx_power", "dBm") +
      reading("Temperature", "temp", "\u00b0C") + reading("Laser bias", "tx_bias", "mA") + reading("Supply voltage", "voltage", "V") +
      settingCell("Errors", fiberErrorsText(r.p)) + "</div>";
    if (ddm.rx_power && ddmLimits(ddm.rx_power)) html += `<div class="vlan-meta pp-note">Rx limits for this module: ${escapeHtml(ddmLimits(ddm.rx_power))} dBm</div>`;
    if (o.supported === false) html += `<div class="vlan-meta pp-note">The switch lists this module as "Unsupported" (not Extreme-branded). That's normal for the lab optics.</div>`;
    for (const L of r.links || []) {
      const fmt = (v, unit) => (v == null ? "-" : `${v.toFixed(2)} ${unit}`);
      html += `<div class="pp-device">
        <div>${kindBadge(L.kind)} <strong>${escapeHtml(L.name)}</strong></div>
        <div class="vlan-meta mono">${escapeHtml([L.iface, L.ip, L.mac].filter(Boolean).join(" \u00b7 "))}</div>
        <div class="settings-grid pp-grid">
          ${settingCell("Device Rx", [fmt(L.devRx, "dBm"), rxChange(L.rxDelta)].filter(Boolean).join(" \u00b7 "), outOfRange(L.devRx, FIBER_RANGE.rx) ? "note-text" : "")}
          ${settingCell("Device Tx", fmt(L.devTx, "dBm"), outOfRange(L.devTx, FIBER_RANGE.tx) ? "note-text" : "")}
          ${settingCell("Loss device \u2192 switch", fmt(L.lossToSw, "dB"))}
          ${settingCell("Loss switch \u2192 device", fmt(L.lossToDev, "dB"))}
          ${settingCell("Device receive errors", String(L.rxErrors ?? "-"))}
          ${L.vendor || L.temp != null ? settingCell("Device SFP", [L.vendor, L.temp != null ? `${L.temp} \u00b0C` : ""].filter(Boolean).join(" \u00b7 ")) : ""}
        </div>
      </div>`;
    }
    html += `<div class="vlan-meta pp-note">Lab range: Rx ${escapeHtml(rangeText(FIBER_RANGE.rx))}, Tx ${escapeHtml(rangeText(FIBER_RANGE.tx))}.</div>`;
    html += r.issues.map((i) => `<div class="${r.level === "bad" ? "issue-text" : "note-text"}">${escapeHtml(i)}</div>`).join("");
    html += r.notes.map((i) => `<div class="legend-hint">${escapeHtml(i)}</div>`).join("");
  }
  el("pp-title").textContent = `Port ${n}`;
  el("pp-body").innerHTML = html;
  el("port-panel").hidden = false;
  document.querySelectorAll(".port.selected").forEach((p) => p.classList.remove("selected"));
  const btn = document.querySelector(`.port[data-fsw="${swIdx}"][data-port="${n}"]`);
  if (btn) btn.classList.add("selected");
}
el("fiber-switches").addEventListener("click", (e) => {
  const p = e.target.closest(".port[data-fsw]");
  if (p) openFiberPortPanel(Number(p.dataset.fsw), Number(p.dataset.port));
});
document.querySelectorAll("#fiber-view .seg").forEach((b) => b.addEventListener("click", () => {
  fiberView = b.dataset.view;
  closePortPanel();
  renderFiber();
}));

async function checkFiber({ quiet = false } = {}) {
  const btn = el("fiber-check");
  if (fiberBusy || btn.disabled) return;
  fiberBusy = true;
  const run = ++fiberRun;
  const done = setBusy(btn, "Testing\u2026");
  el("fiber-progress").textContent = "Reading the switches and each room's NMS\u2026";
  const json = { "Content-Type": "application/json" };
  // Device end, one request per room alongside the switch read; a room that fails doesn't stop the test.
  const creds = credentials();
  const nmsReads = Promise.all(state.rooms.filter((rm) => !rm.secondary).map(async (rm) => {
    try {
      const r = await fetch("/api/fiber/room", { method: "POST", headers: json, body: JSON.stringify({ room_number: rm.number, ...creds }) });
      const d = await r.json().catch(() => ({ detail: r.statusText }));
      return r.ok ? d : { number: rm.number, devices: [], error: d.detail || String(r.status) };
    } catch (e) {
      return { number: rm.number, devices: [], error: String(e.message || e) };
    }
  }));
  try {
    const res = await fetch("/api/switches/live", { method: "POST", headers: json, body: JSON.stringify({ fiber: true }) });
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(d.detail || res.status);
    const nms = {};
    for (const r of await nmsReads) nms[r.number] = r;
    if (run !== fiberRun) return;  // site changed while testing: this result is for the old one
    fiberPrev = fiberLoadPrev();
    fiberLive = d;
    fiberNms = nms;
    switchLive = d;  // also feeds the Network tab (same read, plus the fiber fields)
    try { localStorage.setItem(fiberKey(), JSON.stringify(fiberSnapshot(d, nms))); } catch { /* storage full: no history */ }
    el("fiber-updated").textContent = `tested ${new Date(d.checked_at * 1000).toLocaleTimeString()}`;
    el("fiber-progress").textContent = fiberPrev ? `Compared with the test at ${new Date(fiberPrev.at * 1000).toLocaleString()}` : "First test on this computer - test again later to see changes.";
    el("switches-status").textContent = `Checked ${new Date(d.checked_at * 1000).toLocaleTimeString()}`;
    renderFiber();
    renderSwitches(routerPorts && routerPorts.facility);
    const bad = fiberRows.filter((r) => r.level === "bad").length, warn = fiberRows.filter((r) => r.level === "warn").length;
    for (const s of d.switches.filter((x) => x.error)) logTo(GENERAL_TAB, `Fiber test: ${s.name}: ${s.error}`, "warning");
    for (const r of Object.values(nms).filter((x) => x.error)) logTo(GENERAL_TAB, `Fiber test: OR ${r.number} device end not read: ${r.error}`, "warning");
    for (const r of fiberRows.filter((x) => x.level === "bad")) logTo(GENERAL_TAB, `Fiber test: ${r.sw} port ${r.n}: ${r.issues.join(" ")}`, "warning");
    logTo(GENERAL_TAB, `Fiber test: ${bad || warn ? `${bad} problem${bad === 1 ? "" : "s"}, ${warn} warning${warn === 1 ? "" : "s"}` : "no problems found"}.`, bad || warn ? "warning" : "success");
  } catch (e) {
    if (run !== fiberRun) return;
    el("fiber-progress").textContent = "";
    if (!quiet) toast(`Fiber test failed: ${e.message || e}`, "error");
    logTo(GENERAL_TAB, `Fiber test failed: ${e.message || e}`, quiet ? "warning" : "error");
  } finally {
    fiberBusy = false;
    done();
    el("fiber-check").disabled = !(routerPorts && routerPorts.facility && routerPorts.facility.switches.length);
  }
}
el("fiber-check").addEventListener("click", () => checkFiber());
// Test the first time the tab is opened per site, when a router login is saved (like the Network tab).
document.querySelector('#tabs .tab[data-tab="fiber"]').addEventListener("click", () => {
  if (!fiberLive && hasCred("router-admin-password", "router_admin_password")) checkFiber({ quiet: true });
});
/* Export Excel: one row per link. The columns people read first are on the
   left (result, where, both ends, loss, errors, what's wrong), module details
   on the right. Sheets: Summary, one per switch, and All links. */
const FIBER_EXPORT_HEAD = [
  "Result", "Room", "Device", "Device port", "Switch", "Switch port", "Link",
  "Switch Rx (dBm)", "Device Rx (dBm)", "Loss to switch (dB)", "Loss to device (dB)",
  "Switch Tx (dBm)", "Device Tx (dBm)", "Switch Rx change (dB)", "Device Rx change (dB)",
  "Errors", "Issues", "Notes",
  "Media", "Switch module", "Wavelength", "Switch SFP vendor", "Switch SFP part", "Switch SFP serial",
  "Switch SFP temp (C)", "Switch laser bias (mA)", "Switch SFP voltage (V)", "Device SFP vendor", "Device SFP temp (C)",
  "Device ID", "Site", "Tested",
];
const FIBER_RESULT = { bad: "Problem", warn: "Warning", ok: "OK", idle: "Unused SFP" };
// "Finding - advice" -> "Finding": the advice is in the app and the FAQ.
const shortIssue = (text) => text.split(" - ")[0].replace(/\.$/, "");
const num = (v, digits = 2) => (v == null || Number.isNaN(v) ? "" : Number(v.toFixed(digits)));
function fiberExportRows() {
  const swBy = {};
  for (const F of fiberModel) if (F) swBy[F.S.sw.name] = F.S;
  const reading = (r, key) => (r && r.p.optic.ddm && r.p.optic.ddm[key] ? r.p.optic.ddm[key].value : null);
  const errorsText = (r, L) => {
    const sw = r ? fiberErrorsText(r.p) : "-";
    const parts = [sw !== "0" && sw !== "-" ? `Switch: ${sw}` : "", L && L.rxErrors ? `Device: ${L.rxErrors} receive` : ""].filter(Boolean);
    return parts.join("; ") || "None";
  };
  const roomOf = (r) => {
    const v = r && swBy[r.sw] && swBy[r.sw].owner[r.n];
    return v && v.room ? Number(v.room) : null;
  };
  const tested = new Date(fiberLive.checked_at * 1000).toLocaleString();
  const row = (r, L) => {
    const st = L ? (L.r || L) : r, o = r ? r.p.optic : null;
    const room = L ? L.room : roomOf(r);
    return {
      level: st.level, sw: r ? r.sw : "", n: r ? r.n : 0, iface: L ? L.iface : "",
      // Worst first; fiber before copper/DAC; then by room, switch, port.
      order: [FIBER_ORDER[st.level], r && !r.optical ? 1 : 0, room ?? 999, r ? r.sw : "", r ? r.n : 0, L ? L.iface : ""],
      cells: [
        FIBER_RESULT[st.level], room != null ? `OR ${room}` : "", L ? L.name : (r.far.vlanOnly ? "" : r.far.name), L ? L.iface : "",
        r ? r.sw : "", r ? r.n : "", r ? (r.p.link ? `Up ${speedText(r.p.speed)}`.trim() : "Down") : "",
        num(reading(r, "rx_power")), num(L && L.devRx), num(L && L.lossToSw), num(L && L.lossToDev),
        num(reading(r, "tx_power")), num(L && L.devTx), num(r && r.rxDelta, 1), num(L && L.rxDelta, 1),
        errorsText(r, L), st.issues.map(shortIssue).join("; "), st.notes.join("; "),
        o ? (r.optical ? "Fiber" : "Copper/DAC") : "", o ? o.media || "" : "", o ? o.wavelength || "" : "",
        o ? o.vendor || "" : "", o ? o.part || "" : "", o ? o.serial || "" : "",
        num(reading(r, "temp"), 1), num(reading(r, "tx_bias"), 1), num(reading(r, "voltage")),
        L ? L.vendor : "", num(L && L.temp, 0), L ? L.devId : "", lsSiteName(), tested,
      ],
    };
  };
  const rows = [];
  for (const r of fiberRows) for (const L of r.links && r.links.length ? r.links : [null]) rows.push(row(r, L));
  for (const L of fiberLinks.filter((x) => !x.r)) rows.push(row(null, L));  // device ports no switch port has learned
  const cmp = (a, b) => (typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b)));
  rows.sort((a, b) => a.order.reduce((acc, x, i) => acc || cmp(x, b.order[i]), 0));
  return rows;
}

/* Minimal .xlsx writer (no library): Office Open XML parts with inline
   strings, stored in an uncompressed zip. Text is always written as text, so
   a device name can never become a formula. */
const XLSX_STYLE = { normal: 0, header: 1, problem: 2, warning: 3, title: 4, label: 5 };
const XLSX_STYLES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="14"/><name val="Calibri"/></font></fonts>
<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFDE2E2"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left/><right/><top/><bottom style="thin"><color rgb="FF94A3B8"/></bottom><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>`;
const xmlText = (s) => String(s).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const xlsxCol = (i) => { let s = ""; for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s; return s; };
// One sheet: rows = [{ cells: [...], style }], plus optional frozen header row + filter.
function xlsxSheet({ rows, table = false, widths = null }) {
  const ncols = Math.max(1, ...rows.map((r) => r.cells.length));
  const w = widths || Array.from({ length: ncols }, (_, c) =>
    Math.min(60, Math.max(8, ...rows.map((r) => String(r.cells[c] ?? "").length + 2))));
  const data = rows.map((r, ri) => `<row r="${ri + 1}">${r.cells.map((v, ci) => {
    if (v === "" || v == null) return "";
    const ref = `${xlsxCol(ci)}${ri + 1}`, s = r.style ? ` s="${r.style}"` : "";
    return typeof v === "number" && Number.isFinite(v)
      ? `<c r="${ref}"${s}><v>${v}</v></c>`
      : `<c r="${ref}"${s} t="inlineStr"><is><t xml:space="preserve">${xmlText(v)}</t></is></c>`;
  }).join("")}</row>`).join("");
  const view = table
    ? '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
    : '<sheetViews><sheetView workbookViewId="0"/></sheetViews>';
  const filter = table && rows.length > 1 ? `<autoFilter ref="A1:${xlsxCol(ncols - 1)}${rows.length}"/>` : "";
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">${view}` +
    `<cols>${w.map((x, i) => `<col min="${i + 1}" max="${i + 1}" width="${x}" customWidth="1"/>`).join("")}</cols>` +
    `<sheetData>${data}</sheetData>${filter}</worksheet>`;
}
// Sheet names: max 31 chars, no []:*?/\, unique.
function xlsxSheetNames(names) {
  const used = new Set();
  return names.map((n) => {
    let base = String(n).replace(/[[\]:*?/\\]/g, " ").trim().slice(0, 31) || "Sheet", name = base;
    for (let i = 2; used.has(name.toLowerCase()); i++) name = `${base.slice(0, 28)} ${i}`;
    used.add(name.toLowerCase());
    return name;
  });
}
const CRC_TABLE = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
function crc32(bytes) { let c = 0xffffffff; for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
// Uncompressed ("stored") zip: what an .xlsx is.
function zipStore(files) {
  const enc = new TextEncoder(), now = new Date(), parts = [], central = [];
  const time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
  const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  let offset = 0;
  for (const f of files) {
    const name = enc.encode(f.name), data = enc.encode(f.data), crc = crc32(data);
    const h = new DataView(new ArrayBuffer(30));
    [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [8, 0, 2], [10, time, 2], [12, date, 2], [14, crc, 4],
      [18, data.length, 4], [22, data.length, 4], [26, name.length, 2], [28, 0, 2]]
      .forEach(([o, v, n]) => (n === 4 ? h.setUint32(o, v, true) : h.setUint16(o, v, true)));
    const c = new DataView(new ArrayBuffer(46));
    [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [10, 0, 2], [12, time, 2], [14, date, 2], [16, crc, 4],
      [20, data.length, 4], [24, data.length, 4], [28, name.length, 2], [30, 0, 2], [32, 0, 2], [34, 0, 2], [36, 0, 2],
      [38, 0, 4], [42, offset, 4]]
      .forEach(([o, v, n]) => (n === 4 ? c.setUint32(o, v, true) : c.setUint16(o, v, true)));
    parts.push(h, name, data);
    central.push(c, name);
    offset += 30 + name.length + data.length;
  }
  const cdSize = central.reduce((a, x) => a + x.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  [[0, 0x06054b50, 4], [4, 0, 2], [6, 0, 2], [8, files.length, 2], [10, files.length, 2], [12, cdSize, 4], [16, offset, 4], [20, 0, 2]]
    .forEach(([o, v, n]) => (n === 4 ? end.setUint32(o, v, true) : end.setUint16(o, v, true)));
  return new Blob([...parts, ...central, end], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
}
function xlsxWorkbook(sheets) {
  const names = xlsxSheetNames(sheets.map((s) => s.name));
  const ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';
  const filters = sheets.map((s, i) => (s.table && s.rows.length > 1
    ? `<definedName name="_xlnm._FilterDatabase" localSheetId="${i}" hidden="1">'${names[i].replace(/'/g, "''")}'!$A$1:$${xlsxCol(Math.max(...s.rows.map((r) => r.cells.length)) - 1)}$${s.rows.length}</definedName>` : "")).join("");
  return zipStore([
    { name: "[Content_Types].xml", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>${sheets.map((_, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join("")}</Types>` },
    { name: "_rels/.rels", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>` },
    { name: "xl/workbook.xml", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook ${ns}><sheets>${names.map((n, i) => `<sheet name="${xmlText(n)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")}</sheets>${filters ? `<definedNames>${filters}</definedNames>` : ""}</workbook>` },
    { name: "xl/_rels/workbook.xml.rels", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${sheets.map((_, i) => `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join("")}<Relationship Id="rId${sheets.length + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>` },
    { name: "xl/styles.xml", data: XLSX_STYLES },
    ...sheets.map((s, i) => ({ name: `xl/worksheets/sheet${i + 1}.xml`, data: xlsxSheet(s) })),
  ]);
}

// The fiber test as a workbook: Summary, one sheet per switch, All links.
function fiberWorkbook() {
  const rows = fiberExportRows();
  const rowStyle = (level) => (level === "bad" ? XLSX_STYLE.problem : level === "warn" ? XLSX_STYLE.warning : 0);
  const head = (cells) => ({ cells, style: XLSX_STYLE.header });
  const tested = new Date(fiberLive.checked_at * 1000).toLocaleString();
  // Per-switch sheets leave out what's the same on every row (switch, site, test time).
  const drop = new Set(["Switch", "Site", "Tested"].map((h) => FIBER_EXPORT_HEAD.indexOf(h)));
  const keep = (cells) => cells.filter((_, i) => !drop.has(i));
  const count = (list, f) => list.filter(f).length;
  const roomsAll = Object.keys(fiberNms).length, roomsRead = count(Object.values(fiberNms), (x) => !x.error);

  const summary = [
    { cells: [`Fiber test - ${lsSiteName()}`], style: XLSX_STYLE.title },
    { cells: ["Tested", tested] },
    { cells: ["Lab limits", `Rx ${rangeText(FIBER_RANGE.rx)}, Tx ${rangeText(FIBER_RANGE.tx)}, loss up to ${FIBER_MAX_LOSS} dB per direction`] },
    { cells: ["Device end", `Read from ${roomsRead} of ${roomsAll} rooms' NMS`] },
    { cells: [] },
    head(["Switch", "Fiber ports", "Linked", "Problems", "Warnings", "Unused SFPs", "Copper/DAC", "Status"]),
  ];
  for (const s of fiberLive.switches) {
    const mine = fiberRows.filter((r) => r.sw === s.name), fiber = mine.filter((r) => r.optical);
    const bad = count(mine, (r) => r.level === "bad"), warn = count(mine, (r) => r.level === "warn");
    summary.push({
      cells: [s.name, fiber.length, count(fiber, (r) => r.p.link), bad, warn, count(mine, (r) => r.level === "idle"),
        mine.length - fiber.length, s.error ? `Not tested: ${s.error}` : bad ? "Problems" : warn ? "Warnings" : "OK"],
      style: s.error || bad ? XLSX_STYLE.problem : warn ? XLSX_STYLE.warning : 0,
    });
  }
  const issues = rows.filter((x) => x.level === "bad" || x.level === "warn");
  const col = (name) => FIBER_EXPORT_HEAD.indexOf(name);
  summary.push({ cells: [] }, { cells: [`Problems and warnings (${issues.length})`], style: XLSX_STYLE.label },
    head(["Result", "Room", "Device", "Device port", "Switch", "Switch port", "Issues"]));
  if (!issues.length) summary.push({ cells: ["None"] });
  for (const x of issues) {
    summary.push({ cells: ["Result", "Room", "Device", "Device port", "Switch", "Switch port", "Issues"].map((h) => x.cells[col(h)]), style: rowStyle(x.level) });
  }
  const failed = Object.values(fiberNms).filter((x) => x.error).sort((a, b) => a.number - b.number);
  if (failed.length) {
    summary.push({ cells: [] }, { cells: [`Rooms whose device end wasn't tested (${failed.length})`], style: XLSX_STYLE.label }, head(["Room", "Reason"]));
    for (const f of failed) summary.push({ cells: [`OR ${f.number}`, f.error], style: XLSX_STYLE.warning });
  }

  const sheets = [{ name: "Summary", rows: summary, widths: [16, 14, 34, 14, 16, 12, 90, 30] }];
  for (const s of fiberLive.switches) {
    const mine = rows.filter((x) => x.sw === s.name)
      .sort((a, b) => a.n - b.n || String(a.iface).localeCompare(String(b.iface)));
    sheets.push({
      name: s.name, table: true,
      rows: [head(keep(FIBER_EXPORT_HEAD)), ...(s.error ? [{ cells: [`Not tested: ${s.error}`], style: XLSX_STYLE.problem }]
        : mine.map((x) => ({ cells: keep(x.cells), style: rowStyle(x.level) })))],
    });
  }
  sheets.push({ name: "All links", table: true, rows: [head(FIBER_EXPORT_HEAD), ...rows.map((x) => ({ cells: x.cells, style: rowStyle(x.level) }))] });
  return xlsxWorkbook(sheets);
}
el("fiber-export").addEventListener("click", () => {
  const d = new Date(), p = (x) => String(x).padStart(2, "0");
  const ts = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  lsDownload(`fiber-test-${lsSiteName().replace(/[^A-Za-z0-9]+/g, "-").toLowerCase()}-${ts}.xlsx`, fiberWorkbook(),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
});
el("router-ports-copy").addEventListener("click", async () => {
  const ok = await copyToClipboard(el("router-ports-script").value);
  el("router-ports-status").textContent = ok ? "Copied to clipboard." : "Copy failed - select the text manually.";
});

/* ---------- Facility config import (Settings) ---------- */
let facilityUpload = null;
el("facility-choose").addEventListener("click", () => el("facility-file").click());
el("facility-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  el("facility-status").textContent = `Reading ${file.name}\u2026`;
  el("facility-preview").hidden = true;
  try {
    const res = await fetch("/api/facility/preview", {
      method: "POST",
      headers: { "Content-Type": "application/zip", "X-Filename": file.name },
      body: file,
    });
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(d.detail || res.status);
    facilityUpload = d;
    renderFacilityPreview(d);
  } catch (err) {
    toast(`Can't import ${file.name}: ${err.message || err}`, "error");
  } finally {
    el("facility-status").textContent = "";
  }
});
const FACILITY_CRED_NAMES = { windows_password: "Windows", nms_password: "NMS", extron_password: "Extron", site_api_key: "Site API key", ic_key: "IC key" };
function renderFacilityPreview(d) {
  const targets = d.update
    ? [[d.update.path, `Update <b>${escapeHtml(d.update.name)}</b> (version ${escapeHtml(d.update.version || "?")} &rarr; ${escapeHtml(d.version)})`]]
    : [["new", `Add as a new lab: <b>${escapeHtml(d.facility_name)} (${escapeHtml(d.site_code)})</b> &rarr; <code>config\\${escapeHtml(d.new_lab_file)}</code>`]];
  for (const c of d.same_router) targets.push([c.path, `Update <b>${escapeHtml(c.name)}</b> instead (it uses the same router, ${escapeHtml(d.router.wan_ip)})`]);
  const changes = d.update ? (d.update.changes.length ? d.update.changes : ["No changes to rooms, switches or router since the last import."]) : [];
  el("facility-preview").innerHTML = `
    <div class="settings-grid">
      ${settingCell("Site", `${d.facility_name} (${d.site_code})`)}
      ${settingCell("Version", d.version)}
      ${settingCell("Router", `${d.router.hostname || ""} \u00b7 ${d.router.wan_ip}`)}
      ${settingCell("Rooms", String(d.rooms.length))}
      ${settingCell("Switches", String(d.switches.length))}
      ${settingCell("Passwords", d.credentials.map((c) => FACILITY_CRED_NAMES[c] || c).join(", ") || "none")}
    </div>
    ${d.warnings.map((w) => `<p class="hint warn-text">${escapeHtml(w)}</p>`).join("")}
    ${changes.length ? `<ul class="facility-changes">${changes.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>` : ""}
    <div class="facility-targets">${targets.map(([v, label], i) =>
      `<label class="checkline"><input type="radio" name="facility-target" value="${escapeHtml(v)}" ${i ? "" : "checked"} /> <span>${label}</span></label>`).join("")}</div>
    <label class="checkline"><input type="checkbox" id="facility-qa" ${!d.update || d.update.qa_port_forwards ? "checked" : ""} /> <span>This lab's router has the QA test forwards (SSH 200+n, SWU 300+n, NMS 11000+n)</span></label>
    <p class="hint">Passwords are saved to this lab's <code>.env</code> on this computer only and never ship in the app zip. The uploaded zip itself isn't kept.</p>
    <div class="action-grid">
      <button class="primary" id="facility-import">Import</button>
      <button class="ghost sm" id="facility-cancel">Cancel</button>
    </div>`;
  el("facility-preview").hidden = false;
  el("facility-cancel").addEventListener("click", () => { el("facility-preview").hidden = true; facilityUpload = null; });
  el("facility-import").addEventListener("click", importFacility);
}
async function importFacility() {
  if (!facilityUpload) return;
  const target = document.querySelector('input[name="facility-target"]:checked').value;
  const done = setBusy(el("facility-import"), "Importing\u2026");
  try {
    const res = await fetch("/api/facility/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: facilityUpload.upload_id, target, qa_port_forwards: el("facility-qa").checked }),
    });
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(d.detail || res.status);
    el("facility-preview").hidden = true;
    facilityUpload = null;
    toast(`Imported ${d.active_name}. It's now the active site.`, "success");
    logTo(GENERAL_TAB, `Facility config imported: ${d.active_name} (${d.active}).`, "success");
    await loadProfiles();
    await Promise.all([loadRooms(), loadConnectionInfo(), loadRouterPorts(), loadSetup(true), loadPreflight()]);
    autoCheckLabStatus();
  } catch (e) {
    done();
    toast(`Import failed: ${e.message || e}`, "error");
  }
}
el("router-apply-confirm").addEventListener("change", (e) => {
  el("router-apply-nat").disabled = !e.target.checked;
});
el("router-apply-nat").addEventListener("click", () => {
  const router_admin_username = el("router-admin-username").value.trim();
  const router_admin_password = el("router-admin-password").value || null;  // blank = the saved one (server side)
  const router_enable_password = el("router-enable-password").value || null;
  if (!router_admin_username || !hasCred("router-admin-password", "router_admin_password")) {
    logLine("Enter the router SSH username and password first.", "warning");
    return;
  }
  // Exactly what's in the box (picked rules and any hand edits); the server
  // drops the configure/end/write wrappers and refuses anything but NAT rules.
  const commands = el("router-ports-script").value.split("\n");
  if (!commands.some((l) => /^\s*(no\s+)?ip\s+nat\s/.test(l))) {
    toast("No NAT rules to apply. Pick at least one rule above.", "warning");
    return;
  }
  el("router-apply-confirm").checked = false;
  el("router-apply-nat").disabled = true;
  startJob(
    "/api/router/apply-nat",
    { router_admin_username, router_admin_password, router_enable_password, commands },
    { needRooms: false, title: "Apply Router NAT" }
  );
});

/* ---------- Facility status (Facility tab) ---------- */
// One read-only snapshot per room (/api/lab-status/room), fetched in parallel
// so cards fill in as each room answers.
const labStatus = { results: {}, changes: {}, since: {}, checking: new Set(), run: 0, busy: false, abort: null };
const LS_CONCURRENCY = 6;
const LS_CHECK_ALL_HTML = el("ls-check-all").innerHTML;
// Grey out both buttons while a check runs; "Check All" shows live progress.
function setLabStatusButtons(running, done = 0, total = 0) {
  const all = el("ls-check-all");
  all.disabled = el("ls-check-selected").disabled = running;
  all.innerHTML = running ? `<span class="btn-spin"></span> Checking ${done}/${total}\u2026` : LS_CHECK_ALL_HTML;
}
// Stop the current check (site switch): abort its requests so a new check
// can start right away instead of waiting for the old site's rooms.
function cancelLabStatus() {
  labStatus.run++;
  if (labStatus.abort) labStatus.abort.abort();
  labStatus.abort = null;
  labStatus.busy = false;
  labStatus.checking.clear();
  setLabStatusButtons(false);
}
const LS_STATE = {
  healthy: ["good", "Healthy"], warning: ["warn", "Needs attention"],
  error: ["bad", "Error"], unreachable: ["bad", "Unreachable"],
};
const LS_LICENSE = {
  active: ["good", "License active"], grace: ["warn", "License in grace period"],
  expired: ["bad", "License expired"], none: ["bad", "No license"],
};
const lsPill = (cls, text, title = "") =>
  `<span class="ls-pill ${cls}"${title ? ` title="${escapeHtml(title)}"` : ""}>${escapeHtml(String(text))}</span>`;
const lsDevices = (r, kind) => (r.devices || []).filter((d) => d.kind === kind);
const lsDisplays = (r) => lsDevices(r, "DECODER").flatMap((d) => d.outputs.filter((o) => o.connected));
const lsOnline = (devices) => devices.filter((d) => d.state === "ONLINE").length;

// Installed software: build "860-0108-07-0_2.0.0_MatrixG2-2.0-15" -> "2.0.0 · MatrixG2-2.0-15".
const lsBuild = (r) => (r && r.software && (r.software.build || r.software.version)) || "";
function lsSoftwareText(sw) {
  if (!sw || !(sw.version || sw.build)) return "";
  const parts = (sw.build || "").split("_");
  const branch = parts.length >= 3 ? parts.slice(2).join("_") : sw.build;
  return [sw.version, branch && branch !== sw.version ? branch : ""].filter(Boolean).join(" \u00b7 ");
}
// Builds across the checked rooms, most common first: [[build, text, [room numbers]], ...].
function lsBuildGroups() {
  const by = {};
  for (const rm of state.rooms) {
    const r = labStatus.results[rm.number], b = lsBuild(r);
    if (b) (by[b] = by[b] || [lsSoftwareText(r.software), []])[1].push(rm.number);
  }
  return Object.entries(by).map(([b, [text, rooms]]) => [b, text, rooms]).sort((a, b) => b[2].length - a[2].length);
}
function lsSoftwarePill(r) {
  const b = lsBuild(r);
  if (!b) return "";
  const groups = lsBuildGroups();
  const odd = groups.length > 1 && groups[0][0] !== b;
  const title = `Build ${b}` + (odd ? `\nMost rooms run ${groups[0][0]}` : "");
  return lsPill(odd ? "warn" : "", `SW ${lsSoftwareText(r.software)}`, title);
}

/* "What changed since the last check": the last result per room is kept in
   this browser (per site, no secrets) and compared with each new result. */
const lsHistoryKey = () => `md-facility-last:${el("site-select").value || "default"}`;
function lsLoadHistory() {
  try { return JSON.parse(localStorage.getItem(lsHistoryKey())) || {}; } catch { return {}; }
}
const lsDeviceName = (d) => `${d.kind.charAt(0) + d.kind.slice(1).toLowerCase()} ${d.model || ""} (${d.id})`.replace("  ", " ");
function lsSnapshot(r) {
  return {
    at: Date.now(), state: r.state, booted_at: (r.ssh || {}).booted_at || null, build: lsBuild(r) || null,
    services: r.services || {}, license: r.license ? r.license.state : null, nms_ok: !!(r.nms && r.nms.ok),
    devices: Object.fromEntries((r.devices || []).map((d) => [d.id, { name: lsDeviceName(d), state: d.state, fw: d.firmware || null }])),
    issues: r.issues || [],
  };
}
function lsChanges(prev, cur) {
  if (!prev) return [];
  const out = [], lower = (s) => String(s).toLowerCase();
  const stateText = (s) => (LS_STATE[s] || ["", s])[1];
  if (prev.state !== cur.state) out.push(`${stateText(prev.state)} \u2192 ${stateText(cur.state)}`);
  if (prev.booted_at && cur.booted_at && Math.abs(cur.booted_at - prev.booted_at) > 300) {
    out.push(`Rebooted (${new Date(cur.booted_at * 1000).toLocaleString()})`);
  }
  const short = (b) => { const p = b.split("_"); return p.length >= 3 ? p.slice(2).join("_") : b; };
  if (prev.build && cur.build && prev.build !== cur.build) out.push(`Software ${short(prev.build)} \u2192 ${short(cur.build)}`);
  for (const [k, label] of [["matrix_api", "Matrix API"], ["nms", "Barco NMS"]]) {
    const a = prev.services[k], b = cur.services[k];
    if (a && b && a !== b) out.push(`${label} service ${a} \u2192 ${b}`);
  }
  if (prev.license && cur.license && prev.license !== cur.license) out.push(`License ${prev.license} \u2192 ${cur.license}`);
  const changedIds = new Set();
  if (prev.nms_ok && cur.nms_ok) {
    for (const [id, d] of Object.entries(cur.devices)) {
      const p = prev.devices[id];
      if (!p) { out.push(`New device: ${d.name}`); changedIds.add(id); continue; }
      if (p.state !== d.state) { out.push(`${d.name}: ${lower(p.state)} \u2192 ${lower(d.state)}`); changedIds.add(id); }
      if (p.fw && d.fw && p.fw !== d.fw) { out.push(`${d.name}: firmware ${p.fw} \u2192 ${d.fw}`); changedIds.add(id); }
    }
    for (const [id, p] of Object.entries(prev.devices)) {
      if (!cur.devices[id]) { out.push(`Device gone: ${p.name}`); changedIds.add(id); }
    }
  }
  // Issue changes not already explained by a device change above.
  const aboutChanged = (i) => [...changedIds].some((id) => i.includes(`(${id})`));
  for (const i of cur.issues) if (!prev.issues.includes(i) && !aboutChanged(i)) out.push(`New issue: ${i}`);
  for (const i of prev.issues) if (!cur.issues.includes(i) && !aboutChanged(i)) out.push(`Resolved: ${i}`);
  return out;
}

function lsServicePill(label, state) {
  if (!state) return lsPill("", `${label}: ?`);
  return state === "active" ? lsPill("good", label) : lsPill("bad", `${label}: ${state}`);
}
// Hottest device reading in the room; red if NMS raised any hardware alert.
function lsHottestPill(r) {
  const withHealth = (r.devices || []).filter((d) => d.health);
  if (!withHealth.length) return "";
  let hottest = null, temp = -Infinity;
  for (const d of withHealth) {
    const t = Math.max(d.health.cpu_temp ?? -Infinity, d.health.fpga_temp ?? -Infinity);
    if (t > temp) { temp = t; hottest = d; }
  }
  const alerts = withHealth.flatMap((d) => d.health.alerts.map((a) => `${d.model} ${d.id}: ${a}`));
  if (!Number.isFinite(temp)) return alerts.length ? lsPill("bad", `${alerts.length} hardware alert${alerts.length > 1 ? "s" : ""}`, alerts.join("\n")) : "";
  const title = `Hottest: ${hottest.model} ${hottest.id}` + (alerts.length ? `\n\n${alerts.join("\n")}` : "");
  return lsPill(alerts.length ? "bad" : "", `Hottest ${temp}\u00b0C`, title);
}
function lsCount(label, devices) {
  const online = lsOnline(devices);
  return `<div class="ls-count${online < devices.length ? " bad" : ""}"><span class="k">${label}</span>` +
    `<span class="v">${online}<small> / ${devices.length} online</small></span></div>`;
}
function lsDeviceRows(r) {
  const order = { ENCODER: 0, DECODER: 1 };
  return [...(r.devices || [])].sort((a, b) => (order[a.kind] ?? 2) - (order[b.kind] ?? 2)).map((d) => {
    const ports = d.kind === "ENCODER" ? d.inputs : d.outputs;
    const used = ports.filter((p) => p.connected)
      .map((p) => escapeHtml(p.name) + (p.power && p.power !== "READY" ? ` <span class="mono">(${escapeHtml(p.power.toLowerCase())})</span>` : ""));
    const h = d.health;
    // One "needs attention" badge per device; the reasons are in its tooltip.
    const reasons = [
      d.licensed === false ? "Not licensed" : "",
      d.upgrade_required ? "Needs a firmware upgrade" : "",
      ...(h ? h.alerts : []),
    ].filter(Boolean);
    // Red only for the reading NMS actually flagged.
    const flagged = (prefix) => h && h.alerts.some((a) => a.startsWith(prefix));
    const reading = (text, bad) => `<span${bad ? ' class="ls-bad"' : ""}>${escapeHtml(text)}</span>`;
    const temps = h ? [["CPU", h.cpu_temp], ["FPGA", h.fpga_temp]].filter(([, t]) => t != null)
      .map(([k, t]) => reading(`${k} ${t}\u00b0C`, flagged(`${k} temp`))).join(" \u00b7 ") : "";
    const fans = h && h.fans.length
      ? `<div class="mono">Fan${h.fans.length > 1 ? "s" : ""} ${h.fans.map((f) => reading(`${f.rpm ?? "?"} rpm`, f.alerts.length)).join(" / ")}</div>`
      : "";
    const healthCell = h ? `${temps || "-"}${fans}` : '<span class="mono">n/a</span>';
    return `<tr><td>${escapeHtml(d.kind.charAt(0) + d.kind.slice(1).toLowerCase())}</td>` +
      `<td>${escapeHtml(d.model || "")}<div class="mono">${escapeHtml(d.id || "")}${d.firmware ? ` &middot; fw ${escapeHtml(d.firmware)}` : ""}</div></td>` +
      `<td>${d.state === "ONLINE" ? lsPill("good", "online") : lsPill("bad", d.state.toLowerCase())}` +
      `${reasons.length ? " " + lsPill("warn", "needs attention", reasons.join("\n")) : ""}</td>` +
      `<td>${healthCell}</td>` +
      `<td>${used.length ? used.join("<br>") : '<span class="mono">nothing connected</span>'}</td></tr>`;
  }).join("");
}
function lsCard(room) {
  const r = labStatus.results[room.number];
  const busy = labStatus.checking.has(room.number);
  const [cls, label] = busy ? ["busy", "Checking\u2026"] : r ? LS_STATE[r.state] || ["", r.state] : ["", "Not checked"];
  let body = `<div class="ls-sub">${escapeHtml(room.room_id)}</div>`;
  const changes = !busy && labStatus.changes[room.number];
  if (r) {
    const ssh = r.ssh || {}, svc = r.services || {}, nms = r.nms || {};
    const since = labStatus.since[room.number];
    const sub = [room.room_id, ssh.uptime, r.checked_at && `checked ${new Date(r.checked_at).toLocaleTimeString()}`,
      changes && !changes.length && since && `no changes since ${new Date(since).toLocaleString()}`].filter(Boolean);
    body = `<div class="ls-sub">${escapeHtml(sub.join(" \u00b7 "))}</div>`;
    if (ssh.ok) {
      const lic = r.license ? LS_LICENSE[r.license.state] || ["", r.license.state] : ["", r.secondary ? "No local NMS" : "License unknown"];
      const licTitle = r.license ? r.license.features.map((f) => `${f.name}: ${f.expired ? "expired" : f.grace ? "grace period" : f.permanent ? "permanent" : "active"}`).join("\n") : "";
      body += `<div class="ls-row">${lsServicePill("Matrix API", svc.matrix_api)}${lsServicePill("Barco NMS", svc.nms)}` +
        `${lsPill(lic[0], lic[1] + (r.license && r.license.state === "active" && r.license.permanent ? " (permanent)" : ""), licTitle)}` +
        `${lsSoftwarePill(r)}${lsHottestPill(r)}</div>`;
    }
    if (changes && changes.length) {
      body += `<div class="ls-changes"><span class="k">Changed since ${escapeHtml(new Date(since).toLocaleString())}</span>` +
        `<ul>${changes.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul></div>`;
    }
    if (nms.ok) {
      const displays = lsDisplays(r);
      body += `<div class="ls-counts">${lsCount("Encoders", lsDevices(r, "ENCODER"))}${lsCount("Decoders", lsDevices(r, "DECODER"))}` +
        `<div class="ls-count"><span class="k">Displays</span><span class="v">${displays.length}<small> connected</small></span></div></div>`;
      if (r.devices.length) {
        body += `<details><summary>Devices (${r.devices.length})</summary><div class="table-wrap"><table class="ls-devices">` +
          `<thead><tr><th>Type</th><th>Model / ID</th><th>State</th><th>Temp / Fans</th><th>Sources / Displays</th></tr></thead>` +
          `<tbody>${lsDeviceRows(r)}</tbody></table></div></details>`;
      }
    }
    if (r.issues && r.issues.length) body += `<ul class="ls-issues">${r.issues.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`;
  }
  const changed = changes && changes.length ? lsPill("info", `${changes.length} change${changes.length === 1 ? "" : "s"}`, changes.join("\n")) : "";
  return `<div class="card ls-card ${busy ? "" : (r && r.state) || ""}">` +
    `<div class="card-head"><h2>${escapeHtml(roomName(room))}</h2>${changed}${lsPill(cls, label)}</div>${body}</div>`;
}
function renderLabStatus() {
  el("ls-grid").innerHTML = state.rooms.map(lsCard).join("");
  const done = state.rooms.map((rm) => labStatus.results[rm.number]).filter(Boolean);
  el("ls-export-md").disabled = el("ls-export-csv").disabled = !done.length || labStatus.busy;
  if (!done.length) { el("ls-summary").innerHTML = ""; return; }
  const healthy = done.filter((r) => r.state === "healthy").length;
  const all = (kind) => done.flatMap((r) => lsDevices(r, kind));
  const enc = all("ENCODER"), dec = all("DECODER");
  const withNms = done.filter((r) => r.license);
  const licensed = withNms.filter((r) => r.license.state === "active").length;
  const metric = (label, value, ok, title = "") =>
    `<div class="ls-metric ${ok ? "good" : "warn"}"${title ? ` title="${escapeHtml(title)}"` : ""}><span class="k">${label}</span><span class="v">${escapeHtml(value)}</span></div>`;
  const builds = lsBuildGroups();
  const buildTitle = builds.map(([b, , rooms]) => `${b}: OR ${rooms.join(", ")}`).join("\n");
  el("ls-summary").innerHTML = [
    metric("Rooms healthy", `${healthy} / ${done.length}`, healthy === done.length),
    metric("Encoders online", `${lsOnline(enc)} / ${enc.length}`, lsOnline(enc) === enc.length),
    metric("Decoders online", `${lsOnline(dec)} / ${dec.length}`, lsOnline(dec) === dec.length),
    metric("Displays connected", String(done.reduce((n, r) => n + lsDisplays(r).length, 0)), true),
    metric("Licenses active", `${licensed} / ${withNms.length}`, licensed === withNms.length),
    builds.length ? metric("Software", builds.length === 1 ? (builds[0][1].split(" \u00b7 ")[0] || builds[0][0]) : `${builds.length} builds`,
      builds.length === 1, buildTitle) : "",
  ].join("");
}
async function runLabStatus(numbers) {
  if (labStatus.busy) return;
  if (!numbers.length) { toast("Select at least one room in the sidebar first.", "warning"); return; }
  const creds = credentials();
  if (!hasCred("ssh-password", "ssh_password")) { toast("Enter the SSH password in the sidebar first.", "warning"); return; }
  if (!hasCred("sudo-password", "sudo_password")) toast("No sudo password: only services are checked, not devices or license.", "warning");
  const run = ++labStatus.run;
  const abort = new AbortController();
  labStatus.abort = abort;
  labStatus.busy = true;
  setLabStatusButtons(true, 0, numbers.length);
  numbers.forEach((n) => labStatus.checking.add(n));
  renderLabStatus();
  const history = lsLoadHistory();
  const queue = [...numbers];
  let done = 0;
  el("ls-progress").textContent = "";
  const worker = async () => {
    while (queue.length) {
      const n = queue.shift();
      let result;
      try {
        const res = await fetch("/api/lab-status/room", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ room_number: n, ...creds }),
          signal: abort.signal,
        });
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        result = res.ok ? d : { state: "error", issues: [`Check failed: ${d.detail || res.status}`] };
      } catch (e) {
        if (abort.signal.aborted) return;
        result = { state: "error", issues: [serverDown ? "Matrix Facility Manager isn't running." : `Check failed: ${e}`] };
      }
      if (run !== labStatus.run) return;  // site switched mid-check
      labStatus.results[n] = result;
      // No "ssh" key: the request itself failed, so there's nothing to compare.
      labStatus.changes[n] = result.ssh ? lsChanges(history[n], lsSnapshot(result)) : [];
      labStatus.since[n] = history[n] ? history[n].at : null;
      labStatus.checking.delete(n);
      setLabStatusButtons(true, ++done, numbers.length);
      renderLabStatus();
    }
  };
  await Promise.all(Array.from({ length: Math.min(LS_CONCURRENCY, numbers.length) }, worker));
  if (run !== labStatus.run) return;  // cancelled; the newer check owns the buttons
  labStatus.busy = false;
  labStatus.abort = null;
  setLabStatusButtons(false);
  const results = numbers.map((n) => [n, labStatus.results[n]]);
  const bad = results.filter(([, r]) => r.state !== "healthy");
  // Remember this check as the baseline for the next one. A failed request
  // (the app itself, not the room) isn't a real snapshot, so keep the old one.
  for (const [n, r] of results) if (r.ssh) history[n] = lsSnapshot(r);
  try { localStorage.setItem(lsHistoryKey(), JSON.stringify(history)); } catch { /* storage full/blocked: no history */ }
  renderLabStatus();
  el("ls-updated").textContent = `checked ${new Date().toLocaleTimeString()}`;
  logTo(GENERAL_TAB, `Facility status: ${results.length - bad.length}/${results.length} rooms healthy.`, bad.length ? "warning" : "success");
  for (const [n, r] of bad) logTo(GENERAL_TAB, `OR ${n}: ${(r.issues || []).join("; ") || r.state}`, "warning");
  for (const [n] of results) {
    const c = labStatus.changes[n];
    if (c && c.length) logTo(GENERAL_TAB, `OR ${n} changed since the last check: ${c.join("; ")}`, "info");
  }
  if (routerPorts) renderSwitches(routerPorts.facility);
  if (fiberLive) renderFiber();  // encoder/decoder names come from this check
}
/* Export the last check: Markdown (tickets / handoffs) or CSV (spreadsheets). */
function lsSiteName() {
  const opt = el("site-select").selectedOptions[0];
  return (opt && opt.textContent.trim()) || (routerPorts && routerPorts.site_name) || "lab";
}
function lsDownload(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  addDownload(name, url);  // also listed under Logs -> Downloads (this session)
}
function lsExportName(ext) {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const ts = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  return `facility-${lsSiteName().replace(/[^A-Za-z0-9]+/g, "-").toLowerCase()}-${ts}.${ext}`;
}
const lsChecked = () => state.rooms.map((rm) => [rm, labStatus.results[rm.number]]).filter(([, r]) => r);
const lsTemps = (d) => (d.health ? [d.health.cpu_temp, d.health.fpga_temp] : [null, null]);
function lsExportMarkdown() {
  const cell = (v) => String(v ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");
  const row = (cells) => `| ${cells.map(cell).join(" | ")} |`;
  const rows = lsChecked();
  const summary = [...el("ls-summary").querySelectorAll(".ls-metric")]
    .map((m) => `- **${m.querySelector(".k").textContent}:** ${m.querySelector(".v").textContent}`);
  const out = [`# Facility report: ${lsSiteName()}`, "", `Generated ${new Date().toLocaleString()} by Matrix Facility Manager.`, "", ...summary, "",
    "## Rooms", "", row(["Room", "State", "Software", "Uptime", "Matrix API", "Barco NMS", "License", "Encoders", "Decoders", "Displays"]),
    row(Array(10).fill("---"))];
  for (const [rm, r] of rows) {
    const enc = lsDevices(r, "ENCODER"), dec = lsDevices(r, "DECODER");
    out.push(row([roomName(rm), (LS_STATE[r.state] || ["", r.state])[1], lsSoftwareText(r.software), (r.ssh || {}).uptime,
      (r.services || {}).matrix_api, (r.services || {}).nms, r.license ? r.license.state : "",
      r.nms && r.nms.ok ? `${lsOnline(enc)}/${enc.length}` : "", r.nms && r.nms.ok ? `${lsOnline(dec)}/${dec.length}` : "",
      r.nms && r.nms.ok ? lsDisplays(r).length : ""]));
  }
  const noted = rows.filter(([rm, r]) => (r.issues || []).length || (labStatus.changes[rm.number] || []).length);
  out.push("", "## Issues and changes", "");
  if (!noted.length) out.push("No issues or changes.");
  for (const [rm, r] of noted) {
    out.push(`### ${roomName(rm)} (${(LS_STATE[r.state] || ["", r.state])[1]})`, "");
    for (const i of r.issues || []) out.push(`- ${i}`);
    const c = labStatus.changes[rm.number] || [];
    if (c.length) out.push(`- Changed since ${new Date(labStatus.since[rm.number]).toLocaleString()}: ${c.join("; ")}`);
    out.push("");
  }
  out.push("## Devices", "", row(["Room", "Type", "Model", "ID", "State", "Firmware", "CPU \u00b0C", "FPGA \u00b0C", "Needs attention"]),
    row(Array(9).fill("---")));
  for (const [rm, r] of rows) {
    for (const d of r.devices || []) {
      const reasons = [d.licensed === false ? "not licensed" : "", d.upgrade_required ? "needs firmware upgrade" : "", ...(d.health ? d.health.alerts : [])].filter(Boolean);
      out.push(row([`OR ${rm.number}`, d.kind, d.model, d.id, d.state, d.firmware, ...lsTemps(d), reasons.join("; ")]));
    }
  }
  lsDownload(lsExportName("md"), out.join("\n") + "\n", "text/markdown");
}
function lsExportCsv() {
  const q = csvCell;
  const head = ["room", "room_state", "software_version", "software_build", "uptime", "matrix_api", "barco_nms", "license",
    "device_type", "model", "device_id", "device_state", "firmware", "licensed", "cpu_temp_c", "fpga_temp_c", "device_alerts", "room_issues"];
  const lines = [head.join(",")];
  for (const [rm, r] of lsChecked()) {
    const sw = r.software || {}, svc = r.services || {};
    const base = [rm.number, r.state, sw.version, sw.build, (r.ssh || {}).uptime, svc.matrix_api, svc.nms, r.license ? r.license.state : ""];
    const issues = (r.issues || []).join("; ");
    const devs = r.devices || [];
    if (!devs.length) lines.push([...base, ...Array(9).fill(""), issues].map(q).join(","));
    for (const d of devs) {
      lines.push([...base, d.kind, d.model, d.id, d.state, d.firmware, d.licensed, ...lsTemps(d), (d.health ? d.health.alerts : []).join("; "), issues].map(q).join(","));
    }
  }
  lsDownload(lsExportName("csv"), lines.join("\r\n") + "\r\n", "text/csv");
}
el("ls-export-md").addEventListener("click", lsExportMarkdown);
el("ls-export-csv").addEventListener("click", lsExportCsv);
el("ls-check-all").addEventListener("click", () => runLabStatus(state.rooms.map((r) => r.number)));
el("ls-check-selected").addEventListener("click", () => runLabStatus(selectedRoomNumbers()));
// Facility is the first tab, so fill it in on open / site switch without a click.
function autoCheckLabStatus() {
  if (state.rooms.length && hasCred("ssh-password", "ssh_password")) runLabStatus(state.rooms.map((r) => r.number));
}

/* ---------- Site profiles ---------- */
async function loadProfiles() {
  const data = await (await fetch("/api/profiles")).json();
  const sel = el("site-select");
  sel.innerHTML = "";
  for (const p of data.profiles) {
    const opt = document.createElement("option");
    opt.value = p.path;
    opt.textContent = p.name;
    if (p.path === data.active) opt.selected = true;
    sel.appendChild(opt);
  }
}
el("site-select").addEventListener("change", async (e) => {
  const path = e.target.value;
  try {
    const res = await fetch("/api/profiles/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logLine(`Failed to switch site: ${err.detail}`, "error");
      return;
    }
    const d = await res.json();
    logLine(`Switched site to ${d.active_name}.`, "success");
    // Reload everything for the new site; force-refresh credential fields.
    await Promise.all([loadRooms(), loadConnectionInfo(), loadRouterPorts(), loadSetup(true), loadPreflight()]);
    autoCheckLabStatus();
  } catch (err) {
    logLine(`Site switch failed: ${err}`, "error");
  }
});

/* ---------- Setup / .env prefill ---------- */
function settingCell(k, v, cls = "") {
  return `<div class="setting"><span class="k">${escapeHtml(k)}</span><span class="v ${cls}">${escapeHtml(v)}</span></div>`;
}
function prefill(id, value, force = false) {
  if (!el(id)) return;
  if (value && (force || !el(id).value)) el(id).value = value;
}
// Saved passwords/tokens never reach the page: the server only says whether
// one is saved, and uses it when the field is left blank. The box shows a
// "saved" hint instead. On a site switch (clear), lab passwords typed for the
// previous lab are cleared so they aren't sent to the new one.
function markSaved(id, saved, clear = false) {
  const input = el(id);
  if (!input) return;
  if (input.dataset.ph === undefined) input.dataset.ph = input.placeholder || "";
  if (clear) input.value = "";
  input.placeholder = saved ? "Saved - leave blank to use it" : input.dataset.ph;
}
// A credential is available if it's typed in, or saved on this computer.
const hasCred = (id, key) => !!(el(id).value || (state.setup && state.setup.saved && state.setup.saved[key]));
async function loadSetup(force = false) {
  const s = await (await fetch("/api/setup")).json();

  // Connection card
  const c = s.connection;
  el("settings-connection").innerHTML = [
    settingCell("Router IP", c.router_ip),
    settingCell("SSH user", c.ssh_username),
    settingCell("SSH port base", String(c.ssh_port_base)),
    settingCell("Matrix service", c.service_name),
    settingCell("NMS service", c.nms_service_name),
    settingCell("SWU port", String(c.swu_service_port)),
    settingCell("Shared host", c.same_physical_host ? "yes" : "no"),
    settingCell("Rooms", String(c.room_count)),
  ].join("");

  // Env card
  el("env-path").textContent = s.env_path;
  el("env-badge").textContent = s.env_present ? "detected" : "not found";
  el("env-badge").className = "badge " + (s.env_present ? "subtle" : "subtle");
  const p = s.prefill, sec = s.saved;
  const yn = (val) => (val ? "set" : "not set");
  const cls = (val) => (val ? "on" : "off");
  el("settings-env").innerHTML = [
    settingCell("Artifactory email", p.artifactory_email || "not set", cls(p.artifactory_email)),
    settingCell("Jenkins username", p.jenkins_username || "not set", cls(p.jenkins_username)),
    settingCell("SSH password", yn(sec.ssh_password), cls(sec.ssh_password)),
    settingCell("Sudo password", yn(sec.sudo_password), cls(sec.sudo_password)),
    settingCell("Artifactory token", yn(sec.artifactory_token), cls(sec.artifactory_token)),
    settingCell("Jenkins token", yn(sec.jenkins_token), cls(sec.jenkins_token)),
  ].join("");

  // Prefill live fields. On initial load, only fill empty fields (don't
  // clobber user edits); on a site switch (force), overwrite with the new
  // site's credentials.
  prefill("swu-file", p.swu_file, force);
  prefill("artifactory-email", p.artifactory_email, force);
  prefill("jenkins-username", p.jenkins_username, force);
  prefill("webapp-backend-repo", p.backend_repo, force);
  prefill("webapp-web-repo", p.web_repo, force);
  prefill("webapp-local-dist", p.webapp_dist, force);
  prefill("webapp-local-web", p.webapp_web, force);
  fillFoldersCard(s);
  markSaved("ssh-password", sec.ssh_password, force);
  markSaved("sudo-password", sec.sudo_password, force);
  markSaved("artifactory-token", sec.artifactory_token);
  markSaved("jenkins-token", sec.jenkins_token);
  prefill("router-admin-username", p.router_admin_username, force);
  markSaved("router-admin-password", sec.router_admin_password, force);
  markSaved("router-enable-password", sec.router_enable_password, force);

  // SWU download folder: prefer the .env SWU_FILE dir if given, else the
  // server's default cache folder. Only fill when empty so edits stick.
  if (s.defaults && s.defaults.swu_download_dir) prefill("download-dir", s.defaults.swu_download_dir);

  state.setup = s;
  const fac = s.facility;
  el("facility-badge").textContent = fac ? `${fac.site_code} \u00b7 version ${fac.version}` : "not imported for this lab";
  applyTeam(p.team);
  // First run on this computer: ask for the team, then (per site) the lab credentials.
  if (!p.team) el("team-modal").hidden = false;
  else maybeAskCreds();
}
function maybeAskCreds() {
  const s = state.setup;
  if (s && !s.saved.ssh_password && !credsSkipped.has(s.lab_env_path)) openCredsModal();
}

/* ---------- Team (QA vs Integration) + QA lock ---------- */
// QA-only tabs show for the QA team, and only once the QA password was
// entered when a QA lock is set. The server refuses those features too
// (require_qa in server.py), so hiding the tabs isn't the only barrier.
const qaState = () => (state.setup && state.setup.qa) || { lock: false, unlocked: true };
function applyTeam(team) {
  const qa = team !== "integration" && qaState().unlocked;
  document.querySelectorAll('#tabs .tab[data-team="qa"]').forEach((t) => {
    t.hidden = !qa;
    if (!qa && t.classList.contains("active")) document.querySelector('#tabs .tab[data-tab="lab-status"]').click();
  });
  // QA-only cards inside shared tabs (Actions > NMS Configuration).
  document.querySelectorAll("[data-qa-only]").forEach((c) => (c.hidden = !qa));
  document.querySelectorAll("#team-selector .seg").forEach((b) => b.classList.toggle("active", b.dataset.team === (qa ? "qa" : team ? "integration" : "")));
  renderQaLock();
}
function renderQaLock() {
  const { lock, unlocked } = qaState();
  el("qa-lock-badge").textContent = !lock ? "not set" : unlocked ? "unlocked on this computer" : "locked";
  el("qa-lock-hint").innerHTML = !lock
    ? "No QA password yet, so anyone can choose QA. Set one to keep Tunnels, Config, Software Update and Web App for the QA team. It ships with the app build (only a hash, never the password)."
    : unlocked
      ? "QA features are unlocked here. You can change the shared QA password, lock this computer again, or remove the password."
      : "QA features are locked. Choose <b>QA</b> above and enter the QA password to unlock them.";
  el("qa-lock-manage").hidden = lock && !unlocked;
  el("qa-new-label").textContent = lock ? "New QA password" : "QA password";
  el("qa-set-label").textContent = lock ? "Change QA password" : "Set QA password";
  el("qa-lock-now").hidden = el("qa-remove-password").hidden = !lock;
}
async function postJson(url, body = {}) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await res.json().catch(() => ({ detail: res.statusText }));
  if (!res.ok) throw new Error(d.detail || res.status);
  return d;
}
async function saveTeam(team) {
  // Choosing QA while locked: ask for the QA password first.
  if (team === "qa" && !qaState().unlocked && !(await askQaPassword())) {
    applyTeam(state.setup.prefill.team);
    return false;
  }
  try {
    const d = await postJson("/api/setup/team", { team });
    state.setup.prefill.team = team;
    if (d.qa) state.setup.qa = d.qa;
    applyTeam(team);
    toast(`Team set to ${team === "qa" ? "QA" : "Integration"}.`, "success");
    return true;
  } catch (e) {
    toast(`Couldn't save team: ${e.message || e}`, "error");
    return false;
  }
}
let qaResolve = null;
function askQaPassword() {
  el("qa-password").value = "";
  el("qa-error").hidden = true;
  el("qa-modal").hidden = false;
  el("qa-password").focus();
  return new Promise((resolve) => { qaResolve = resolve; });
}
function closeQaModal(ok) {
  el("qa-modal").hidden = true;
  if (qaResolve) qaResolve(ok);
  qaResolve = null;
}
async function submitQaPassword() {
  const done = setBusy(el("qa-modal-unlock"), "Checking\u2026");
  try {
    const d = await postJson("/api/qa/unlock", { password: el("qa-password").value, remember: el("qa-remember").checked });
    state.setup.qa = d.qa;
    closeQaModal(true);
  } catch (e) {
    el("qa-error").textContent = e.message || String(e);
    el("qa-error").hidden = false;
    el("qa-password").select();
  } finally {
    done();
  }
}
el("qa-modal-unlock").addEventListener("click", submitQaPassword);
el("qa-password").addEventListener("keydown", (e) => { if (e.key === "Enter") submitQaPassword(); });
el("qa-modal-cancel").addEventListener("click", () => closeQaModal(false));
el("qa-modal-close").addEventListener("click", () => closeQaModal(false));
el("qa-set-password").addEventListener("click", async () => {
  const pw = el("qa-new-password").value;
  const { lock } = qaState();
  if (!confirm(lock
    ? "Change the shared QA password?\n\nEveryone else needs the new one (and a new app build for coworkers' copies)."
    : "Set a QA password?\n\nAfter this, Tunnels, Config, Software Update and Web App need it. Rebuild the app so coworkers' copies get it.")) return;
  try {
    const d = await postJson("/api/qa/password", { password: pw });
    state.setup.qa = d.qa;
    el("qa-new-password").value = "";
    applyTeam(state.setup.prefill.team);
    toast(lock ? "QA password changed." : "QA password set. This computer stays unlocked.", "success");
  } catch (e) {
    toast(`Couldn't save the QA password: ${e.message || e}`, "error");
  }
});
el("qa-lock-now").addEventListener("click", async () => {
  try {
    const d = await postJson("/api/qa/lock");
    state.setup.qa = d.qa;
    applyTeam(state.setup.prefill.team);
    toast("QA features locked on this computer.", "success");
  } catch (e) {
    toast(`Couldn't lock: ${e.message || e}`, "error");
  }
});
el("qa-remove-password").addEventListener("click", async () => {
  if (!confirm("Remove the QA password?\n\nAnyone who picks QA will see Tunnels, Config, Software Update and Web App again.")) return;
  try {
    const d = await postJson("/api/qa/remove");
    state.setup.qa = d.qa;
    applyTeam(state.setup.prefill.team);
    toast("QA password removed.", "success");
  } catch (e) {
    toast(`Couldn't remove the QA password: ${e.message || e}`, "error");
  }
});
document.querySelectorAll(".team-choice").forEach((b) => b.addEventListener("click", async () => {
  el("team-modal").hidden = true;
  if (!(await saveTeam(b.dataset.team))) { el("team-modal").hidden = false; return; }
  maybeAskCreds();
}));
document.querySelectorAll("#team-selector .seg").forEach((b) => b.addEventListener("click", () => saveTeam(b.dataset.team)));

/* ---------- First-run / saved credentials ---------- */
const credsSkipped = new Set();
// "saved" fields are passwords/tokens: the page only knows whether one is
// saved, never its value, so those boxes start empty ("blank keeps the saved one").
const CREDS_FIELDS = {
  "creds-ssh": ["saved", "ssh_password"],
  "creds-sudo": ["saved", "sudo_password"],
  "creds-art-email": ["prefill", "artifactory_email"],
  "creds-art-token": ["saved", "artifactory_token"],
  "creds-jenkins-user": ["prefill", "jenkins_username"],
  "creds-jenkins-token": ["saved", "jenkins_token"],
  "creds-router-user": ["prefill", "router_admin_username"],
  "creds-router-pass": ["saved", "router_admin_password"],
  "creds-router-enable": ["saved", "router_enable_password"],
  "creds-switch-pass": ["saved", "switch_password"],
  "creds-windows-pass": ["saved", "windows_password"],
};
function openCredsModal() {
  const s = state.setup;
  if (!s) return;
  el("creds-site").textContent = s.connection.site_name || "this lab";
  for (const [id, [group, key]] of Object.entries(CREDS_FIELDS)) {
    const saved = s[group][key];
    el(id).value = group === "saved" ? "" : saved || "";
    if (group === "saved") {
      el(id).type = "password";
      el(id).placeholder = saved ? "blank keeps the saved one" : "";
      credsRevealButton(el(id));
      // Status next to the label: saved / not set / will replace saved.
      const field = el(id).closest(".field");
      field.classList.add("secret");
      field.classList.toggle("is-saved", !!saved);
      field.classList.remove("replacing");
    }
  }
  el("creds-path").textContent = `Lab passwords are saved to ${s.lab_env_path}`;
  el("creds-error").hidden = true;
  el("creds-modal").hidden = false;
  el("creds-ssh").focus();
}
// Eye button inside a password box: show / hide what's typed or saved.
const EYE_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
function credsRevealButton(input) {
  let wrap = input.parentElement;
  if (!wrap.classList.contains("pw-wrap")) {
    wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    input.replaceWith(wrap);
    wrap.appendChild(input);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-eye";
    btn.innerHTML = EYE_SVG;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.classList.toggle("on", show);
      btn.title = show ? "Hide password" : "Show password";
    });
    wrap.appendChild(btn);
  }
  const btn = wrap.querySelector(".pw-eye");
  btn.classList.remove("on");
  btn.title = "Show password";
}
// Sidebar SSH / sudo passwords get the same eye.
for (const id of ["ssh-password", "sudo-password", "artifactory-token", "jenkins-token", "router-admin-password",
  "router-enable-password", "qa-password", "qa-new-password"]) {
  credsRevealButton(el(id));
}
function skipCredsModal() {
  if (state.setup) credsSkipped.add(state.setup.lab_env_path);
  el("creds-modal").hidden = true;
}
el("creds-modal-close").addEventListener("click", skipCredsModal);
el("creds-modal-skip").addEventListener("click", skipCredsModal);
el("edit-creds").addEventListener("click", openCredsModal);
el("creds-modal").addEventListener("keydown", (e) => { if (e.key === "Enter") el("creds-modal-save").click(); });
el("creds-modal").addEventListener("input", (e) => {
  const field = e.target.closest(".field.secret");
  if (!field) return;
  field.classList.toggle("replacing", field.classList.contains("is-saved") && !!e.target.value);
});
el("creds-modal-save").addEventListener("click", async () => {
  // Only send what changed; blank (or an unchanged name) = keep what's saved.
  const s = state.setup;
  const body = {};
  for (const [id, [group, key]] of Object.entries(CREDS_FIELDS)) {
    const v = el(id).value;
    body[key] = v.trim() && (group === "saved" || v !== (s[group][key] || "")) ? v : null;
  }
  const saved = Object.values(body).filter((v) => v !== null).length;
  el("creds-error").hidden = true;
  const done = setBusy(el("creds-modal-save"), "Saving\u2026");
  try {
    const res = await fetch("/api/setup/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail);
    }
    el("creds-modal").hidden = true;
    const msg = saved ? `Saved ${saved} value${saved > 1 ? "s" : ""} on this computer.` : "No changes to save.";
    toast(msg, saved ? "success" : "info");
    logTo(GENERAL_TAB, `Credentials: ${msg}`, "success");
    await loadSetup(true);
    loadPreflight();
    if (!Object.keys(labStatus.results).length) autoCheckLabStatus();
  } catch (e) {
    const msg = serverDown ? "Matrix Facility Manager isn't running - start it again, then retry." : `Couldn't save: ${e.message || e}`;
    el("creds-error").textContent = msg;
    el("creds-error").hidden = false;
    toast(msg, "error");
    logTo(GENERAL_TAB, `Saving credentials failed: ${e.message || e}`, "error");
  } finally {
    done();
  }
});

/* ---------- Local folders (Settings) ---------- */
// Settings input id -> [saved .env field, live field it fills on other tabs]
const FOLDER_FIELDS = {
  "fold-swu-download-dir": ["swu_download_dir", "download-dir"],
  "fold-swu-file": ["swu_file", "swu-file"],
  "fold-backend-repo": ["backend_repo", "webapp-backend-repo"],
  "fold-web-repo": ["web_repo", "webapp-web-repo"],
  "fold-webapp-dist": ["webapp_dist", "webapp-local-dist"],
  "fold-webapp-web": ["webapp_web", "webapp-local-web"],
};
function fillFoldersCard(s) {
  for (const [id, [key]] of Object.entries(FOLDER_FIELDS)) el(id).value = s.prefill[key] || "";
  el("fold-swu-download-dir").placeholder = `Default: ${s.defaults.builtin_swu_download_dir}`;
}
el("folders-save").addEventListener("click", async () => {
  // Strip the quotes Explorer's "Copy as path" adds.
  const clean = (v) => v.trim().replace(/^"(.*)"$/, "$1");
  const body = {};
  for (const [id, [key]] of Object.entries(FOLDER_FIELDS)) body[key] = clean(el(id).value);
  const status = el("folders-status");
  status.textContent = "";
  const done = setBusy(el("folders-save"), "Saving\u2026");
  try {
    const res = await fetch("/api/setup/save-paths", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || res.statusText);
    // Push the saved folders into the Software Update / Web App fields right away.
    const builtin = state.setup ? state.setup.defaults.builtin_swu_download_dir : "";
    for (const [id, [key, live]] of Object.entries(FOLDER_FIELDS)) {
      el(id).value = body[key];
      el(live).value = body[key] || (live === "download-dir" ? builtin : el(live).value);
    }
    const when = new Date().toLocaleTimeString();
    if (d.warnings.length) {
      status.textContent = `Saved at ${when} - ${d.warnings.join("; ")}`;
      toast(`Folders saved, but ${d.warnings.length} not found on this computer.`, "warning");
    } else {
      status.textContent = `\u2714 Saved at ${when}`;
      toast("Folders saved.", "success");
    }
    logTo(GENERAL_TAB, `Folders saved to ${d.saved_to}.`, "success");
    d.warnings.forEach((w) => logTo(GENERAL_TAB, w, "warning"));
    loadPreflight();
  } catch (e) {
    const msg = serverDown ? "Matrix Facility Manager isn't running - start it again, then retry." : `Couldn't save: ${e.message || e}`;
    status.textContent = msg;
    toast(msg, "error");
    logTo(GENERAL_TAB, `Saving folders failed: ${e.message || e}`, "error");
  } finally {
    done();
  }
});

/* ---------- Setup Check (preflight) ---------- */
const PF_FIX_LABEL = {
  creds: "Enter passwords",
  "creds-shared": "Enter my Artifactory/Jenkins details",
  folders: "Set my folders",
};
let preflightLogged = false;
function runPreflightFix(action) {
  if (action === "folders") {
    openSettingsTab();
    el("folders-card").scrollIntoView({ behavior: "smooth", block: "start" });
    el("fold-swu-download-dir").focus();
    return;
  }
  openCredsModal();
  if (action === "creds-shared") {
    el("creds-shared").open = true;
    el("creds-art-email").focus();
  }
}
function openSettingsTab() {
  document.querySelector('#tabs .tab[data-tab="settings"]').click();
}
async function loadPreflight() {
  const badge = el("preflight-badge");
  badge.textContent = "checking\u2026";
  badge.className = "badge subtle";
  let s;
  try {
    s = await (await fetch("/api/preflight")).json();
  } catch (e) {
    badge.textContent = "check failed";
    logTo(GENERAL_TAB, `Setup Check failed: ${e}`, "error");
    return;
  }
  const worst = s.errors ? "error" : s.warnings ? "warn" : "ok";
  badge.className = `badge pf-${worst}`;
  badge.textContent = s.errors ? `${s.errors} problem${s.errors > 1 ? "s" : ""}`
    : s.warnings ? `ready \u00b7 ${s.warnings} optional` : "all good";

  el("preflight-list").innerHTML = s.checks.map((c) => {
    const bad = c.status !== "ok";
    const fix = bad && c.fix ? `<span class="pf-fix">${escapeHtml(c.fix)}</span>` : "";
    const btn = bad && c.action
      ? `<button class="ghost sm" data-pf-fix="${escapeHtml(c.action)}">${escapeHtml(PF_FIX_LABEL[c.action] || "Fix")}</button>`
      : "<span></span>";
    return `<div class="pf-item ${c.status}"><span class="pf-dot"></span>` +
      `<span class="pf-label">${escapeHtml(c.label)}</span>` +
      `<span class="pf-detail">${escapeHtml(c.detail)}${fix}</span>${btn}</div>`;
  }).join("");

  // Banner only for blocking problems; optional gaps live in Settings.
  const errors = s.checks.filter((c) => c.status === "error");
  const banner = el("setup-banner");
  banner.hidden = errors.length === 0;
  banner.className = "setup-banner error";
  if (errors.length) {
    el("setup-banner-text").textContent = errors.length === 1
      ? `Setup needs attention: ${errors[0].label} - ${errors[0].detail}`
      : `Setup needs attention: ${errors.length} problems (${errors.map((c) => c.label).join(", ")}).`;
    const fixable = errors.find((c) => c.action);
    const fixBtn = el("setup-banner-fix");
    fixBtn.hidden = !fixable;
    if (fixable) {
      fixBtn.textContent = PF_FIX_LABEL[fixable.action];
      fixBtn.dataset.pfFix = fixable.action;
    }
  }

  if (!preflightLogged) {
    preflightLogged = true;
    if (s.errors) logTo(GENERAL_TAB, `Setup Check: ${s.errors} problem(s) to fix - see Settings > Setup Check.`, "error");
    else if (s.warnings) logTo(GENERAL_TAB, `Setup Check: ready. ${s.warnings} optional item(s) not set up (Settings > Setup Check).`, "info");
    else logTo(GENERAL_TAB, "Setup Check: all good.", "success");
  }
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-pf-fix]");
  if (b) runPreflightFix(b.dataset.pfFix);
});
el("setup-banner-open").addEventListener("click", openSettingsTab);

/* ---------- FAQ ---------- */
// Content lives in faq.js (window.FAQ_SECTIONS); answers are trusted HTML.
function renderFaq() {
  const sections = window.FAQ_SECTIONS || [];
  el("faq-list").innerHTML = sections.map((s) =>
    `<div class="faq-section"><h3>${escapeHtml(s.title)}</h3>` +
    s.items.map((it) =>
      `<details class="faq-item"><summary>${escapeHtml(it.q)}</summary><div class="faq-answer">${it.a}</div></details>`
    ).join("") + "</div>"
  ).join("");
  el("faq-count").textContent = `${sections.reduce((n, s) => n + s.items.length, 0)} answers`;
}
function filterFaq() {
  const terms = el("faq-search").value.toLowerCase().split(/\s+/).filter(Boolean);
  let shown = 0;
  document.querySelectorAll("#faq-list .faq-section").forEach((sec) => {
    let secShown = 0;
    sec.querySelectorAll(".faq-item").forEach((item) => {
      const text = item.textContent.toLowerCase();
      const match = terms.every((t) => text.includes(t));
      item.hidden = !match;
      item.open = terms.length > 0 && match;
      secShown += match;
    });
    sec.hidden = secShown === 0;
    shown += secShown;
  });
  el("faq-empty").hidden = shown > 0;
}
renderFaq();
el("faq-search").addEventListener("input", filterFaq);
el("faq-expand").addEventListener("click", () => {
  const items = [...document.querySelectorAll("#faq-list .faq-item:not([hidden])")];
  const open = !items.every((d) => d.open);
  items.forEach((d) => (d.open = open));
  el("faq-expand").textContent = open ? "Collapse all" : "Expand all";
});
el("preflight-rerun").addEventListener("click", loadPreflight);
function setRoomStatus(number, status) {
  const dot = document.querySelector(`.status[data-status="${number}"]`);
  if (dot) dot.className = `status ${status}`;
}
function clearRoomStatuses() {
  document.querySelectorAll(".room-chip .status").forEach((d) => (d.className = "status"));
}
function selectedRoomNumbers() {
  return Array.from(state.selected.values());
}
function credentials() {
  return {
    ssh_password: el("ssh-password").value || null,
    sudo_password: el("sudo-password").value || null,
  };
}

/* ---------- Segmented controls ---------- */
document.querySelectorAll("#deploy-concurrency .seg").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#deploy-concurrency .seg").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.concurrency = Number(btn.dataset.value);
  });
});
function concurrencyValue() {
  const n = state.concurrency;
  return { sequential: n === 1, max_concurrency: n === 0 ? null : n };
}
let webappConcurrency = 1;
document.querySelectorAll("#webapp-concurrency .seg").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#webapp-concurrency .seg").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    webappConcurrency = Number(btn.dataset.value);
  });
});
document.querySelectorAll("#service-selector .seg").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#service-selector .seg").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.service = btn.dataset.service;
  });
});

/* ---------- Job lifecycle ---------- */
function connectToJob(jobId, tabId) {
  const tab = ctabs.tabs[tabId];
  if (tab) { tab.jobId = jobId; tab.finished = false; }
  setConsoleTabStatus(tabId, "running");
  jobStarted();
  updateCancelButton();
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/jobs/${jobId}`);
  let hadFailure = false;
  let ended = false;
  const endOnce = () => { if (!ended) { ended = true; jobEnded(); } };
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    switch (data.type) {
      case "log": logTo(tabId, data.message, data.level || "detail"); break;
      case "room_status":
        setRoomStatus(data.room, data.status);
        if (data.status === "failed") hadFailure = true;
        break;
      case "artifact": addDownload(data.name, data.url); logTo(tabId, `Saved ${data.name}`, "success"); break;
      case "swu_downloaded":
        el("swu-file").value = data.path;
        logTo(tabId, `SWU path set to ${data.path}`, "info");
        break;
      case "all_done":
        logTo(tabId, "Job finished.", "info");
        if (tab) tab.finished = true;
        setConsoleTabStatus(tabId, hadFailure ? "failed" : "success");
        endOnce();
        updateCancelButton();
        break;
    }
  };
  ws.onerror = () => logTo(tabId, "WebSocket error.", "error");
  ws.onclose = endOnce;
}
async function startJob(path, body, { needRooms = true, title = "Job" } = {}) {
  if (needRooms && (!body.room_numbers || body.room_numbers.length === 0)) {
    logLine("Select at least one room first.", "warning");
    return;
  }
  // Mark this job's target rooms as queued in the sidebar until each starts
  // running (backend then emits running -> success/failed per room).
  const targets = body.room_numbers || (body.room_number != null ? [body.room_number] : []);
  for (const n of targets) setRoomStatus(n, "queued");
  setConfigFullscreen(false);  // so the job's console output is visible
  const tabId = createConsoleTab(title, { closeable: true });
  logTo(tabId, `Starting ${title}\u2026`, "info");
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logTo(tabId, `Failed to start: ${err.detail}`, "error");
      setConsoleTabStatus(tabId, "failed");
      return;
    }
    const { job_id } = await res.json();
    connectToJob(job_id, tabId);
  } catch (e) {
    logTo(tabId, `Request failed: ${e}`, "error");
    setConsoleTabStatus(tabId, "failed");
  }
}

const ACTION_TITLES = {
  check_uptime: "Uptime", check_disk_space: "Disk Space", check_specs: "Specs",
  check_system_errors: "System Errors", nms_bandwidth: "NMS Bandwidth",
  nms_link_bandwidth: "Link Bandwidth", remove_overlay: "Remove Overlay",
  get_nms_password: "NMS Password", set_log_debug: "Log Debug",
  configure_web_app: "Web App Config", reboot: "Reboot", shutdown: "Shutdown",
  remove_fingerprint: "Remove Fingerprint", run_command: "Run Command",
  get_logs: "Service Logs", get_full_journal: "System Logs",
  export_bundle: "Support Bundle", add_trusted_endpoint: "Trusted Endpoint",
  enable_matrix_app_debug: "Enable DevTools", disable_matrix_app_debug: "Disable DevTools",
  enable_admin_app_debug: "Enable Admin DevTools", disable_admin_app_debug: "Disable Admin DevTools",
  wake_display: "Wake Display",
  reset_web_app: "Web App Reset", diag_web_app: "Web App Diagnostics",
};
function actionTitle(action) {
  return ACTION_TITLES[action] || action.replace(/_/g, " ");
}

/* ---------- Deploy ---------- */
el("start-deploy").addEventListener("click", () => {
  if (!el("swu-file").value) { logLine("Enter an SWU file path first.", "warning"); return; }
  const { sequential, max_concurrency } = concurrencyValue();
  startJob("/api/jobs/deploy", {
    room_numbers: selectedRoomNumbers(),
    ...credentials(),
    swu_file: el("swu-file").value,
    sequential,
    max_concurrency,
  }, { title: "Software Update" });
});

/* ---------- SWU file browser ---------- */
function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}
async function browseLoad(path) {
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  let data;
  try {
    const res = await fetch(`/api/fs/list${q}`);
    if (!res.ok) { const e = await res.json().catch(() => ({ detail: res.statusText })); throw new Error(e.detail); }
    data = await res.json();
  } catch (e) {
    el("browse-entries").innerHTML = `<div class="browse-empty">${escapeHtml(String(e.message || e))}</div>`;
    return;
  }
  el("browse-path").value = data.dir;
  el("browse-up").dataset.parent = data.parent || "";
  el("browse-up").disabled = !data.parent;
  const box = el("browse-entries");
  box.innerHTML = "";
  if (!data.entries.length) {
    box.innerHTML = '<div class="browse-empty">No sub-folders or .swu files here.</div>';
    return;
  }
  const folderIco = '<svg class="b-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>';
  const fileIco = '<svg class="b-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>';
  for (const e of data.entries) {
    const row = document.createElement("div");
    row.className = "browse-entry " + (e.is_dir ? "dir" : "swu");
    row.innerHTML = (e.is_dir ? folderIco : fileIco) +
      `<span class="b-name">${escapeHtml(e.name)}</span>` +
      (e.is_dir ? "" : `<span class="b-size">${fmtSize(e.size)}</span>`);
    row.addEventListener("click", () => {
      if (e.is_dir) browseLoad(e.path);
      else { el("swu-file").value = e.path; closeBrowse(); logLine(`Selected SWU: ${e.path}`, "info"); }
    });
    box.appendChild(row);
  }
}
function closeBrowse() { el("browse-modal").hidden = true; }
el("browse-swu").addEventListener("click", () => {
  el("browse-modal").hidden = false;
  const cur = el("swu-file").value.trim();
  // Start in the folder of the current path if set, else the default.
  browseLoad(cur ? cur.replace(/[\\/][^\\/]*$/, "") : "");
});
el("browse-up").addEventListener("click", (e) => { const p = e.currentTarget.dataset.parent; if (p) browseLoad(p); });
el("browse-go").addEventListener("click", () => browseLoad(el("browse-path").value.trim()));
el("browse-path").addEventListener("keydown", (e) => { if (e.key === "Enter") browseLoad(el("browse-path").value.trim()); });
el("browse-modal-close").addEventListener("click", closeBrowse);
el("browse-modal-cancel").addEventListener("click", closeBrowse);
el("browse-modal").addEventListener("click", (e) => { if (e.target === el("browse-modal")) closeBrowse(); });

/* ---------- Web app build & deploy ---------- */
const WEBAPP_MODES = {
  deploy: {
    go: "Deploy", artifacts: "Built artifacts",
    hint: "Uploads an existing build (the two folders below) to each selected room and restarts matrix-api.",
  },
  "build-deploy": {
    go: "Build & Deploy", artifacts: "Built artifacts (optional - leave blank to use <backend repo>\\dist and <web repo>\\dist\\arthrex-synergy-matrix)",
    hint: "Runs git pull + npm install + npm run build in both repos, then deploys the result to each selected room.",
  },
  build: {
    go: "Build", artifacts: "Built artifacts (optional - leave blank to use <backend repo>\\dist and <web repo>\\dist\\arthrex-synergy-matrix)",
    hint: "Runs git pull + npm install + npm run build in both repos and checks their versions match. Nothing is deployed; no rooms needed.",
  },
};
let webappMode = "deploy";
function setWebappMode(mode) {
  webappMode = mode;
  const m = WEBAPP_MODES[mode];
  document.querySelectorAll("#webapp-mode .seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  el("webapp-build-fields").hidden = mode === "deploy";
  el("webapp-concurrency").hidden = mode === "build";
  el("webapp-artifacts-label").textContent = m.artifacts;
  el("webapp-mode-hint").textContent = m.hint;
  el("webapp-go-label").textContent = m.go;
}
document.querySelectorAll("#webapp-mode .seg").forEach((b) => b.addEventListener("click", () => setWebappMode(b.dataset.mode)));
setWebappMode("deploy");
el("start-webapp-deploy").addEventListener("click", () => {
  const doBuild = webappMode !== "deploy";
  const buildOnly = webappMode === "build";
  const backendRepo = el("webapp-backend-repo").value.trim();
  const webRepo = el("webapp-web-repo").value.trim();
  const localDist = el("webapp-local-dist").value.trim();
  const localWeb = el("webapp-local-web").value.trim();
  if (doBuild && (!backendRepo || !webRepo)) {
    logLine("Enter both source repo paths to build from source.", "warning");
    return;
  }
  if (!doBuild && (!localDist || !localWeb)) {
    logLine("Enter the backend dist and web assets folders, or switch Mode to 'Build + deploy'.", "warning");
    return;
  }
  startJob("/api/jobs/webapp-deploy", {
    room_numbers: selectedRoomNumbers(),
    ...credentials(),
    do_build: doBuild,
    backend_repo: backendRepo || null,
    web_repo: webRepo || null,
    local_dist: localDist || null,
    local_web: localWeb || null,
    build_only: buildOnly,
    sequential: webappConcurrency === 1,
    max_concurrency: webappConcurrency === 0 ? null : webappConcurrency,
  }, { needRooms: !buildOnly, title: buildOnly ? "Web App Build" : "Web App Deploy" });
});

/* ---------- System actions ---------- */
function runSystemAction(action, extra = {}, title = null) {
  startJob("/api/jobs/system-action", {
    room_numbers: selectedRoomNumbers(),
    ...credentials(),
    action,
    custom_command: el("custom-command") ? el("custom-command").value : "",
    use_sudo: el("use-sudo") ? el("use-sudo").checked : false,
    sequential: true,
    ...extra,
  }, { title: title || actionTitle(action) });
}
// Destructive actions ask first. Returns true when no rooms are selected so
// startJob can show its usual "select a room" warning instead.
function confirmOnRooms(what) {
  const rooms = selectedRoomNumbers();
  if (!rooms.length) return true;
  return confirm(`${what} on ${rooms.map((n) => "OR " + n).join(", ")}?`);
}
const CONFIRM_ACTIONS = {
  reboot: "Reboot",
  shutdown: "Power OFF (it will NOT come back on by itself)",
  reset_web_app: "Remove the deployed web app and restore the original entrypoint",
};
document.querySelectorAll("button[data-action]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const confirmText = CONFIRM_ACTIONS[btn.dataset.action];
    if (confirmText && !confirmOnRooms(confirmText)) return;
    const extra = {};
    let title = actionTitle(btn.dataset.action);
    if (btn.dataset.bandwidth) {
      extra.bandwidth = btn.dataset.bandwidth;
      title = `NMS ${btn.dataset.bandwidth === "LIMITED" ? "Limited" : "Max"} BW`;
    }
    runSystemAction(btn.dataset.action, extra, title);
  });
});
function runWithLogRange(action) {
  const since = el("log-since");
  const range = since.options[since.selectedIndex].text.replace(/^Last /, "");
  runSystemAction(action, { since_hours: Number(since.value) }, `${actionTitle(action)} (${range})`);
}
el("download-logs").addEventListener("click", () => runWithLogRange(el("log-scope").value));
el("show-errors").addEventListener("click", () => runWithLogRange("check_system_errors"));

/* ---------- Services ---------- */
const SERVICE_ACTIONS = {
  matrix: { status: "matrix_api_status", restart: "restart_service", stop: "stop_service", label: "Matrix API" },
  nms: { status: "nms_status", restart: "restart_nms_service", stop: "stop_nms_service", label: "Barco NMS" },
};
async function copyToClipboard(text) {
  // Try the async Clipboard API first (needs a focused, secure context).
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through to legacy path */ }
  // Legacy fallback: works without focus/secure-context in most browsers.
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

// Fetch the first selected room's NMS password and copy it to the clipboard.
// Returns after copying so callers can THEN open tabs (opening first steals
// focus and makes the async clipboard write fail).
async function fetchAndCopyNmsPassword(roomNumbers) {
  let copied = false;
  for (const n of roomNumbers) {
    try {
      const res = await fetch("/api/room/nms-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room_number: n, ...credentials() }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({ detail: res.statusText }));
        logTo(GENERAL_TAB, `OR ${n} NMS password: ${e.detail}`, "error");
        continue;
      }
      const d = await res.json();
      // The password itself is never written to the console.
      if (!copied) {
        const ok = await copyToClipboard(d.password);
        logTo(
          GENERAL_TAB,
          ok ? `Copied OR ${n} NMS password to clipboard.`
             : `Couldn't copy OR ${n}'s NMS password to the clipboard - click NMS again with this window focused.`,
          ok ? "success" : "warning",
        );
        copied = true;
      }
    } catch (e) {
      logTo(GENERAL_TAB, `OR ${n} NMS password error: ${e}`, "error");
    }
  }
}

el("svc-stream").addEventListener("click", () => {
  const rooms = selectedRoomNumbers();
  if (!rooms.length) { logLine("Select a room first.", "warning"); return; }
  const room = rooms[0];
  const label = SERVICE_ACTIONS[state.service].label;
  if (rooms.length > 1) logLine(`Live stream follows one room; using OR ${room}.`, "warning");
  startJob("/api/jobs/stream-logs", { room_number: room, service: state.service, ...credentials() },
    { needRooms: false, title: `Live: ${label} OR ${room}` });
});
el("svc-status").addEventListener("click", () => runSystemAction(SERVICE_ACTIONS[state.service].status, {}, `${SERVICE_ACTIONS[state.service].label} Status`));
el("svc-restart").addEventListener("click", () => runSystemAction(SERVICE_ACTIONS[state.service].restart, {}, `${SERVICE_ACTIONS[state.service].label} Restart`));
el("svc-stop").addEventListener("click", () => confirmOnRooms(`Stop ${SERVICE_ACTIONS[state.service].label}`) && runSystemAction(SERVICE_ACTIONS[state.service].stop, {}, `${SERVICE_ACTIONS[state.service].label} Stop`));

/* ---------- NMS link bandwidth + trusted endpoint ---------- */
el("apply-link-bw").addEventListener("click", () => {
  const kbps = Number(el("link-kbps").value);
  if (!kbps) { logLine("Enter a link bandwidth in kbps.", "warning"); return; }
  runSystemAction("nms_link_bandwidth", { link_bandwidth_kbps: kbps });
});
el("add-trusted").addEventListener("click", () => {
  const ep = el("trusted-endpoint").value.trim();
  if (!ep) { logLine("Enter a trusted endpoint.", "warning"); return; }
  runSystemAction("add_trusted_endpoint", { trusted_endpoint: ep });
});

/* ---------- Config editor ---------- */
// Deploy always targets the room the editor content was loaded from; if the
// picker moves to another room, deploy is blocked until that room is loaded.
function syncConfigDeploy() {
  const loaded = state.configLoadedRoom;
  const picked = Number(el("config-room").value);
  el("config-deploy").disabled = loaded == null || loaded !== picked;
  el("config-apply-selected").disabled = loaded == null;
  if (loaded != null && loaded !== picked) {
    el("config-status").textContent = `Editor holds OR ${loaded} - click Load for OR ${picked}`;
  } else if (loaded != null) {
    el("config-status").textContent = `Loaded OR ${loaded}`;
  }
}
el("config-room").addEventListener("change", syncConfigDeploy);
function setConfigFullscreen(on) {
  el("config-card").classList.toggle("fullscreen", on);
  el("config-fullscreen").textContent = on ? "Exit full screen (Esc)" : "Full screen";
  if (on) el("config-editor").focus();
}
el("config-fullscreen").addEventListener("click", () => setConfigFullscreen(!el("config-card").classList.contains("fullscreen")));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && el("config-card").classList.contains("fullscreen")) setConfigFullscreen(false);
});
el("config-load").addEventListener("click", async () => {
  const roomNumber = Number(el("config-room").value);
  el("config-status").textContent = "Loading\u2026";
  try {
    const res = await fetch("/api/config/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_number: roomNumber, ...credentials() }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      el("config-status").textContent = "Load failed";
      logLine(`Config load failed: ${err.detail}`, "error");
      return;
    }
    const data = await res.json();
    state.configOriginal = JSON.parse(data.content);
    el("config-editor").value = data.content;
    state.configLoadedRoom = data.room_number;
    syncConfigDeploy();
    logLine(`Loaded config for OR ${data.room_number} (${data.content.length} bytes).`, "success");
  } catch (e) {
    el("config-status").textContent = "Load failed";
    logLine(`Config load error: ${e}`, "error");
  }
});
el("config-deploy").addEventListener("click", () => {
  const roomNumber = state.configLoadedRoom;
  if (roomNumber == null) return;
  const content = el("config-editor").value;
  try {
    JSON.parse(content);
  } catch (e) {
    logLine(`Config is not valid JSON: ${e}`, "error");
    return;
  }
  startJob("/api/config/deploy", { room_number: roomNumber, content, ...credentials() }, { needRooms: false, title: `Config OR ${roomNumber}` });
});

// Field-level diff of two JSON values. Objects recurse by key. Lists of plain
// values (e.g. trustedEndPoints) become add/remove-item ops so each room
// keeps its own entries. Lists of objects recurse by index when the length
// is unchanged; otherwise the whole list is replaced.
function diffJson(a, b, path = [], out = []) {
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  const isPlainList = (x) => Array.isArray(x) && x.every((v) => v === null || typeof v !== "object");
  if (isObj(a) && isObj(b)) {
    for (const k of Object.keys(b)) {
      if (!(k in a)) out.push({ op: "set", path: [...path, k], value: b[k] });
      else diffJson(a[k], b[k], [...path, k], out);
    }
    for (const k of Object.keys(a)) if (!(k in b)) out.push({ op: "delete", path: [...path, k] });
  } else if (isPlainList(a) && isPlainList(b)) {
    for (const v of b) if (!a.includes(v)) out.push({ op: "add_item", path, value: v });
    for (const v of a) if (!b.includes(v)) out.push({ op: "remove_item", path, value: v });
  } else if (Array.isArray(a) && Array.isArray(b) && a.length === b.length) {
    b.forEach((v, i) => diffJson(a[i], v, [...path, i], out));
  } else if (JSON.stringify(a) !== JSON.stringify(b)) {
    out.push({ op: "set", path, value: b });
  }
  return out;
}
function fmtConfigPath(path) {
  return path.map((k, i) => (typeof k === "number" ? `[${k}]` : i ? `.${k}` : k)).join("");
}
el("config-apply-selected").addEventListener("click", () => {
  if (state.configLoadedRoom == null) return;
  let edited;
  try {
    edited = JSON.parse(el("config-editor").value);
  } catch (e) {
    logLine(`Config is not valid JSON: ${e}`, "error");
    return;
  }
  const changes = diffJson(state.configOriginal, edited);
  if (!changes.length) { logLine(`No changes from OR ${state.configLoadedRoom}'s loaded config.`, "warning"); return; }
  if (changes.some((c) => !c.path.length)) { logLine("The top level of the config must stay an object.", "error"); return; }
  const rooms = selectedRoomNumbers();
  if (!rooms.length) { logLine("Tick the rooms to apply the changes to in the sidebar.", "warning"); return; }
  const short = (v) => { const s = JSON.stringify(v); return s.length > 80 ? s.slice(0, 77) + "..." : s; };
  const verb = { delete: () => "(remove field)", add_item: (c) => "+ " + short(c.value),
    remove_item: (c) => "\u2212 " + short(c.value), set: (c) => "= " + short(c.value) };
  const lines = changes.slice(0, 15).map((c) => `  ${fmtConfigPath(c.path)} ${verb[c.op](c)}`);
  if (changes.length > 15) lines.push(`  ...and ${changes.length - 15} more`);
  const msg = `Apply ${changes.length} change(s) to ${rooms.map((n) => "OR " + n).join(", ")}?\n\n` +
    `${lines.join("\n")}\n\nOnly these fields change on each room; matrix-api restarts where something changed.`;
  if (!confirm(msg)) return;
  startJob("/api/config/apply-changes", {
    room_numbers: rooms,
    changes,
    ...credentials(),
    sequential: true,
  }, { title: `Config changes \u2192 ${rooms.length} room${rooms.length > 1 ? "s" : ""}` });
});

/* ---------- Tunnels ---------- */
const APP_NAMES = { matrix: "Matrix App", admin: "Matrix Admin App" };
const DEVTOOLS_STEPS = {
  matrix: [
    "<b>Enable DevTools</b> (restarts the Matrix App on the room's screen; needs sudo).",
    "<b>Open DevTools</b> to inspect it in Chrome/Edge, or <b>Screenshot</b>.",
    "<b>Disable DevTools</b> when you're done.",
  ],
  admin: [
    "<b>Enable DevTools</b> (restarts the admin app in the background; needs sudo).",
    "<b>Open DevTools</b>. It wakes the room's screen, brings up the admin login screen, and opens DevTools in Chrome/Edge. Type the admin password in the page preview there (or at the room).",
    "Inspect it, or click <b>Screenshot</b>.",
    "<b>Disable DevTools</b> when you're done. This also returns the room to the kiosk.",
  ],
};
const tunnelApp = () => el("tunnel-app").value;
function renderDevtoolsSteps() {
  el("devtools-steps").innerHTML = DEVTOOLS_STEPS[tunnelApp()].map((s) => `<li>${s}</li>`).join("");
}
el("tunnel-app").addEventListener("change", renderDevtoolsSteps);
renderDevtoolsSteps();

for (const [id, verb] of [["enable-devtools", "Enable"], ["disable-devtools", "Disable"]]) {
  el(id).addEventListener("click", () => {
    const n = Number(el("tunnel-room").value);
    const app = tunnelApp();
    if (!confirm(`${verb} DevTools for the ${APP_NAMES[app]} on OR ${n}?\n\nThis restarts the ${APP_NAMES[app]} (needs the sudo password).`)) return;
    startJob("/api/jobs/system-action", {
      room_numbers: [n],
      ...credentials(),
      action: `${verb.toLowerCase()}_${app === "admin" ? "admin" : "matrix"}_app_debug`,
      sequential: true,
    }, { needRooms: false, title: `${verb} DevTools (${APP_NAMES[app]}) OR ${n}` });
  });
}
el("wake-display").addEventListener("click", () => {
  const n = Number(el("tunnel-room").value);
  startJob("/api/jobs/system-action", {
    room_numbers: [n],
    ...credentials(),
    action: "wake_display",
    sequential: true,
  }, { needRooms: false, title: `Wake Display OR ${n}` });
});

async function openAppDevtools(app) {
  const name = APP_NAMES[app];
  const roomNumber = Number(el("tunnel-room").value);
  setActiveConsoleTab(GENERAL_TAB);
  logTo(GENERAL_TAB, `Opening ${name} DevTools tunnel for OR ${roomNumber}\u2026`, "info");
  try {
    logTo(GENERAL_TAB, app === "admin"
      ? `Waking the screen and bringing up the admin login screen on OR ${roomNumber}\u2026`
      : `Waking the screen on OR ${roomNumber}\u2026`, "detail");
    const res = await fetch("/api/matrix-app/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_number: roomNumber, ...credentials(), app }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logTo(GENERAL_TAB, `${name} DevTools failed: ${err.detail}`, "error");
      return;
    }
    const t = await res.json();
    if (t.repaired) logTo(GENERAL_TAB, "The admin app was on a broken page, so its login page was reloaded.", "warning");
    logLinkTo(GENERAL_TAB, `OR ${t.room_number} ${name}${t.title ? " (" + t.title + ")" : ""}:`, t.url);
    logTo(GENERAL_TAB, "Opened in a new tab (use Chrome/Edge). If it later says disconnected, the app restarted - click Open DevTools again.", "detail");
    window.open(safeUrl(t.url), "_blank", "noopener");
    await refreshTunnels();
  } catch (e) {
    logTo(GENERAL_TAB, `${name} DevTools error: ${e}`, "error");
  }
}
el("open-devtools").addEventListener("click", () => openAppDevtools(tunnelApp()));
el("screenshot-app").addEventListener("click", () => screenshotApp(tunnelApp(), el("screenshot-app")));
async function screenshotApp(app, btn) {
  const roomNumber = Number(el("tunnel-room").value);
  setActiveConsoleTab(GENERAL_TAB);
  logTo(GENERAL_TAB, `Capturing ${APP_NAMES[app]} screenshot for OR ${roomNumber}\u2026`, "info");
  btn.disabled = true;
  try {
    const res = await fetch("/api/matrix-app/screenshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_number: roomNumber, ...credentials(), app }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logTo(GENERAL_TAB, `Screenshot failed: ${err.detail}`, "error");
      return;
    }
    const s = await res.json();
    addDownload(s.name, s.url);
    logLinkTo(GENERAL_TAB, `OR ${s.room_number} screenshot saved (also under Logs \u2192 Downloads):`, s.url);
    const a = document.createElement("a");
    a.href = s.url;
    a.download = s.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) {
    logTo(GENERAL_TAB, `Screenshot error: ${e}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function refreshTunnels() {
  const list = await (await fetch("/api/tunnel/list")).json();
  const box = el("tunnels-list");
  if (!list.length) {
    box.innerHTML = '<span class="empty-hint">No active tunnels.</span>';
    return;
  }
  box.innerHTML = "";
  for (const t of list) {
    const row = document.createElement("div");
    row.className = "dl-item";
    const target = t.url
      ? `<a href="${escapeHtml(safeUrl(t.url))}" target="_blank" rel="noopener">${escapeHtml(t.url)}</a>`
      : `127.0.0.1:${t.local_port} (TCP)`;
    row.innerHTML =
      `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h20M6 12a6 6 0 0 1 12 0"/></svg>` +
      `<span>OR ${t.room_number} &middot; ${escapeHtml(t.label)} &rarr; ${target}</span>` +
      `<button class="danger sm" data-close="${t.id}" style="margin-left:auto;">Close</button>`;
    box.appendChild(row);
  }
  box.querySelectorAll("button[data-close]").forEach((b) => {
    b.addEventListener("click", async () => {
      await fetch(`/api/tunnel/${b.dataset.close}/close`, { method: "POST" });
      logLine("Tunnel closed.", "warning");
      refreshTunnels();
    });
  });
}

/* ---------- Integrations ---------- */
function startDownload(branch) {
  startJob("/api/jobs/download-latest", {
    artifactory_email: el("artifactory-email").value || null,
    artifactory_token: el("artifactory-token").value || null,  // blank = the saved one (server side)
    cache_dir: el("download-dir").value || null,
    branch: branch || null,
  }, { needRooms: false, title: branch ? `Download SWU (${branch})` : "Download SWU" });
}

function closeBranchModal() {
  el("branch-modal").hidden = true;
}
function openBranchModal(branches) {
  const box = el("branch-modal-options");
  box.innerHTML = "";
  branches.forEach((b, i) => {
    const label = document.createElement("label");
    label.className = "branch-option" + (i === 0 ? " checked" : "");
    label.innerHTML =
      `<input type="radio" name="branch-choice" value="${escapeHtml(b.label)}"${i === 0 ? " checked" : ""} />` +
      `<span class="b-label">${escapeHtml(b.label)}</span>` +
      `<span class="b-path">${escapeHtml(b.build_path)}</span>`;
    label.querySelector("input").addEventListener("change", () => {
      box.querySelectorAll(".branch-option").forEach((o) => o.classList.remove("checked"));
      label.classList.add("checked");
    });
    box.appendChild(label);
  });
  el("branch-modal").hidden = false;
}

el("branch-modal-close").addEventListener("click", closeBranchModal);
el("branch-modal-cancel").addEventListener("click", closeBranchModal);
el("branch-modal").addEventListener("click", (e) => {
  if (e.target === el("branch-modal")) closeBranchModal();
});
el("branch-modal-confirm").addEventListener("click", () => {
  const chosen = el("branch-modal-options").querySelector("input[name=branch-choice]:checked");
  closeBranchModal();
  startDownload(chosen ? chosen.value : null);
});

el("download-latest").addEventListener("click", async () => {
  let branches = [];
  try {
    branches = await (await fetch("/api/artifactory/branches")).json();
  } catch (e) {
    logLine(`Could not load build branches: ${e}`, "warning");
  }
  if (branches.length <= 1) {
    startDownload(branches.length === 1 ? branches[0].label : null);
    return;
  }
  openBranchModal(branches);
});
el("trigger-build").addEventListener("click", () => {
  startJob("/api/jobs/trigger-build", {
    jenkins_username: el("jenkins-username").value || null,
    jenkins_token: el("jenkins-token").value || null,  // blank = the saved one (server side)
  }, { needRooms: false, title: "Jenkins Build" });
});

/* ---------- Room selection buttons ---------- */
el("select-all").addEventListener("click", () => {
  document.querySelectorAll("#rooms input[type=checkbox]").forEach((cb) => {
    cb.checked = true;
    cb.closest(".room-chip").classList.add("checked");
    state.selected.add(Number(cb.dataset.room));
  });
  updateCounter();
});
el("select-none").addEventListener("click", () => {
  document.querySelectorAll("#rooms input[type=checkbox]").forEach((cb) => {
    cb.checked = false;
    cb.closest(".room-chip").classList.remove("checked");
  });
  state.selected.clear();
  updateCounter();
});

el("cancel-job").addEventListener("click", async () => {
  const t = ctabs.tabs[ctabs.active];
  if (!t || !t.jobId || t.finished) return;
  await fetch(`/api/jobs/${t.jobId}/cancel`, { method: "POST" });
  logTo(ctabs.active, "Cancellation requested.", "warning");
});

/* ---------- Tooltips ---------- */
const ACTION_TOOLTIPS = {
  check_uptime: "Show time since last boot (uptime) for each selected room.",
  check_disk_space: "Show free space on /tmp, where SWU artifacts extract.",
  check_specs: "Show kernel/OS, CPU, memory, and root filesystem usage.",
  remove_overlay: "Remove the NMS video overlay (matrixEmptyOverlay) and restart barco-nms.",
  set_log_debug: "Set matrix.api log level to debug and restart matrix-api.",
  configure_web_app: "Set web-app folders/pairing key, trust all room API origins, then restart.",
  reboot: "Reboot the room and wait for it to come back online.",
  shutdown: "Power off the room (no automatic reboot).",
  remove_fingerprint: "Remove this room's cached SSH host key from your local known_hosts.",
  run_command: "Run an arbitrary shell command on each selected room.",
  export_bundle: "Run the room's get-support collector and download the official support bundle zip.",
  reset_web_app: "Remove staged/deployed web app files and restore the original systemd entrypoint (index.js).",
  diag_web_app: "Dump web asset listings, config values, service status, and AppArmor denials.",
};
const ID_TOOLTIPS = {
  "tunnel-app": "Which app on the room's screen the buttons below act on. The admin app is the one shown after Exit Kiosk Mode.",
  "enable-devtools": "Turn on Chrome DevTools for the selected app (Matrix App: port 9222, Admin App: port 9223) and restart it. The launcher is backed up first. Needs sudo.",
  "open-devtools": "Wake the room's screen and open the selected app's DevTools in a new tab (use Chrome or Edge). For the Admin App, this also brings its login screen up on the room.",
  "screenshot-app": "Wake the room's screen and save a PNG of the selected app (DevTools must be on). For the Admin App, it's brought on screen first.",
  "disable-devtools": "Turn off DevTools for the selected app and restart it. Needs sudo.",
  "wake-display": "Turn a sleeping room screen back on. Needs sudo.",
  "show-errors": "List error-level kernel and service messages for the selected time range in the console.",
  "download-logs": "Download logs for the chosen scope and time range from each selected room.",
  "log-scope": "Matrix services = only matrix-api and barco-nms. Entire system = every systemd unit plus kernel messages.",
  "log-since": "How far back the export goes - keeps files small for the devs.",
  "svc-status": "Show systemctl status for the selected service.",
  "svc-stream": "Follow the selected service's journal live (journalctl -f). Cancel to stop.",
  "copy-console": "Copy the active console tab's text to the clipboard.",
  "svc-restart": "Restart the selected service.",
  "svc-stop": "Stop the selected service.",
  "apply-link-bw": "Set the NMS interop link bandwidth (kbps) and restart barco-nms.",
  "add-trusted": "Add an endpoint to apiServer.trustedEndPoints and restart matrix-api.",
  "browse-swu": "Browse the machine for an .swu file to deploy.",
  "start-deploy": "Upload the SWU, install via swupdate-client, and wait for reboot.",
  "start-webapp-deploy": "Run the selected Mode: deploy an existing build, build then deploy, or build only.",
  "config-fullscreen": "Make the config editor fill the whole window (Esc to exit).",
  "config-load": "Load the selected room's live matrix.api.config.json into the editor.",
  "config-deploy": "Validate, push the whole edited file back to the loaded room, and restart matrix-api.",
  "config-apply-selected": "Send only the fields you changed to every room ticked in the sidebar. Each room keeps its other values.",
  "download-latest": "Download the newest SWU build from Artifactory to the folder above.",
  "trigger-build": "Trigger a new Embedded Builder Jenkins build (matrix / wrynose).",
  "reload-setup": "Re-read prefill values from your .env file.",
  "edit-creds": "Change the SSH/sudo passwords saved for this lab, or the shared Artifactory/Jenkins credentials.",
  "ls-check-all": "Check every room in this site: services, encoders/decoders/displays seen by NMS, and the NMS license. Read-only.",
  "ls-check-selected": "Same check, only for the rooms ticked in the sidebar.",
  "router-ports-refresh": "Reload the port plan and generated commands for the active site's router.",
  "router-live-check": "Log in to the router (read-only) and compare its real NAT rules with this plan. Uses the router login saved for this lab.",
  "facility-choose": "Pick a facility planner export (<SITE>-version<N>.zip) to add it as a lab, or update the lab it came from.",
  "router-ports-copy": "Copy the Cisco NAT block in the box (the rules you picked, plus any edits) to the clipboard.",
  "router-apply-nat": "SSH into the router, enter config mode, run the NAT lines in the box above, then write memory. Only static NAT lines are accepted. Only enabled after you tick the confirm box.",
  "nat-pick-missing": "Pick only the forwards the live router is missing (needs Check live router first).",
  "nat-reset": "Throw away your hand edits and go back to the commands for the rules you picked.",
  "select-all": "Select every room.",
  "select-none": "Deselect every room.",
  "theme-toggle": "Switch between dark and light theme.",
  "clear-console": "Clear the console output.",
  "cancel-job": "Request cancellation of the running job.",
};
function applyTooltips() {
  document.querySelectorAll("button[data-action]").forEach((btn) => {
    let tip = ACTION_TOOLTIPS[btn.dataset.action];
    if (btn.dataset.action === "nms_bandwidth") {
      tip = `Push the ${btn.dataset.bandwidth === "LIMITED" ? "Limited" : "Max"}-bandwidth golden NMS config and restart barco-nms.`;
    }
    if (tip) btn.title = tip;
  });
  for (const [id, tip] of Object.entries(ID_TOOLTIPS)) {
    if (el(id)) el(id).title = tip;
  }
}
applyTooltips();

el("reload-setup").addEventListener("click", () => {
  loadSetup().then(() => logLine("Reloaded setup from .env.", "success")).catch((e) => logLine(`Setup reload failed: ${e}`, "error"));
});

loadProfiles().catch((e) => logLine(`Failed to load profiles: ${e}`, "error"));
const roomsReady = loadRooms().catch((e) => logLine(`Failed to load rooms: ${e}`, "error"));
loadConnectionInfo().catch((e) => logLine(`Failed to load connection info: ${e}`, "error"));
loadRouterPorts().catch((e) => logLine(`Failed to load router ports: ${e}`, "error"));
const setupReady = loadSetup().catch((e) => logLine(`Failed to load setup: ${e}`, "error"));
Promise.all([roomsReady, setupReady]).then(autoCheckLabStatus);
loadPreflight();
refreshTunnels().catch(() => {});
