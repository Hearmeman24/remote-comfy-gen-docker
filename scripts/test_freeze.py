#!/usr/bin/env python3
"""Regression guard for freeze.py's node-list parsing.

The original parser scraped the Dockerfile for a `RUN for repo in ...` block
that no longer exists (the node loop became `while read -r url sha < nodes.lock`),
so it returned ZERO repos and would silently empty nodes.lock on the next freeze.
These tests pin nodes.lock as the canonical source of the node list.

Run: python3 scripts/test_freeze.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze  # noqa: E402


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


if __name__ == "__main__":
    test_parses_all_custom_nodes_excluding_core()
    test_render_round_trips_to_disk()
    print("ok: freeze.py parses nodes.lock as canonical and round-trips")
