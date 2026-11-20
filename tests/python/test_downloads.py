import asyncio
import json
import os

from conftest import job_named, wait_for


async def start(client, *models):
    resp = await client.post("/model_manager/download", json={"models": list(models)})
    assert resp.status == 200
    return await resp.json()


def model(url, name="m.safetensors", directory="vae"):
    return {"url": url, "name": name, "directory": directory}


async def test_download_completes_into_model_folder(mm, client, files, comfy):
    url, data = files.add("m.safetensors", 300_000)
    result = await start(client, model(url))
    assert len(result["started"]) == 1 and result["errors"] == []

    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["status"] == "done")
    target = comfy.models / "vae" / "m.safetensors"
    assert target.read_bytes() == data
    assert not os.path.exists(f"{target}.part")
    assert job["downloaded"] == job["total"] == len(data)

    events = [m for m in mm.server.messages if m[0] == "model_manager.jobs"]
    statuses = [e[1]["jobs"][0]["status"] for e in events]
    assert statuses[0] == "queued" and statuses[-1] == "done" and "downloading" in statuses
    assert "path" not in events[-1][1]["jobs"][0], "server paths are not sent to the browser"


async def test_rejected_models_are_reported(client):
    result = await start(client, model("https://evil.example/m.safetensors"))
    assert result["started"] == [] and "only https" in result["errors"][0]


async def test_existing_or_active_downloads_are_not_duplicated(mm, client, files, comfy):
    files.delay = 0.02
    url, _ = files.add("m.safetensors", 400_000)
    first = await start(client, model(url))
    second = await start(client, model(url))
    assert len(first["started"]) == 1 and second["started"] == []
    await wait_for(lambda: job_named(mm, "m.safetensors")["status"] == "done")
    third = await start(client, model(url))
    assert third["started"] == [] and len(mm.jobs) == 1


async def test_check_lists_missing_models_with_size(client, files, comfy):
    url, data = files.add("m.safetensors", 1234)
    (comfy.models / "vae").mkdir(parents=True)
    (comfy.models / "vae" / "have.safetensors").write_bytes(b"x")
    resp = await client.post(
        "/model_manager/check",
        json={
            "models": [
                model(url),
                model(url, name="have.safetensors"),
                model("https://evil.example/x.safetensors", name="x.safetensors"),
            ]
        },
    )
    body = await resp.json()
    assert [(m["name"], m["size"], m["downloading"]) for m in body["missing"]] == [("m.safetensors", len(data), False)]
    assert len(body["errors"]) == 1


async def test_pause_and_resume_continues_with_a_range_request(mm, client, files, comfy):
    files.delay = 0.02
    url, data = files.add("m.safetensors", 1_500_000)
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["downloaded"] > 300_000)

    resp = await client.post(f"/model_manager/jobs/{job['id']}/pause")
    assert resp.status == 200
    await wait_for(lambda: job["task"] is None)
    part = comfy.models / "vae" / "m.safetensors.part"
    paused_at = part.stat().st_size
    assert job["status"] == "paused" and 0 < paused_at < len(data)

    files.delay = 0
    await client.post(f"/model_manager/jobs/{job['id']}/resume")
    await wait_for(lambda: job["status"] == "done")
    assert (comfy.models / "vae" / "m.safetensors").read_bytes() == data
    assert ("GET", "m.safetensors", f"bytes={paused_at}-") in files.requests


async def test_resume_restarts_when_server_ignores_range(mm, client, files, comfy):
    files.delay = 0.02
    _, data = files.add("m.safetensors", 1_000_000)
    url = f"{files.base}/norange/m.safetensors"
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["downloaded"] > 200_000)
    await client.post(f"/model_manager/jobs/{job['id']}/pause")
    await wait_for(lambda: job["task"] is None)
    files.delay = 0
    await client.post(f"/model_manager/jobs/{job['id']}/resume")
    await wait_for(lambda: job["status"] == "done")
    assert (comfy.models / "vae" / "m.safetensors").read_bytes() == data


async def test_cancel_removes_partial_file(mm, client, files, comfy):
    files.delay = 0.02
    url, _ = files.add("m.safetensors", 1_500_000)
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["downloaded"] > 200_000)
    resp = await client.post(f"/model_manager/jobs/{job['id']}/cancel")
    assert resp.status == 200
    assert job["status"] == "cancelled"
    assert not (comfy.models / "vae" / "m.safetensors.part").exists()
    assert not (comfy.models / "vae" / "m.safetensors").exists()


async def test_interrupted_connection_fails_and_can_be_resumed(mm, client, files, comfy):
    url, data = files.add("m.safetensors", 1_000_000)
    files.truncate_at = 400_000
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["status"] == "error")
    assert job["error"] == "Connection lost during the download. Resume to continue."
    partial = (comfy.models / "vae" / "m.safetensors.part").read_bytes()
    assert 0 < len(partial) <= 400_000 and data.startswith(partial), "keeps a valid prefix of the file"

    files.truncate_at = None
    await client.post(f"/model_manager/jobs/{job['id']}/resume")
    await wait_for(lambda: job["status"] == "done")
    assert (comfy.models / "vae" / "m.safetensors").read_bytes() == data


async def test_gated_and_missing_files_explain_the_error(mm, client, files):
    files.add("m.safetensors", 10)
    await start(
        client,
        model(f"{files.base}/gated/m.safetensors", name="gated.safetensors"),
        model(f"{files.base}/files/absent.safetensors", name="absent.safetensors"),
    )
    gated, absent = job_named(mm, "gated.safetensors"), job_named(mm, "absent.safetensors")
    await wait_for(lambda: gated["status"] == "error" and absent["status"] == "error")
    assert "Access denied (HTTP 401)" in gated["error"] and "gated" in gated["error"]
    assert "HTTP 404" in absent["error"]


async def test_refuses_download_that_does_not_fit_on_disk(mm, client, files, comfy, monkeypatch):
    monkeypatch.setattr(mm, "_disk_usage", lambda: {"total": 10_000_000, "used": 9_900_000, "free": 100_000})
    url, _ = files.add("m.safetensors", 500_000)
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["status"] == "error")
    assert "Not enough disk space" in job["error"]
    assert not (comfy.models / "vae" / "m.safetensors").exists()


async def test_at_most_two_downloads_run_at_once(mm, client, files):
    files.delay = 0.03
    urls = [files.add(f"m{i}.safetensors", 600_000)[0] for i in range(3)]
    await start(client, *[model(u, name=f"m{i}.safetensors") for i, u in enumerate(urls)])
    seen_max = 0
    while not all(j["status"] == "done" for j in mm.jobs.values()):
        seen_max = max(seen_max, sum(j["status"] == "downloading" for j in mm.jobs.values()))
        await asyncio.sleep(0.01)
    assert seen_max == 2


async def test_job_actions_validate_state(mm, client, files):
    url, _ = files.add("m.safetensors", 1000)
    await start(client, model(url))
    job = job_named(mm, "m.safetensors")
    await wait_for(lambda: job["status"] == "done")
    assert (await client.post(f"/model_manager/jobs/{job['id']}/pause")).status == 409
    assert (await client.post("/model_manager/jobs/nope/pause")).status == 404
    assert (await client.post(f"/model_manager/jobs/{job['id']}/remove")).status == 200
    assert job["id"] not in mm.jobs


async def test_clear_removes_only_finished_jobs(mm, client, files):
    files.delay = 0.05
    done_url, _ = files.add("done.safetensors", 1000)
    slow_url, _ = files.add("slow.safetensors", 2_000_000)
    await start(client, model(done_url, name="done.safetensors"), model(slow_url, name="slow.safetensors"))
    await wait_for(lambda: job_named(mm, "done.safetensors")["status"] == "done")
    await client.post("/model_manager/jobs/clear")
    assert [j["name"] for j in mm.jobs.values()] == ["slow.safetensors"]
    listed = await (await client.get("/model_manager/jobs")).json()
    assert [j["name"] for j in listed["jobs"]] == ["slow.safetensors"]


async def test_state_is_saved_for_restarts(mm, client, files, comfy):
    url, _ = files.add("m.safetensors", 1000)
    await start(client, model(url))
    await wait_for(lambda: job_named(mm, "m.safetensors")["status"] == "done")
    saved = json.loads((comfy.user / "__model_manager" / "downloads.json").read_text())
    assert [(r["name"], r["status"]) for r in saved] == [("m.safetensors", "done")]
    assert "task" not in saved[0] and "path" not in saved[0]
