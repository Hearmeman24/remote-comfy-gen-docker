#!/usr/bin/env python3
"""Snapshot local ComfyUI + custom-node SHAs into nodes.lock.

Reads the Dockerfile's `for repo in ...` block as the canonical node list
(the set of nodes that ship in the image). For each repo it tries to read
the SHA from the local clone at ~/src/comfy/ComfyUI/custom_nodes/<dir>;
falls back to `git ls-remote <url> HEAD` if the local copy is missing.
Captures ComfyUI core SHA the same way from ~/src/comfy/ComfyUI/.git.

Also warns on:
  - Local-only nodes not in the Dockerfile (you might want to ship them)
  - Dockerfile-listed nodes missing locally (we used remote HEAD fallback)

Output: nodes.lock at the repo root, one entry per line:
    <repo_url> <sha>
Sorted by repo URL for diff stability. Run from anywhere; the script
locates serverless-docker and ComfyUI by following its own path.

Usage:
    python3 scripts/freeze.py            # write nodes.lock
    python3 scripts/freeze.py --check    # exit 1 if lockfile would change
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # serverless-docker/
DOCKERFILE = REPO_ROOT / "Dockerfile"
LOCKFILE = REPO_ROOT / "nodes.lock"
LOCAL_COMFYUI = Path.home() / "src" / "comfy" / "ComfyUI"
COMFYUI_REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"

# Sentinel comments in the Dockerfile for the node-loop block; if these
# move/change, update them here.
NODE_BLOCK_START = "RUN for repo in"
NODE_BLOCK_END = "do "  # any continuation that ends the `for repo in ... ;`


def parse_dockerfile_repos() -> list[str]:
    """Return the ordered list of custom-node repo URLs from the Dockerfile."""
    in_block = False
    repos: list[str] = []
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not in_block:
            if NODE_BLOCK_START in stripped:
                in_block = True
            continue
        # End of the repo list: line starting `do \` or `done`
        if stripped.startswith("do ") or stripped.startswith("done"):
            break
        # Repo line ends with `\` and contains a URL ending in .git
        m = re.search(r"(https://github\.com/[^\s]+\.git)", line)
        if m:
            repos.append(m.group(1))
    return repos


def local_dir_for(repo_url: str) -> Path | None:
    """Find the local clone path for a repo URL. Tries exact basename match first,
    then case-insensitive against custom_nodes/* dirs."""
    base = repo_url.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[:-4]
    candidates = LOCAL_COMFYUI / "custom_nodes"
    if not candidates.is_dir():
        return None
    exact = candidates / base
    if (exact / ".git").is_dir():
        return exact
    for child in sorted(candidates.iterdir()):
        if child.name.lower() == base.lower() and (child / ".git").is_dir():
            return child
    return None


def sha_from_local(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def sha_from_remote(url: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return out.stdout.split()[0] if out.stdout.strip() else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def gather_local_only() -> list[str]:
    """Return basenames of local custom_nodes dirs not in the Dockerfile list."""
    docker_basenames = {url.rsplit("/", 1)[-1].removesuffix(".git").lower()
                        for url in parse_dockerfile_repos()}
    local_dir = LOCAL_COMFYUI / "custom_nodes"
    if not local_dir.is_dir():
        return []
    extras = []
    for child in sorted(local_dir.iterdir()):
        if not child.is_dir() or not (child / ".git").is_dir():
            continue
        if child.name.lower() not in docker_basenames:
            extras.append(child.name)
    return extras


def freeze() -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Return (entries, missing_local, local_only).
    entries = [(repo_url, sha, source), ...] sorted by repo_url.
    source = "local" or "remote".
    """
    repos = parse_dockerfile_repos()
    entries: list[tuple[str, str, str]] = []
    missing_local: list[str] = []

    # ComfyUI core first
    core_path = LOCAL_COMFYUI / ".git"
    if core_path.is_dir():
        sha = sha_from_local(LOCAL_COMFYUI)
        if sha:
            entries.append((COMFYUI_REPO_URL, sha, "local"))
        else:
            print(f"WARN: ComfyUI core at {LOCAL_COMFYUI} has .git but no HEAD", file=sys.stderr)
    else:
        print(f"WARN: ComfyUI core not found locally at {LOCAL_COMFYUI}; using remote HEAD", file=sys.stderr)
        sha = sha_from_remote(COMFYUI_REPO_URL)
        if sha:
            entries.append((COMFYUI_REPO_URL, sha, "remote"))

    # Custom nodes
    for url in repos:
        local = local_dir_for(url)
        if local:
            sha = sha_from_local(local)
            if sha:
                entries.append((url, sha, "local"))
                continue
            print(f"WARN: local {local} has .git but no HEAD; trying remote", file=sys.stderr)
        else:
            missing_local.append(url)
        sha = sha_from_remote(url)
        if sha:
            entries.append((url, sha, "remote"))
        else:
            print(f"ERROR: could not get SHA for {url} (no local clone, ls-remote failed)", file=sys.stderr)

    entries.sort(key=lambda e: e[0].lower())
    return entries, missing_local, gather_local_only()


def render(entries: list[tuple[str, str, str]]) -> str:
    """Render the lockfile. Format: '<repo_url> <sha>' per line, sorted, plus a header comment."""
    header = (
        "# nodes.lock — pinned SHAs for ComfyUI core + every custom node.\n"
        "# Generated by scripts/freeze.py from the local ComfyUI tree.\n"
        "# Format: <repo_url> <sha>\n"
    )
    body = "\n".join(f"{url} {sha}" for url, sha, _ in entries) + "\n"
    return header + body


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true",
                   help="Exit 1 if the lockfile on disk would change. Don't write.")
    args = p.parse_args()

    entries, missing_local, local_only = freeze()
    if not entries:
        print("FATAL: no entries collected", file=sys.stderr)
        sys.exit(2)

    new = render(entries)

    if missing_local:
        print(f"\nNOTE: {len(missing_local)} Dockerfile-listed node(s) not in your local "
              f"~/src/comfy/ComfyUI/custom_nodes/ — used remote HEAD fallback:",
              file=sys.stderr)
        for url in missing_local:
            print(f"  - {url}", file=sys.stderr)

    if local_only:
        print(f"\nNOTE: {len(local_only)} local-only node dir(s) NOT in Dockerfile — "
              f"will not ship. If you use any in workflows, add to Dockerfile:",
              file=sys.stderr)
        for name in local_only:
            print(f"  - {name}", file=sys.stderr)

    if args.check:
        existing = LOCKFILE.read_text() if LOCKFILE.exists() else ""
        if existing != new:
            print("\nFAIL: nodes.lock is stale. Run scripts/freeze.py to regenerate.", file=sys.stderr)
            sys.exit(1)
        print("nodes.lock is up to date.", file=sys.stderr)
        return

    LOCKFILE.write_text(new)
    print(f"\nWrote {LOCKFILE} ({len(entries)} entries).", file=sys.stderr)


if __name__ == "__main__":
    main()
