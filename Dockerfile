FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# PyTorch first so this heavy layer stays cached across ComfyUI updates.
# Override for other hardware, e.g. https://download.pytorch.org/whl/rocm6.4 or .../cpu
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
RUN pip install torch torchvision torchaudio --index-url ${TORCH_INDEX_URL}

# Branch, tag or commit to build (e.g. v0.46.0)
ARG COMFYUI_REF=master
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /opt/ComfyUI \
    && git -C /opt/ComfyUI checkout ${COMFYUI_REF}

WORKDIR /opt/ComfyUI
RUN pip install -r requirements.txt -r manager_requirements.txt

COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY model-manager /opt/builtin_nodes/model-manager
COPY idle-unload /opt/builtin_nodes/idle-unload
COPY extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml

EXPOSE 8188
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--enable-manager"]
