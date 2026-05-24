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

# Clone runtime — idempotent so container restarts (which RunPod pods can do
# after CMD exits, since the container disk persists) don't fail on a stale
# /runtime directory.
if [ -d /runtime/.git ]; then
    git -C /runtime fetch --depth 1 origin "$RUNTIME_REPO_REF"
    git -C /runtime reset --hard FETCH_HEAD
else
    rm -rf /runtime
    git clone --depth 1 --branch "$RUNTIME_REPO_REF" "$RUNTIME_REPO_URL" /runtime
fi
echo "[installer] runtime at $(git -C /runtime rev-parse HEAD)"

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
python -m download_handler --job /tmp/job.json
HANDLER_RC=$?
echo "[installer] download_handler exited with rc=$HANDLER_RC"

# Self-terminate so billing stops. RunPod pods bill for the entire wall time
# the pod is allocated — CMD exiting does NOT stop billing the way it does
# for serverless workers. We delete ourselves via the REST API; the caller
# (BlockFlow) reads the {"ok": ...} JSON from the saved pod logs.
#
# Opt-in: requires RUNPOD_API_KEY in env. Without it, the container exits
# but the pod stays allocated until the caller deletes it.
if [ -n "${RUNPOD_API_KEY:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
    echo "[installer] self-terminating pod $RUNPOD_POD_ID"
    sleep 2  # flush logs
    curl -s -X DELETE \
        "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" \
        -H "Authorization: Bearer $RUNPOD_API_KEY" >/dev/null || true
fi

exit "$HANDLER_RC"
