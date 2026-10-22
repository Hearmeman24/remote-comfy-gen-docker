#!/bin/bash
set -euo pipefail

# Keep existing RunPod log forwarding behavior without changing start_script.sh.
if [[ -n "${LOG_RECEIVER_URL:-}" && -z "${_LOG_FORWARDER_ACTIVE:-}" ]]; then
    export _LOG_FORWARDER_ACTIVE=1
    exec > >(python3 /log_forwarder.py) 2>&1
fi

echo "[baked-regional] Starting pinned ComfyGen runtime..."
RUNTIME_DIR="/opt/comfygen-runtime"
RUNTIME_REPO_URL="${RUNTIME_REPO_URL:-https://github.com/Hearmeman24/remote-comfy-gen-handler.git}"
RUNTIME_REPO_REF="60fdc14d6b3a4bccf181f8aa8e6983d0311766da"
GITHUB_PAT="${GITHUB_PAT:-}"
if [[ -z "$RUNTIME_REPO_URL" ]]; then
    echo "[baked-regional] ERROR: RUNTIME_REPO_URL not set"
    exit 1
fi

git_with_pat() {
    if [[ -n "$GITHUB_PAT" ]]; then
        local auth_header
        auth_header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_PAT" | base64 | tr -d '\n')"
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader \
            GIT_CONFIG_VALUE_0="$auth_header" git "$@"
    else
        git "$@"
    fi
}

if [[ -d "$RUNTIME_DIR/.git" ]]; then
    git -C "$RUNTIME_DIR" remote set-url origin "$RUNTIME_REPO_URL"
    CURRENT="$(git -C "$RUNTIME_DIR" rev-parse HEAD 2>/dev/null || echo none)"
    if [[ "$CURRENT" != "$RUNTIME_REPO_REF" ]]; then
        git_with_pat -C "$RUNTIME_DIR" fetch --depth=1 origin "$RUNTIME_REPO_REF"
        git_with_pat -C "$RUNTIME_DIR" checkout --detach FETCH_HEAD
    fi
else
    rm -rf "$RUNTIME_DIR"
    git_with_pat clone --filter=blob:none --no-checkout "$RUNTIME_REPO_URL" "$RUNTIME_DIR"
    git_with_pat -C "$RUNTIME_DIR" fetch --depth=1 origin "$RUNTIME_REPO_REF"
    git_with_pat -C "$RUNTIME_DIR" checkout --detach FETCH_HEAD
fi
ACTUAL="$(git -C "$RUNTIME_DIR" rev-parse HEAD)"
if [[ "$ACTUAL" != "$RUNTIME_REPO_REF" ]]; then
    echo "[baked-regional] ERROR: runtime HEAD $ACTUAL does not match pinned $RUNTIME_REPO_REF"
    exit 1
fi
unset GITHUB_PAT

echo "[baked-regional] Runtime checked out at $ACTUAL; launching start.sh..."
exec bash "$RUNTIME_DIR/start.sh"
