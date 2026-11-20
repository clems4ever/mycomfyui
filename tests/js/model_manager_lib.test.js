import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  collectModels, describeJob, formatBytes, formatDuration, namesInUse, sortJobs,
} from "../../model-manager/web/model_manager_lib.js";

const model = (name, directory = "vae") => ({ name, directory, url: `https://huggingface.co/x/${name}` });
const node = (values, models = [], mode = 0) => ({ mode, widgets_values: values, properties: { models } });

describe("formatBytes", () => {
  it("picks a unit and precision by magnitude", () => {
    assert.equal(formatBytes(null), "?");
    assert.equal(formatBytes(512), "512 B");
    assert.equal(formatBytes(520_212), "520 KB");
    assert.equal(formatBytes(334_641_190), "335 MB");
    assert.equal(formatBytes(4_850_000_000), "4.85 GB");
    assert.equal(formatBytes(19_530_000_000), "19.5 GB");
    assert.equal(formatBytes(140_000_000_000), "140 GB");
    assert.equal(formatBytes(1_820_000_000_000), "1.82 TB");
  });
});

describe("formatDuration", () => {
  it("is empty for unknown or finished durations", () => {
    assert.equal(formatDuration(0), "");
    assert.equal(formatDuration(-5), "");
    assert.equal(formatDuration(Infinity), "");
    assert.equal(formatDuration(NaN), "");
  });
  it("uses seconds, minutes, then hours", () => {
    assert.equal(formatDuration(4.2), "5 s");
    assert.equal(formatDuration(150), "3 min");
    assert.equal(formatDuration(3 * 3600 + 12 * 60), "3 h 12 min");
  });
});

describe("collectModels", () => {
  it("returns only models selected in a widget of an active node", () => {
    const wf = {
      nodes: [
        node(["a.safetensors"], [model("a.safetensors"), model("unselected.safetensors")]),
        node(["bypassed.safetensors"], [model("bypassed.safetensors")], 4),
        node(["muted.safetensors"], [model("muted.safetensors")], 2),
      ],
    };
    assert.deepEqual(collectModels(wf).map((m) => m.name), ["a.safetensors"]);
  });

  it("reads object-style widget values, subgraphs and workflow-level models, without duplicates", () => {
    const wf = {
      nodes: [node({ ckpt: "dict.safetensors" }, [model("dict.safetensors", "checkpoints")])],
      definitions: { subgraphs: [{ nodes: [node(["sub.safetensors"], [model("sub.safetensors", "loras")])] }] },
      models: [model("top.safetensors", "diffusion_models"), model("dict.safetensors", "checkpoints")],
    };
    assert.deepEqual(
      collectModels(wf).map((m) => `${m.directory}/${m.name}`),
      ["checkpoints/dict.safetensors", "loras/sub.safetensors", "diffusion_models/top.safetensors"],
    );
  });

  it("ignores incomplete model entries and empty workflows", () => {
    assert.deepEqual(collectModels({}), []);
    const wf = { nodes: [node(["x.safetensors"], [{ name: "x.safetensors", directory: "vae" }])] };
    assert.deepEqual(collectModels(wf), []);
  });
});

describe("namesInUse", () => {
  it("collects string widget values from all graphs", () => {
    const wf = {
      nodes: [node(["a.safetensors", 1.0, null])],
      definitions: { subgraphs: [{ nodes: [node({ lora: "b.safetensors" })] }] },
    };
    assert.deepEqual([...namesInUse(wf)].sort(), ["a.safetensors", "b.safetensors"]);
  });
});

describe("sortJobs", () => {
  it("puts active downloads first and recent finished ones before older", () => {
    const jobs = [
      { id: "old-done", status: "done", created: 1, finished: 10 },
      { id: "failed", status: "error", created: 2 },
      { id: "new-done", status: "done", created: 3, finished: 20 },
      { id: "running", status: "downloading", created: 4 },
      { id: "paused", status: "paused", created: 5 },
      { id: "waiting", status: "queued", created: 6 },
    ];
    assert.deepEqual(sortJobs(jobs).map((j) => j.id), ["running", "waiting", "paused", "failed", "new-done", "old-done"]);
    assert.equal(jobs[0].id, "old-done", "does not mutate its input");
  });
});

describe("describeJob", () => {
  const base = { downloaded: 104_000_000, total: 335_000_000, speed: 0 };

  it("shows percentage, sizes, speed and time left while downloading", () => {
    const d = describeJob({ ...base, status: "downloading", speed: 53_400_000 });
    assert.equal(d.meta, "31% · 104 MB of 335 MB");
    assert.equal(d.rate, "53.4 MB/s · 5 s left");
    assert.ok(Math.abs(d.fraction - 0.3104) < 0.001);
  });

  it("says starting before the first speed sample", () => {
    assert.equal(describeJob({ ...base, status: "downloading" }).rate, "starting…");
  });

  it("omits the time left when the size is unknown", () => {
    const d = describeJob({ status: "downloading", downloaded: 1_000_000, total: null, speed: 2_000_000 });
    assert.equal(d.rate, "2.0 MB/s");
    assert.equal(d.fraction, 0);
  });

  it("describes the other states", () => {
    assert.equal(describeJob({ ...base, status: "paused" }).meta, "Paused · 104 MB of 335 MB");
    assert.equal(describeJob({ status: "queued", downloaded: 0, total: 335_000_000 }).meta, "Waiting for a free slot · 335 MB");
    assert.equal(describeJob({ status: "error", downloaded: 0, total: null }).meta, "Failed");
    assert.equal(describeJob({ status: "cancelled", downloaded: 5, total: 10 }).meta, "Cancelled");
    const done = describeJob({ status: "done", downloaded: 335_000_000, total: 335_000_000 });
    assert.equal(done.meta, "Downloaded · 335 MB");
    assert.equal(done.fraction, 1);
    assert.equal(describeJob({ ...base, status: "paused" }).rate, "");
  });
});
