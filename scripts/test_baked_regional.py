#!/usr/bin/env python3
"""Focused local contract tests for the baked-regional image path."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_FILESYSTEM_LAYERS = 39  # Observed v38 manifest layer count, confirmed by the coordinator.
LAYER_BUDGET = 120  # Keep below overlay2's 128 lower-layer limit.
MODEL_ASSET_COUNT = 15


def helper_module():
    spec = importlib.util.spec_from_file_location(
        "build_baked_regional_layers", ROOT / "scripts/build_baked_regional_layers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_in_stage(dockerfile: str, stage_marker: str) -> list[str]:
    stage_start = dockerfile.index(stage_marker)
    stage = dockerfile[stage_start:]
    return [line for line in stage.splitlines() if line.startswith("COPY ")]


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


def test_recipe_hash_binds_every_notice_file_including_future_additions() -> None:
    module = helper_module()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "scripts").mkdir()
        (root / "notices" / "existing").mkdir(parents=True)
        (root / "notices" / "existing" / "LICENSE").write_text("license A")
        (root / "Dockerfile.baked-regional-layer").write_text("layer dockerfile")
        (root / "download_baked_model.py").write_text("downloader")
        (root / "MODEL-NOTICES.md").write_text("model notice")
        helper_path = root / "scripts" / "build_baked_regional_layers.py"
        helper_path.write_text("builder")
        old_values = (module.ROOT, module.LAYER_DOCKERFILE, module.__file__)
        try:
            module.ROOT = root
            module.LAYER_DOCKERFILE = root / "Dockerfile.baked-regional-layer"
            module.__file__ = str(helper_path)
            original = module.recipe_sha256()
            (root / "notices" / "future").mkdir()
            (root / "notices" / "future" / "NOTICE").write_text("new license notice")
            assert module.recipe_sha256() != original
        finally:
            module.ROOT, module.LAYER_DOCKERFILE, module.__file__ = old_values


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
    notice_copy = "COPY --link notices/ /usr/share/comfygen-model-licenses/"
    for dockerfile in (layer, final, one_shot):
        assert dockerfile.count(notice_copy) == 1
        assert "COPY --link MODEL-NOTICES.md /usr/share/comfygen-model-licenses/MODEL-NOTICES.md" in dockerfile
    notice_files = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "notices").rglob("*") if path.is_file())
    expected_license_files = {
        "notices/apache/LICENSE",
        "notices/krea/LICENSE.pdf",
        "notices/krea/NOTICE",
        "notices/rife/LICENSE",
        "notices/sam/LICENSE",
        "notices/scail/LICENSE",
    }
    assert expected_license_files <= set(notice_files)
    assert "COPY --link extra_model_paths.baked-regional.yaml" in final
    assert "COPY --link --chmod=0755 start_script.baked-regional.sh" in final
    required_krea_notice = (
        "Krea 2 is licensed under the Krea 2 Community License Agreement. "
        "For more information, visit https://krea.ai/krea-2-licensing.\n"
    )
    assert (ROOT / "notices/krea/NOTICE").read_text() == required_krea_notice
    paths = (ROOT / "extra_model_paths.baked-regional.yaml").read_text()
    assert "/runpod-volume/ComfyUI/models" in paths
    assert "baked_models:" not in paths
    assert "\nRUN " not in layer.split("AS image", 1)[1]
    assert "\nRUN " not in final.split("FROM ", 1)[1]
    assert "ARG HF_TOKEN" not in layer + final
    assert "ENV HF_TOKEN" not in layer + final


def test_incremental_chain_stays_below_filesystem_layer_budget() -> None:
    layer = (ROOT / "Dockerfile.baked-regional-layer").read_text()
    final = (ROOT / "Dockerfile.baked-regional-final").read_text()
    model_copy_count = len(copy_in_stage(layer, "AS image"))
    final_copy_count = len(copy_in_stage(final, "AS final"))
    projected = BASE_FILESYSTEM_LAYERS + MODEL_ASSET_COUNT * model_copy_count + final_copy_count
    assert model_copy_count == 3  # model, MODEL-NOTICES.md, notices/ directory
    assert final_copy_count == 5  # config, startup script, logger, MODEL-NOTICES.md, notices/
    assert projected == 89
    assert projected <= LAYER_BUDGET


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
