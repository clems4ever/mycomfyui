# mycomfyui

[![CI](https://github.com/clems4ever/mycomfyui/actions/workflows/ci.yml/badge.svg)](https://github.com/clems4ever/mycomfyui/actions/workflows/ci.yml)

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) in Docker for NVIDIA GPUs, with
ComfyUI-Manager and a **Model Manager** panel: download a workflow's models straight to
the server, follow the downloads live, and keep an eye on disk space.

![Model Manager with downloads in progress](docs/screenshots/model-manager.png)

## Run it

Needs an NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```sh
docker compose up -d
```

This pulls `ghcr.io/clems4ever/mycomfyui:latest`, which is rebuilt on every push to
`main`. Then open http://localhost:8188. Models, outputs and settings are kept in `./data`.

To update, run `docker compose pull && docker compose up -d`. To build from source
instead, replace the `image:` line in `docker-compose.yml` with `build: .`.

## Model Manager

Open a workflow with missing models and it offers to download them to the server:

![Download prompt for a workflow's missing models](docs/screenshots/download-prompt.png)

Downloads can be paused, resumed and cancelled, and pick up where they left off after a
restart. Installed models are listed by folder, with the ones the open workflow uses
marked **in use**, and can be deleted to free space:

![Installed models list](docs/screenshots/installed-models.png)

For gated models (e.g. FLUX.1-dev), put `HF_TOKEN=hf_...` in a `.env` file next to
`docker-compose.yml`.

## GPU memory

ComfyUI keeps models in VRAM between runs. Once nothing has run for 3 minutes, they are
unloaded from the GPU (they stay cached in RAM, so the next run starts quickly). Set
`IDLE_UNLOAD_MINUTES` in `.env` to change the delay, or to `0` to keep models loaded.

> [!WARNING]
> Anyone who can reach port 8188 can install custom nodes and download or delete models.
> Keep it on a trusted network.

## License

[MIT](LICENSE)
