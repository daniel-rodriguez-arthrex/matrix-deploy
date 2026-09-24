const state = {
  rooms: [],
  selected: new Set(),
  currentJobId: null,
  ws: null,
  concurrency: 1,
  service: "matrix",
};

const el = (id) => document.getElementById(id);

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
  });
});

/* ---------- Console tabs ---------- */
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

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
    `<a href="${encodeURI(url)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:underline;">${escapeHtml(url)}</a>`;
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
  logLine(ok ? "Console copied to clipboard." : "Copy failed \u2014 select the text manually.", ok ? "info" : "warning");
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
    window.open(appUrls[n], "_blank");
    return;
  }
  // Copy the password FIRST (while this page still has focus), then open the
  // demonstrator - otherwise the new tab steals focus and clipboard fails.
  await fetchAndCopyNmsPassword([n]);
  logLinkTo(GENERAL_TAB, `OR ${n} NMS Demonstrator:`, demoUrls[n]);
  window.open(demoUrls[n], "_blank");
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
    await Promise.all([loadRooms(), loadConnectionInfo(), loadSetup(true), loadPreflight()]);
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
  const p = s.prefill, sec = s.secrets;
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
  prefill("ssh-password", sec.ssh_password, force);
  prefill("sudo-password", sec.sudo_password, force);
  prefill("artifactory-token", sec.artifactory_token, force);
  prefill("jenkins-token", sec.jenkins_token, force);

  // SWU download folder: prefer the .env SWU_FILE dir if given, else the
  // server's default cache folder. Only fill when empty so edits stick.
  if (s.defaults && s.defaults.swu_download_dir) prefill("download-dir", s.defaults.swu_download_dir);

  state.setup = s;
  // First run for this site: ask for the lab credentials once.
  if (!sec.ssh_password && !credsSkipped.has(s.lab_env_path)) openCredsModal();
}

/* ---------- First-run / saved credentials ---------- */
const credsSkipped = new Set();
const CREDS_FIELDS = {
  "creds-ssh": ["secrets", "ssh_password"],
  "creds-sudo": ["secrets", "sudo_password"],
  "creds-art-email": ["prefill", "artifactory_email"],
  "creds-art-token": ["secrets", "artifactory_token"],
  "creds-jenkins-user": ["prefill", "jenkins_username"],
  "creds-jenkins-token": ["secrets", "jenkins_token"],
};
function openCredsModal() {
  const s = state.setup;
  if (!s) return;
  el("creds-site").textContent = s.connection.site_name || "this lab";
  for (const [id, [group, key]] of Object.entries(CREDS_FIELDS)) {
    const saved = s[group][key];
    el(id).value = group === "prefill" ? saved || "" : "";
    if (group === "secrets") el(id).placeholder = saved ? "saved \u2014 leave blank to keep" : "";
  }
  el("creds-path").textContent = `Lab passwords are saved to ${s.lab_env_path}`;
  el("creds-modal").hidden = false;
  el("creds-ssh").focus();
}
function skipCredsModal() {
  if (state.setup) credsSkipped.add(state.setup.lab_env_path);
  el("creds-modal").hidden = true;
}
el("creds-modal-close").addEventListener("click", skipCredsModal);
el("creds-modal-skip").addEventListener("click", skipCredsModal);
el("edit-creds").addEventListener("click", openCredsModal);
el("creds-modal").addEventListener("keydown", (e) => { if (e.key === "Enter") el("creds-modal-save").click(); });
el("creds-modal-save").addEventListener("click", async () => {
  const body = {};
  for (const [id, [, key]] of Object.entries(CREDS_FIELDS)) body[key] = el(id).value.trim() ? el(id).value : null;
  try {
    const res = await fetch("/api/setup/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logTo(GENERAL_TAB, `Saving credentials failed: ${err.detail}`, "error");
      return;
    }
    el("creds-modal").hidden = true;
    logTo(GENERAL_TAB, "Credentials saved on this computer.", "success");
    await loadSetup(true);
    loadPreflight();
  } catch (e) {
    logTo(GENERAL_TAB, `Saving credentials failed: ${e}`, "error");
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
  try {
    const res = await fetch("/api/setup/save-paths", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || res.statusText);
    // Push the saved folders into the Deploy / Web App fields right away.
    const builtin = state.setup ? state.setup.defaults.builtin_swu_download_dir : "";
    for (const [id, [key, live]] of Object.entries(FOLDER_FIELDS)) {
      el(id).value = body[key];
      el(live).value = body[key] || (live === "download-dir" ? builtin : el(live).value);
    }
    status.textContent = d.warnings.length ? `Saved, with ${d.warnings.length} warning(s) - see console.` : "Saved.";
    logTo(GENERAL_TAB, `Folders saved to ${d.saved_to}.`, "success");
    d.warnings.forEach((w) => logTo(GENERAL_TAB, w, "warning"));
    loadPreflight();
  } catch (e) {
    status.textContent = "Save failed - see console.";
    logTo(GENERAL_TAB, `Saving folders failed: ${e.message || e}`, "error");
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
      ? `Setup needs attention: ${errors[0].label} \u2014 ${errors[0].detail}`
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
    if (s.errors) logTo(GENERAL_TAB, `Setup Check: ${s.errors} problem(s) to fix \u2014 see Settings > Setup Check.`, "error");
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
  configure_web_app: "Web App Config", fix_room_config_race: "Fix Config Race",
  matrix_api_certs: "API Certs", reboot: "Reboot", shutdown: "Shutdown",
  remove_fingerprint: "Remove Fingerprint", run_command: "Run Command",
  get_logs: "Service Logs", get_full_journal: "System Logs",
  export_bundle: "Support Bundle", add_trusted_endpoint: "Trusted Endpoint",
  enable_matrix_app_debug: "Enable DevTools", disable_matrix_app_debug: "Disable DevTools",
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
  }, { title: "SWU Deploy" });
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
    go: "Build & Deploy", artifacts: "Built artifacts (optional - defaults to each repo's dist folder)",
    hint: "Runs git pull + npm install + npm run build in both repos, then deploys the result to each selected room.",
  },
  build: {
    go: "Build", artifacts: "Built artifacts (optional - defaults to each repo's dist folder)",
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
  matrix_api_certs: "Regenerate the matrix.api TLS certs and restart matrix-api",
  fix_room_config_race: "Patch room-config-generator ordering",
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
      logTo(GENERAL_TAB, `OR ${n} NMS password: ${d.password}`, "success");
      if (!copied) {
        const ok = await copyToClipboard(d.password);
        logTo(
          GENERAL_TAB,
          ok ? `Copied OR ${n} NMS password to clipboard.`
             : "Could not auto-copy \u2014 select the password above to copy it manually.",
          ok ? "info" : "warning",
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
el("enable-matrix-app").addEventListener("click", () => {
  const n = Number(el("tunnel-room").value);
  if (!confirm(`Enable Chrome DevTools on OR ${n}? This relaunches the Matrix App kiosk (needs the sudo password).`)) return;
  startJob("/api/jobs/system-action", {
    room_numbers: [n],
    ...credentials(),
    action: "enable_matrix_app_debug",
    sequential: true,
  }, { needRooms: false, title: `Enable DevTools OR ${n}` });
});
el("disable-matrix-app").addEventListener("click", () => {
  const n = Number(el("tunnel-room").value);
  if (!confirm(`Disable Chrome DevTools on OR ${n}? This relaunches the Matrix App kiosk (needs the sudo password).`)) return;
  startJob("/api/jobs/system-action", {
    room_numbers: [n],
    ...credentials(),
    action: "disable_matrix_app_debug",
    sequential: true,
  }, { needRooms: false, title: `Disable DevTools OR ${n}` });
});

el("open-matrix-app").addEventListener("click", async () => {
  const roomNumber = Number(el("tunnel-room").value);
  setActiveConsoleTab(GENERAL_TAB);
  logTo(GENERAL_TAB, `Opening Matrix App DevTools tunnel for OR ${roomNumber}\u2026`, "info");
  try {
    const res = await fetch("/api/matrix-app/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_number: roomNumber, ssh_password: el("ssh-password").value || null }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logTo(GENERAL_TAB, `Matrix App DevTools failed: ${err.detail}`, "error");
      return;
    }
    const t = await res.json();
    logLinkTo(GENERAL_TAB, `OR ${t.room_number} Matrix App${t.title ? " (" + t.title + ")" : ""}:`, t.url);
    logTo(GENERAL_TAB, "Open the link above in a Chromium-based browser (Chrome/Edge) for DevTools.", "detail");
    window.open(t.url, "_blank");
    await refreshTunnels();
  } catch (e) {
    logTo(GENERAL_TAB, `Matrix App DevTools error: ${e}`, "error");
  }
});

el("screenshot-matrix-app").addEventListener("click", async () => {
  const roomNumber = Number(el("tunnel-room").value);
  const btn = el("screenshot-matrix-app");
  setActiveConsoleTab(GENERAL_TAB);
  logTo(GENERAL_TAB, `Capturing Matrix App screenshot for OR ${roomNumber}\u2026`, "info");
  btn.disabled = true;
  try {
    const res = await fetch("/api/matrix-app/screenshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_number: roomNumber, ssh_password: el("ssh-password").value || null }),
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
});

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
      ? `<a href="${t.url}" target="_blank" rel="noopener">${t.url}</a>`
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
    artifactory_email: el("artifactory-email").value,
    artifactory_token: el("artifactory-token").value,
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
    jenkins_username: el("jenkins-username").value,
    jenkins_token: el("jenkins-token").value,
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
  fix_room_config_race: "Patch room-config-generator ordering to wait on NMS network init.",
  matrix_api_certs: "Regenerate the matrix.api self-signed TLS cert/key pair and restart.",
  reboot: "Reboot the room and wait for it to come back online.",
  shutdown: "Power off the room (no automatic reboot).",
  remove_fingerprint: "Remove this room's cached SSH host key from your local known_hosts.",
  run_command: "Run an arbitrary shell command on each selected room.",
  export_bundle: "Run the room's get-support collector and download the official support bundle zip.",
  reset_web_app: "Remove staged/deployed web app files and restore the original systemd entrypoint (index.js).",
  diag_web_app: "Dump web asset listings, config values, service status, and AppArmor denials.",
};
const ID_TOOLTIPS = {
  "enable-matrix-app": "Relaunch the Matrix App kiosk with Chrome DevTools enabled (port 9222) over SSH. Needs sudo.",
  "disable-matrix-app": "Remove the DevTools flags from the Matrix App launcher and relaunch the kiosk so port 9222 closes. Needs sudo.",
  "open-matrix-app": "Tunnel to the room's Matrix App Chrome DevTools (port 9222) and open the inspector.",
  "show-errors": "List error-level kernel and service messages for the selected time range in the console.",
  "download-logs": "Download logs for the chosen scope and time range from each selected room.",
  "log-scope": "Matrix services = only matrix-api and barco-nms. Entire system = every systemd unit plus kernel messages.",
  "log-since": "How far back the export goes - keeps files small for the devs.",
  "screenshot-matrix-app": "Capture a PNG of the room's Matrix App via DevTools (enable Remote Debugging first) and download it.",
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
loadRooms().catch((e) => logLine(`Failed to load rooms: ${e}`, "error"));
loadConnectionInfo().catch((e) => logLine(`Failed to load connection info: ${e}`, "error"));
loadSetup().catch((e) => logLine(`Failed to load setup: ${e}`, "error"));
loadPreflight();
refreshTunnels().catch(() => {});
