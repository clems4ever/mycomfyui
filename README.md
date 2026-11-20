# mycomfyui

[![CI](https://github.com/clems4ever/mycomfyui/actions/workflows/ci.yml/badge.svg)](https://github.com/clems4ever/mycomfyui/actions/workflows/ci.yml)
[![Publish image](https://github.com/clems4ever/mycomfyui/actions/workflows/publish.yml/badge.svg)](https://github.com/clems4ever/mycomfyui/actions/workflows/publish.yml)

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) in Docker for NVIDIA GPUs, with
ComfyUI-Manager enabled and a built-in **Model Manager** panel.

## Run

Needs a recent NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```sh
docker compose up -d --build
```

Open http://localhost:8188. Models, custom nodes, inputs, outputs and settings are
stored under `./data/`, so they survive rebuilds.

### Prebuilt image

Every push to `main` that passes CI publishes the CUDA image to
`ghcr.io/clems4ever/mycomfyui`, tagged `latest` and `sha-<commit>`. To use it instead
of building locally, replace `build: .` with
`image: ghcr.io/clems4ever/mycomfyui:latest` in `docker-compose.yml`.

### Build options

| Build arg | Default | Purpose |
|---|---|---|
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cu128` | PyTorch build. Use `.../whl/cu126` for GPUs older than the RTX 20 series, `.../whl/cpu` for CPU only. |
| `COMFYUI_REF` | `master` | ComfyUI branch, tag or commit to build. |

### Gated models

Some models (e.g. FLUX.1-dev) require accepting a license and a token. Put the
tokens in a `.env` file next to `docker-compose.yml`:

```sh
HF_TOKEN=hf_...
CIVITAI_TOKEN=...
```

## Model Manager

A sidebar panel (**Manager** icon, or the **Models** button next to **Run**) that
downloads models on the server, straight into the right `models/` folder. ComfyUI's
own "Download" buttons save files in the browser instead.

- Offers to download a workflow's missing models when you open it.
- Live progress, speed and time left; pause, resume (continues where it stopped),
  cancel and retry. Two downloads run at a time, the rest are queued.
- Disk usage of the models disk, with a warning when downloads won't fit.
- Installed models grouped by folder, with search, sorting, "in use" badges for
  the open workflow, and delete.

- Survives restarts: the download list is saved in
  `data/user/__model_manager/downloads.json`, and downloads that were running or
  queued resume automatically from their partial files when the server comes back.
  Paused and failed downloads stay as they were.

Downloads are limited to Hugging Face and Civitai.

## Security

ComfyUI-Manager runs in `personal_cloud` mode (set on first start in
`data/user/__manager/config.ini`) so it can install nodes while listening on
`0.0.0.0`. Anyone who can reach port 8188 can install custom nodes (i.e. run code)
and download or delete models. Keep it on a trusted network, or put an
authenticating reverse proxy in front of it.

## Development

```sh
pip install -r requirements-dev.txt
npm ci

# Lint
ruff check . && ruff format --check .
npm run lint
shellcheck entrypoint.sh    # also checked in CI: hadolint (Dockerfile), actionlint (workflows)

# Unit tests
git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git   # the Python tests use its folder_paths module
pytest
npm test
```

The Python tests run the extension against ComfyUI's real model-folder code, with a
local file server standing in for Hugging Face. They cover validation, downloads,
pause/resume with HTTP ranges, cancel, dropped connections, disk-space checks, the
download queue, storage listing, deletion and resuming after a restart.

End-to-end tests drive the UI with Playwright against a running container:

```sh
docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu -t mycomfyui:ci .
mkdir -p data/models/{checkpoints,loras,vae} data/{custom_nodes,input,output,user}
docker run -d --name comfyui -p 8188:8188 -v "$PWD/data/models:/opt/ComfyUI/models" \
  -v "$PWD/data/user:/opt/ComfyUI/user" mycomfyui:ci \
  python main.py --listen 0.0.0.0 --port 8188 --enable-manager --cpu
playwright install chromium
pytest tests/e2e
```

One end-to-end test downloads a 0.5 MB model from Hugging Face, so it needs internet access.

### CI

`.github/workflows/ci.yml` runs on every push and pull request: lint, unit tests,
and the end-to-end tests against a CPU build of the image (GitHub's runners have no
GPU). `.github/workflows/publish.yml` builds the CUDA image and pushes it to GHCR
after CI succeeds on `main`.

## Layout

| Path | What it is |
|---|---|
| `Dockerfile` | Image: Python 3.12, PyTorch, ComfyUI, ComfyUI-Manager, Model Manager |
| `docker-compose.yml` | GPU access, port, volumes, tokens |
| `entrypoint.sh` | Seeds the ComfyUI-Manager config on first start |
| `extra_model_paths.yaml` | Loads built-in nodes from outside the `custom_nodes` volume |
| `model-manager/` | The Model Manager extension (server routes + sidebar panel) |
| `tests/python/`, `tests/js/`, `tests/e2e/` | Unit tests (server and panel logic) and browser tests |
| `.github/workflows/` | CI and image publishing |
