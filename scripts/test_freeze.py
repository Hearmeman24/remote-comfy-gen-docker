#!/usr/bin/env python3
"""Regression guard for freeze.py's node-list parsing.

The original parser scraped the Dockerfile for a `RUN for repo in ...` block
that no longer exists (the node loop became `while read -r url sha < nodes.lock`),
so it returned ZERO repos and would silently empty nodes.lock on the next freeze.
These tests pin nodes.lock as the canonical source of the node list.

Run: python3 scripts/test_freeze.py
"""

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze  # noqa: E402
import write_runtime_manifest  # noqa: E402


def test_parses_all_custom_nodes_excluding_core() -> None:
    repos = freeze.parse_locked_repos()
    lock_lines = [
        ln for ln in freeze.LOCKFILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    expected = [ln.split()[0] for ln in lock_lines if ln.split()[0] != freeze.COMFYUI_REPO_URL]
    assert repos == expected, f"parsed {len(repos)} repos, expected {len(expected)}"
    assert repos, "parser returned zero repos — would empty nodes.lock on freeze"
    assert freeze.COMFYUI_REPO_URL not in repos, "ComfyUI core must be captured separately"


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
    """The pinned ComfyUI core SHA must carry the MiniMax H3 nodes.

    H3 support (comfy_extras/nodes_minimax_h3.py) landed in ComfyUI v0.30.0 via
    PR #15224. Nothing else in the image provides those node classes, so a pin
    older than that ships an image where every H3 workflow fails to load.
    Hits the network — the lockfile records a SHA, not a tree.
    """
    sha = write_runtime_manifest.read_comfyui_sha()
    url = (
        "https://raw.githubusercontent.com/comfyanonymous/ComfyUI/"
        f"{sha}/comfy_extras/nodes_minimax_h3.py"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            source = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise AssertionError(
                f"ComfyUI @ {sha} has no comfy_extras/nodes_minimax_h3.py — "
                "pin predates v0.30.0"
            ) from None
        raise
    for cls in ("EmptyMiniMaxH3LatentAV", "MiniMaxH3ImageToVideo",
                "MiniMaxH3ReferenceToVideo", "MiniMaxH3SigmaShift"):
        assert f"class {cls}(" in source, f"{cls} missing from ComfyUI @ {sha}"


if __name__ == "__main__":
    test_parses_all_custom_nodes_excluding_core()
    test_render_round_trips_to_disk()
    test_upscale_node_pack_is_baked()
    test_comfyui_pin_ships_minimax_h3()
    print("ok: freeze.py parses nodes.lock as canonical and round-trips")
