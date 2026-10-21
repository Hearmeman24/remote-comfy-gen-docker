#!/usr/bin/env python3
"""Regression guard for freeze.py's node-list parsing.

The original parser scraped the Dockerfile for a `RUN for repo in ...` block
that no longer exists (the node loop became `while read -r url sha < nodes.lock`),
so it returned ZERO repos and would silently empty nodes.lock on the next freeze.
These tests pin nodes.lock as the canonical source of the node list.

Run: python3 scripts/test_freeze.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze  # noqa: E402
import write_runtime_manifest  # noqa: E402


def test_parses_all_child_owned_custom_nodes() -> None:
    repos = freeze.parse_locked_repos()
    lock_lines = [
        ln for ln in freeze.LOCKFILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    expected = [ln.split()[0] for ln in lock_lines]
    assert repos == expected, f"parsed {len(repos)} repos, expected {len(expected)}"
    assert repos, "parser returned zero repos — would empty nodes.lock on freeze"
    assert freeze.COMFYUI_REPO_URL not in repos, "ComfyUI core belongs to the father image"
    assert freeze.MANAGER_REPO_URL not in repos, "ComfyUI-Manager belongs to the father image"


def test_render_round_trips_to_disk() -> None:
    """render() of the current lockfile entries must reproduce the file byte-for-byte,
    proving a re-freeze of unchanged local SHAs is a no-op (not a node drop)."""
    entries = [
        (ln.split()[0], ln.split()[1], "local")
        for ln in freeze.LOCKFILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    entries.sort(key=lambda e: e[0].lower())
    assert freeze.render(entries) == freeze.LOCKFILE.read_text()


def test_upscale_node_pack_is_baked() -> None:
    """HearmemanAI-UpscaleModel replaces core's ImageUpscaleWithModel and must ship
    in the image, not be cold-start cloned by the runtime installer."""
    url = "https://github.com/Hearmeman24/ComfyUI-HearmemanAI-Upscale.git"
    assert url in freeze.parse_locked_repos(), f"{url} missing from nodes.lock"


def test_comfyui_pin_ships_minimax_h3() -> None:
    """The father image's ComfyUI ref must carry the MiniMax H3 nodes.

    H3 support landed in ComfyUI v0.30.0. The father tag is the single version
    source, so reject any older ComfyUI coordinate without reaching the network.
    """
    base = write_runtime_manifest.read_base_image()
    match = re.search(r"-comfy(\d+)\.(\d+)\.(\d+)-", base)
    assert match, f"base image does not expose its ComfyUI version: {base}"
    assert tuple(map(int, match.groups())) >= (0, 30, 0), base


def test_runtime_manifest_reports_father_image() -> None:
    base = write_runtime_manifest.read_base_image()
    manifest = json.loads(write_runtime_manifest.render_manifest("v35", base))
    section = manifest["comfygen_serverless"]
    assert section["base_image"] == base
    assert "comfyui_sha" not in section


if __name__ == "__main__":
    test_parses_all_child_owned_custom_nodes()
    test_render_round_trips_to_disk()
    test_upscale_node_pack_is_baked()
    test_comfyui_pin_ships_minimax_h3()
    test_runtime_manifest_reports_father_image()
    print("ok: freeze.py parses nodes.lock as canonical and round-trips")
