# Make ComfyGen serverless a child of the shared ComfyUI base

- **Status:** accepted
- **Work type:** migration
- **Authority:** implementation requested

## Review surface

### Outcome

`hearmeman/comfyui-serverless` inherits the same CUDA 13 father image used by the pod
templates. CUDA, Python, Torch, ComfyUI, SageAttention, ComfyUI-Manager, and the
CivitAI downloader have one owner: `comfyui-runtime/base/Dockerfile`. The
serverless image adds only ComfyGen's custom nodes, RunPod dependencies,
serverless configuration, and its entrypoint.

### Done when

- [x] The serverless Dockerfile builds `FROM` the shared `comfyui-base` selected
  by `pins.json` — source: user outcome.
- [x] The father publishes only the CUDA 13 variant used across BlockFlow;
  CUDA 12.8 is no longer a maintained build path — source: user outcome.
- [x] The child image no longer installs its own CUDA, Python, Torch, ComfyUI,
  SageAttention, ComfyUI-Manager, or CivitAI downloader — source: shared-base
  ownership invariant.
- [x] CI passes the `pins.json` base into the Docker build and the runtime
  manifest identifies that base — source: release traceability invariant.
- [x] Existing custom-node locks, RunPod startup, model paths, and endpoint
  rollout sequencing remain intact — source: repository invariants.
- [x] Static regression tests prove the ownership boundary and existing test
  suites pass — source: verification.

## Execution contract

### Problem and evidence

The production image currently duplicates the father image's whole GPU stack.
It starts directly from NVIDIA CUDA, creates Python 3.12, installs the Torch
trio, clones ComfyUI, installs SageAttention, and clones the CivitAI downloader
(`Dockerfile`). The shared base already owns each of those layers
(`../comfyui-runtime/base/Dockerfile`), and the pod templates already consume it
through `ARG BASE_IMAGE` plus `pins.json` (`../comfyui-minimax/Dockerfile`,
`../comfyui-minimax/pins.json`).

- **Verified:** The serverless-only runtime lifecycle remains separate: the
  image entrypoint clones `remote-comfy-gen-handler` and executes its `start.sh`
  (`start_script.sh:13-55`).
- **Verified:** The shared pod runtime's `src/start.sh` cannot replace that
  handler because it owns a persistent-pod lifecycle rather than a RunPod
  serverless handler (`../comfyui-runtime/src/start.sh`).
- **Verified:** The serverless handler expects ComfyUI-Manager at the historical
  `ComfyUI-Manager` path, while the father image installs it as lowercase
  `comfyui-manager` (`../remote_comfy_generator/serverless-runtime/preflight.py:18-21`,
  `../comfyui-runtime/base/Dockerfile:130-140`).

### Scope and non-goals

- **In scope:** serverless worker Docker inheritance, base pin ownership,
  custom-node lock ownership, CI build wiring, runtime-manifest traceability,
  architecture documentation, and regression tests.
- **Non-goals:** replacing `remote-comfy-gen-handler` with the pod boot runtime,
  changing job payloads, changing model-volume paths, rebuilding the CPU-only
  installer image, publishing a Docker tag, or repointing the live endpoint.

### Owners, invariants, and approach

- **Authoritative owner:** `comfyui-runtime/base/Dockerfile` owns the shared GPU
  software stack; its immutable Docker tag records CUDA, ComfyUI, and Torch.
- **Authoritative child pin:** `serverless-docker/pins.json` selects the father
  image consumed by CircleCI.
- **Must preserve:** `/ComfyUI`, `/opt/venv`, inherited `PIP_CONSTRAINT`,
  `/runpod-volume/ComfyUI/models`, the current custom-node SHAs, RunPod handler
  startup, versioned serverless image tags, and rollout-before-manifest ordering.
- **Approach:** follow the existing pod-template pattern. Build from
  `hearmeman/comfyui-base:cu130-comfy0.32.0-torch2.11.0`, install only
  serverless-specific packages and locked nodes, adapt the father's lowercase
  Manager directory to the serverless handler's historical uppercase path, and
  publish `base_image` instead of duplicating ComfyUI's SHA in `nodes.lock`.

### Verification

- **Regression seam:** source-level contract tests validate ownership and CI
  wiring without requiring paid GPU infrastructure.
- **Focused check:** `python3 scripts/test_image_contract.py`
- **Wider check:** `python3 scripts/test_freeze.py && python3 .circleci/test_generate_continue.py`
- **Build boundary:** build the Linux image when a Docker daemon is available;
  a real SageAttention kernel launch still requires a RunPod GPU canary.

### Rollout and recovery

- **Rollout:** merge and tag the serverless repository only after validation;
  its existing CircleCI workflow builds the child, updates endpoint
  `5fkobbksgrtww6`, waits for rollout, and only then publishes the manifest.
- **Recovery:** do not deploy from this task. A later production rollout remains
  recoverable by repointing the endpoint template to the previous versioned
  `hearmeman/comfyui-serverless:vN` image.
