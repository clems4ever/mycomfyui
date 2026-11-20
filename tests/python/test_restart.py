import json

from conftest import allow_test_server, job_named, load_extension, wait_for


def save_state(comfy, records):
    state = comfy.user / "__model_manager" / "downloads.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(records))


def record(url, name, status, **extra):
    return {
        "id": name,
        "url": url,
        "directory": "vae",
        "name": name,
        "status": status,
        "downloaded": 0,
        "total": None,
        "error": None,
        "created": 1.0,
        "finished": None,
        **extra,
    }


def restart():
    """Import the extension again, as ComfyUI does on startup, allowing the test server."""
    mm = load_extension()
    allow_test_server(mm)
    mm.jobs.clear()
    mm._resume_after_restart = mm._load()
    return mm


async def test_interrupted_downloads_resume_after_restart(comfy, files):
    url, data = files.add("running.safetensors", 800_000)
    queued_url, queued_data = files.add("queued.safetensors", 2000)
    vae = comfy.models / "vae"
    vae.mkdir(parents=True)
    (vae / "running.safetensors.part").write_bytes(data[:300_000])
    save_state(
        comfy,
        [
            record(url, "running.safetensors", "downloading", downloaded=250_000),
            record(queued_url, "queued.safetensors", "queued"),
            record(url, "paused.safetensors", "paused"),
            record(url, "failed.safetensors", "error", error="boom"),
            record(url, "finished.safetensors", "done", finished=2.0),
        ],
    )

    mm = restart()
    assert sorted(j["name"] for j in mm._resume_after_restart) == ["queued.safetensors", "running.safetensors"]
    assert job_named(mm, "running.safetensors")["downloaded"] == 300_000, "progress comes from the partial file"

    await mm._resume_downloads(mm.server.app)
    running = job_named(mm, "running.safetensors")
    await wait_for(lambda: running["status"] == "done" and job_named(mm, "queued.safetensors")["status"] == "done")
    assert (vae / "running.safetensors").read_bytes() == data
    assert (vae / "queued.safetensors").read_bytes() == queued_data
    assert ("GET", "running.safetensors", "bytes=300000-") in files.requests
    assert job_named(mm, "paused.safetensors")["status"] == "paused"
    assert job_named(mm, "failed.safetensors")["status"] == "error"
    assert job_named(mm, "finished.safetensors")["status"] == "done"
    assert mm._resume_downloads in mm.server.app.on_startup


async def test_download_finished_elsewhere_is_marked_done(comfy, files):
    url, _ = files.add("m.safetensors", 100)
    (comfy.models / "vae").mkdir(parents=True)
    (comfy.models / "vae" / "m.safetensors").write_bytes(b"x")
    save_state(comfy, [record(url, "m.safetensors", "downloading")])
    mm = restart()
    assert mm._resume_after_restart == []
    assert job_named(mm, "m.safetensors")["status"] == "done"


async def test_invalid_or_corrupt_state_is_ignored(comfy):
    save_state(comfy, [record("https://evil.example/x.safetensors", "x.safetensors", "downloading"), {"garbage": True}])
    mm = restart()
    assert mm.jobs == {} and mm._resume_after_restart == []

    (comfy.user / "__model_manager" / "downloads.json").write_text("{not json")
    mm = restart()
    assert mm.jobs == {}


async def test_saved_history_is_capped(comfy, files):
    url, _ = files.add("m.safetensors", 10)
    mm = restart()
    for i in range(mm.FINISHED_HISTORY + 10):
        job = mm._new_job(url, "vae", f"m{i}.safetensors", f"/x/m{i}", status="done", finished=float(i))
        mm.jobs[job["id"]] = job
    mm._save()
    saved = json.loads((comfy.user / "__model_manager" / "downloads.json").read_text())
    assert len(saved) == mm.FINISHED_HISTORY
    assert min(r["finished"] for r in saved) == 10.0, "the oldest finished entries are dropped"
