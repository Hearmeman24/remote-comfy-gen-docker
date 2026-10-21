#!/bin/bash
set -euo pipefail

# --- Log forwarding: capture ALL output from the very start ---
# Gracefully no-ops if LOG_RECEIVER_URL is not set.
if [[ -n "${LOG_RECEIVER_URL:-}" && -z "${_LOG_FORWARDER_ACTIVE:-}" ]]; then
    export _LOG_FORWARDER_ACTIVE=1
    exec > >(python3 /log_forwarder.py) 2>&1
fi

echo "[start_script] Starting ComfyUI serverless worker..."

RUNTIME_DIR="/opt/comfygen-runtime"
RUNTIME_REPO_URL="${RUNTIME_REPO_URL:-}"
RUNTIME_REPO_REF="${RUNTIME_REPO_REF:-main}"
GITHUB_PAT="${GITHUB_PAT:-}"

# --- Clone or pull runtime repo ---
if [ -z "$RUNTIME_REPO_URL" ]; then
    echo "[start_script] ERROR: RUNTIME_REPO_URL not set"
    exit 1
fi

# Supply private-repository credentials only to the Git process. Keeping the
# remote URL clean prevents the token from being written into .git/config.
git_with_pat() {
    if [ -n "$GITHUB_PAT" ]; then
        local auth_header
        auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_PAT" | base64 | tr -d '\n')"
        GIT_CONFIG_COUNT=1 \
            GIT_CONFIG_KEY_0=http.extraheader \
            GIT_CONFIG_VALUE_0="$auth_header" \
            git "$@"
    else
        git "$@"
    fi
}

if [ -d "$RUNTIME_DIR/.git" ]; then
    cd "$RUNTIME_DIR"
    git remote set-url origin "$RUNTIME_REPO_URL"

    # Skip fetch if already at the latest remote commit
    LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null || echo "none")
    REMOTE_HASH=$(git_with_pat ls-remote origin "$RUNTIME_REPO_REF" 2>/dev/null | cut -f1 || echo "unknown")

    if [ "$LOCAL_HASH" = "$REMOTE_HASH" ] && [ "$REMOTE_HASH" != "unknown" ]; then
        echo "[start_script] Runtime already at $RUNTIME_REPO_REF ($LOCAL_HASH) — skipping fetch"
    else
        echo "[start_script] Pulling latest runtime code ($LOCAL_HASH -> $REMOTE_HASH)..."
        git_with_pat fetch origin
        git reset --hard "origin/$RUNTIME_REPO_REF"
    fi
else
    echo "[start_script] Cloning runtime repo..."
    git_with_pat clone --depth 1 --branch "$RUNTIME_REPO_REF" "$RUNTIME_REPO_URL" "$RUNTIME_DIR"
fi

# The runtime does not need the repository token after its checkout is ready.
unset GITHUB_PAT

echo "[start_script] Runtime repo ready at $RUNTIME_DIR (ref: $RUNTIME_REPO_REF)"

# --- Hand off to runtime start.sh ---
echo "[start_script] Launching runtime start.sh..."
exec bash "$RUNTIME_DIR/start.sh"
