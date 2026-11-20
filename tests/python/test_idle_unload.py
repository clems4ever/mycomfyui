import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from aiohttp import web
from conftest import server_module, wait_for

EXTENSION = Path(__file__).resolve().parents[2] / "idle-unload" / "__init__.py"


class FakeQueue:
    def __init__(self):
        self.remaining = 0
        self.flags = []

    def get_tasks_remaining(self):
        return self.remaining

    def set_flag(self, name, data):
        self.flags.append((name, data))


def load(monkeypatch, minutes=None):
    if minutes is None:
        monkeypatch.delenv("IDLE_UNLOAD_MINUTES", raising=False)
    else:
        monkeypatch.setenv("IDLE_UNLOAD_MINUTES", minutes)
    server_module.PromptServer.instance = type("S", (), {"app": web.Application(), "prompt_queue": FakeQueue()})()
    spec = importlib.util.spec_from_file_location(f"idle_unload_{uuid.uuid4().hex}", EXTENSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("minutes", "seconds"),
    [(None, 180), ("", 180), ("10", 600), ("0.5", 30), ("0", 0), ("-1", 0), ("soon", 180)],
)
def test_timeout(monkeypatch, minutes, seconds):
    assert load(monkeypatch, minutes)._timeout() == seconds


async def test_disabled_starts_nothing(monkeypatch):
    mod = load(monkeypatch, "0")
    await mod._start(mod.server.app)
    assert mod._task is None


@pytest.fixture
async def idle(monkeypatch):
    mod = load(monkeypatch)
    mod.CHECK_INTERVAL = 0.01
    state = {"loaded": True}
    monkeypatch.setattr(mod, "_models_loaded", lambda: state["loaded"])
    task = asyncio.create_task(mod._watch(0.1))
    yield mod, mod.server.prompt_queue, state
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_unloads_after_idle_timeout(idle):
    _, queue, _ = idle
    await asyncio.sleep(0.05)
    assert queue.flags == []
    await wait_for(lambda: queue.flags == [("unload_models", True)], timeout=1)


async def test_waits_while_queue_is_busy(idle):
    _, queue, _ = idle
    queue.remaining = 1
    await asyncio.sleep(0.3)
    assert queue.flags == []
    queue.remaining = 0
    await wait_for(lambda: queue.flags, timeout=1)


async def test_nothing_to_unload(idle):
    _, queue, state = idle
    state["loaded"] = False
    await asyncio.sleep(0.3)
    assert queue.flags == []


async def test_unloads_once_per_idle_period(idle):
    _, queue, state = idle
    await wait_for(lambda: queue.flags, timeout=1)
    state["loaded"] = False  # the worker has unloaded them
    await asyncio.sleep(0.3)
    assert len(queue.flags) == 1
