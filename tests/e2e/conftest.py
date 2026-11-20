"""End-to-end tests against a running ComfyUI container.

Start the image with ./data mounted (see .github/workflows/ci.yml), then run
  COMFYUI_URL=http://localhost:8188 DATA_DIR=./data pytest tests/e2e
"""

import os
import time
import urllib.request
from pathlib import Path

import pytest

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()


@pytest.fixture(scope="session")
def comfyui_url():
    deadline = time.time() + 180
    while True:
        try:
            with urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=5):
                return COMFYUI_URL
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(2)


@pytest.fixture(scope="session")
def data_dir():
    return DATA_DIR


@pytest.fixture
def page(comfyui_url, browser, request):
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(comfyui_url)
    page.wait_for_function("window.app?.graph && window.app.extensionManager", timeout=60_000)
    page.wait_for_timeout(2000)
    page.keyboard.press("Escape")  # first-run dialogs
    yield page
    shots = Path("e2e-screenshots")
    shots.mkdir(exist_ok=True)
    page.screenshot(path=str(shots / f"{request.node.name}.png"))
    assert [e for e in errors if "model_manager" in e or "ModelManager" in e] == []
    page.close()
