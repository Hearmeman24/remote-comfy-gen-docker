#!/usr/bin/env python3
"""Write BlockFlow's ComfyGen runtime manifest for a released Docker tag.

The serverless Docker release pipeline publishes this file to
Hearmeman24/blockflow-presets after image rollout and smoke tests pass. BlockFlow
then reads that registry file when provisioning new RunPod endpoints.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCKFILE = REPO_ROOT / "nodes.lock"
COMFYUI_REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"
IMAGE_REPOSITORY = "hearmeman/comfyui-serverless"
TAG_RE = re.compile(r"^v\d+$")

REQUIRED_CLASSES = [
    "ModelNoiseScale",
    "EmptyHiDreamO1LatentImage",
    "HiDreamO1PatchSeamSmoothing",
]


def read_comfyui_sha(lockfile: Path = LOCKFILE) -> str:
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) == 2 and parts[0] == COMFYUI_REPO_URL:
            return parts[1]
    raise RuntimeError(f"{COMFYUI_REPO_URL} not found in {lockfile}")


def render_manifest(tag: str, comfyui_sha: str) -> str:
    if not TAG_RE.match(tag):
        raise ValueError(f"tag must match v<N>, got {tag!r}")
    manifest = {
        "manifest_version": 1,
        "comfygen_serverless": {
            "channel": "stable",
            "image": f"{IMAGE_REPOSITORY}:{tag}",
            "tag": tag,
            "comfyui_sha": comfyui_sha,
            "required_classes": REQUIRED_CLASSES,
        },
    }
    return json.dumps(manifest, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Released Docker tag, for example v24")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write runtime-manifest.json",
    )
    args = parser.parse_args()

    try:
        manifest = render_manifest(args.tag, read_comfyui_sha())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest, encoding="utf-8")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
