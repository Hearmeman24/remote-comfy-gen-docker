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
EXTRA_MODEL_PATHS = ROOT / "extra_model_paths.yaml"
START_SCRIPT = ROOT / "start_script.sh"

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


def test_all_supplied_workflow_node_packs_have_immutable_pins() -> None:
    expected = {
        "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git":
            "4490bd1f482e026674543386bb2a4d176da245b9",
        "https://github.com/Jonseed/ComfyUI-Detail-Daemon.git":
            "3394e44afea04ed0188fb37b21f0d9952469766b",
        "https://github.com/Hearmeman24/ComfyUI-OpenRouter-Simple.git":
            "79a217c596f808a133305a9e9cb7c49cda6bc184",
    }
    locked = {
        line.split()[0]: line.split()[1]
        for line in LOCKFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for url, sha in expected.items():
        assert locked.get(url) == sha, f"missing or changed immutable pin: {url}"
        assert re.fullmatch(r"[0-9a-f]{40}", locked[url])


def test_workflow_model_categories_resolve_to_network_volume() -> None:
    source = EXTRA_MODEL_PATHS.read_text(encoding="utf-8")
    assert "base_path: /runpod-volume/ComfyUI/models" in source
    assert re.search(r"^\s+seedvr2: SEEDVR2$", source, re.MULTILINE)
    assert re.search(r"^\s+vae_approx: vae_approx$", source, re.MULTILINE)
    dockerfile = _dockerfile()
    assert "ENV AUX_ANNOTATOR_CKPTS_PATH=/runpod-volume/ComfyUI/models/annotator_ckpts" in dockerfile


def test_runtime_checkout_is_container_local_and_keeps_credentials_out_of_git_config() -> None:
    source = START_SCRIPT.read_text(encoding="utf-8")
    assert 'RUNTIME_DIR="/opt/comfygen-runtime"' in source
    assert 'RUNTIME_DIR="/runpod-volume/' not in source
    assert 'RUNTIME_REPO_REF="${RUNTIME_REPO_REF:-main}"' in source
    assert 'git remote set-url origin "$RUNTIME_REPO_URL"' in source
    assert 'git_with_pat clone --depth 1 --branch "$RUNTIME_REPO_REF" "$RUNTIME_REPO_URL"' in source
    assert "unset GITHUB_PAT" in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print("all passed")
