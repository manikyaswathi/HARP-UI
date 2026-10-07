"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { loaded: false, expiresAt: null, expiryTimer: null, systems: [], apps: [], systemDetails: {}, selected: null, pollTimer: null, lastStatus: {} };
const ACTIVE = ["RUNNING", "WAITING_FOR_LOGIN", "FINALIZING"];
const STATUS_CLASS = {
  DONE: "s-ok", FINISHED: "s-ok", DONE_WITH_ERRORS: "s-warn", WAITING_FOR_LOGIN: "s-warn",
  FAILED: "s-bad", SUBMIT_FAILED: "s-bad", CANCELLED: "s-bad",
};
const pill = (s) => `<span class="pill ${STATUS_CLASS[s] || "s-run"}">${esc(s)}</span>`;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "/api/login") {
    const msg = (await res.json().catch(() => ({}))).detail || "Please log in again";
    if (path !== "/api/me") showLogin(msg);
    throw new Error(msg);
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data.detail || data || res.statusText);
  return data;
}

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}

// ------------------------------------------------------------------ auth
// The page sends the TAPIS credentials to the HARP backend (over HTTPS),
// which gets the token with tapipy and keeps it. The page never sees the
// token; it only holds an httpOnly session cookie.
const insecure = location.protocol !== "https:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);

function showLogin(reason = "") {
  $("#login-view").hidden = false; $("#app-view").hidden = true; $("#whoami").hidden = true;
  $("#login-reason").textContent = reason;
  clearInterval(state.expiryTimer);
}

async function showApp(me) {
  $("#login-view").hidden = true; $("#app-view").hidden = false; $("#whoami").hidden = false;
  $("#who").textContent = `${me.username} @ ${me.base_url.replace("https://", "")}`;
  state.expiresAt = me.expires_at;
  clearInterval(state.expiryTimer);
  updateExpiry(); state.expiryTimer = setInterval(updateExpiry, 30000);
  // The form is only hidden while logging in again, so nothing typed is lost.
  if (state.loaded) return;
  try {
    [state.systems, state.apps] = await Promise.all([api("/api/tapis/systems"), api("/api/tapis/apps")]);
    state.loaded = true;
  } catch (e) { toast(`Could not load TAPIS systems/apps: ${e.message}`); }
  fillStorageSystems();
  if (!$("#run-sets").children.length) addRunSet();
  if (!$("#targets").children.length) addTarget();
  refreshCampaigns();
}

// Running campaigns pause when the token expires, so warn early.
function updateExpiry() {
  if (!state.expiresAt) { $("#token-expiry").textContent = ""; return; }
  const mins = Math.round((state.expiresAt * 1000 - Date.now()) / 60000);
  if (mins <= 1) return showLogin("Your TAPIS token expired. Log in again — running campaigns resume automatically.");
  $("#token-expiry").textContent = mins >= 120 ? `token valid ${Math.floor(mins / 60)}h` : `token expires in ${mins} min`;
  $("#renew").hidden = mins > 30;
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  const f = Object.fromEntries(new FormData(ev.target));
  const body = { base_url: f.base_url.trim().replace(/\/+$/, "") };
  if (f.access_token.trim()) body.access_token = f.access_token.trim();
  else if (f.username && f.password) Object.assign(body, { username: f.username.trim(), password: f.password });
  else { $("#login-error").textContent = "Enter your username and password, or paste a token."; return; }
  const btn = ev.submitter; btn.disabled = true;
  try { showApp(await api("/api/login", { method: "POST", body })); }
  catch (e) { $("#login-error").textContent = e.message; }
  finally { btn.disabled = false; ev.target.password.value = ""; }
});
if (insecure) {
  $("#login-error").textContent = "This page is not using https://. Open it over https before logging in.";
  $("#login-form button[type=submit]").disabled = true;
}
$("#renew").onclick = () => showLogin("Renew your TAPIS token.");
$("#logout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  state.loaded = false;
  showLogin();
};

// ------------------------------------------------------------------ tabs
document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $("#tab-new").hidden = b.dataset.tab !== "new";
  $("#tab-campaigns").hidden = b.dataset.tab !== "campaigns";
  if (b.dataset.tab === "campaigns") refreshCampaigns();
}));
const openTab = (name) => document.querySelector(`.tabs button[data-tab="${name}"]`).click();

// -------------------------------------------------------------- run sets
function addRunSet(rs = { run_type: "SD", parameters: {} }) {
  const div = document.createElement("div");
  div.className = "item run-set";
  const lines = Object.entries(rs.parameters).map(([k, v]) => `${k} = ${v.join(", ")}`).join("\n");
  div.innerHTML = `
    <button class="danger remove" title="Remove">×</button>
    <label>Run type
      <select class="rs-type">
        <option value="SD">SD – scaled-down runs</option>
        <option value="FS">FS – full-scale runs</option>
        <option value="test_data">test_data – held-out runs</option>
      </select></label>
    <label>Parameters <textarea class="rs-params" rows="4" placeholder="method = pow, factorial&#10;n = 10, 1000"></textarea></label>`;
  $(".rs-type", div).value = rs.run_type;
  $(".rs-params", div).value = lines;
  $(".remove", div).onclick = () => div.remove();
  $("#run-sets").append(div);
}
$("#add-run-set").onclick = () => addRunSet();

function parseValue(v) {
  const t = v.trim();
  return t !== "" && !isNaN(Number(t)) ? Number(t) : t;
}
function parseParams(text) {
  const params = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const i = line.indexOf("=");
    if (i < 0) throw new Error(`Parameter line needs "name = values": ${line}`);
    params[line.slice(0, i).trim()] = line.slice(i + 1).split(",").map(parseValue).filter((v) => v !== "");
  }
  return params;
}

// --------------------------------------------------------------- targets
const systemOptions = (filter, selected) =>
  `<option value="">— choose —</option>` + state.systems.filter(filter).map((s) =>
    `<option value="${esc(s.id)}" ${s.id === selected ? "selected" : ""}>${esc(s.id)} (${esc(s.host)})</option>`).join("");

function addTarget(t = {}) {
  const div = document.createElement("div");
  div.className = "item target";
  const appKey = t.app_id ? `${t.app_id}@${t.app_version || "1.0.0"}` : "";
  const appOpts = `<option value="">— choose —</option>` + state.apps.map((a) => {
    const k = `${a.id}@${a.version}`;
    return `<option value="${esc(k)}" ${k === appKey ? "selected" : ""}>${esc(a.id)} v${esc(a.version)}</option>`;
  }).join("") + (appKey && !state.apps.some((a) => `${a.id}@${a.version}` === appKey)
    ? `<option value="${esc(appKey)}" selected>${esc(appKey)}</option>` : "");
  div.innerHTML = `
    <button class="danger remove" title="Remove">×</button>
    <div class="grid">
      <label>Execution system <select class="t-system">${systemOptions((s) => s.can_exec, t.system_id)}</select></label>
      <label>HARP app (container) <select class="t-app">${appOpts}</select></label>
      <label>Queue <select class="t-queue"><option value="">system default</option></select></label>
      <label>Scheduler options <input class="t-sched" placeholder="-A PAS0000" value="${esc(t.scheduler_options)}"></label>
      <label>Nodes <input class="t-nodes" type="number" min="1" value="${esc(t.node_count ?? 1)}"></label>
      <label>Cores per node <input class="t-cores" type="number" min="1" value="${esc(t.cores_per_node ?? 1)}"></label>
      <label>Memory (MB) <input class="t-mem" type="number" min="1" value="${esc(t.memory_mb ?? 4000)}"></label>
      <label>Max minutes per job <input class="t-min" type="number" min="1" value="${esc(t.max_minutes ?? 30)}"></label>
      <label>Max concurrent jobs <input class="t-conc" type="number" min="1" value="${esc(t.max_concurrent_jobs ?? 1)}">
        <small>Respect the queue's per-user job limit.</small></label>
      <label>Weight (share of jobs) <input class="t-weight" type="number" min="1" value="${esc(t.weight ?? 1)}"></label>
    </div>`;
  $(".remove", div).onclick = () => div.remove();
  const sysSel = $(".t-system", div);
  sysSel.onchange = () => loadQueues(div, sysSel.value);
  if (t.system_id) loadQueues(div, t.system_id, t.queue);
  $("#targets").append(div);
}
$("#add-target").onclick = () => addTarget();

async function loadQueues(div, systemId, selected) {
  const sel = $(".t-queue", div);
  sel.innerHTML = `<option value="">system default</option>`;
  if (!systemId) return;
  try {
    const sys = state.systemDetails[systemId] ||= await api(`/api/tapis/systems/${encodeURIComponent(systemId)}`);
    for (const q of sys.queues) {
      const o = new Option(`${q.name} (≤${q.max_cores ?? "?"} cores, ≤${q.max_minutes ?? "?"} min, ${q.max_jobs_per_user ?? "?"} jobs/user)`, q.name);
      sel.add(o);
    }
    if (selected) sel.value = selected;
  } catch (e) { toast(`Queues for ${systemId}: ${e.message}`); }
}

// --------------------------------------------------------------- storage
function fillStorageSystems(selected) {
  $("#f-storage-system").innerHTML = systemOptions(() => true, selected ?? $("#f-storage-system").value);
}

async function browse(path) {
  const system = $("#f-storage-system").value;
  if (!system) return toast("Choose a storage system first");
  $("#browser").hidden = false;
  $("#browser").dataset.path = path;
  $("#browser-path").textContent = `${system}:${path}`;
  const list = $("#browser-list");
  list.innerHTML = "<li>Loading…</li>";
  try {
    const items = await api(`/api/tapis/files?system_id=${encodeURIComponent(system)}&path=${encodeURIComponent(path)}`);
    const parent = path.replace(/\/[^/]+\/?$/, "") || "/";
    list.innerHTML = (path !== "/" ? `<li data-path="${esc(parent)}">⬑ ..</li>` : "") +
      items.filter((f) => f.type === "dir").map((f) => {
        const p = (path.replace(/\/$/, "") + "/" + f.name);
        return `<li data-path="${esc(p)}">📁 ${esc(f.name)}</li>`;
      }).join("");
    list.querySelectorAll("li[data-path]").forEach((li) => (li.onclick = () => browse(li.dataset.path)));
  } catch (e) { list.innerHTML = `<li class="error">${esc(e.message)}</li>`; }
}
$("#browse").onclick = () => browse($("#f-storage-path").value || "/");
$("#browser-use").onclick = () => { $("#f-storage-path").value = $("#browser").dataset.path; $("#browser").hidden = true; };
$("#browser-mkdir").onclick = async () => {
  const name = prompt("New folder name");
  if (!name) return;
  const path = $("#browser").dataset.path.replace(/\/$/, "") + "/" + name;
  try { await api("/api/tapis/mkdir", { method: "POST", body: { system_id: $("#f-storage-system").value, path } }); browse(path); }
  catch (e) { toast(e.message); }
};

// ------------------------------------------------------------ spec I/O
function collectSpec() {
  const num = (sel, root = document) => { const v = $(sel, root).value; return v === "" ? null : Number(v); };
  return {
    name: $("#f-name").value.trim(),
    application: $("#f-application").value.trim(),
    command: $("#f-command").value.trim(),
    workdir: $("#f-workdir").value.trim(),
    repetitions: num("#f-repetitions"),
    run_timeout_sec: num("#f-timeout"),
    combos_per_job: num("#f-combos"),
    distribution: $("#f-distribution").value,
    run_sets: [...document.querySelectorAll(".run-set")].map((d) => ({
      run_type: $(".rs-type", d).value, parameters: parseParams($(".rs-params", d).value),
    })),
    targets: [...document.querySelectorAll(".target")].map((d) => {
      const [app_id, app_version] = ($(".t-app", d).value || "@").split("@");
      return {
        system_id: $(".t-system", d).value, app_id, app_version, queue: $(".t-queue", d).value || null,
        scheduler_options: $(".t-sched", d).value.trim() || null,
        node_count: num(".t-nodes", d), cores_per_node: num(".t-cores", d), memory_mb: num(".t-mem", d),
        max_minutes: num(".t-min", d), max_concurrent_jobs: num(".t-conc", d), weight: num(".t-weight", d),
      };
    }),
    storage: { system_id: $("#f-storage-system").value, path: $("#f-storage-path").value.trim() },
  };
}

function loadSpec(s) {
  $("#f-name").value = s.name || ""; $("#f-application").value = s.application || "";
  $("#f-command").value = s.command || ""; $("#f-workdir").value = s.workdir || "";
  $("#f-repetitions").value = s.repetitions ?? 1; $("#f-timeout").value = s.run_timeout_sec ?? "";
  $("#f-combos").value = s.combos_per_job ?? 1; $("#f-distribution").value = s.distribution || "split";
  $("#run-sets").innerHTML = ""; (s.run_sets || []).forEach(addRunSet);
  $("#targets").innerHTML = ""; (s.targets || [{}]).forEach(addTarget);
  fillStorageSystems(s.storage?.system_id || "");
  $("#f-storage-path").value = s.storage?.path || "";
}

$("#load-example").onclick = () => loadSpec({
  name: "euler-sweep", application: "euler",
  command: "python3 calc_e.py {method} {n} {precision}", workdir: "/app/01-eulers_number",
  repetitions: 3, run_timeout_sec: 600, combos_per_job: 2, distribution: "split",
  run_sets: [
    { run_type: "SD", parameters: { method: ["pow", "factorial"], n: [10, 100, 1000], precision: [64] } },
    { run_type: "FS", parameters: { method: ["pow", "factorial"], n: [100000, 1000000], precision: [64, 512] } },
    { run_type: "test_data", parameters: { method: ["pow", "factorial"], n: [50000], precision: [256] } },
  ],
  targets: [{}], storage: {},
});
$("#export-spec").onclick = () => {
  try {
    const blob = new Blob([JSON.stringify(collectSpec(), null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "harp_sweep.json" });
    a.click(); URL.revokeObjectURL(a.href);
  } catch (e) { toast(e.message); }
};
$("#import-spec").onchange = async (ev) => {
  try { loadSpec(JSON.parse(await ev.target.files[0].text())); } catch (e) { toast(`Invalid JSON: ${e.message}`); }
  ev.target.value = "";
};

// ---------------------------------------------------- check / preview / launch
const out = (html) => ($("#form-output").innerHTML = html);
async function withBusy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { out(`<p class="error">${esc(e.message)}</p>`); } finally { btn.disabled = false; }
}

$("#check").onclick = (ev) => withBusy(ev.target, async () => {
  const spec = collectSpec();
  out("<p class='hint'>Testing access through TAPIS…</p>");
  const r = await api("/api/tapis/check", { method: "POST", body: { targets: spec.targets, storage: spec.storage } });
  const row = (name, ok, detail) => `<tr><td>${esc(name)}</td><td>${ok ? pill("OK") : pill("FAILED")}</td><td>${esc(detail)}</td></tr>`;
  out(`<table><tr><th>Location</th><th>Access</th><th>Detail</th></tr>
    ${r.targets.map((t) => row(`exec: ${t.system_id}`, t.ok, t.error || "system, credentials, queue and app reachable")).join("")}
    ${r.storage ? row(`storage: ${spec.storage.system_id}:${spec.storage.path}`, r.storage.ok,
      r.storage.error || `${r.storage.steps.join(" → ")} via TAPIS Files`) : row("storage", false, "choose a storage system and folder")}
  </table>`);
});

$("#preview").onclick = (ev) => withBusy(ev.target, async () => {
  const r = await api("/api/campaigns/preview", { method: "POST", body: collectSpec() });
  const s = r.summary;
  out(`<p><b>${s.combinations}</b> combinations → <b>${s.jobs}</b> TAPIS jobs → <b>${s.total_runs}</b> application runs</p>
    <table><tr><th>System</th><th>Jobs</th><th>Runs</th></tr>
    ${Object.entries(s.per_system).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.jobs}</td><td>${v.runs}</td></tr>`).join("")}</table>
    <details><summary>Job list</summary><table><tr><th>Job</th><th>System</th><th>Combinations</th></tr>
    ${r.jobs.map((j) => `<tr><td class="mono">${esc(j.name)}</td><td>${esc(j.system_id)}</td>
      <td class="mono">${j.combinations.map((c) => esc(JSON.stringify(c))).join("<br>")}</td></tr>`).join("")}</table></details>`);
});

$("#launch").onclick = (ev) => withBusy(ev.target, async () => {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  const c = await api("/api/campaigns", { method: "POST", body: collectSpec() });
  toast(`Campaign ${c.name} launched: ${c.jobs_total} TAPIS jobs`);
  out("");
  state.selected = c.id;
  openTab("campaigns");
});

// ------------------------------------------------------------- campaigns
async function refreshCampaigns() {
  let list;
  try { list = await api("/api/campaigns"); } catch { return; }
  for (const c of list) {
    const prev = state.lastStatus[c.id];
    if (prev && ACTIVE.includes(prev) && !ACTIVE.includes(c.status)) notifyDone(c);
    state.lastStatus[c.id] = c.status;
  }
  $("#campaign-list").innerHTML = list.length ? list.map((c) => `
    <li data-id="${esc(c.id)}" class="${c.id === state.selected ? "active" : ""}">
      <div><b>${esc(c.name)}</b> ${pill(c.status)}</div>
      <small>${esc(c.created_at)} · ${c.jobs_done}/${c.jobs_total} jobs</small>
    </li>`).join("") : `<li class="hint">No campaigns yet.</li>`;
  $("#campaign-list").querySelectorAll("li[data-id]").forEach((li) =>
    (li.onclick = () => { state.selected = li.dataset.id; refreshCampaigns(); }));
  if (state.selected) await renderCampaign(state.selected);
  clearTimeout(state.pollTimer);
  if (list.some((c) => ACTIVE.includes(c.status))) state.pollTimer = setTimeout(refreshCampaigns, 5000);
}

function notifyDone(c) {
  const msg = `HARP campaign ${c.name} is ${c.status}`;
  toast(msg, 8000);
  if ("Notification" in window && Notification.permission === "granted") new Notification(msg);
}

async function renderCampaign(id) {
  let c;
  try { c = await api(`/api/campaigns/${id}`); } catch (e) { $("#campaign-detail").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  const pct = c.jobs_total ? Math.round((100 * c.jobs_done) / c.jobs_total) : 0;
  const active = ACTIVE.includes(c.status);
  const banner = c.status === "DONE" || c.status === "DONE_WITH_ERRORS"
    ? `<div class="banner ${STATUS_CLASS[c.status]}"><b>Done.</b> ${c.result.rows} profiling rows from
       ${c.result.jobs_finished}/${c.result.jobs_total} jobs saved to
       <code>${esc(c.storage.system_id)}:${esc(c.result.csv_path)}</code>
       <a class="button" href="/api/campaigns/${esc(c.id)}/csv">Download CSV</a></div>`
    : c.status === "FAILED" ? `<div class="banner s-bad"><b>No profiling data was produced.</b> Check the job errors below.</div>`
    : c.status === "WAITING_FOR_LOGIN" ? `<div class="banner s-warn">Paused – the server lost your TAPIS session. Log in again to resume.</div>` : "";
  $("#campaign-detail").innerHTML = `
    <div class="row" style="justify-content:space-between">
      <h2>${esc(c.name)} ${pill(c.status)}</h2>
      <div class="row">
        ${active ? `<button class="danger" id="c-cancel">Cancel</button>` : ""}
        ${!active && Object.keys(c.job_counts).some((s) => ["FAILED", "CANCELLED", "SUBMIT_FAILED"].includes(s))
          ? `<button class="ghost" id="c-resubmit">Resubmit failed jobs</button>` : ""}
      </div>
    </div>
    ${banner}
    ${c.error ? `<p class="error">${esc(c.error)}</p>` : ""}
    <div>${c.jobs_done} / ${c.jobs_total} jobs ended · ${Object.entries(c.job_counts).map(([s, n]) => `${pill(s)} ${n}`).join(" ")}</div>
    <div class="progress"><div style="width:${pct}%"></div></div>
    <p class="hint">Results folder: <code>${esc(c.storage.system_id)}:${esc(c.campaign_dir)}</code></p>
    <details><summary>Activity</summary><div class="events">${c.events.slice().reverse().map((e) =>
      `<div><span class="hint">${esc(e.at)}</span> ${esc(e.message)}</div>`).join("")}</div></details>
    <table><tr><th>Job</th><th>System</th><th>Type</th><th>Combos</th><th>Status</th><th>TAPIS UUID</th></tr>
      ${c.jobs.map((j) => `<tr><td class="mono">${esc(j.name)}</td><td>${esc(j.system_id)}</td><td>${esc(j.run_type)}</td>
        <td>${j.combinations}</td><td>${pill(j.status)}${j.error ? `<div class="error">${esc(j.error)}</div>` : ""}</td>
        <td class="mono">${esc(j.uuid || "")}</td></tr>`).join("")}</table>`;
  $("#c-cancel")?.addEventListener("click", async () => {
    if (!confirm("Cancel all remaining TAPIS jobs of this campaign?")) return;
    try { await api(`/api/campaigns/${id}/cancel`, { method: "POST" }); refreshCampaigns(); } catch (e) { toast(e.message); }
  });
  $("#c-resubmit")?.addEventListener("click", async () => {
    try { const r = await api(`/api/campaigns/${id}/resubmit`, { method: "POST" }); toast(`Resubmitting ${r.resubmitted} jobs`); refreshCampaigns(); }
    catch (e) { toast(e.message); }
  });
}

// ------------------------------------------------------------------ boot
api("/api/me").then(showApp).catch(() => showLogin());
