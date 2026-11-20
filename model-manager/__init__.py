"""Model manager: download, pause, resume and cancel model downloads on the server,
see disk usage, and delete installed models.

The built-in "Download" buttons in the Missing Models panel hand the URL to the
browser, which saves the file on the client machine. Here the server fetches it
instead, so it lands in the right folder no matter where the browser runs.
"""
import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

import folder_paths
from server import PromptServer

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}

# Same sources the frontend's own Download button accepts.
ALLOWED_HOSTS = ("huggingface.co", "civitai.com", "civitai.red")
ALLOWED_EXTENSIONS = (".safetensors", ".sft", ".ckpt", ".pth", ".pt", ".gguf")
TOKEN_ENV = {"huggingface.co": "HF_TOKEN", "civitai.com": "CIVITAI_TOKEN", "civitai.red": "CIVITAI_TOKEN"}
# Folder keys that aren't model folders, and legacy aliases of other keys.
NON_MODEL_FOLDERS = {"custom_nodes", "configs"}
LEGACY_FOLDERS = {"unet", "clip"}
CHUNK_SIZE = 4 * 1024 * 1024
MAX_PARALLEL = 2
PROGRESS_INTERVAL = 0.5
SAVE_INTERVAL = 5
FINISHED_HISTORY = 50
ACTIVE = ("queued", "downloading")
PERSISTED = ("id", "url", "directory", "name", "status", "downloaded", "total", "error", "created", "finished")
# The download list survives restarts; it lives in the user directory, which is a volume.
STATE_FILE = os.path.join(folder_paths.get_user_directory(), "__model_manager", "downloads.json")

log = logging.getLogger("model-manager")
server = PromptServer.instance
routes = server.routes
jobs = {}
_slots = None
_last_save = 0


class Paused(Exception):
    pass


def _host(url):
    return (urlparse(url).hostname or "").lower()


def _auth_headers(url):
    token = os.environ.get(TOKEN_ENV.get(_host(url), ""), "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _model_folders():
    keys = [k for k in folder_paths.folder_names_and_paths if k not in NON_MODEL_FOLDERS]
    return sorted(keys, key=lambda k: (k in LEGACY_FOLDERS, k))


def _validate(model):
    """Return (url, directory, name, target_path) or raise ValueError."""
    url, directory, name = model.get("url"), model.get("directory"), model.get("name")
    if not (isinstance(url, str) and isinstance(directory, str) and isinstance(name, str)):
        raise ValueError("model needs url, directory and name")
    if urlparse(url).scheme != "https" or _host(url) not in ALLOWED_HOSTS:
        raise ValueError(f"{name}: only https downloads from {', '.join(ALLOWED_HOSTS)} are allowed")
    if not name.lower().endswith(ALLOWED_EXTENSIONS):
        raise ValueError(f"{name}: unsupported file type")
    if directory in NON_MODEL_FOLDERS:
        raise ValueError(f"{name}: '{directory}' is not a model folder")
    try:
        base = folder_paths.get_folder_paths(directory)[0]
    except (KeyError, IndexError):
        raise ValueError(f"{name}: unknown model folder '{directory}'")
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, name))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"{name}: invalid file name")
    return url, directory, name, target


def _exists(directory, name):
    return folder_paths.get_full_path(directory, name) is not None


def _job_for(path, statuses):
    return next((j for j in jobs.values() if j["path"] == path and j["status"] in statuses), None)


def _public(job):
    return {k: v for k, v in job.items() if k not in ("path", "task", "_last_sent", "_rate_mark")}


def _broadcast(job=None, force=True):
    """Push job state to every open browser tab over ComfyUI's websocket."""
    now = time.monotonic()
    if job is not None:
        if not force and now - job["_last_sent"] < PROGRESS_INTERVAL:
            return
        job["_last_sent"] = now
    server.send_sync("model_manager.jobs", {"jobs": [_public(j) for j in jobs.values()]})
    _save(force=force or job is None)


def _save(force=True):
    """Write the download list to disk; progress-only updates are throttled."""
    global _last_save
    now = time.monotonic()
    if not force and now - _last_save < SAVE_INTERVAL:
        return
    _last_save = now
    finished = sorted((j for j in jobs.values() if j["status"] in ("done", "cancelled")),
                      key=lambda j: j["finished"] or j["created"], reverse=True)
    dropped = {j["id"] for j in finished[FINISHED_HISTORY:]}
    records = [{k: j[k] for k in PERSISTED} for j in jobs.values() if j["id"] not in dropped]
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(records, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.error("Could not save download list: %s", e)


def _new_job(url, directory, name, path, **fields):
    job = {"id": uuid.uuid4().hex, "url": url, "directory": directory, "name": name, "path": path,
           "status": "queued", "downloaded": 0, "total": None, "speed": 0, "error": None,
           "created": time.time(), "finished": None, "task": None, "_last_sent": 0}
    job.update(fields)
    return job


def _load():
    """Restore the download list saved before the last shutdown.

    Returns the jobs that were queued or downloading, to be restarted once the
    server is up. Paused and failed downloads stay as they were.
    """
    try:
        with open(STATE_FILE) as f:
            records = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.error("Could not read saved download list: %s", e)
        return []
    to_resume = []
    for rec in records:
        try:
            url, directory, name, path = _validate(rec)
        except (ValueError, AttributeError):
            continue
        fields = {k: rec[k] for k in PERSISTED if k in rec and k not in ("url", "directory", "name")}
        job = _new_job(url, directory, name, path, **fields)
        part = path + ".part"
        if job["status"] not in ("done", "cancelled") and os.path.exists(path):
            job.update(status="done", finished=job["finished"] or time.time())
        elif os.path.exists(part):
            job["downloaded"] = os.path.getsize(part)
        if job["status"] in ACTIVE:
            job["status"] = "queued"
            to_resume.append(job)
        jobs[job["id"]] = job
    return to_resume


def _models_root():
    return os.path.abspath(folder_paths.models_dir)


def _disk_usage():
    usage = shutil.disk_usage(_models_root())
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def _remaining_bytes(exclude=None):
    return sum(max((j["total"] or 0) - j["downloaded"], 0)
               for j in jobs.values() if j["status"] in ACTIVE and j is not exclude)


async def _remote_size(session, url):
    try:
        async with session.head(url, headers=_auth_headers(url), allow_redirects=True) as resp:
            return resp.content_length if resp.status == 200 else None
    except Exception:
        return None


def _describe_error(status, url):
    if status in (401, 403):
        env = TOKEN_ENV.get(_host(url), "a token")
        return (f"Access denied (HTTP {status}). The model may be gated: accept its license on the "
                f"website, then set {env} for the container.")
    if status == 404:
        return "File not found at that URL (HTTP 404)."
    return f"Download failed (HTTP {status})."


def _fmt_gb(n):
    return f"{n / 1e9:.1f} GB"


async def _download(job):
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_PARALLEL)
    part = job["path"] + ".part"
    try:
        async with _slots:
            if job["status"] != "queued":
                return
            job.update(status="downloading", error=None, speed=0)
            _broadcast(job)
            os.makedirs(os.path.dirname(job["path"]), exist_ok=True)
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            headers = _auth_headers(job["url"])
            if offset:
                headers["Range"] = f"bytes={offset}-"
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(job["url"], headers=headers) as resp:
                    if resp.status == 206:
                        mode = "ab"
                    elif resp.status == 200:
                        offset, mode = 0, "wb"  # server ignored the range; start over
                    elif resp.status == 416 and offset:
                        os.remove(part)  # stale partial file; retry from scratch
                        raise RuntimeError("Partial file was out of date and has been removed. Retry to start over.")
                    else:
                        raise RuntimeError(_describe_error(resp.status, job["url"]))
                    job["downloaded"] = offset
                    if resp.content_length is not None:
                        job["total"] = offset + resp.content_length
                    needed = (job["total"] or 0) - offset + _remaining_bytes(exclude=job)
                    free = _disk_usage()["free"]
                    if job["total"] and needed > free:
                        raise RuntimeError(f"Not enough disk space: downloads need {_fmt_gb(needed)}, "
                                           f"only {_fmt_gb(free)} free.")
                    _broadcast(job)
                    job["_rate_mark"] = (time.monotonic(), offset)
                    with open(part, mode) as f:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            if job["status"] != "downloading":
                                raise Paused()
                            await asyncio.to_thread(f.write, chunk)
                            job["downloaded"] += len(chunk)
                            t0, b0 = job["_rate_mark"]
                            now = time.monotonic()
                            if now - t0 >= 1:
                                rate = (job["downloaded"] - b0) / (now - t0)
                                job["speed"] = rate if not job["speed"] else 0.6 * rate + 0.4 * job["speed"]
                                job["_rate_mark"] = (now, job["downloaded"])
                            _broadcast(job, force=False)
            if job["total"] and job["downloaded"] < job["total"]:
                raise RuntimeError("Connection closed before the download finished. Resume to continue.")
            os.replace(part, job["path"])
            job.update(status="done", speed=0, finished=time.time())
            log.info("Downloaded %s to %s", job["name"], job["path"])
    except (Paused, asyncio.CancelledError):
        # pause keeps the .part file so resume can continue from it; cancel removes it
        if job["status"] == "cancelled" and os.path.exists(part):
            os.remove(part)
        job["speed"] = 0
    except Exception as e:
        job.update(status="error", error=str(e) or type(e).__name__, speed=0)
        log.error("Failed to download %s: %s", job["name"], job["error"])
    finally:
        if job["task"] is asyncio.current_task():
            job["task"] = None
        _broadcast(job)


async def _start(job):
    # A paused/cancelled task may still be unwinding; let it finish with the .part file first.
    old = job["task"]
    if old and not old.done():
        await asyncio.wait([old])
    job["status"] = "queued"
    job["task"] = asyncio.create_task(_download(job))


def _stop(job, status):
    job["status"] = status
    if job["task"]:
        job["task"].cancel()


@routes.post("/model_manager/check")
async def check(request):
    """Return the given models that are not on the server yet, with their sizes."""
    body = await request.json()
    missing, errors = [], []
    for model in body.get("models", []):
        try:
            url, directory, name, path = _validate(model)
        except ValueError as e:
            errors.append(str(e))
            continue
        if not _exists(directory, name):
            missing.append({"url": url, "directory": directory, "name": name,
                            "downloading": _job_for(path, ACTIVE + ("paused",)) is not None})
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        sizes = await asyncio.gather(*(_remote_size(session, m["url"]) for m in missing))
    for model, size in zip(missing, sizes):
        model["size"] = size
    return web.json_response({"missing": missing, "errors": errors})


@routes.post("/model_manager/download")
async def download(request):
    body = await request.json()
    started, errors = [], []
    for model in body.get("models", []):
        try:
            url, directory, name, path = _validate(model)
        except ValueError as e:
            errors.append(str(e))
            continue
        if _exists(directory, name):
            continue
        existing = _job_for(path, ACTIVE + ("paused", "error"))
        if existing:
            if existing["status"] in ("paused", "error"):
                await _start(existing)
                started.append(existing["id"])
            continue
        job = _new_job(url, directory, name, path, total=model.get("size"))
        jobs[job["id"]] = job
        await _start(job)
        started.append(job["id"])
    _broadcast()
    return web.json_response({"started": started, "errors": errors})


@routes.get("/model_manager/jobs")
async def list_jobs(request):
    return web.json_response({"jobs": [_public(j) for j in jobs.values()]})


@routes.post("/model_manager/jobs/{id}/{action}")
async def job_action(request):
    job = jobs.get(request.match_info["id"])
    action = request.match_info["action"]
    if job is None:
        return web.json_response({"error": "No such download"}, status=404)
    status = job["status"]
    if action == "pause" and status in ACTIVE:
        _stop(job, "paused")
    elif action == "resume" and status in ("paused", "error"):
        await _start(job)
    elif action == "cancel" and status in ACTIVE + ("paused", "error"):
        task = job["task"]
        _stop(job, "cancelled")
        if task:
            await asyncio.wait([task])  # the task removes its .part file
        part = job["path"] + ".part"
        if os.path.exists(part):
            os.remove(part)
    elif action == "remove" and status in ("done", "cancelled"):
        del jobs[job["id"]]
    else:
        return web.json_response({"error": f"Can't {action} a download that is {status}"}, status=409)
    _broadcast()
    return web.json_response({"ok": True})


@routes.post("/model_manager/jobs/clear")
async def clear_jobs(request):
    for job_id in [i for i, j in jobs.items() if j["status"] in ("done", "cancelled")]:
        del jobs[job_id]
    _broadcast()
    return web.json_response({"ok": True})


def _list_models():
    seen, models = set(), []
    for directory in _model_folders():
        for name in folder_paths.get_filename_list(directory):
            path = folder_paths.get_full_path(directory, name)
            if not path:
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                st = os.stat(real)
            except OSError:
                continue
            models.append({"directory": directory, "name": name, "size": st.st_size, "modified": st.st_mtime})
    return models


@routes.get("/model_manager/storage")
async def storage(request):
    models = await asyncio.to_thread(_list_models)
    return web.json_response({
        "disk": _disk_usage(),
        "models_bytes": sum(m["size"] for m in models),
        "pending_bytes": _remaining_bytes(),
        "models": models,
    })


@routes.post("/model_manager/models/delete")
async def delete_model(request):
    body = await request.json()
    directory, name = body.get("directory"), body.get("name")
    if directory in NON_MODEL_FOLDERS or directory not in folder_paths.folder_names_and_paths:
        return web.json_response({"error": "Unknown model folder"}, status=400)
    path = folder_paths.get_full_path(directory, name) if isinstance(name, str) else None
    if not path:
        return web.json_response({"error": "Model not found"}, status=404)
    roots = [os.path.abspath(p) for p in folder_paths.get_folder_paths(directory)]
    real = os.path.abspath(path)
    if not any(os.path.commonpath([r, real]) == r for r in roots):
        return web.json_response({"error": "Model is outside the model folders"}, status=400)
    size = os.path.getsize(real)
    await asyncio.to_thread(os.remove, real)
    log.info("Deleted model %s (%d bytes)", real, size)
    return web.json_response({"ok": True, "freed": size})


_resume_after_restart = _load()


async def _resume_downloads(app):
    if _resume_after_restart:
        log.info("Resuming %d download(s) interrupted by the last shutdown", len(_resume_after_restart))
    for job in _resume_after_restart:
        await _start(job)


server.app.on_startup.append(_resume_downloads)
