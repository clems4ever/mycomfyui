def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


async def test_storage_lists_models_and_disk_usage(client, comfy):
    write(comfy.models / "checkpoints" / "big.safetensors", 3000)
    write(comfy.models / "loras" / "style" / "small.safetensors", 1000)
    write(comfy.models / "loras" / "notes.txt", 50)  # not a model file
    write(comfy.root / "custom_nodes" / "node.safetensors", 999)  # not a model folder
    body = await (await client.get("/model_manager/storage")).json()
    listed = sorted((m["directory"], m["name"], m["size"]) for m in body["models"])
    assert listed == [("checkpoints", "big.safetensors", 3000), ("loras", "style/small.safetensors", 1000)]
    assert body["models_bytes"] == 4000
    assert body["pending_bytes"] == 0
    disk = body["disk"]
    assert disk["total"] >= disk["used"] > 0 and disk["free"] > 0


async def test_files_shared_by_two_folders_are_listed_once(client, comfy):
    # models/unet belongs to diffusion_models; listing must not count it twice
    write(comfy.models / "unet" / "flux.safetensors", 2000)
    body = await (await client.get("/model_manager/storage")).json()
    assert [(m["directory"], m["name"]) for m in body["models"]] == [("diffusion_models", "flux.safetensors")]


async def test_delete_model_frees_space(client, comfy):
    target = comfy.models / "vae" / "old.safetensors"
    write(target, 2500)
    resp = await client.post("/model_manager/models/delete", json={"directory": "vae", "name": "old.safetensors"})
    assert resp.status == 200 and (await resp.json())["freed"] == 2500
    assert not target.exists()
    body = await (await client.get("/model_manager/storage")).json()
    assert body["models"] == []


async def test_delete_refuses_anything_outside_model_folders(client, comfy):
    outside = comfy.root / "secret.safetensors"
    write(outside, 10)
    write(comfy.root / "custom_nodes" / "x.safetensors", 10)
    cases = [
        ({"directory": "vae", "name": "../../secret.safetensors"}, 404),
        ({"directory": "vae", "name": str(outside)}, 404),
        ({"directory": "custom_nodes", "name": "x.safetensors"}, 400),
        ({"directory": "nope", "name": "x.safetensors"}, 400),
        ({"directory": "vae", "name": "missing.safetensors"}, 404),
        ({"directory": "vae", "name": None}, 404),
    ]
    for body, status in cases:
        resp = await client.post("/model_manager/models/delete", json=body)
        assert resp.status == status, body
    assert outside.exists() and (comfy.root / "custom_nodes" / "x.safetensors").exists()
