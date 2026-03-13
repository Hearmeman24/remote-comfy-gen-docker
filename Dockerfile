FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_PREFER_BINARY=1 \
    CMAKE_BUILD_PARALLEL_LEVEL=8

# System deps (libgl1 replaces libgl1-mesa-glx on Ubuntu 24.04)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3.12-dev python3-pip \
    build-essential gcc g++ ninja-build \
    git git-lfs aria2 ffmpeg wget curl ca-certificates \
    libgl1 libglib2.0-0 libopenblas-dev liblapack-dev \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && python3.12 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"

# ComfyUI + all deps (includes torch, safetensors, transformers, etc.)
# Keep ComfyUI in /ComfyUI (baked into image) for fast cold starts
RUN pip install --upgrade pip setuptools wheel packaging && \
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /ComfyUI && \
    pip install -r /ComfyUI/requirements.txt

# Freeze torch versions to prevent custom node deps from upgrading/downgrading
RUN pip freeze | grep -E "^(torch|torchvision|torchaudio|torchsde)==" > /torch-constraint.txt

# SageAttention (pre-built wheel)
COPY sageattention-2.2.0-cp312-cp312-linux_x86_64.whl /tmp/
RUN pip install /tmp/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl && \
    rm /tmp/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl

# RunPod SDK + runtime deps
RUN pip install runpod boto3 requests websocket-client

# Clone and install all custom nodes (baked for fast cold starts)
# Each node: clone repo, install requirements.txt (constrained to frozen torch),
# run install.py if present. This avoids runtime dep installation.
RUN for repo in \
    https://github.com/M1kep/ComfyLiterals.git \
    https://github.com/yolain/ComfyUI-Easy-Use.git \
    https://github.com/Fannovel16/ComfyUI-Frame-Interpolation.git \
    https://github.com/kijai/ComfyUI-KJNodes.git \
    https://github.com/theUpsider/ComfyUI-Logic.git \
    https://github.com/ltdrdata/ComfyUI-Manager.git \
    https://github.com/gabe-init/ComfyUI-Openrouter_node.git \
    https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git \
    https://github.com/wallen0322/ComfyUI-WanAnimate-Enhancer.git \
    https://github.com/kijai/ComfyUI-WanAnimatePreprocess.git \
    https://github.com/kijai/ComfyUI-WanVideoWrapper.git \
    https://github.com/kijai/ComfyUI-segment-anything-2.git \
    https://github.com/Suzie1/ComfyUI_Comfyroll_CustomNodes.git \
    https://github.com/JPS-GER/ComfyUI_JPS-Nodes.git \
    https://github.com/chflame163/ComfyUI_LayerStyle.git \
    https://github.com/aining2022/ComfyUI_Swwan.git \
    https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git \
    https://github.com/cubiq/ComfyUI_essentials.git \
    https://github.com/ClownsharkBatwing/RES4LYF.git \
    https://github.com/chrisgoringe/cg-image-picker.git \
    https://github.com/chrisgoringe/cg-use-everywhere.git \
    https://github.com/Jordach/comfy-plasma.git \
    https://github.com/Fannovel16/comfyui_controlnet_aux.git \
    https://github.com/bash-j/mikey_nodes.git \
    https://github.com/rgthree/rgthree-comfy.git \
    https://github.com/WASasquatch/was-node-suite-comfyui.git; \
  do \
    cd /ComfyUI/custom_nodes; \
    repo_dir=$(basename "$repo" .git); \
    echo "=== Installing $repo_dir ==="; \
    git clone --depth 1 "$repo"; \
    if [ -f "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" ]; then \
      pip install -r "/ComfyUI/custom_nodes/$repo_dir/requirements.txt" \
        --constraint /torch-constraint.txt || \
        echo "WARNING: some deps failed for $repo_dir (continuing)"; \
    fi; \
    if [ -f "/ComfyUI/custom_nodes/$repo_dir/install.py" ]; then \
      cd "/ComfyUI/custom_nodes/$repo_dir" && \
      python install.py || \
        echo "WARNING: install.py failed for $repo_dir (continuing)"; \
    fi; \
  done

# Verify key imports (fail fast if something is broken)
RUN python3 -c "\
import torch; print(f'PyTorch {torch.__version__}'); \
import safetensors; print(f'safetensors {safetensors.__version__}'); \
import transformers; print(f'transformers {transformers.__version__}'); \
import kornia; print('kornia OK'); \
import spandrel; print('spandrel OK'); \
import sageattention; print('sageattention OK'); \
"

# CivitAI downloader (uses aria2c which is already installed above)
RUN git clone --depth 1 https://github.com/Hearmeman24/CivitAI_Downloader /tools/civitai-downloader

# extra_model_paths.yaml tells ComfyUI to look at network volume for models
COPY extra_model_paths.yaml /ComfyUI/extra_model_paths.yaml
COPY log_forwarder.py /log_forwarder.py
COPY start_script.sh /start_script.sh
RUN chmod +x /start_script.sh

CMD ["/start_script.sh"]
