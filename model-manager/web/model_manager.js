import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAB_ID = "model-manager";
const TOGGLE_COMMAND = `Workspace.ToggleSidebarTab.${TAB_ID}`;
const DOWNLOAD_COMMAND = "ModelManager.DownloadMissing";
const STORAGE_REFRESH_MS = 5000;
// LiteGraph modes for muted and bypassed nodes, which don't need their models.
const INACTIVE_MODES = new Set([2, 4]);
const ACTIVE = new Set(["queued", "downloading"]);
const STATUS_ORDER = { downloading: 0, queued: 1, paused: 2, error: 3, done: 4, cancelled: 5 };

const state = {
  jobs: [],
  storage: null,
  missing: [], // missing models of the open workflow
  search: "",
  sort: "size",
  collapsed: new Set(),
};
// Missing-model sets already offered, so switching tabs doesn't ask again.
const offered = new Set();
const lastStatus = new Map();
let ui = null; // DOM of the sidebar panel while it is open
let storageTimer = null;

// ---------- helpers ----------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

const icon = (name) => h("i", { class: `pi pi-${name}`, "aria-hidden": "true" });

function formatBytes(n) {
  if (n == null) return "?";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : n >= 1e10 ? 1 : 2)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
  return `${n} B`;
}

function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.ceil(seconds)} s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function toast(severity, summary, detail, life = 5000) {
  app.extensionManager?.toast?.add({ severity, summary, detail, life });
}

async function confirmDialog(title, message, type) {
  const dialog = app.extensionManager?.dialog;
  if (dialog?.confirm) return (await dialog.confirm({ title, message, type })) === true;
  return window.confirm(`${title}\n\n${message}`);
}

async function request(path, body) {
  const resp = await api.fetchApi(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

async function refreshNodeDefinitions() {
  // Updates the model dropdowns and clears the frontend's "missing model" errors.
  const command = app.extensionManager?.command;
  if (command?.execute) await command.execute("Comfy.RefreshNodeDefinitions");
  else await app.refreshComboInNodes?.();
}

function openPanel() {
  const tabs = app.extensionManager?.sidebarTab;
  if (tabs?.activeSidebarTabId === TAB_ID) return;
  if (tabs?.toggleSidebarTab) tabs.toggleSidebarTab(TAB_ID);
  else app.extensionManager?.command?.execute(TOGGLE_COMMAND);
}

// ---------- workflow inspection ----------

function workflowGraphs() {
  const wf = (app.rootGraph ?? app.graph).serialize();
  return [wf, ...(wf.definitions?.subgraphs ?? [])];
}

// Mirrors how the frontend finds a workflow's models: each node lists the models
// it can use in properties.models, and only the ones selected in a widget count.
export function collectModels(workflow) {
  const found = new Map();
  const add = (m) => {
    if (m?.name && m?.url && m?.directory) found.set(`${m.directory}/${m.name}`, m);
  };
  const graphs = [workflow, ...(workflow.definitions?.subgraphs ?? [])];
  for (const graph of graphs) {
    for (const node of graph.nodes ?? []) {
      if (INACTIVE_MODES.has(node.mode)) continue;
      const values = Object.values(node.widgets_values ?? {});
      for (const m of node.properties?.models ?? []) {
        if (values.includes(m.name)) add(m);
      }
    }
  }
  for (const m of workflow.models ?? []) add(m);
  return [...found.values()];
}

// File names selected anywhere in the open workflow, to flag models in use.
function namesInUse() {
  const names = new Set();
  for (const graph of workflowGraphs()) {
    for (const node of graph.nodes ?? []) {
      for (const v of Object.values(node.widgets_values ?? {})) if (typeof v === "string") names.add(v);
    }
  }
  return names;
}

// ---------- server state ----------

async function loadJobs() {
  const { jobs } = await request("/model_manager/jobs");
  for (const j of jobs) lastStatus.set(j.id, j.status);
  state.jobs = jobs;
  renderDownloads();
}

async function loadStorage() {
  state.storage = await request("/model_manager/storage");
  renderStorage();
  renderModels();
}

async function checkMissing() {
  const models = collectModels((app.rootGraph ?? app.graph).serialize());
  const result = models.length ? await request("/model_manager/check", { models }) : { missing: [], errors: [] };
  state.missing = result.missing;
  renderMissing();
  return result;
}

function onJobsUpdate(jobs) {
  let finished = false;
  for (const job of jobs) {
    const prev = lastStatus.get(job.id);
    if (prev && prev !== job.status) {
      if (job.status === "done") {
        toast("success", "Model downloaded", `${job.directory}/${job.name}`);
        finished = true;
      } else if (job.status === "error") {
        toast("error", `Download failed: ${job.name}`, job.error, 0);
      }
    }
    lastStatus.set(job.id, job.status);
  }
  state.jobs = jobs;
  renderDownloads();
  if (finished) {
    refreshNodeDefinitions();
    if (ui) loadStorage().catch(console.error);
    checkMissing().catch(console.error);
  }
  scheduleStorageRefresh();
}

// Disk usage changes while downloading; keep it current while the panel is open.
function scheduleStorageRefresh() {
  const busy = state.jobs.some((j) => ACTIVE.has(j.status));
  if (ui && busy && !storageTimer) {
    storageTimer = setInterval(() => loadStorage().catch(console.error), STORAGE_REFRESH_MS);
  } else if ((!ui || !busy) && storageTimer) {
    clearInterval(storageTimer);
    storageTimer = null;
  }
}

async function startDownloads(models) {
  const { started, errors } = await request("/model_manager/download", { models });
  for (const err of errors) toast("error", "Model can't be downloaded", err, 0);
  if (started.length) openPanel();
}

async function jobAction(id, action) {
  try {
    await request(`/model_manager/jobs/${id}/${action}`, {});
  } catch (e) {
    toast("error", "Download manager", e.message);
  }
}

async function downloadMissing({ askFirst = false } = {}) {
  let result;
  try {
    result = await checkMissing();
  } catch (e) {
    toast("error", "Could not check models", String(e), 0);
    return;
  }
  for (const err of result.errors) toast("error", "Model can't be downloaded", err, 0);
  const toStart = result.missing.filter((m) => !m.downloading);
  if (!toStart.length) {
    if (!askFirst) openPanel();
    return;
  }
  if (askFirst) {
    const key = toStart.map((m) => `${m.directory}/${m.name}`).sort().join("|");
    if (offered.has(key)) return;
    offered.add(key);
    const total = toStart.reduce((sum, m) => sum + (m.size ?? 0), 0);
    const list = toStart.map((m) => `• ${m.directory}/${m.name} (${formatBytes(m.size)})`).join("\n");
    const ok = await confirmDialog(
      "Download missing models to the server?",
      `This workflow needs ${toStart.length} model(s) that aren't on the server (${formatBytes(total)}):\n\n${list}`,
    );
    if (!ok) return;
  }
  await startDownloads(toStart);
}

async function deleteModel(model, inUse) {
  const ok = await confirmDialog(
    "Delete model?",
    `${model.directory}/${model.name}\n\nThis frees ${formatBytes(model.size)} on the server.` +
      (inUse ? "\n\nThe open workflow uses this model; it will need to be downloaded again." : ""),
    "delete",
  );
  if (!ok) return;
  try {
    const { freed } = await request("/model_manager/models/delete", { directory: model.directory, name: model.name });
    toast("success", "Model deleted", `${model.name} · ${formatBytes(freed)} freed`);
  } catch (e) {
    toast("error", "Could not delete model", e.message, 0);
  }
  await loadStorage().catch(console.error);
  await refreshNodeDefinitions();
  checkMissing().catch(console.error);
}

// ---------- rendering ----------

function renderStorage() {
  if (!ui || !state.storage) return;
  const { disk, models_bytes: models, pending_bytes: pending } = state.storage;
  const other = Math.max(disk.used - models, 0);
  const reserved = Math.min(pending, disk.free);
  const pct = (n) => `${(100 * n) / disk.total}%`;
  const short = pending > disk.free;
  const low = !short && disk.free / disk.total < 0.1;

  const legendRow = (cls, label, value, title) =>
    h("div", { class: "mm-legend-row", title }, h("span", { class: `mm-swatch ${cls}` }), h("span", {}, label), h("span", { class: "mm-num" }, value));

  ui.storage.replaceChildren(...[
    h("div", { class: "mm-hero" },
      h("span", { class: "mm-hero-value" }, formatBytes(disk.free)),
      h("span", { class: "mm-hero-label" }, `free of ${formatBytes(disk.total)}`)),
    h("div", { class: "mm-meter", role: "img", "aria-label": `Disk: ${formatBytes(models)} models, ${formatBytes(other)} other files, ${formatBytes(disk.free)} free` },
      models > 0 && h("div", { class: "mm-seg mm-seg-models", style: `width:${pct(models)}`, title: `Models: ${formatBytes(models)}` }),
      other > 0 && h("div", { class: "mm-seg mm-seg-other", style: `width:${pct(other)}`, title: `Other files: ${formatBytes(other)}` }),
      reserved > 0 && h("div", { class: "mm-seg mm-seg-pending", style: `width:${pct(reserved)}`, title: `Still to download: ${formatBytes(pending)}` })),
    h("div", { class: "mm-legend" },
      legendRow("mm-seg-models", "Models", formatBytes(models)),
      legendRow("mm-seg-other", "Other files", formatBytes(other), "Everything else on the same disk"),
      pending > 0 && legendRow("mm-seg-pending", "Still to download", formatBytes(pending)),
      legendRow("mm-seg-free", "Free", formatBytes(disk.free))),
    short && h("div", { class: "mm-status mm-status-critical" }, icon("times-circle"),
      `Not enough space: downloads need ${formatBytes(pending)}, only ${formatBytes(disk.free)} free. Delete models below to make room.`),
    low && h("div", { class: "mm-status mm-status-warning" }, icon("exclamation-triangle"), "Disk is almost full."),
  ].filter(Boolean));
}

function renderMissing() {
  if (!ui) return;
  const pending = state.missing.filter((m) => !m.downloading);
  if (!pending.length) return ui.missing.replaceChildren();
  const total = pending.reduce((s, m) => s + (m.size ?? 0), 0);
  ui.missing.replaceChildren(
    h("div", { class: "mm-banner" },
      h("div", {},
        h("div", { class: "mm-banner-title" }, `This workflow needs ${pending.length} more model${pending.length > 1 ? "s" : ""}`),
        h("div", { class: "mm-muted" }, `${formatBytes(total)} · ${pending.map((m) => m.name).join(", ")}`)),
      h("button", { class: "mm-btn mm-btn-primary", onclick: () => startDownloads(pending).catch((e) => toast("error", "Download", e.message)) },
        icon("download"), "Download")),
  );
}

function jobElement(job) {
  const el = h("div", { class: "mm-job", "data-id": job.id });
  el.innerHTML = `
    <div class="mm-job-top"><span class="mm-job-name"></span><span class="mm-chip"></span><span class="mm-job-actions"></span></div>
    <div class="mm-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="mm-progress-fill"></div></div>
    <div class="mm-job-bottom"><span class="mm-job-meta mm-num"></span><span class="mm-job-rate mm-num"></span></div>
    <div class="mm-job-error"></div>`;
  el.querySelector(".mm-job-actions").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (btn) jobAction(el.dataset.id, btn.dataset.action);
  });
  return el;
}

const JOB_BUTTONS = {
  downloading: [["pause", "pause", "Pause"], ["cancel", "times", "Cancel"]],
  queued: [["pause", "pause", "Pause"], ["cancel", "times", "Cancel"]],
  paused: [["resume", "play", "Resume"], ["cancel", "times", "Cancel"]],
  error: [["resume", "refresh", "Retry"], ["cancel", "times", "Discard"]],
  done: [["remove", "eye-slash", "Hide"]],
  cancelled: [["remove", "eye-slash", "Hide"]],
};

function updateJobElement(el, job) {
  el.dataset.status = job.status;
  const name = el.querySelector(".mm-job-name");
  name.textContent = job.name;
  name.title = `${job.directory}/${job.name}`;
  el.querySelector(".mm-chip").textContent = job.directory;

  const frac = job.total ? Math.min(job.downloaded / job.total, 1) : 0;
  const fill = el.querySelector(".mm-progress-fill");
  fill.style.width = `${(job.status === "done" ? 1 : frac) * 100}%`;
  el.querySelector(".mm-progress").setAttribute("aria-valuenow", Math.round(frac * 100));

  const sizes = !job.downloaded ? "" : job.total ? `${formatBytes(job.downloaded)} of ${formatBytes(job.total)}` : formatBytes(job.downloaded);
  const eta = job.speed ? formatDuration((job.total - job.downloaded) / job.speed) : "";
  const rate = job.status === "downloading"
    ? [job.speed ? `${(job.speed / 1e6).toFixed(1)} MB/s` : "starting…", eta && `${eta} left`].filter(Boolean).join(" · ")
    : "";
  el.querySelector(".mm-job-rate").textContent = rate;
  const meta = {
    downloading: [`${Math.floor(frac * 100)}%`, sizes],
    queued: ["Waiting for a free slot", job.total && formatBytes(job.total)],
    paused: ["Paused", sizes],
    error: ["Failed", sizes],
    done: ["Downloaded", formatBytes(job.total ?? job.downloaded)],
    cancelled: ["Cancelled"],
  }[job.status] ?? [job.status];
  el.querySelector(".mm-job-meta").textContent = meta.filter(Boolean).join(" · ");
  el.querySelector(".mm-job-error").textContent = job.status === "error" ? job.error : "";

  const actions = el.querySelector(".mm-job-actions");
  const key = job.status;
  if (actions.dataset.for !== key) {
    actions.dataset.for = key;
    actions.replaceChildren(...(JOB_BUTTONS[key] ?? []).map(([action, ic, label]) =>
      h("button", { class: `mm-icon-btn${action === "cancel" ? " mm-danger" : ""}`, "data-action": action, title: label, "aria-label": `${label} ${job.name}` }, icon(ic))));
  }
}

function renderDownloads() {
  if (!ui) return;
  const jobs = [...state.jobs].sort((a, b) =>
    (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || (b.finished ?? b.created) - (a.finished ?? a.created));
  const active = jobs.filter((j) => ACTIVE.has(j.status)).length;
  const finished = jobs.filter((j) => j.status === "done" || j.status === "cancelled").length;
  ui.downloadsCount.textContent = active ? `${active} active` : "";
  ui.clearBtn.hidden = !finished;

  const existing = new Map([...ui.jobs.children].map((el) => [el.dataset.id, el]));
  const ordered = jobs.map((job) => {
    const el = existing.get(job.id) ?? jobElement(job);
    updateJobElement(el, job);
    return el;
  });
  // Re-append only when the order changed, so buttons don't move under the cursor.
  const current = [...ui.jobs.children];
  if (ordered.length !== current.length || ordered.some((el, i) => el !== current[i])) ui.jobs.replaceChildren(...ordered);
  ui.jobsEmpty.hidden = jobs.length > 0;
}

function renderModels() {
  if (!ui || !state.storage) return;
  const inUse = namesInUse();
  const query = state.search.trim().toLowerCase();
  const models = state.storage.models.filter((m) => !query || `${m.directory}/${m.name}`.toLowerCase().includes(query));
  const largest = Math.max(1, ...state.storage.models.map((m) => m.size));
  const sorters = {
    size: (a, b) => b.size - a.size,
    name: (a, b) => a.name.localeCompare(b.name),
    recent: (a, b) => b.modified - a.modified,
  };
  const groups = new Map();
  for (const m of models) {
    if (!groups.has(m.directory)) groups.set(m.directory, []);
    groups.get(m.directory).push(m);
  }
  const groupList = [...groups.entries()]
    .map(([dir, items]) => ({ dir, items: items.sort(sorters[state.sort]), size: items.reduce((s, m) => s + m.size, 0) }))
    .sort((a, b) => (state.sort === "name" ? a.dir.localeCompare(b.dir) : b.size - a.size));

  ui.modelsSummary.textContent = `${state.storage.models.length} files · ${formatBytes(state.storage.models_bytes)}`;
  if (!groupList.length) {
    ui.models.replaceChildren(h("div", { class: "mm-empty" },
      query ? "No models match your search." : "No models installed yet. Open a workflow and download its models, or add files to the models folder."));
    return;
  }
  ui.models.replaceChildren(...groupList.map(({ dir, items, size }) => {
    const collapsed = state.collapsed.has(dir) && !query;
    return h("div", { class: "mm-group" },
      h("button", {
        class: "mm-group-head", "aria-expanded": String(!collapsed),
        onclick: () => { state.collapsed.has(dir) ? state.collapsed.delete(dir) : state.collapsed.add(dir); renderModels(); },
      },
        icon(collapsed ? "chevron-right" : "chevron-down"),
        h("span", { class: "mm-group-name" }, dir),
        h("span", { class: "mm-muted mm-num" }, `${items.length} · ${formatBytes(size)}`)),
      !collapsed && h("div", { class: "mm-group-body" }, ...items.map((m) => {
        const used = inUse.has(m.name);
        return h("div", { class: "mm-model" },
          h("div", { class: "mm-model-main" },
            h("div", { class: "mm-model-line" },
              h("span", { class: "mm-model-name", title: `${m.directory}/${m.name}` }, m.name),
              used && h("span", { class: "mm-badge", title: "Selected in the open workflow" }, "in use")),
            h("div", { class: "mm-sizebar", title: formatBytes(m.size) },
              h("div", { class: "mm-sizebar-fill", style: `width:${Math.max((100 * m.size) / largest, 1)}%` }))),
          h("span", { class: "mm-model-size mm-num" }, formatBytes(m.size)),
          h("button", { class: "mm-icon-btn mm-danger", title: "Delete", "aria-label": `Delete ${m.name}`, onclick: () => deleteModel(m, used) }, icon("trash")));
      })));
  }));
}

function renderPanel(container) {
  injectStyles();
  const search = h("input", { class: "mm-input", type: "search", placeholder: "Search models", "aria-label": "Search models", value: state.search });
  search.addEventListener("input", () => { state.search = search.value; renderModels(); });
  const sort = h("select", { class: "mm-input mm-select", "aria-label": "Sort models" },
    h("option", { value: "size" }, "Largest first"), h("option", { value: "name" }, "Name"), h("option", { value: "recent" }, "Recently added"));
  sort.value = state.sort;
  sort.addEventListener("change", () => { state.sort = sort.value; renderModels(); });

  ui = {
    storage: h("div", { class: "mm-card mm-storage" }, h("div", { class: "mm-muted" }, "Loading disk usage…")),
    missing: h("div"),
    downloadsCount: h("span", { class: "mm-muted" }),
    clearBtn: h("button", { class: "mm-btn mm-btn-ghost", onclick: () => request("/model_manager/jobs/clear", {}).catch(console.error) }, "Clear finished"),
    jobs: h("div", { class: "mm-jobs" }),
    jobsEmpty: h("div", { class: "mm-empty" }, "No downloads. Open a workflow with missing models to download them here."),
    modelsSummary: h("span", { class: "mm-muted mm-num" }),
    models: h("div", { class: "mm-models" }),
  };

  container.replaceChildren(h("div", { class: "mm-root" },
    h("div", { class: "mm-header" },
      h("h2", {}, "Model Manager"),
      h("button", { class: "mm-icon-btn", title: "Refresh", "aria-label": "Refresh", onclick: () => { loadStorage(); loadJobs(); checkMissing(); } }, icon("refresh"))),
    h("div", { class: "mm-section-title" }, h("h3", {}, "Storage"), h("span", { class: "mm-muted" }, "disk holding the models folder")),
    ui.storage,
    h("div", { class: "mm-section-title" }, h("h3", {}, "Downloads"), ui.downloadsCount, h("span", { class: "mm-spacer" }), ui.clearBtn),
    ui.missing, ui.jobs, ui.jobsEmpty,
    h("div", { class: "mm-section-title" }, h("h3", {}, "Installed models"), h("span", { class: "mm-spacer" }), ui.modelsSummary),
    h("div", { class: "mm-toolbar" }, search, sort),
    ui.models,
  ));
  renderDownloads();
  renderMissing();
  loadStorage().catch((e) => ui && ui.storage.replaceChildren(h("div", { class: "mm-status mm-status-critical" }, icon("times-circle"), `Could not load storage: ${e.message}`)));
  loadJobs().catch(console.error);
  checkMissing().catch(console.error);
  scheduleStorageRefresh();
}

let stylesInjected = false;
function injectStyles() {
  if (stylesInjected) return;
  stylesInjected = true;
  document.head.append(h("style", {}, `
.mm-root {
  --mm-surface: var(--card, var(--comfy-menu-secondary-bg, #262729));
  --mm-text: var(--foreground, var(--fg-color, #fff));
  --mm-muted: var(--muted-foreground, var(--descrip-text, #8a8a8a));
  --mm-border: var(--border, var(--border-color, #505051));
  --mm-accent: var(--primary, #0b8ce9);
  --mm-accent-track: color-mix(in srgb, var(--mm-accent) 22%, transparent);
  --mm-neutral: var(--mm-muted);
  --mm-good: #0ca30c;
  --mm-warning: var(--warning, #fab219);
  --mm-critical: var(--destructive, #d03b3b);
  display: flex; flex-direction: column; gap: 10px; padding: 12px 14px 24px;
  color: var(--mm-text); font-size: 13px; line-height: 1.4; height: 100%; overflow-y: auto; box-sizing: border-box;
}
.mm-root * { box-sizing: border-box; }
.mm-header { display: flex; align-items: center; justify-content: space-between; }
.mm-header h2 { margin: 0; font-size: 15px; font-weight: 600; }
.mm-section-title { display: flex; align-items: baseline; gap: 8px; margin-top: 10px; }
.mm-section-title h3 { margin: 0; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--mm-muted); }
.mm-spacer { flex: 1; }
.mm-muted { color: var(--mm-muted); font-size: 12px; }
.mm-num { font-variant-numeric: tabular-nums; }
.mm-card { background: var(--mm-surface); border: 1px solid var(--mm-border); border-radius: 10px; padding: 14px; }
.mm-hero { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.mm-hero-value { font-size: 48px; font-weight: 600; line-height: 1; letter-spacing: -.02em; }
.mm-hero-label { color: var(--mm-muted); }
.mm-meter { display: flex; gap: 2px; height: 12px; border-radius: 4px; background: var(--mm-accent-track); overflow: hidden; }
.mm-seg { height: 100%; min-width: 3px; }
.mm-seg:first-child { border-radius: 4px 0 0 4px; }
.mm-seg-models { background: var(--mm-accent); }
.mm-seg-other { background: var(--mm-neutral); }
.mm-seg-pending, .mm-swatch.mm-seg-pending {
  background: repeating-linear-gradient(45deg, var(--mm-accent) 0 2px, transparent 2px 5px);
  box-shadow: inset 0 0 0 1px var(--mm-accent);
}
.mm-seg-free { background: var(--mm-accent-track); }
.mm-legend { display: grid; gap: 4px; margin-top: 10px; }
.mm-legend-row { display: grid; grid-template-columns: 12px 1fr auto; align-items: center; gap: 8px; color: var(--mm-muted); }
.mm-legend-row .mm-num { color: var(--mm-text); }
.mm-swatch { width: 10px; height: 10px; border-radius: 3px; }
.mm-status { display: flex; gap: 8px; align-items: flex-start; margin-top: 10px; padding: 8px 10px; border-radius: 8px; font-size: 12px; }
.mm-status i { margin-top: 2px; }
.mm-status-warning { background: color-mix(in srgb, var(--mm-warning) 14%, transparent); }
.mm-status-warning i { color: var(--mm-warning); }
.mm-status-critical { background: color-mix(in srgb, var(--mm-critical) 16%, transparent); }
.mm-status-critical i { color: var(--mm-critical); }
.mm-banner { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 10px;
  border: 1px dashed var(--mm-accent); background: color-mix(in srgb, var(--mm-accent) 8%, transparent); }
.mm-banner > div { flex: 1; min-width: 0; }
.mm-banner .mm-muted { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mm-banner-title { font-weight: 600; }
.mm-btn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; border: 1px solid transparent; padding: 5px 10px;
  font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap; }
.mm-btn-primary { background: var(--mm-accent); color: #fff; }
.mm-btn-primary:hover { filter: brightness(1.1); }
.mm-btn-ghost { background: none; color: var(--mm-muted); padding: 2px 6px; }
.mm-btn-ghost:hover { color: var(--mm-text); }
.mm-icon-btn { display: inline-grid; place-items: center; width: 26px; height: 26px; border-radius: 6px; border: none;
  background: none; color: var(--mm-muted); cursor: pointer; flex: none; }
.mm-icon-btn:hover, .mm-icon-btn:focus-visible { background: color-mix(in srgb, var(--mm-text) 10%, transparent); color: var(--mm-text); }
.mm-icon-btn.mm-danger:hover, .mm-icon-btn.mm-danger:focus-visible { color: var(--mm-critical); }
.mm-jobs { display: flex; flex-direction: column; gap: 8px; }
.mm-job { background: var(--mm-surface); border: 1px solid var(--mm-border); border-radius: 10px; padding: 10px 12px; display: grid; gap: 7px; }
.mm-job-top, .mm-job-bottom { display: flex; align-items: center; gap: 8px; min-width: 0; }
.mm-job-name { flex: 1; min-width: 0; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mm-chip { font-size: 11px; padding: 1px 7px; border-radius: 999px; color: var(--mm-muted); border: 1px solid var(--mm-border); white-space: nowrap; }
.mm-job-meta, .mm-job-rate { color: var(--mm-muted); font-size: 12px; white-space: nowrap; }
.mm-job-meta { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.mm-job-rate { color: var(--mm-text); }
.mm-job-actions { display: flex; gap: 2px; margin: -5px -6px -5px 0; }
.mm-job-error { color: var(--mm-critical); font-size: 12px; }
.mm-job-error:empty { display: none; }
.mm-progress { height: 6px; border-radius: 4px; background: var(--mm-accent-track); overflow: hidden; }
.mm-progress-fill { height: 100%; border-radius: 4px; background: var(--mm-accent); transition: width .45s ease-out; position: relative; overflow: hidden; }
.mm-job[data-status="downloading"] .mm-progress-fill::after {
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.35), transparent);
  animation: mm-shimmer 1.6s linear infinite; transform: translateX(-100%);
}
@keyframes mm-shimmer { to { transform: translateX(100%); } }
@media (prefers-reduced-motion: reduce) {
  .mm-progress-fill { transition: none; }
  .mm-job[data-status="downloading"] .mm-progress-fill::after { animation: none; display: none; }
}
.mm-job[data-status="queued"] .mm-progress-fill, .mm-job[data-status="paused"] .mm-progress-fill { background: var(--mm-neutral); }
.mm-job[data-status="error"] .mm-progress-fill { background: var(--mm-critical); }
.mm-job[data-status="done"] .mm-progress-fill { background: var(--mm-good); }
.mm-job[data-status="done"] .mm-job-meta::before { content: "\\2713  "; color: var(--mm-good); }
.mm-job[data-status="done"], .mm-job[data-status="cancelled"] { opacity: .72; }
.mm-job[data-status="cancelled"] .mm-progress { display: none; }
.mm-empty { color: var(--mm-muted); font-size: 12px; padding: 10px 2px; }
.mm-empty[hidden] { display: none; }
.mm-toolbar { display: flex; gap: 6px; }
.mm-input { flex: 1; min-width: 0; height: 30px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--mm-border);
  background: var(--comfy-input-bg, transparent); color: var(--mm-text); font: inherit; font-size: 12px; }
.mm-select { flex: none; width: auto; padding-right: 6px; }
.mm-input:focus { outline: 2px solid var(--mm-accent); outline-offset: -1px; }
.mm-models { display: flex; flex-direction: column; gap: 6px; }
.mm-group { border: 1px solid var(--mm-border); border-radius: 10px; overflow: hidden; background: var(--mm-surface); }
.mm-group-head { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px 10px; background: none; border: none;
  color: var(--mm-text); font: inherit; cursor: pointer; text-align: left; }
.mm-group-head:hover { background: color-mix(in srgb, var(--mm-text) 5%, transparent); }
.mm-group-head i { font-size: 10px; color: var(--mm-muted); }
.mm-group-name { flex: 1; font-weight: 600; }
.mm-group-body { border-top: 1px solid var(--mm-border); }
.mm-model { display: flex; align-items: center; gap: 8px; padding: 7px 6px 7px 12px; }
.mm-model + .mm-model { border-top: 1px solid color-mix(in srgb, var(--mm-border) 50%, transparent); }
.mm-model:hover { background: color-mix(in srgb, var(--mm-text) 4%, transparent); }
.mm-model-main { flex: 1; min-width: 0; display: grid; gap: 5px; }
.mm-model-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.mm-model-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mm-badge { flex: none; font-size: 10px; padding: 0 6px; border-radius: 999px; color: var(--mm-accent);
  border: 1px solid color-mix(in srgb, var(--mm-accent) 60%, transparent); }
.mm-sizebar { height: 3px; border-radius: 2px; background: var(--mm-accent-track); overflow: hidden; }
.mm-sizebar-fill { height: 100%; border-radius: 2px; background: var(--mm-accent); }
.mm-model-size { flex: none; font-size: 12px; color: var(--mm-muted); min-width: 58px; text-align: right; }
`));
}

// ---------- registration ----------

app.registerExtension({
  name: "ModelManager",
  commands: [
    { id: DOWNLOAD_COMMAND, label: "Download missing models to server", icon: "pi pi-download", function: () => downloadMissing() },
  ],
  menuCommands: [{ path: ["Extensions"], commands: [DOWNLOAD_COMMAND, TOGGLE_COMMAND] }],
  actionBarButtons: [
    {
      icon: "icon-[lucide--download] size-4",
      label: "Models",
      tooltip: "Model Manager: downloads, disk space and installed models",
      onClick: () => app.extensionManager?.command?.execute(TOGGLE_COMMAND),
    },
  ],
  async setup() {
    app.extensionManager.registerSidebarTab({
      id: TAB_ID,
      icon: "pi pi-server",
      title: "Model Manager",
      tooltip: "Model Manager",
      label: "Manager",
      type: "custom",
      render: (el) => renderPanel(el),
      destroy: () => {
        ui = null;
        scheduleStorageRefresh();
      },
    });
    api.addEventListener("model_manager.jobs", ({ detail }) => onJobsUpdate(detail.jobs));
    api.addEventListener("reconnected", () => loadJobs().catch(console.error));
    loadJobs().catch(console.error);
  },
  async afterConfigureGraph() {
    // Let the workflow finish loading before inspecting it.
    setTimeout(() => {
      downloadMissing({ askFirst: true }).catch(console.error);
      renderModels();
    }, 500);
  },
});
