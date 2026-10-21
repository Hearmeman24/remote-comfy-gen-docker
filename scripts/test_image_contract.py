#!/usr/bin/env python3
"""Regression tests for the shared-father-image ownership boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
PINS = ROOT / "pins.json"
LOCKFILE = ROOT / "nodes.lock"

COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
MANAGER_REPO = "https://github.com/ltdrdata/ComfyUI-Manager.git"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _pins() -> dict[str, str]:
    return json.loads(PINS.read_text(encoding="utf-8"))


def _locked_urls() -> list[str]:
    return [
        line.split()[0]
        for line in LOCKFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_dockerfile_inherits_pinned_shared_base() -> None:
    source = _dockerfile()
    base_image = _pins().get("base_image", "")
    assert re.fullmatch(
        r"hearmeman/comfyui-base:cu130-comfy\d+\.\d+\.\d+"
        r"(?:-[0-9a-f]{8})?-torch\d+\.\d+\.\d+",
        base_image,
    ), base_image
    assert f"ARG BASE_IMAGE={base_image}" in source
    assert "FROM ${BASE_IMAGE}" in source
    assert "FROM nvidia/cuda:" not in source


def test_child_does_not_reinstall_father_owned_stack() -> None:
    source = _dockerfile()
    forbidden = {
        "Python virtual environment": "python3.12 -m venv",
        "Torch trio": "pip install torch==",
        "ComfyUI core": "git clone --filter=blob:none https://github.com/comfyanonymous/ComfyUI.git",
        "SageAttention wheel": "sageattention-2.2.0-cp312-cp312-linux_x86_64.whl",
        "CivitAI downloader": "ARG CIVITAI_DL_SHA=",
    }
    found = [name for name, marker in forbidden.items() if marker in source]
    assert not found, f"child still owns father layers: {found}"
    assert not list(ROOT.glob("sageattention-*.whl")), "Sage wheels belong to the father repo"


def test_child_lock_contains_only_child_owned_custom_nodes() -> None:
    urls = _locked_urls()
    assert urls, "the serverless child must retain its custom-node set"
    assert COMFYUI_REPO not in urls, "ComfyUI core belongs to the father image"
    assert MANAGER_REPO not in urls, "ComfyUI-Manager belongs to the father image"


def test_serverless_manager_path_is_adapted_without_reinstalling_manager() -> None:
    source = _dockerfile()
    assert "/ComfyUI/custom_nodes/comfyui-manager" in source
    assert "/ComfyUI/custom_nodes/ComfyUI-Manager" in source
    assert MANAGER_REPO not in source


def test_build_verifies_and_logs_inherited_comfyui_version_and_commit() -> None:
    source = _dockerfile()
    assert "comfyui_version.__version__" in source
    assert '["git", "-C", "/ComfyUI", "rev-parse", "HEAD"]' in source
    assert 'Path("/comfyui-approved-ref")' in source
    assert 'f"{approved}^{{commit}}"' in source
    assert "if head != resolved:" in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print("all passed")
