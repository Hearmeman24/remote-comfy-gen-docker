# DESIGN — cu130 / NVFP4 migration (image v27, full replace of v26)

Status: DESIGN ONLY — no Dockerfile edits, no tag, no push until the human approves.
Author: architect (team `cu130-migration`), 2026-06-18.

## Goal

Move the ComfyGen serverless image from CUDA 12.8 / torch cu128 (released v26) to
CUDA 13.0 / torch cu130 so ComfyUI's native `UNETLoader` loads nvfp4 `_comfy`
checkpoints **compact** (~8.4 GB/expert) instead of upcasting to fp16/fp8. Ship as
**v27, a full replace of v26** (no dual-track). SageAttention stays — rebuilt for
cu130 from source inside the Dockerfile.

Validated as ground truth on a live RTX PRO 6000 (sm_120, driver 580, CUDA 13) pod
today: nvfp4 loads compact through `UNETLoader` on cu130 (ComfyUI bug #11864 fixed),
and the fix **requires** cu130 + cuBLAS 13.x — on cu128 the FP4 matmul returns
`NOT_SUPPORTED` and falls back. The cu128 SageAttention wheel fails to import on
cu130 (`undefined symbol _ZN3c10…`, links `libcudart.so.12`), so it must be rebuilt.

---

## VERIFICATION RESULTS (no blockers)

All external claims below were fetched today, 2026-06-18.

### Base image — RESOLVED
**`nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04`** exists and is the cu130 analog of the
current cu128 devel base.
- Registry confirmed (direct query, not the summarizer):
  `https://hub.docker.com/v2/repositories/nvidia/cuda/tags?name=13.0.3-cudnn-devel-ubuntu24.04`
  → returns `13.0.3-cudnn-devel-ubuntu24.04`, last updated 2026-04-14.
- NVIDIA did NOT fold cudnn out of the image for 13.x — the `-cudnn-devel-` variant
  still exists (13.0.2 and 13.0.3 both present). Use **13.0.3** (newest patch).
- This is a **devel** image: its layer Dockerfile
  (`https://gitlab.com/nvidia/container-images/cuda/-/raw/master/dist/13.0.3/ubuntu2404/devel/Dockerfile`)
  installs `cuda-minimal-build-13-0` (pulls `cuda-nvcc-13-0`), `cuda-cudart-dev-13-0`,
  and `libcublas-dev-13-0`. **nvcc 13.0, cudart-dev, and cublas-dev are already in the
  base.** → The apt `cuda-nvcc-13-0 / cuda-cudart-dev-13-0 / cuda-cccl-13-0` install
  from the validation recipe was only needed because the validation *runtime* pod had no
  toolkit. **In our devel image it is unnecessary** (see Sage build block — this is a
  simplification vs. the brief's assumption).

### Torch pin — RESOLVED (NOT 2.12.1 — see torchaudio ceiling)
**`torch==2.11.0  torchvision==0.26.0  torchaudio==2.11.0`, all `+cu130`, cp312.**

This is the **minimal coherent jump**: same torch *minor* as v26 (2.11.0), only the
CUDA build tag moves cu128 → cu130. Chosen over 2.12.1 for a hard availability reason:

- torch cu130 cp312 wheels present (grep of `https://download.pytorch.org/whl/cu130/torch/`):
  `2.10.0`, `2.11.0`, `2.12.0`, `2.12.1` — all `+cu130-cp312-cp312-manylinux_2_28_x86_64.whl`.
- torchvision cu130 cp312 present (`…/whl/cu130/torchvision/`): `0.24.0 … 0.27.1`,
  including **`0.26.0+cu130`** (pairs with torch 2.11) and `0.27.1+cu130` (pairs with 2.12.1).
- **torchaudio cu130 cp312 caps at `2.11.0`** (`…/whl/cu130/torchaudio/`): only
  `2.10.0+cu130` and `2.11.0+cu130` exist. **There is NO `torchaudio 2.12.x+cu130`
  cp312 wheel.**

ComfyUI's `requirements.txt` lists bare `torchvision`, `torchaudio`, `torchsde`
(verified at `~/src/comfy/ComfyUI/requirements.txt:5-7`). The torch family must be
version-coherent. Therefore:
- **torch 2.12.1 is NOT buildable** as a coherent cu130 set — torchaudio 2.12.x+cu130
  does not exist, so pip would fail to resolve a matching torchaudio (or silently pull a
  CPU build). Picking 2.12.1 is a trap.
- **torch 2.11.0 + torchvision 0.26.0 + torchaudio 2.11.0** all have cu130 cp312 wheels
  → fully coherent, lowest-risk.

The nvfp4 fix does **not** depend on torch *minor* — it depends on cu130 + cuBLAS 13.x,
which torch 2.11.0+cu130 ships (the cu130 torch wheel bundles `nvidia-cublas-cu13` /
links the 13.x cuBLAS). Validation used nightly 2.14.0.dev only because that was the pod's
torch; the cause of the fix is the CUDA/cuBLAS major, not the torch minor. So 2.11.0+cu130
is expected to deliver the compact-load behavior. **The smoke matrix is the gate that
confirms this on the real endpoint (see Risks).**

`torchsde` is pure-Python (not on the cu130 index — correct; it has no CUDA build) and
installs from PyPI as today.

### SageAttention — RESOLVED
Build from source **in the Dockerfile** (replaces the baked-wheel COPY+install). SHA
`d1a57a546c3d395b1ffcbeecc66d81db76f3b4b5` (the proven one). Build args exactly as the
proven recipe: `CUDA_HOME=/usr/local/cuda-13.0`, `TORCH_CUDA_ARCH_LIST="8.0;8.9;12.0"`
(semicolons; `9.0` excluded — Hopper wgmma can't assemble for sm_120). nvcc cross-compiles
for target arches without a GPU, so this runs on CPU CircleCI. Toolkit is already in the
devel base (no apt nvcc install needed). Full block below.

### cuBLAS / comfy-kitchen — RESOLVED
- cuBLAS 13.x is satisfied by the torch cu130 wheel's bundled `nvidia-cublas-cu13`
  (the same mechanism that makes `supports_nvfp4_compute()`'s matmul actually work on
  cu130). The devel base ALSO carries `libcublas-dev-13-0` for the nvcc build. Both are
  13.x → coherent.
- `comfy-kitchen` is already in ComfyUI's `requirements.txt` and ComfyUI's pinned core
  (nodes.lock:11) already has `nvfp4` in `QUANT_ALGOS`. **No node bump needed** —
  `nodes.lock` is unchanged in this PR.

---

## EXACT ORDERED DOCKERFILE CHANGES

Each change cites the current line in `serverless-docker/Dockerfile` (read at SHA on
branch `main`, 2026-06-18). Apply in order.

### Change 1 — base image (`Dockerfile:7`)
```
- FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
+ FROM nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04
```

### Change 2 — torch pin cu128 → cu130 (`Dockerfile:32-40`)
Replace the comment block + the `pip install torch …` line. The comment must be rewritten
because it currently explains the cu128 pinning rationale, which is now wrong.

Current (`Dockerfile:32-40`):
```
# Pin torch to a cu128 build BEFORE anything else installs torch. … cu128 …
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip setuptools wheel packaging && \
    pip install torch==2.11.0 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```
New:
```
# Pin the torch family to cu130 BEFORE anything else installs torch. The base image
# ships nvcc 13.0, and ComfyUI's requirements.txt lists bare torch/torchvision/torchaudio
# that would otherwise resolve to the default-CUDA wheel. cu130 is REQUIRED for native
# nvfp4 compact loading (cuBLAS 13.x FP4 matmul; on cu128 it returns NOT_SUPPORTED and
# ComfyUI falls back to fp16/fp8). All three pins are the highest mutually-coherent cu130
# cp312 wheels: torchaudio cu130 caps at 2.11.0, so the whole family pins to the 2.11.0
# line (torchvision 0.26.0 pairs with torch 2.11.0). The later `-r requirements.txt`
# then sees the torch family already satisfied and skips it.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip setuptools wheel packaging && \
    pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 \
        --index-url https://download.pytorch.org/whl/cu130
```
**Contract note:** torchvision and torchaudio are now **explicitly pinned** (were bare).
This is required — bare names against the cu130 index would let pip pick a torchvision/
torchaudio that mismatches torch 2.11.0. The freeze on `Dockerfile:60-61`
(`pip freeze | grep … > /torch-constraint.txt`) then captures these exact versions and
constrains every downstream custom-node install, exactly as today.

### Change 3 — SageAttention: baked wheel → from-source build (`Dockerfile:63-67`)
Delete the COPY + wheel-install block and replace with a from-source build. The stale
`.whl` file is also deleted from the repo (see Deploy Checklist / repo hygiene).

Current (`Dockerfile:63-67`):
```
# SageAttention (pre-built wheel)
COPY sageattention-2.2.0-cp312-cp312-linux_x86_64.whl /tmp/
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install /tmp/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl && \
    rm /tmp/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl
```
New:
```
# SageAttention — built from source for cu130 (the old cu128 wheel links libcudart.so.12
# and fails to import on cu130). The devel base already provides nvcc 13.0 + cudart-dev +
# cublas-dev, so no extra CUDA apt install is needed. nvcc cross-compiles for the target
# arches without a GPU present (arch is compile-time), so this builds on CPU CI.
#   - SHA d1a57a5 is the proven build point.
#   - TORCH_CUDA_ARCH_LIST uses ';' (setup.py splits on ';'/',', NOT spaces) and EXCLUDES
#     9.0: setup.py broadcasts the full arch list to every module, and the Hopper
#     _qattn_sm90 wgmma kernel cannot assemble for sm_120a. Net coverage: Ampere(8.0) +
#     Ada(8.9) + Blackwell(12.0); no Hopper (sm_90 falls back to default attn, no crash).
# Import success here is NOT proof the kernel runs — the authoritative check is the
# runtime kernel probe in serverless-runtime/start.sh, which only enables
# --use-sage-attention if a real sageattn launch completes on the actual GPU.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    git clone https://github.com/thu-ml/SageAttention.git /tmp/sageattention && \
    git -C /tmp/sageattention checkout d1a57a546c3d395b1ffcbeecc66d81db76f3b4b5 && \
    CUDA_HOME=/usr/local/cuda-13.0 \
    TORCH_CUDA_ARCH_LIST="8.0;8.9;12.0" \
    pip install --no-build-isolation /tmp/sageattention && \
    rm -rf /tmp/sageattention
```
**Notes for the builder (raise any of these with the architect before deviating):**
- `--no-build-isolation` is deliberate: the build must compile against the **already-installed
  torch 2.11.0+cu130** (build isolation would pull a fresh, possibly-mismatched torch into
  the build env). This matches the proven `python setup.py` recipe, which ran in the live env.
- Confirm the upstream repo URL/canonical remote for SHA `d1a57a5` before building
  (`thu-ml/SageAttention` is the canonical home; verify the SHA resolves there). If the SHA
  is not reachable on that remote, STOP and raise with the architect — do not substitute a
  different SHA.
- `CMAKE_BUILD_PARALLEL_LEVEL=8` is already set in ENV (`Dockerfile:13`) — the build will
  parallelize. Expect **+~5 min** CI wall time on this layer (it's CPU nvcc cross-compile of
  3 arch targets × several modules).
- If `pip install <dir>` does not trigger the nvcc build for this SHA (some SageAttention
  revisions need `python setup.py install`), fall back to the proven invocation
  `cd /tmp/sageattention && CUDA_HOME=… TORCH_CUDA_ARCH_LIST=… python setup.py install`
  inside the same RUN. Either is acceptable; the env vars and SHA are the load-bearing parts.

### Change 4 — import-verify layer (`Dockerfile:101-108`) — KEEP, unchanged
The `import sageattention; print('sageattention OK')` line still makes sense: it catches a
broken/incompatible build at image-build time (e.g. the libcudart symbol error we're fixing
would surface here as an ImportError, failing the build fast). It does NOT prove the kernel
runs — but that's by design; the runtime probe in `start.sh:92-105` is the real gate. No edit.

### Change 5 — repo hygiene: delete the stale wheel
`git rm serverless-docker/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl`
(27.9 MB; nothing references it after Change 3). Removing it also prevents accidental
re-introduction of the cu128 artifact.

### NOT changing
- `nodes.lock` — no node bump needed (nvfp4 already in pinned ComfyUI core).
- `start.sh` (runtime repo) — the SageAttention kernel probe (`start.sh:92-105`) is
  arch-agnostic; it launches a real `sageattn` on whatever GPU is present and gates the
  flag. It works unchanged on cu130. No runtime change ships with this image.
- `extra_model_paths.yaml`, `start_script.sh`, `log_forwarder.py`, CivitAI layer — untouched.
- `.circleci/` config — untouched; the existing `^v[0-9]+$` tag pipeline
  (build → update_endpoint → wait_for_rollout → smoke matrix → publish_runtime_manifest →
  notify_done) runs as-is. **CI builds on CPU dind executors** (config.yml docker-dind);
  the from-source Sage build is CPU nvcc cross-compile, so it fits the existing executor.

---

## freeze.py BUG — DECISION: fix in this PR (low-risk, 2-line)

`scripts/freeze.py:41` sets `NODE_BLOCK_START = "RUN for repo in"`, but the Dockerfile's
node loop (`Dockerfile:78-98`) is a `while read -r url sha; do … done < /nodes.lock` loop —
there is no `for repo in` line. So `parse_dockerfile_repos()` (`freeze.py:45-62`) never
enters its block and returns **zero repos**. Effect: `freeze.py` writes a `nodes.lock`
containing **only the ComfyUI core entry** and drops all 28 custom nodes, and
`gather_local_only()` (`freeze.py:105-118`) flags every shipped node as "local-only."
This is a latent footgun for the human's next node-bump release (it would silently empty the
lockfile). It is independent of cu130 but cheap and adjacent.

**Fix (in `freeze.py`):** point the parser at the actual loop and at the real repo source.
The cleanest, lowest-risk fix is to **parse `nodes.lock` itself** as the canonical node
list rather than scraping the Dockerfile, OR retarget the sentinels to the `while read`
loop and read URLs from `nodes.lock` (which the Dockerfile already treats as canonical —
`Dockerfile:42-46, 73-77`). Recommended: change the Dockerfile-scrape to read the existing
`nodes.lock` URLs as the node set (the Dockerfile no longer hardcodes URLs — it reads them
from `nodes.lock`), which makes the parser match reality. Builder to implement against this
contract; **architect signs off on the exact parser change before merge** (it touches the
release tool's source-of-truth semantics).

**If the builder judges the fix non-trivial** (e.g. it would change freeze semantics in a
way that needs its own test), SPLIT IT OUT: ship the cu130 Dockerfile change alone, file
the freeze.py fix as a follow-up. Do not let a tooling fix gate the cu130 release. Raise
with the architect to make that call.

---

## RISKS + SMOKE MATRIX (the validation gate)

The CI smoke matrix (one `smoke_test` job per blockflow preset, expanded from the live
manifest by `generate_continue.py`) IS the acceptance gate. v27 is accepted only if the
full `build-deploy-smoke` workflow goes green end-to-end.

| # | Risk | Why it might bite | Detection / gate |
|---|------|-------------------|------------------|
| R1 | nvfp4 compact-load doesn't reproduce on torch **2.11.0**+cu130 (validation used 2.14.dev) | The fix is attributed to cu130+cuBLAS13, not torch minor | **CLEARED by the breaker.** The nvfp4 path rides on the cuBLAS-13 FP4 kernel AND `torch.nn.functional.scaled_mm`, both present on 2.11.0+cu130: comfy-kitchen gates on `hasattr(F, "scaled_mm")` (present in 2.11), and ComfyUI's nvfp4 gate is `major >= 10` only (no torch-version check). So 2.11.0+cu130 hits the same path as the validated 2.14.dev. **Remaining gap → see "manual nvfp4 verification" below:** no CI smoke preset loads an nvfp4 WAN checkpoint (all 6 presets are bf16/fp8/gguf; the only fp4 string is LTX's text encoder, not a WAN UNET), so **CI-green does NOT prove the headline feature.** A hard manual post-rollout verification step closes this (Deploy Checklist step 5). |
| R2 | SageAttention build fails or builds zero usable kernels on cu130 | New from-source path; arch list / no-build-isolation subtleties | Build layer fails fast (ImportError at `Dockerfile:101-108`) OR runtime probe (`start.sh:92-105`) disables the flag → worker still runs on default attn (no crash). Smoke matrix still passes functionally; perf-only loss. |
| R3 | `was-node-suite-comfyui` (nodes.lock:31) fails to import on cu130 (1 of 28 failed on the validation pod) | Some dep incompatible with cu130/torch 2.11 | Non-fatal: ComfyUI tolerates IMPORT FAILED nodes; `start.sh` repair loop retries deps. Smoke matrix is the judge — if no shipped preset uses a `was-node-suite` class, irrelevant. **Watch item, not a blocker.** Breaker to confirm no smoke preset depends on a was-node-suite class_type. |
| R4 | A custom node pins torch and the cu130 constraint conflicts | `/torch-constraint.txt` now carries cu130 versions | Same mechanism as today (constraint file). Node installs that fail print WARNING and continue (`Dockerfile:91`). Caught by smoke if a needed node breaks. |
| R5 | RunPod host pool too narrow after Min-CUDA bump | Setting "Minimum CUDA 13.0" excludes hosts with driver < 580 | **Mild** — production hosts already run driver 580 (per brief). Mitigation: do the Min-CUDA bump only AFTER rollout is green (deploy checklist step). Rollback re-widens. |
| R6 | FlashBoot warm workers keep the v26 image/wheel after deploy | FlashBoot skips `start_script.sh` cold-boot path | Known behavior. `wait_for_rollout` waits for pods on the new tag; warm v26 workers age out on true cold start. Same as every prior release. |

**Acceptance = full `build-deploy-smoke` green AND the manual nvfp4 verification
(Deploy Checklist step 5) passes.** The breaker confirmed NO CI smoke preset loads an
nvfp4 WAN checkpoint, so CI-green is necessary but **not sufficient** for the headline
goal. The manual nvfp4 load test is therefore a HARD acceptance gate, not optional — see
the Deploy Checklist.

---

## DEPLOY CHECKLIST (human executes on return — nothing here runs now)

The implementation lives on branch `cu130-nvfp4-migration` (single commit, NOT pushed).
The build is a no-node-bump release (`nodes.lock` unchanged), so per
`serverless-docker/CLAUDE.md` "Standard release":

1. **Review the diff.** `git -C serverless-docker/ diff main...cu130-nvfp4-migration` —
   confirm: base image bump, torch family pin to the `+cu130` 2.11.0 line, Sage from-source
   block, stale `.whl` removed, (optional) freeze.py fix. Nothing else should be touched.
2. **Push the branch + merge to `main`** (human's call):
   ```bash
   git -C serverless-docker/ push origin cu130-nvfp4-migration
   # open/merge PR into main (or fast-forward main locally), then:
   ```
3. **Tag + push `v27`** (this triggers CircleCI; the `^v[0-9]+$` filter fires the full pipeline):
   ```bash
   git -C serverless-docker/ tag -a v27 -m "v27 — CUDA 13.0 / torch cu130, native nvfp4 compact load; Sage rebuilt for cu130"
   git -C serverless-docker/ push origin v27
   ```
4. **Watch the pipeline + verify the smoke matrix is fully green**
   (use the CircleCI recipes in `serverless-docker/CLAUDE.md`):
   `build_and_push` (expect ~+5 min vs v26 for the Sage source build) →
   `update_endpoint` (PATCHes template imageName to `hearmeman/comfyui-serverless:v27`) →
   `wait_for_rollout` (polls endpoint pods onto the v27 tag, 30 min timeout) →
   `smoke_test` matrix (one job per preset) → `publish_runtime_manifest` → `notify_done`
   (FlowBot pings on start/build/done). If a single preset fails, rerun-from-failed reruns
   only that preset (cheap), not the build.
5. **MANUAL nvfp4 VERIFICATION — HARD GATE, do not skip.**
   The breaker confirmed **no CI smoke preset loads an nvfp4 WAN UNET** (all 6 presets are
   bf16/fp8/gguf; the only `fp4` string in the manifest is LTX's text encoder, not a WAN
   UNET). So smoke-green does NOT prove the headline nvfp4 feature works — this step is the
   actual proof and is **required before trusting v27**.
   - After v27 is rolled out (step 4 green), submit a **WAN 2.2 I2V** workflow to the
     endpoint using a native **`UNETLoader`** pointed at an nvfp4 `_comfy` checkpoint —
     e.g. `Wan2.2-I2V-A14B_NVFP4_Sparse_high_comfy.safetensors` +
     `Wan2.2-I2V-A14B_NVFP4_Sparse_low_comfy.safetensors`, with `wan_2.1_vae` and the
     `umt5` text encoder, on the live endpoint.
   - **PASS criteria:** each expert loads **COMPACT (~8.4 GB VRAM/expert)** — NOT the
     ~16 GB (fp8) or ~33 GB (fp16) upcast — and the generation **completes**. Compact load
     + successful gen is the proof nvfp4 did NOT silently fall back (ComfyUI bug #11864).
   - **FAIL (upcasts to ~16/33 GB, OOMs, or errors):** nvfp4 is not working on v27 →
     **ROLLBACK to v26** (below). Do not leave production on a half-working image where the
     headline feature silently degrades.
6. **Post-deploy RunPod setting — AFTER step 5 passes:** set the endpoint's
   **"Minimum CUDA version" → 13.0** (RunPod endpoint settings UI). Do this ONLY after v27
   is live and nvfp4-verified, so cu130 workers never land on a < 580 driver. Impact is mild
   (production hosts already on driver 580). Manual UI step — NOT in CI, NOT done by any agent.
7. **Brain-sync the staled gotcha page.** The v26 gotcha
   `docs/brain/pages/gotcha/sageattention-cu128-blackwell-build.md` now describes a
   superseded reality (cu128 pin, baked wheel, Min-CUDA 12.8). After v27 is live, update it
   (or add a cu130 successor page) so the brain reflects: cu130 base, Sage built from source,
   torch 2.11.0+cu130, Min-CUDA 13.0. Run the `brain-sync` skill / bump `updated:` to today.
   Cite this spec. (Per the brain invariants — every page edit bumps `updated:`, new pages
   land in `MOC.md`.)

## ROLLBACK

If v27 is bad in production:
1. **Re-point the endpoint to v26** — fastest path is to re-run the deploy with the v26 tag
   so `update_endpoint` PATCHes the template imageName back:
   - If v26 tag still exists on the docker repo: the simplest revert is to PATCH the
     endpoint template's `imageName` back to `hearmeman/comfyui-serverless:v26` directly
     (RunPod template UI, or the same REST PATCH `update_endpoint` uses), then let pods roll.
     This does NOT require a CI run.
   - Wait for pods to roll back to v26 (true cold start; FlashBoot warm v27 workers age out).
2. **Revert the "Minimum CUDA version"** back to **12.8** (v26 needs only ≥12.8 driver) so
   the host pool re-widens to match the cu128 image — per the v26 gotcha note
   (`docs/brain/pages/gotcha/sageattention-cu128-blackwell-build.md:38`).
3. v26 is unchanged on Docker Hub (full-replace means we changed the tag we *deploy*, not v26
   itself), so rollback is just a redeploy of an existing, known-good image.

---

## OPEN ITEMS — RESOLVED by the breaker (2026-06-18)

These were posed as paper-attack questions; the breaker has answered them. Recorded here so
the *why* survives.

1. **R1 (torch 2.11.0 vs 2.14.dev for nvfp4) — CLEARED.** The nvfp4 path rides on the
   cuBLAS-13 FP4 kernel AND `torch.nn.functional.scaled_mm`, both present on 2.11.0+cu130.
   comfy-kitchen gates on `hasattr(F, "scaled_mm")` (present in 2.11); ComfyUI's nvfp4 gate
   is `major >= 10` only (no torch-version check). No torch-2.12+ dependency exists, so the
   torchaudio-cu130-caps-at-2.11 constraint is NOT in tension with the feature. The "drop
   torchaudio" escape hatch is unnecessary — do not pursue it.
2. **nvfp4 smoke-preset existence — CONFIRMED ABSENT (gap closed by process).** No CI smoke
   preset loads an nvfp4 WAN UNET (6 presets bf16/fp8/gguf; only fp4 string is LTX's text
   encoder). CI-green ≠ goal-met. Closed by the **HARD manual nvfp4 verification** in Deploy
   Checklist step 5, now an acceptance gate.
3. **Sage build invocation & canonical remote — builder to confirm at build time** (item
   retained in Change 3's builder notes; either `pip install <dir>` or `setup.py install` is
   acceptable, env vars + SHA are load-bearing). No defect found in the proposed block.
4. **`--no-build-isolation` torch visibility — sound** (the build must see the installed
   torch 2.11.0+cu130 for ABI match; that's exactly why isolation is disabled).
5. **freeze.py fix — builder's branch includes it; no Dockerfile defects found.** Architect
   still signs off on the exact parser change semantics before merge (release-tool source of
   truth). Splitting to a follow-up remains acceptable.
