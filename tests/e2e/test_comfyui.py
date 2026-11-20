import json
import time
import urllib.request

from playwright.sync_api import expect

TINY_MODEL_URL = "https://huggingface.co/hf-internal-testing/tiny-random-bert/resolve/main/model.safetensors"


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def open_panel(page):
    page.evaluate("window.app.extensionManager.command.execute('Workspace.ToggleSidebarTab.model-manager')")
    expect(page.locator(".mm-root")).to_be_visible()


def test_extension_and_manager_are_loaded(comfyui_url, data_dir):
    assert "/extensions/model-manager/model_manager.js" in get_json(f"{comfyui_url}/extensions")
    storage = get_json(f"{comfyui_url}/model_manager/storage")
    assert storage["disk"]["total"] > 0
    config = (data_dir / "user" / "__manager" / "config.ini").read_text()
    assert "network_mode = personal_cloud" in config, "entrypoint seeds ComfyUI-Manager's config"


def test_panel_shows_storage_and_installed_models(page, data_dir):
    seeded = data_dir / "models" / "loras" / "e2e-seed.safetensors"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_bytes(b"\0" * 2_000_000)
    open_panel(page)
    expect(page.locator(".mm-hero-label")).to_contain_text("free of")
    expect(page.locator(".mm-legend")).to_contain_text("Models")
    row = page.locator(".mm-model", has_text="e2e-seed.safetensors")
    expect(row).to_be_visible()
    expect(row.locator(".mm-model-size")).to_have_text("2 MB")
    expect(page.locator('[data-testid="action-bar-buttons"] button')).to_have_count(1)


def test_missing_model_is_downloaded_from_the_workflow_prompt(page, data_dir):
    target = data_dir / "models" / "vae" / "e2e-tiny.safetensors"
    target.unlink(missing_ok=True)
    workflow = {
        "last_node_id": 1, "last_link_id": 0, "links": [], "groups": [], "config": {}, "extra": {}, "version": 0.4,
        "nodes": [{
            "id": 1, "type": "VAELoader", "pos": [400, 200], "size": [320, 60], "flags": {}, "order": 0, "mode": 0,
            "outputs": [{"name": "VAE", "type": "VAE", "links": None}],
            "properties": {"models": [{"name": "e2e-tiny.safetensors", "url": TINY_MODEL_URL, "directory": "vae"}]},
            "widgets_values": ["e2e-tiny.safetensors"],
        }],
    }  # fmt: skip
    page.evaluate("wf => window.app.loadGraphData(wf)", workflow)
    dialog = page.get_by_role("dialog").filter(has_text="Download missing models to the server?")
    expect(dialog).to_contain_text("vae/e2e-tiny.safetensors")
    dialog.get_by_role("button", name="Confirm").click()

    job = page.locator(".mm-job", has_text="e2e-tiny.safetensors")
    expect(job).to_have_attribute("data-status", "done", timeout=60_000)
    expect(job.locator(".mm-job-meta")).to_contain_text("Downloaded")
    assert target.stat().st_size > 0
    expect(page.locator(".mm-model", has_text="e2e-tiny.safetensors").locator(".mm-badge")).to_have_text("in use")


def test_model_can_be_deleted_from_the_panel(page, data_dir):
    victim = data_dir / "models" / "checkpoints" / "e2e-delete-me.safetensors"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"\0" * 1_000_000)
    open_panel(page)
    row = page.locator(".mm-model", has_text="e2e-delete-me.safetensors")
    row.get_by_role("button", name="Delete e2e-delete-me.safetensors").click()
    dialog = page.get_by_role("dialog").filter(has_text="Delete model?")
    expect(dialog).to_contain_text("This frees 1 MB on the server.")
    dialog.get_by_role("button", name="Delete").click()
    expect(row).to_have_count(0)
    deadline = time.time() + 10
    while victim.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not victim.exists()
