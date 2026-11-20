"""Idle unload: take models off the GPU once ComfyUI has been idle for a while.

ComfyUI keeps models in VRAM between runs and only evicts them when another
workflow needs the room, so an idle server holds on to the GPU indefinitely.
After IDLE_UNLOAD_MINUTES (default 3) with an empty queue, this asks the prompt
worker to unload them, the same as the "Unload Models" button. They stay cached
in RAM, so the next run loads them back quickly. IDLE_UNLOAD_MINUTES=0 turns it off.
"""

import asyncio
import logging
import os
import time

from server import PromptServer

NODE_CLASS_MAPPINGS = {}

ENV = "IDLE_UNLOAD_MINUTES"
DEFAULT_MINUTES = 3
CHECK_INTERVAL = 10

log = logging.getLogger("idle-unload")
server = PromptServer.instance
_task = None


def _timeout():
    """Idle time in seconds before unloading, or 0 when disabled."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return DEFAULT_MINUTES * 60
    try:
        minutes = float(raw)
    except ValueError:
        log.warning("Ignoring %s=%r, not a number; using %s minutes", ENV, raw, DEFAULT_MINUTES)
        return DEFAULT_MINUTES * 60
    return max(minutes, 0) * 60


def _models_loaded():
    # Imported here so the module loads without torch in the unit tests.
    import comfy.model_management

    return any(not m.is_dead() for m in list(comfy.model_management.current_loaded_models))


async def _watch(timeout):
    idle_since = time.monotonic()
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        queue = server.prompt_queue
        if queue.get_tasks_remaining() or not _models_loaded():
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= timeout:
            log.info("Idle for %g minutes, unloading models from VRAM", timeout / 60)
            # Handled by the prompt worker thread, like POST /free.
            queue.set_flag("unload_models", True)
            idle_since = time.monotonic()


async def _start(app):
    global _task
    timeout = _timeout()
    if timeout:
        _task = asyncio.create_task(_watch(timeout))


server.app.on_startup.append(_start)
