#!/usr/bin/env python3
"""Plan or publish the baked-regional image one verified model layer at a time.

Dry-run is the default. `--apply` requires an already available BuildKit-capable
Docker engine, Docker Hub login, HF_TOKEN in the environment, and 90 GiB free
scratch. Each model is copied into a new content-named registry image layer;
the dedicated Buildx builder is removed after each successful layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "pins.baked-regional.json"
MANIFEST = ROOT / "baked-models.manifest.json"
LAYER_DOCKERFILE = ROOT / "Dockerfile.baked-regional-layer"
FINAL_DOCKERFILE = ROOT / "Dockerfile.baked-regional-final"
MIN_FREE_BYTES = 90 * 1024**3
EXPECTED_BASE = "hearmeman/comfyui-serverless:v38@sha256:897b7f4ab6c24536e3e25d426f9b72363677e81981a3d4817f8443dbed5cde95"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_recipe_items(items: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, contents in items:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()


def recipe_sha256() -> str:
    root = ROOT.resolve()
    fixed_inputs = (
        LAYER_DOCKERFILE.resolve(),
        root / "download_baked_model.py",
        root / "MODEL-NOTICES.md",
        Path(__file__).resolve(),
    )
    notices = root / "notices"
    # The Dockerfile copies the entire notices tree. Bind every regular file
    # into layer identity so adding or changing a license can never reuse an
    # intermediate built with a different notice set.
    notice_inputs = sorted(
        (path for path in notices.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    recipe_inputs = (*fixed_inputs, *notice_inputs)
    return hash_recipe_items([
        (path.relative_to(root).as_posix(), path.read_bytes()) for path in recipe_inputs
    ])


def load_inputs() -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    assets = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(pins, dict) or not isinstance(assets, list) or not assets:
        raise ValueError("invalid pins or baked model manifest")
    if pins.get("base_image") != EXPECTED_BASE or pins.get("platform") != "linux/amd64":
        raise ValueError("baked image must start from the pinned v38 amd64 manifest")
    manifest_sha = sha256_file(MANIFEST)
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict) or not asset.get("asset_id") or not asset.get("sha256"):
            raise ValueError("malformed baked model manifest entry")
        if asset["asset_id"] in seen:
            raise ValueError(f"duplicate manifest asset ID: {asset['asset_id']}")
        seen.add(asset["asset_id"])
        if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValueError(f"invalid asset hash: {asset['asset_id']}")
    if sum(x["bytes"] for x in assets) != int(pins["baked_bytes"]):
        raise ValueError("baked manifest total does not match pins")
    return pins, sorted(assets, key=lambda x: (x["bytes"], x["asset_id"])), manifest_sha, recipe_sha256()


def run(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, check=check, text=True,
                          capture_output=capture)


def inspect_digest(ref: str) -> str:
    result = run(["docker", "buildx", "imagetools", "inspect", ref], capture=True)
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError(f"registry response omitted an immutable digest for {ref}")
    return match.group(1)


def inspect_labels(ref: str) -> dict[str, str] | None:
    result = run(["docker", "buildx", "imagetools", "inspect", "--format",
                  "{{json .Image.Config.Labels}}", ref], check=False, capture=True)
    if result.returncode:
        return None
    try:
        labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return labels if isinstance(labels, dict) else None


def content_tag(prefix: str, index: int, asset: dict[str, Any], manifest_sha: str,
                base_digest: str, recipe_sha: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", asset["asset_id"]).strip("-.")[:28]
    tag = (f"{prefix}-layer-{index:02d}-{safe_id}-m{manifest_sha[:10]}-"
           f"a{asset['sha256'][:10]}-b{base_digest.removeprefix('sha256:')[:10]}-r{recipe_sha[:10]}")
    if len(tag) > 128:
        raise ValueError("release tag is too long to form a valid Docker tag")
    return tag


def labels_match(ref: str, previous: str, asset: dict[str, Any], manifest_sha: str,
                 recipe_sha: str) -> bool:
    labels = inspect_labels(ref)
    return bool(labels and
                labels.get("org.hearmemanai.comfygen.layer") == "baked-model" and
                labels.get("org.hearmemanai.comfygen.base-image") == previous and
                labels.get("org.hearmemanai.comfygen.model-asset-id") == asset["asset_id"] and
                labels.get("org.hearmemanai.comfygen.model-asset-sha256") == asset["sha256"] and
                labels.get("org.hearmemanai.comfygen.model-manifest-sha256") == manifest_sha and
                labels.get("org.hearmemanai.comfygen.layer-recipe-sha256") == recipe_sha)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def build_layer(image: str, tag: str, previous: str, asset: dict[str, Any],
                manifest_sha: str, recipe_sha: str, builder: str) -> str:
    run(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"])
    try:
        run(["docker", "buildx", "inspect", "--bootstrap", builder])
        run([
            "docker", "buildx", "build", "--builder", builder,
            "--platform", "linux/amd64", "--progress=plain",
            "-f", str(LAYER_DOCKERFILE),
            "--build-arg", f"BASE_IMAGE={previous}",
            "--build-arg", f"MODEL_ASSET_ID={asset['asset_id']}",
            "--build-arg", f"MODEL_ASSET_SHA256={asset['sha256']}",
            "--build-arg", f"MODEL_MANIFEST_SHA256={manifest_sha}",
            "--build-arg", f"RECIPE_SHA256={recipe_sha}",
            "--secret", "id=hf_token,env=HF_TOKEN",
            "-t", f"{image}:{tag}", "--push", ".",
        ])
        return inspect_digest(f"{image}:{tag}")
    finally:
        # Only this script-created builder is removed; no shared/default builder
        # or Docker images are pruned.
        run(["docker", "buildx", "rm", "--force", builder], check=False)


def build_final(image: str, tag: str, previous: str, manifest_sha: str, recipe_sha: str,
                builder: str) -> str:
    run(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"])
    try:
        run(["docker", "buildx", "inspect", "--bootstrap", builder])
        run([
            "docker", "buildx", "build", "--builder", builder,
            "--platform", "linux/amd64", "--progress=plain",
            "-f", str(FINAL_DOCKERFILE),
            "--build-arg", f"BASE_IMAGE={previous}",
            "--build-arg", f"MODEL_MANIFEST_SHA256={manifest_sha}",
            "--build-arg", f"RECIPE_SHA256={recipe_sha}",
            "-t", f"{image}:{tag}", "--push", ".",
        ])
        return inspect_digest(f"{image}:{tag}")
    finally:
        run(["docker", "buildx", "rm", "--force", builder], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="docker.io/hearmeman/comfyui-serverless")
    parser.add_argument("--release-tag", default="scail-krea-v1")
    parser.add_argument("--limit", type=int, help="process only the first N smallest assets (pilot use)")
    parser.add_argument("--min-free-gib", type=int, default=90)
    parser.add_argument("--receipt", type=Path, default=ROOT / "baked-regional-build-receipt.json")
    parser.add_argument("--apply", action="store_true", help="download, push intermediate layers, then push final overlay")
    args = parser.parse_args()
    pins, assets, manifest_sha, recipe_sha = load_inputs()
    if args.limit is not None:
        if args.limit < 1 or args.limit > len(assets):
            parser.error(f"--limit must be between 1 and {len(assets)}")
        assets = assets[:args.limit]
    if args.release_tag != "scail-krea-v1" and not re.fullmatch(r"scail-krea-v[0-9]+", args.release_tag):
        parser.error("release tag must be scail-krea-v<N>")
    plan = {
        "apply": args.apply, "base_image": pins["base_image"], "platform": pins["platform"],
        "manifest_sha256": manifest_sha, "release_tag": args.release_tag,
        "recipe_sha256": recipe_sha,
        "asset_count": len(assets), "asset_bytes": sum(x["bytes"] for x in assets),
        "minimum_free_gib_per_step": args.min_free_gib,
        "assets_in_order": [{"asset_id": x["asset_id"], "bytes": x["bytes"], "sha256": x["sha256"]} for x in assets],
    }
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0
    if not os.environ.get("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN must be injected through the CircleCI Docker Hub context")
    free = free_bytes(ROOT)
    minimum = args.min_free_gib * 1024**3
    if free < minimum:
        raise RuntimeError(f"refusing build: {free / 1024**3:.1f} GiB free; need at least {args.min_free_gib} GiB")
    if pins["baked_bytes"] > 0 and not args.limit and len(assets) != pins["baked_asset_count"]:
        raise RuntimeError("full release asset count differs from pinned manifest")
    current = pins["base_image"]
    prefix = f"{args.release_tag}-{manifest_sha[:10]}"
    receipts = []
    for index, asset in enumerate(assets, start=1):
        previous = current
        # Include the immediate parent digest so a tag can never be resumed
        # against a different layer chain with otherwise identical assets.
        parent_digest = previous.rsplit("@", 1)[1]
        tag = content_tag(prefix, index, asset, manifest_sha, parent_digest, recipe_sha)
        candidate = f"{args.image}:{tag}"
        if labels_match(candidate, current, asset, manifest_sha, recipe_sha):
            digest = inspect_digest(candidate)
            print(f"Reuse verified layer {index}/{len(assets)} {asset['asset_id']} digest={digest}")
        else:
            # If the content-addressed tag exists but its contract differs, fail
            # rather than replacing it or trusting a mutable intermediate.
            exists = run(["docker", "buildx", "imagetools", "inspect", candidate], check=False, capture=True)
            if exists.returncode == 0:
                raise RuntimeError(f"existing content-named tag failed label verification: {candidate}")
            if free_bytes(ROOT) < minimum:
                raise RuntimeError(f"disk preflight failed before layer {index}: free space below {args.min_free_gib} GiB")
            builder = f"scail-baked-{os.getpid()}-{index}-{int(time.time())}"
            digest = build_layer(args.image, tag, current, asset, manifest_sha, recipe_sha, builder)
            if not labels_match(candidate, current, asset, manifest_sha, recipe_sha):
                raise RuntimeError(f"published layer labels failed verification: {candidate}")
        current = f"{args.image}@{digest}"
        receipts.append({"index": index, "asset_id": asset["asset_id"], "asset_sha256": asset["sha256"],
                         "tag": candidate, "parent_image": previous, "digest": digest,
                         "image": current})
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps({**plan, "completed_layers": receipts, "current_base": current}, indent=2)+"\n", encoding="utf-8")
    if args.limit:
        print(json.dumps({"pilot_complete": True, "last_verified_image": current, "layers": receipts}, indent=2))
        return 0
    final_digest = build_final(args.image, args.release_tag, current, manifest_sha, recipe_sha,
                               f"scail-baked-final-{os.getpid()}-{int(time.time())}")
    output = {"completed": True, "image": f"{args.image}:{args.release_tag}",
              "digest": final_digest, "manifest_sha256": manifest_sha,
              "recipe_sha256": recipe_sha,
              "layers": receipts, "final_base": current}
    args.receipt.write_text(json.dumps(output, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
