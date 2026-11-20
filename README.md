# mycomfyui

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

## Layout

| Path | What it is |
|---|---|
| `Dockerfile` | Image: Python 3.12, PyTorch, ComfyUI, ComfyUI-Manager, Model Manager |
| `docker-compose.yml` | GPU access, port, volumes, tokens |
| `entrypoint.sh` | Seeds the ComfyUI-Manager config on first start |
| `extra_model_paths.yaml` | Loads built-in nodes from outside the `custom_nodes` volume |
| `model-manager/` | The Model Manager extension (server routes + sidebar panel) |
