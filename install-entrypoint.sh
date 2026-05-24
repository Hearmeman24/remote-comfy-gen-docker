#!/bin/bash
# CPU installer pod entrypoint.
#
# Clones serverless-runtime, fetches the BlockFlow preset manifest, translates
# preset.models into a download_handler job, and exec's the handler in CLI
# mode. Pod exits with the handler's status: 0 on success, non-zero on failure.
# RunPod stops billing on pod exit.
set -euo pipefail

echo "[installer] starting"

: "${PRESET_ID:?env var required}"
: "${PRESET_REGISTRY_MANIFEST:=https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/manifest.json}"
: "${RUNTIME_REPO_URL:=https://github.com/Hearmeman24/remote-comfy-gen-handler.git}"
: "${RUNTIME_REPO_REF:=main}"

# Volume preflight — fail fast with a clear exit code (2) so the BlockFlow
# poller can distinguish "infra wrong" from "download failed".
#
# Mount-path quirk: RunPod **pods** mount network volumes at /workspace, but
# **serverless workers** mount the same volume at /runpod-volume — and
# download_handler.py hardcodes /runpod-volume/ComfyUI/models so worker code
# remains a single source of truth. If we're on a pod (/workspace exists,
# /runpod-volume doesn't), symlink to keep paths consistent.
if [ ! -d /runpod-volume ] && [ -d /workspace ]; then
    ln -s /workspace /runpod-volume
    echo "[installer] symlinked /runpod-volume -> /workspace (pod-mount mode)"
fi
if [ ! -d /runpod-volume ]; then
    echo "[installer] FATAL: network volume not mounted (neither /runpod-volume nor /workspace)"
    exit 2
fi
if ! touch /runpod-volume/.installer-write-test 2>/dev/null; then
    echo "[installer] FATAL: /runpod-volume is not writable"
    exit 2
fi
rm -f /runpod-volume/.installer-write-test

# Clone runtime — same pattern as the GPU image's start_script.sh, minus the
# warm-pull optimization (one-shot pod, no reuse).
git clone --depth 1 --branch "$RUNTIME_REPO_REF" "$RUNTIME_REPO_URL" /runtime
echo "[installer] runtime cloned at $(git -C /runtime rev-parse HEAD)"

echo "[installer] fetching preset $PRESET_ID"
PRESET_URL=$(curl -fsSL "$PRESET_REGISTRY_MANIFEST" \
    | python3 -c "import json,sys; m=json.load(sys.stdin); print(next(p['preset_url'] for p in m['presets'] if p['id']==sys.argv[1]))" "$PRESET_ID")
PRESET_JSON=$(curl -fsSL "$PRESET_URL")

# Translate preset.models → download_handler batch. Worker dispatch shape:
# {"input": {"command": "download", "downloads": [...]}}.
echo "$PRESET_JSON" | python3 -c "
import json, sys
preset = json.load(sys.stdin)
batch = [
    {'source': 'url', 'url': m['url'], 'destination_path': m['dest'], 'sha256': m['sha256']}
    for m in preset.get('models', [])
]
print(json.dumps({'input': {'command': 'download', 'downloads': batch}}))
" > /tmp/job.json

cd /runtime
exec python -m download_handler --job /tmp/job.json
