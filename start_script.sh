#!/bin/bash
set -euo pipefail

# --- Log forwarding: capture ALL output from the very start ---
# Gracefully no-ops if LOG_RECEIVER_URL is not set.
if [[ -n "${LOG_RECEIVER_URL:-}" && -z "${_LOG_FORWARDER_ACTIVE:-}" ]]; then
    export _LOG_FORWARDER_ACTIVE=1
    exec > >(python3 /log_forwarder.py) 2>&1
fi

echo "[start_script] Starting ComfyUI serverless worker..."

RUNTIME_DIR="/runpod-volume/runtime"
RUNTIME_REPO_URL="${RUNTIME_REPO_URL:-}"
RUNTIME_REPO_REF="${RUNTIME_REPO_REF:-main}"
GITHUB_PAT="${GITHUB_PAT:-}"

# --- Clone or pull runtime repo ---
if [ -z "$RUNTIME_REPO_URL" ]; then
    echo "[start_script] ERROR: RUNTIME_REPO_URL not set"
    exit 1
fi

# Inject PAT into URL if provided
if [ -n "$GITHUB_PAT" ]; then
    CLONE_URL=$(echo "$RUNTIME_REPO_URL" | sed "s|https://|https://${GITHUB_PAT}@|")
else
    CLONE_URL="$RUNTIME_REPO_URL"
fi

if [ -d "$RUNTIME_DIR/.git" ]; then
    cd "$RUNTIME_DIR"
    git remote set-url origin "$CLONE_URL"

    # Skip fetch if already at the latest remote commit
    LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null || echo "none")
    REMOTE_HASH=$(git ls-remote origin "$RUNTIME_REPO_REF" 2>/dev/null | cut -f1 || echo "unknown")

    if [ "$LOCAL_HASH" = "$REMOTE_HASH" ] && [ "$REMOTE_HASH" != "unknown" ]; then
        echo "[start_script] Runtime already at $RUNTIME_REPO_REF ($LOCAL_HASH) — skipping fetch"
    else
        echo "[start_script] Pulling latest runtime code ($LOCAL_HASH -> $REMOTE_HASH)..."
        git fetch origin
        git reset --hard "origin/$RUNTIME_REPO_REF"
    fi
else
    echo "[start_script] Cloning runtime repo..."
    git clone --depth 1 --branch "$RUNTIME_REPO_REF" "$CLONE_URL" "$RUNTIME_DIR"
fi

echo "[start_script] Runtime repo ready at $RUNTIME_DIR (ref: $RUNTIME_REPO_REF)"

# --- Hand off to runtime start.sh ---
echo "[start_script] Launching runtime start.sh..."
exec bash "$RUNTIME_DIR/start.sh"
