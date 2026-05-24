#!/bin/bash
# CPU installer pod entrypoint.
#
# Two modes:
#   - server (default): boot installer_server on :3000 and let BlockFlow drive
#     it over RunPod's HTTP proxy. POST /install/<preset_id> returns an SSE
#     stream; POST /shutdown self-terminates the pod.
#   - oneshot (legacy / ixd): if PRESET_ID env is set or INSTALLER_MODE=oneshot,
#     fetch the manifest, run download_handler, exit. Pod exit stops billing
#     for serverless workers but NOT for pods — see self-terminate block below.
set -euo pipefail

echo "[installer] starting"

: "${PRESET_REGISTRY_MANIFEST:=https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/manifest.json}"
: "${RUNTIME_REPO_URL:=https://github.com/Hearmeman24/remote-comfy-gen-handler.git}"
: "${RUNTIME_REPO_REF:=main}"
: "${INSTALLER_MODE:=server}"

# Mount-path quirk: pods mount the network volume at /workspace, workers at
# /runpod-volume — download_handler hardcodes /runpod-volume so symlink it.
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

# Clone the runtime — idempotent so container restarts don't fail on a stale
# /runtime directory.
if [ -d /runtime/.git ]; then
    git -C /runtime fetch --depth 1 origin "$RUNTIME_REPO_REF"
    git -C /runtime reset --hard FETCH_HEAD
else
    rm -rf /runtime
    git clone --depth 1 --branch "$RUNTIME_REPO_REF" "$RUNTIME_REPO_URL" /runtime
fi
echo "[installer] runtime at $(git -C /runtime rev-parse HEAD)"

cd /runtime

# Mode branch — PRESET_ID env or INSTALLER_MODE=oneshot picks the legacy path.
if [ -n "${PRESET_ID:-}" ] || [ "$INSTALLER_MODE" = "oneshot" ]; then
    : "${PRESET_ID:?PRESET_ID required for oneshot mode}"
    echo "[installer] mode=oneshot preset=$PRESET_ID"
    PRESET_URL=$(curl -fsSL "$PRESET_REGISTRY_MANIFEST" \
        | python3 -c "import json,sys; m=json.load(sys.stdin); print(next(p['preset_url'] for p in m['presets'] if p['id']==sys.argv[1]))" "$PRESET_ID")
    PRESET_JSON=$(curl -fsSL "$PRESET_URL")
    echo "$PRESET_JSON" | python3 -c "
import json, sys
preset = json.load(sys.stdin)
batch = [
    {'source': 'url', 'url': m['url'], 'destination_path': m['dest'], 'sha256': m['sha256']}
    for m in preset.get('models', [])
]
print(json.dumps({'input': {'command': 'download', 'downloads': batch}}))
" > /tmp/job.json

    python -m download_handler --job /tmp/job.json
    HANDLER_RC=$?
    echo "[installer] download_handler exited with rc=$HANDLER_RC"

    if [ -n "${RUNPOD_API_KEY:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
        echo "[installer] self-terminating pod $RUNPOD_POD_ID"
        sleep 2
        curl -s -X DELETE \
            "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" \
            -H "Authorization: Bearer $RUNPOD_API_KEY" >/dev/null || true
    fi
    exit "$HANDLER_RC"
fi

# Server mode: requires INSTALLER_TOKEN so the RunPod HTTP proxy isn't
# wide-open. BlockFlow injects this at pod-spawn time.
: "${INSTALLER_TOKEN:?INSTALLER_TOKEN required for server mode}"
echo "[installer] mode=server port=${INSTALLER_PORT:-3000}"
exec python -m installer_server \
    --port "${INSTALLER_PORT:-3000}" \
    --token "$INSTALLER_TOKEN"
