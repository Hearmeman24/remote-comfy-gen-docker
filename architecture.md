# Architecture

> This document is progressively updated across sessions and may be incomplete.

## Overview

Agent-first CLI for executing ComfyUI workflows on remote RunPod serverless endpoints. Two-repo design separates the infrequently-changing Docker image from the frequently-changing runtime handler.

## Repository Structure

| Repo | Path | Purpose | Tag strategy |
|------|------|---------|--------------|
| `serverless-docker` | `serverless-docker/` | Docker image: ComfyUI + baked custom nodes | Version tags (v10, v11, v12…) |
| `serverless-runtime` | `serverless-runtime/` | RunPod handler + startup scripts | Branch push only (no tags) |
| `comfy_gen` | `comfy_gen/` | CLI tool (`pip install -e .`) | — |

## Docker Image (`serverless-docker`)

### Base
- CUDA base image + ComfyUI cloned to `/ComfyUI` (baked, not deleted)
- SageAttention + RunPod SDK installed

### Baked Custom Nodes (25 nodes, added v11)
All cloned to `/ComfyUI/custom_nodes/` at build time with `--constraint /torch-constraint.txt`:

```
ComfyLiterals, ComfyUI-Easy-Use, ComfyUI-Frame-Interpolation, ComfyUI-KJNodes,
ComfyUI-Logic, ComfyUI-Manager, ComfyUI-Openrouter_node, ComfyUI-VideoHelperSuite,
ComfyUI-WanAnimate-Enhancer, ComfyUI-WanAnimatePreprocess, ComfyUI-WanVideoWrapper,
ComfyUI-segment-anything-2, ComfyUI_Comfyroll_CustomNodes, ComfyUI_JPS-Nodes,
ComfyUI_LayerStyle, ComfyUI_Swwan, ComfyUI_UltimateSDUpscale, ComfyUI_essentials,
RES4LYF, cg-image-picker, cg-use-everywhere, comfy-plasma, comfyui_controlnet_aux,
mikey_nodes, rgthree-comfy, was-node-suite-comfyui
```

**Critical constraint**: Do NOT bake `onnxruntime-gpu` — it causes segfaults in RunPod's cpuset-constrained containers (root cause of v8 crash-loop). Use `onnxruntime` (CPU-only) if needed.

### Model Paths
`extra_model_paths.yaml` (baked at `/ComfyUI/extra_model_paths.yaml`) points all model types at the network volume:
```yaml
comfyui:
  base_path: /runpod-volume/ComfyUI/
  checkpoints: models/checkpoints/
  loras: models/loras/
  vae: models/vae/
  # ... all standard types + latent_upscale_models
```

`start.sh` patches this file at runtime via `sed` to add any missing keys (e.g. `latent_upscale_models`), avoiding Docker rebuilds for config additions.

## Runtime Handler (`serverless-runtime`)

### Startup Flow
1. **`start_script.sh`** (Docker image): Sets up log forwarding → clones/pulls runtime repo → execs `start.sh`
2. **`start.sh`** (runtime repo): Patches `extra_model_paths.yaml` → starts ComfyUI from `/ComfyUI` with `--extra-model-paths-config` → starts RunPod worker

### Handler (`worker.py`) — Job Processing Flow
```
receive job
  → apply overrides (model substitutions, etc.)
  → MODEL PRE-FLIGHT: check all workflow model files exist on disk
      → if missing: return error with list of missing filenames (fast fail, no ComfyUI needed)
  → NODE PRE-FLIGHT (preflight.py): scan workflow class_types against installed nodes
      → if missing: clone repo + pip install deps → restart ComfyUI
  → queue_prompt to ComfyUI via SSH (RunPod proxy blocks direct POST)
  → poll for completion → collect outputs → upload to S3
  → on 400 "node X does not exist": fallback to node_installer.ensure_nodes() + retry
```

### Pre-flight Checks

#### Node Pre-flight (`preflight.py`)
- **No running ComfyUI required** — pure filesystem scan
- Loads `extension-node-map.json` from baked ComfyUI-Manager
- Builds reverse map: `class_type → repo_url` (first-match-wins)
- Excludes core ComfyUI repo from the custom node lookup
- Diffs workflow `class_type` values against installed `custom_nodes/` directories
- Handles both API format (`class_type` key) and web format (`type` key in nodes array) — but serverless only receives API format
- Returns `{"missing": {"RepoName": "https://github.com/..."}, "installed": [...]}`

#### Model Pre-flight
- Extracts model filenames from loader nodes via `_extract_model_refs()`
- Walks `/ComfyUI/models/` (baked) and `/runpod-volume/ComfyUI/models/` (network volume)
- Also reads paths from `extra_model_paths.yaml`
- Fast fail: returns error JSON with `missing_models` list before any ComfyUI interaction

### Key Constraints
- **RunPod proxy blocks POST to ComfyUI** — all workflow submission via SSH with `-tt` flag
- **SSH pattern**: pipe commands via stdin with `; exit`, clean PTY output (ANSI codes)
- **ComfyUI path**: `/ComfyUI` (baked image), models on `/runpod-volume/ComfyUI/models/` (network volume)
- **ComfyUI restart** passes `--extra-model-paths-config /ComfyUI/extra_model_paths.yaml`

## CLI Tool (`comfy_gen`)

### Key Module: `serverless.py`
- `submit()` — builds RunPod API payload, sends to serverless endpoint
- Logs full request payload to `~/.comfy-gen/logs.txt` (append mode, timestamped)
- All CLI stdout is valid JSON; human-readable logs go to stderr or log file

### Config
- `~/.comfy-gen/config.json` — RunPod API key, endpoint ID, S3 credentials, SSH config

## Cold Start Optimization

| Technique | Impact | Status |
|-----------|--------|--------|
| Bake 25 custom nodes into Docker image | ~58s → ~5s ComfyUI startup | **Done (v11)** |
| Bake node deps with torch constraints | No pip install on cold start | **Done (v11)** |
| Pre-flight node check (before ComfyUI starts) | Eliminates observe-error-restart cycle | **Done** |
| `sed` patch for extra_model_paths.yaml | Config changes without Docker rebuild | **Done (v12)** |
| Keep warm worker (Min Workers = 1) | Eliminates cold start for first request | Available in RunPod settings |

**Result**: Cold starts reduced from >60s to ~5-12s.

## RunPod API Integration

Dynamic pod discovery via GraphQL API using `machine.podHostId`:
```
SSH address = {machine.podHostId}@ssh.runpod.io
```

API key stored in `RUNPOD_API_KEY` environment variable (set in `~/.zshrc`).

## Known Issues / Broken Nodes
These nodes fail to import on every startup (code bugs, not missing deps):
- **ComfyUI-VibeVoice** — duplicate transformers registration
- **ComfyUI-TeaCache** — broken import path (`No module named 'ComfyUI-TeaCache.models'`)
- **ComfyUI_LayerStyle_Advance** — `AutoModelForVision2Seq` doesn't exist in installed transformers version

These are harmless — worker runs fine without them. Repair loop attempts are futile for these.

## Version History (Docker)

| Tag | Key Changes |
|-----|------------|
| v7 | Working baseline — no baked custom nodes |
| v8–v9 | Added `onnxruntime-gpu` → crash-loop (SIGTERM at 1.4s) |
| v10 | Reverted to v7 Dockerfile |
| v11 | Baked 25 custom nodes + `extra_model_paths.yaml` |
| v12 | Added `latent_upscale_models` to baked yaml |
