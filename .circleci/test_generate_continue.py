#!/usr/bin/env python3
"""Regression guard for the generated continuation config.

The smoke matrix was removed (HEA-82): it gated the whole pipeline on RunPod
being healthy at that exact minute, and RunPod-side flakes — control-plane 500s
on deployCpuPod, proxy 404s on POST /install — failed the pipeline on presets
whose image was fine. These tests pin the shape that replaced it.

Run: python3 .circleci/test_generate_continue.py
"""

import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parent / "generate_continue.py"


def generated() -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True
    )
    return yaml.safe_load(out.stdout)


def test_no_smoke_job() -> None:
    config = generated()
    assert "smoke_test" not in config["jobs"]
    named = [list(j)[0] for j in config["workflows"]["build-deploy"]["jobs"]]
    assert "smoke_test" not in named, named


def test_manifest_publish_follows_rollout() -> None:
    config = generated()
    jobs = {list(j)[0]: list(j.values())[0] for j in config["workflows"]["build-deploy"]["jobs"]}
    assert jobs["publish_runtime_manifest"]["requires"] == ["wait_for_rollout"]
    assert jobs["notify_done"]["requires"] == ["publish_runtime_manifest"]


def test_generation_needs_no_network() -> None:
    """The preset manifest was fetched only to expand the smoke matrix.

    Checks the generator's own imports — the emitted config still contains an
    inline `import urllib.request` inside update_endpoint's heredoc.
    """
    source = SCRIPT.read_text()
    assert "\nimport urllib" not in source, "generation should no longer reach the network"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all passed")
