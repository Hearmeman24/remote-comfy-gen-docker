# syntax=docker/dockerfile:1.7
# ComfyGen's RunPod serverless image is a thin child of the shared HearmemanAI
# ComfyUI base. comfyui-runtime/base/Dockerfile owns CUDA, Python, Torch,
# ComfyUI, ComfyUI-Manager, SageAttention, and the CivitAI downloader.
# pins.json is the release source of truth; CircleCI passes the same value as
# BASE_IMAGE, while this default keeps a plain local build coherent.
ARG BASE_IMAGE=hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9b-torch2.11.0
FROM ${BASE_IMAGE}
ARG BASE_IMAGE

LABEL org.hearmemanai.comfyui.base-image="${BASE_IMAGE}"

ENV PYTHONDONTWRITEBYTECODE=1
# ComfyUI-ControlNet-Aux caches HF annotator assets under <repo id>/<filename>.
# The global volume preloads the exact DWPreprocessor models at these paths.
ENV AUX_ANNOTATOR_CKPTS_PATH=/runpod-volume/ComfyUI/models/annotator_ckpts

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
# The workflow-critical additions below must install their declared requirements
# successfully; optional pack dependency failures retain the warning path.
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
      required_workflow_node=false; \
      case "$repo_dir" in \
        ComfyUI-SeedVR2_VideoUpscaler|ComfyUI-Detail-Daemon|ComfyUI-OpenRouter-Simple) \
          required_workflow_node=true; \
          test -f "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" || { \
            echo "FATAL: required workflow node $repo_dir has no requirements.txt"; \
            exit 1; \
          } ;; \
      esac; \
      if [ -f "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" ]; then \
        if [ "$required_workflow_node" = true ]; then \
          pip install -r "/ComfyUI/custom_nodes/$repo_dir/requirements.txt"; \
        else \
          pip install -r "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" || \
            echo "WARNING: some deps failed for $repo_dir (continuing)"; \
        fi; \
      fi; \
      if [ -f "/ComfyUI/custom_nodes/$repo_dir/install.py" ]; then \
        cd "/ComfyUI/custom_nodes/$repo_dir"; \
        if [ "$required_workflow_node" = true ]; then \
          python install.py; \
        else \
          python install.py || \
            echo "WARNING: install.py failed for $repo_dir (continuing)"; \
        fi; \
      fi; \
    done < /nodes.lock

# Fail the build if the inherited father contract or essential child imports
# are broken. The real SageAttention kernel probe remains in the handler boot.
RUN PYTHONPATH=/ComfyUI python3 - <<'PY'
import subprocess
from pathlib import Path

import comfyui_version

approved = Path("/comfyui-approved-ref").read_text(encoding="utf-8").strip()
head = subprocess.check_output(
    ["git", "-C", "/ComfyUI", "rev-parse", "HEAD"], text=True
).strip()
resolved = subprocess.check_output(
    ["git", "-C", "/ComfyUI", "rev-parse", "--verify", f"{approved}^{{commit}}"],
    text=True,
).strip()
print(f"ComfyUI {comfyui_version.__version__}; git HEAD {head}; approved {resolved}")
if head != resolved:
    raise SystemExit(f"ComfyUI HEAD {head} does not match approved ref {resolved}")
PY

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
