# Contributing

```sh
pip install -r requirements-dev.txt
npm ci
git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git   # used by the Python tests
```

| | |
|---|---|
| Lint | `ruff check . && ruff format --check .`, `npm run lint`, `shellcheck entrypoint.sh` |
| Unit tests | `pytest` (Python) and `npm test` (JavaScript) |
| End-to-end tests | `pytest tests/e2e` against a running container, see below |

The Python tests run the Model Manager against ComfyUI's real model-folder code, with a
local file server standing in for Hugging Face.

End-to-end tests drive the UI with Playwright. One of them downloads a 0.5 MB model from
Hugging Face, so it needs internet access.

```sh
docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu -t mycomfyui:ci .
mkdir -p data/models/{checkpoints,loras,vae} data/user
docker run -d --name comfyui -p 8188:8188 -v "$PWD/data/models:/opt/ComfyUI/models" \
  -v "$PWD/data/user:/opt/ComfyUI/user" mycomfyui:ci \
  python main.py --listen 0.0.0.0 --port 8188 --enable-manager --cpu
playwright install chromium
pytest tests/e2e
```

CI (`.github/workflows/ci.yml`) runs all of the above, plus Hadolint and actionlint, on
every push and pull request. After CI passes on `main`, `publish.yml` pushes the CUDA
image to `ghcr.io/clems4ever/mycomfyui`.
