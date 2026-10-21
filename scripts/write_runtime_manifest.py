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
PINS_FILE = REPO_ROOT / "pins.json"
IMAGE_REPOSITORY = "hearmeman/comfyui-serverless"
TAG_RE = re.compile(r"^v\d+$")
BASE_IMAGE_RE = re.compile(
    r"^hearmeman/comfyui-base:cu130-comfy\d+\.\d+\.\d+(?:-[0-9a-f]{8})?-torch\d+\.\d+\.\d+$"
)

REQUIRED_CLASSES = [
    "ModelNoiseScale",
    "EmptyHiDreamO1LatentImage",
    "HiDreamO1PatchSeamSmoothing",
]


def read_base_image(pins_file: Path = PINS_FILE) -> str:
    pins = json.loads(pins_file.read_text(encoding="utf-8"))
    base_image = pins.get("base_image") if isinstance(pins, dict) else None
    if not isinstance(base_image, str) or not BASE_IMAGE_RE.fullmatch(base_image):
        raise RuntimeError(f"invalid base_image in {pins_file}: {base_image!r}")
    return base_image


def render_manifest(tag: str, base_image: str) -> str:
    if not TAG_RE.match(tag):
        raise ValueError(f"tag must match v<N>, got {tag!r}")
    manifest = {
        "manifest_version": 1,
        "comfygen_serverless": {
            "channel": "stable",
            "image": f"{IMAGE_REPOSITORY}:{tag}",
            "tag": tag,
            "base_image": base_image,
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
        manifest = render_manifest(args.tag, read_base_image())
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest, encoding="utf-8")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
