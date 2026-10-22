#!/usr/bin/env python3
"""Focused local contract tests for the baked-regional image path."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def helper_module():
    spec = importlib.util.spec_from_file_location(
        "build_baked_regional_layers", ROOT / "scripts/build_baked_regional_layers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifests_and_pins_match_scope() -> None:
    pins = json.loads((ROOT / "pins.baked-regional.json").read_text())
    baked = json.loads((ROOT / "baked-models.manifest.json").read_text())
    regional = json.loads((ROOT / "regional-models.manifest.json").read_text())
    assert len(baked) == pins["baked_asset_count"] == 15
    assert len(regional) == pins["regional_asset_count"] == 3
    assert sum(item["bytes"] for item in baked) == pins["baked_bytes"] == 86_556_007_470
    assert sum(item["bytes"] for item in regional) == pins["regional_bytes"] == 9_518_837_253
    assert {x["asset_id"] for x in baked}.isdisjoint({x["asset_id"] for x in regional})


def test_dry_run_selects_small_pilot_and_does_not_build() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_baked_regional_layers.py"), "--limit", "1"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    plan = json.loads(result.stdout)
    assert plan["apply"] is False
    assert plan["asset_count"] == 1
    assert plan["assets_in_order"][0]["asset_id"] == "taew2_1.pth"
    assert "docker" not in result.stderr.lower()


def test_each_layer_tag_binds_previous_digest() -> None:
    module = helper_module()
    asset = {"asset_id": "taew2_1.pth", "sha256": "a" * 64}
    common = ("scail-krea-v1-abc123", 1, asset, "b" * 64)
    first = module.content_tag(*common, "sha256:" + "c" * 64, "e" * 64)
    second = module.content_tag(*common, "sha256:" + "d" * 64, "e" * 64)
    assert first != second
    assert "-b" + "c" * 10 + "-r" + "e" * 10 in first
    recipe_variant = module.content_tag(*common, "sha256:" + "c" * 64, "f" * 64)
    assert recipe_variant != first
    before_license_update = module.hash_recipe_items([
        ("notices/krea/NOTICE", b"license wording A"),
        ("Dockerfile.baked-regional-layer", b"recipe"),
    ])
    after_license_update = module.hash_recipe_items([
        ("notices/krea/NOTICE", b"license wording B"),
        ("Dockerfile.baked-regional-layer", b"recipe"),
    ])
    assert before_license_update != after_license_update


def test_incremental_dockerfiles_preserve_linked_base_and_secret_contract() -> None:
    layer = (ROOT / "Dockerfile.baked-regional-layer").read_text()
    final = (ROOT / "Dockerfile.baked-regional-final").read_text()
    one_shot = (ROOT / "Dockerfile.baked-regional").read_text()
    pins = json.loads((ROOT / "pins.baked-regional.json").read_text())
    assert pins["huggingface_hub_version"] == "2.2.0"
    assert pins["click_version"] == "8.5.0"
    assert '"huggingface_hub==2.2.0" "click==8.5.0"' in layer
    assert '"huggingface_hub==2.2.0" "click==8.5.0"' in one_shot
    assert 'huggingface_hub[cli]' not in layer + one_shot
    assert "hf version" in layer and "hf download --help" in layer and "--quiet" in layer
    assert "hf version" in one_shot and "hf download --help" in one_shot and "--quiet" in one_shot
    assert "RUN --mount=type=secret,id=hf_token,required=true" in layer
    downloader = (ROOT / "download_baked_model.py").read_text()
    assert '"hf", "download"' in downloader
    assert "COPY --link --from=model-download /baked-models/" in layer
    assert "/ComfyUI/models/" in layer
    assert "COPY --link notices/krea/NOTICE" in layer
    assert "COPY --link notices/apache/LICENSE" in layer
    assert "COPY --link notices/sam/LICENSE" in layer
    assert "COPY --link extra_model_paths.baked-regional.yaml" in final
    assert "COPY --link --chmod=0755 start_script.baked-regional.sh" in final
    assert "COPY --link notices/krea/LICENSE.pdf" in final
    assert "COPY --link notices/krea/NOTICE" in final
    required_krea_notice = (
        "Krea 2 is licensed under the Krea 2 Community License Agreement. "
        "For more information, visit https://krea.ai/krea-2-licensing.\n"
    )
    assert (ROOT / "notices/krea/NOTICE").read_text() == required_krea_notice
    assert "COPY --link notices/apache/LICENSE" in final
    assert "COPY --link notices/scail/LICENSE" in final
    assert "COPY --link notices/rife/LICENSE" in final
    paths = (ROOT / "extra_model_paths.baked-regional.yaml").read_text()
    assert "/runpod-volume/ComfyUI/models" in paths
    assert "baked_models:" not in paths
    assert "\nRUN " not in layer.split("AS image", 1)[1]
    assert "\nRUN " not in final.split("FROM ", 1)[1]
    assert "ARG HF_TOKEN" not in layer + final
    assert "ENV HF_TOKEN" not in layer + final


def test_runtime_wrapper_pins_and_authenticates_private_checkout() -> None:
    wrapper = (ROOT / "start_script.baked-regional.sh").read_text()
    assert "RUNTIME_REPO_REF=\"60fdc14d6b3a4bccf181f8aa8e6983d0311766da\"" in wrapper
    assert "git_with_pat clone --filter=blob:none --no-checkout" in wrapper
    assert "git_with_pat -C \"$RUNTIME_DIR\" checkout --detach FETCH_HEAD" in wrapper
    assert "unset GITHUB_PAT" in wrapper


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all passed")


if __name__ == "__main__":
    main()
