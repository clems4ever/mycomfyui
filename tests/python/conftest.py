"""Fixtures for the Model Manager extension.

The extension runs against ComfyUI's real ``folder_paths`` module, so tests need
a ComfyUI checkout: set COMFYUI_PATH, or put one at ./ComfyUI. Only ComfyUI's web
server is replaced, by a stand-in that records what the extension broadcasts.
"""

import asyncio
import copy
import importlib.util
import os
import sys
import types
import uuid
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "model-manager" / "__init__.py"
COMFYUI = Path(os.environ.get("COMFYUI_PATH", ROOT / "ComfyUI")).resolve()

if not (COMFYUI / "folder_paths.py").exists():
    pytest.exit(f"ComfyUI checkout not found at {COMFYUI}; set COMFYUI_PATH", returncode=2)
sys.path.insert(0, str(COMFYUI))
sys.argv = sys.argv[:1]  # comfy.cli_args parses the command line on import
import folder_paths  # noqa: E402

ORIGINAL_FOLDERS = copy.deepcopy(folder_paths.folder_names_and_paths)


class FakePromptServer:
    def __init__(self):
        self.routes = web.RouteTableDef()
        self.app = web.Application()
        self.messages = []

    def send_sync(self, event, data, sid=None):
        self.messages.append((event, copy.deepcopy(data)))


server_module = types.ModuleType("server")
server_module.PromptServer = types.SimpleNamespace(instance=None)
sys.modules["server"] = server_module


@pytest.fixture
def comfy(tmp_path, monkeypatch):
    """Point every ComfyUI folder at a temporary directory."""
    models = tmp_path / "models"
    folders = {}
    for key, (paths, extensions) in ORIGINAL_FOLDERS.items():
        parent = models if key not in ("custom_nodes", "datasets") else tmp_path
        folders[key] = ([str(parent / Path(p).name) for p in paths], extensions)
    monkeypatch.setattr(folder_paths, "folder_names_and_paths", folders)
    monkeypatch.setattr(folder_paths, "models_dir", str(models))
    old_user_dir = folder_paths.get_user_directory()
    folder_paths.set_user_directory(str(tmp_path / "user"))
    folder_paths.filename_list_cache.clear()
    yield types.SimpleNamespace(root=tmp_path, models=models, user=tmp_path / "user", folder_paths=folder_paths)
    folder_paths.set_user_directory(old_user_dir)
    folder_paths.filename_list_cache.clear()


def load_extension():
    server_module.PromptServer.instance = FakePromptServer()
    name = f"model_manager_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, EXTENSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def allow_test_server(module):
    module.ALLOWED_HOSTS = ("127.0.0.1",)
    module.ALLOWED_SCHEMES = ("http",)
    module.CHUNK_SIZE = 64 * 1024


@pytest.fixture
async def mm(comfy):
    """A freshly imported extension that may download from the local test server."""
    module = load_extension()
    allow_test_server(module)
    yield module
    tasks = [j["task"] for j in module.jobs.values() if j.get("task")]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def client(mm, aiohttp_client):
    app = web.Application()
    app.add_routes(mm.server.routes)
    return await aiohttp_client(app)


class FileServer:
    """Serves test files with optional Range support, throttling and failures."""

    def __init__(self):
        self.files = {}
        self.delay = 0.0
        self.chunk = 64 * 1024
        self.requests = []
        self.truncate_at = None  # stop sending after this many bytes
        self.base = ""

    def add(self, name, size):
        self.files[name] = os.urandom(size)
        return f"{self.base}/files/{name}", self.files[name]

    async def serve(self, request):
        name = request.match_info["name"]
        if name not in self.files:
            return web.Response(status=404)
        data = self.files[name]
        rng = request.headers.get("Range")
        self.requests.append((request.method, name, rng))
        start = int(rng.removeprefix("bytes=").split("-")[0]) if rng and request.path.startswith("/files") else 0
        if start >= len(data) and start:
            return web.Response(status=416)
        body = data[start:]
        status = 206 if start else 200
        headers = {"Content-Length": str(len(body))}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        resp = web.StreamResponse(status=status, headers=headers)
        await resp.prepare(request)
        if request.method == "HEAD":
            return resp
        limit = len(body) if self.truncate_at is None else max(self.truncate_at - start, 0)
        for i in range(0, limit, self.chunk):
            await resp.write(body[i : min(i + self.chunk, limit)])
            if self.delay:
                await asyncio.sleep(self.delay)
        if limit < len(body):
            request.transport.close()
            return resp
        await resp.write_eof()
        return resp

    async def gated(self, request):
        return web.Response(status=401)

    def app(self):
        app = web.Application()
        app.router.add_get("/files/{name}", self.serve)
        app.router.add_get("/norange/{name}", self.serve)
        app.router.add_get("/gated/{name}", self.gated)
        return app


@pytest.fixture
async def files(aiohttp_server):
    fs = FileServer()
    server = await aiohttp_server(fs.app())
    fs.base = str(server.make_url("")).rstrip("/")
    return fs


async def wait_for(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def job_named(mm, name):
    return next(j for j in mm.jobs.values() if j["name"] == name)
