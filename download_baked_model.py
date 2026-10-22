#!/usr/bin/env python3
"""Download and verify one pinned model using the Hugging Face `hf` CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("baked-models manifest must be a non-empty JSON array")
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each baked model entry must be an object")
        for key in ("asset_id", "source", "revision", "source_path", "target_path", "license_id", "notice"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"model entry is missing {key}")
        if item["asset_id"] in seen:
            raise ValueError(f"duplicate asset_id: {item['asset_id']}")
        seen.add(item["asset_id"])
        if not re.fullmatch(r"[0-9a-f]{40}", item["revision"]):
            raise ValueError(f"revision must be a full immutable commit for {item['asset_id']}")
        if not HEX64.fullmatch(item.get("sha256", "")):
            raise ValueError(f"invalid SHA-256 for {item['asset_id']}")
        if not isinstance(item.get("bytes"), int) or item["bytes"] <= 0:
            raise ValueError(f"invalid byte count for {item['asset_id']}")
        for key in ("source_path", "target_path"):
            value = item[key]
            path_value = PurePosixPath(value)
            if path_value.is_absolute() or ".." in path_value.parts:
                raise ValueError(f"unsafe {key} for {item['asset_id']}")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, entry: dict[str, Any]) -> None:
    if not path.is_file():
        raise ValueError(f"download did not produce the expected file: {path}")
    size = path.stat().st_size
    if size != entry["bytes"]:
        raise ValueError(f"byte count mismatch for {entry['asset_id']}: {size} != {entry['bytes']}")
    digest = sha256_file(path)
    if digest != entry["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {entry['asset_id']}: {digest}")


def download_one(entry: dict[str, Any], root: Path, token_file: Path) -> Path:
    if not token_file.is_file():
        raise ValueError("BuildKit HF token secret is missing")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("BuildKit HF token secret is empty")
    target_rel = PurePosixPath(entry["target_path"])
    target = root.joinpath(*target_rel.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_root = root / ".hf-staging" / re.sub(r"[^A-Za-z0-9_.-]", "_", entry["asset_id"])
    hf_home = root / ".hf-home" / re.sub(r"[^A-Za-z0-9_.-]", "_", entry["asset_id"])
    if target.exists():
        raise ValueError(f"refusing to overwrite existing baked model: {target}")
    shutil.rmtree(stage_root, ignore_errors=True)
    shutil.rmtree(hf_home, ignore_errors=True)
    stage_root.mkdir(parents=True)
    hf_home.mkdir(parents=True)
    env: dict[str, str] | None = None
    try:
        env = os.environ.copy()
        env.update({"HF_TOKEN": token, "HF_HOME": str(hf_home), "HF_HUB_DISABLE_TELEMETRY": "1"})
        # The secret is never placed in argv, build args, or image environment.
        subprocess.run([
            "hf", "download", entry["source"], entry["source_path"],
            "--revision", entry["revision"], "--local-dir", str(stage_root), "--quiet",
        ], env=env, check=True)
        staged = stage_root.joinpath(*PurePosixPath(entry["source_path"]).parts)
        verify_file(staged, entry)
        os.replace(staged, target)
        verify_file(target, entry)
    finally:
        # Remove HF local-dir metadata, token-bearing process environment, and
        # local cache before this RUN layer is committed.
        token = ""
        if env is not None:
            env.pop("HF_TOKEN", None)
        shutil.rmtree(stage_root, ignore_errors=True)
        shutil.rmtree(hf_home, ignore_errors=True)
        shutil.rmtree(stage_root.parent, ignore_errors=True)
        shutil.rmtree(hf_home.parent, ignore_errors=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/baked-models.manifest.json"))
    parser.add_argument("--asset-id")
    parser.add_argument("--target-root", type=Path, default=Path("/ComfyUI/models"))
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/hf_token"))
    parser.add_argument("--validate-manifest-only", action="store_true")
    args = parser.parse_args()
    models = load_manifest(args.manifest)
    if args.validate_manifest_only:
        print(json.dumps({"assets": len(models), "bytes": sum(x["bytes"] for x in models)}, sort_keys=True))
        return 0
    if not args.asset_id:
        parser.error("--asset-id is required unless --validate-manifest-only is used")
    entry = next((item for item in models if item["asset_id"] == args.asset_id), None)
    if entry is None:
        raise ValueError(f"unknown baked asset ID: {args.asset_id}")
    path = download_one(entry, args.target_root, args.token_file)
    print(f"Verified baked model {entry['asset_id']} ({entry['bytes']} bytes, sha256 {entry['sha256']}) at {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
