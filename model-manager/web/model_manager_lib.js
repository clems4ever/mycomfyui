// Pure helpers for the Model Manager panel. Kept free of ComfyUI imports so
// they can be unit-tested in Node.

// LiteGraph modes for muted and bypassed nodes, which don't need their models.
export const INACTIVE_MODES = new Set([2, 4]);
export const ACTIVE = new Set(["queued", "downloading"]);
const STATUS_ORDER = { downloading: 0, queued: 1, paused: 2, error: 3, done: 4, cancelled: 5 };

export function formatBytes(n) {
  if (n == null) return "?";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e11 ? 0 : n >= 1e10 ? 1 : 2)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
  return `${n} B`;
}

export function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.ceil(seconds)} s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// Mirrors how the frontend finds a workflow's models: each node lists the models
// it can use in properties.models, and only the ones selected in a widget count.
export function collectModels(workflow) {
  const found = new Map();
  const add = (m) => {
    if (m?.name && m?.url && m?.directory) found.set(`${m.directory}/${m.name}`, m);
  };
  for (const graph of [workflow, ...(workflow.definitions?.subgraphs ?? [])]) {
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

// File names selected anywhere in a workflow, to flag installed models in use.
export function namesInUse(workflow) {
  const names = new Set();
  for (const graph of [workflow, ...(workflow.definitions?.subgraphs ?? [])]) {
    for (const node of graph.nodes ?? []) {
      for (const v of Object.values(node.widgets_values ?? {})) if (typeof v === "string") names.add(v);
    }
  }
  return names;
}

// Active downloads first, then the most recently finished.
export function sortJobs(jobs) {
  return [...jobs].sort((a, b) =>
    (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
    (b.finished ?? b.created) - (a.finished ?? a.created));
}

// Text and progress shown on a download card.
export function describeJob(job) {
  const fraction = job.status === "done" ? 1 : job.total ? Math.min(job.downloaded / job.total, 1) : 0;
  const sizes = !job.downloaded ? "" : job.total ? `${formatBytes(job.downloaded)} of ${formatBytes(job.total)}` : formatBytes(job.downloaded);
  const eta = job.speed && job.total ? formatDuration((job.total - job.downloaded) / job.speed) : "";
  const rate = job.status === "downloading"
    ? [job.speed ? `${(job.speed / 1e6).toFixed(1)} MB/s` : "starting…", eta && `${eta} left`].filter(Boolean).join(" · ")
    : "";
  const meta = {
    downloading: [`${Math.floor(fraction * 100)}%`, sizes],
    queued: ["Waiting for a free slot", job.total && formatBytes(job.total)],
    paused: ["Paused", sizes],
    error: ["Failed", sizes],
    done: ["Downloaded", formatBytes(job.total ?? job.downloaded)],
    cancelled: ["Cancelled"],
  }[job.status] ?? [job.status];
  return { fraction, meta: meta.filter(Boolean).join(" · "), rate };
}
