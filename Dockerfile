# syntax=docker/dockerfile:1.7
# ComfyGen's RunPod serverless image is a thin child of the shared HearmemanAI
# ComfyUI base. comfyui-runtime/base/Dockerfile owns CUDA, Python, Torch,
# ComfyUI, ComfyUI-Manager, SageAttention, and the CivitAI downloader.
# pins.json is the release source of truth; CircleCI passes the same value as
# BASE_IMAGE, while this default keeps a plain local build coherent.
ARG BASE_IMAGE=hearmeman/comfyui-base:cu130-comfy0.32.0-torch2.11.0
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

LABEL org.hearmemanai.comfyui.base-image="${BASE_IMAGE}"

ENV PYTHONDONTWRITEBYTECODE=1

# Libraries required by the broad serverless custom-node set but not by every
# ComfyUI child. CUDA, Python, compilers, git, ffmpeg, and aria2 are inherited.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libopenblas-dev liblapack-dev

# The father installs Manager once, as lowercase comfyui-manager. The
# serverless handler's offline preflight has historically used the uppercase
# path, so adapt the inherited directory without cloning or installing a
# second Manager copy.
RUN test -d /ComfyUI/custom_nodes/comfyui-manager \
    && test ! -e /ComfyUI/custom_nodes/ComfyUI-Manager \
    && mv /ComfyUI/custom_nodes/comfyui-manager \
        /ComfyUI/custom_nodes/ComfyUI-Manager

# RunPod SDK and handler-only dependencies.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install runpod boto3 requests websocket-client

# Child-owned custom nodes only. The inherited PIP_CONSTRAINT points at the
# father's /torch-constraint.txt, so node requirements cannot replace the
# father-owned Torch family.
COPY nodes.lock /nodes.lock
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    set -e; \
    test "$PIP_CONSTRAINT" = "/torch-constraint.txt"; \
    while read -r url sha; do \
      case "$url" in ''|'#'*) continue ;; esac; \
      cd /ComfyUI/custom_nodes; \
      repo_dir=$(basename "$url" .git); \
      echo "=== Installing $repo_dir @ $sha ==="; \
      git clone --filter=blob:none "$url" "$repo_dir"; \
      git -C "$repo_dir" checkout "$sha"; \
      if [ -f "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" ]; then \
        pip install -r "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" || \
          echo "WARNING: some deps failed for $repo_dir (continuing)"; \
      fi; \
      if [ -f "/ComfyUI/custom_nodes/$repo_dir/install.py" ]; then \
        cd "/ComfyUI/custom_nodes/$repo_dir" && \
        python install.py || \
          echo "WARNING: install.py failed for $repo_dir (continuing)"; \
      fi; \
    done < /nodes.lock

# Fail the build if the inherited father contract or essential child imports
# are broken. The real SageAttention kernel probe remains in the handler boot.
RUN test -s /comfyui-approved-ref \
    && test -d /ComfyUI/custom_nodes/ComfyUI-Manager \
    && python3 -c "\
import torch; print(f'PyTorch {torch.__version__}'); \
import safetensors; print(f'safetensors {safetensors.__version__}'); \
import transformers; print(f'transformers {transformers.__version__}'); \
import kornia; print('kornia OK'); \
import spandrel; print('spandrel OK'); \
import sageattention; print('sageattention OK'); \
"

# Serverless-only configuration and boot handoff. start_script.sh clones the
# remote ComfyGen handler at boot; the pod runtime's start.sh is intentionally
# not used by this image.
COPY extra_model_paths.yaml /ComfyUI/extra_model_paths.yaml
COPY log_forwarder.py /log_forwarder.py
COPY start_script.sh /start_script.sh
RUN chmod +x /start_script.sh

CMD ["/start_script.sh"]
