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


def test_worker_build_uses_base_image_from_pins() -> None:
    config = generated()
    command = next(
        step["run"]["command"]
        for step in config["jobs"]["build_and_push"]["steps"]
        if isinstance(step, dict)
        and "run" in step
        and step["run"].get("name", "").startswith("Build & push ComfyUI")
    )
    assert "json.load(open('pins.json'))" in command
    assert '--build-arg BASE_IMAGE="${BASE_IMAGE}"' in command


def test_worker_build_waits_for_contract_verification() -> None:
    config = generated()
    jobs = config["workflows"]["build-deploy"]["jobs"]
    named = {list(job)[0]: list(job.values())[0] for job in jobs}
    assert "verify" in named
    assert named["build_and_push"]["requires"] == ["verify"]


def test_build_only_workflow_builds_without_deploy_publish_or_notifications() -> None:
    config = generated()
    jobs = config["workflows"]["build-image-only"]["jobs"]
    named = {list(job)[0]: list(job.values())[0] for job in jobs}
    assert set(named) == {"verify", "build_and_push"}
    assert named["build_and_push"]["requires"] == ["verify"]
    assert named["build_and_push"]["build_only"] is True
    assert named["build_and_push"]["context"] == ["docker-hub"]
    assert all(
        "deploy" not in name and "publish" not in name and "notify" not in name
        for name in named
    )


def test_build_only_tag_normalizes_to_worker_v_tag() -> None:
    config = generated()
    command = next(
        step["run"]["command"]
        for step in config["jobs"]["build_and_push"]["steps"]
        if isinstance(step, dict)
        and "run" in step
        and step["run"].get("name", "").startswith("Build & push ComfyUI")
    )
    assert 'TAG="${CIRCLE_TAG:-latest}"' in command
    assert '[[ ! "$TAG" =~ ^build-v[0-9]+$ ]]' in command
    assert 'TAG="${TAG#build-}"' in command


def test_build_start_and_result_notifications_are_conditionally_gated() -> None:
    config = generated()
    steps = config["jobs"]["build_and_push"]["steps"]
    gated = [
        step["when"]
        for step in steps
        if isinstance(step, dict)
        and "when" in step
        and step["when"].get("condition") == {"equal": [False, "<< parameters.build_only >>"]}
    ]
    assert len(gated) == 2, "both build-start and build-result notifications must be gated"


def test_release_workflow_remains_full_and_does_not_change_the_tag() -> None:
    config = generated()
    jobs = config["workflows"]["build-deploy"]["jobs"]
    named = {list(job)[0]: list(job.values())[0] for job in jobs}
    assert set(named) == {
        "verify",
        "build_and_push",
        "update_endpoint",
        "wait_for_rollout",
        "publish_runtime_manifest",
        "notify_done",
    }
    assert "build_only" not in named["build_and_push"]


def test_build_only_is_tag_only_and_setup_routes_build_tags() -> None:
    setup_config = yaml.safe_load((SCRIPT.parent / "config.yml").read_text())
    setup_filters = setup_config["workflows"]["setup"]["jobs"][0]["setup"]["filters"]
    assert "build-" in setup_filters["tags"]["only"]
    assert setup_filters["branches"]["ignore"] == "/.*/"

    workflow_jobs = generated()["workflows"]["build-image-only"]["jobs"]
    for job in workflow_jobs:
        config = list(job.values())[0]
        assert config["filters"]["tags"]["only"] == "/^build-v[0-9]+$/"
        assert config["filters"]["branches"]["ignore"] == "/.*/"


def test_baked_regional_route_is_separate_and_build_only() -> None:
    config = generated()
    jobs = config["workflows"]["build-baked-regional-only"]["jobs"]
    named = {list(job)[0]: list(job.values())[0] for job in jobs}
    assert set(named) == {"verify", "build_baked_regional"}
    build = named["build_baked_regional"]
    assert build["requires"] == ["verify"]
    assert build["context"] == ["docker-hub"]
    assert build["filters"]["tags"]["only"] == "/^baked-v[0-9]+$/"
    assert build["filters"]["branches"]["ignore"] == "/.*/"
    command = next(
        step["run"]["command"]
        for step in config["jobs"]["build_baked_regional"]["steps"]
        if isinstance(step, dict) and "run" in step
        and step["run"].get("name", "").startswith("Build and push the baked-regional")
    )
    assert "scripts/build_baked_regional_layers.py" in command
    assert "--apply" in command and 'scail-krea-${CIRCLE_TAG#baked-}' in command
    assert "update_endpoint" not in command
    assert "notify" not in command.lower()
    assert "publish_runtime_manifest" not in command


def test_setup_routes_baked_tags() -> None:
    setup_config = yaml.safe_load((SCRIPT.parent / "config.yml").read_text())
    setup_filters = setup_config["workflows"]["setup"]["jobs"][0]["setup"]["filters"]
    assert "baked-" in setup_filters["tags"]["only"]


def test_final_baked_regional_route_is_v4_registry_only() -> None:
    config = generated()
    jobs = config["workflows"]["finalize-baked-regional-only"]["jobs"]
    named = {list(job)[0]: list(job.values())[0] for job in jobs}
    assert set(named) == {"verify", "finalize_baked_regional"}
    finalizer = named["finalize_baked_regional"]
    assert finalizer["requires"] == ["verify"]
    assert finalizer["context"] == ["docker-hub"]
    assert finalizer["filters"]["tags"]["only"] == "/^finalize-baked-v4$/"
    assert finalizer["filters"]["branches"]["ignore"] == "/.*/"

    job = config["jobs"]["finalize_baked_regional"]
    assert job["executor"] == "python"
    assert config["executors"]["python"]["docker"][0]["image"] == "cimg/python:3.12"
    runs = [step["run"] for step in job["steps"] if isinstance(step, dict) and "run" in step]
    assert runs[0]["name"] == "Test registry finalizer offline"
    assert runs[0]["command"] == "python3 scripts/test_finalize_baked_regional.py"
    run_command = runs[1]["command"]
    assert '"finalize-baked-v4"' in run_command
    assert 'RELEASE_TAG="scail-krea-${CIRCLE_TAG#finalize-baked-}"' in run_command
    assert "scripts/finalize_baked_regional.py" in run_command
    assert "--base-image \"docker.io/hearmeman/comfyui-serverless@sha256:6bf2cdc384eb262ae9526acd1c0d075ef4e2dd9fd761ce1d3f5bcf6e1f6cb7f3\"" in run_command
    assert "--expected-model-manifest-sha256 992faae2db315947e357070bab3377b76dfd63af00cf67bafaf7f60e6942cc9d" in run_command
    assert "--publish" in run_command
    assert "cat \"${RECEIPT}\"" in run_command
    assert "update_endpoint" not in run_command
    assert "publish_runtime_manifest" not in run_command
    assert "flowbot" not in run_command.lower()
    assert job["steps"][-1]["store_artifacts"]["destination"] == "baked-regional-finalization/receipt.json"
    assert job["steps"][-1]["store_artifacts"]["path"] == "/tmp/baked-regional-finalize-receipt.json"


def test_setup_routes_final_baked_tags() -> None:
    setup_config = yaml.safe_load((SCRIPT.parent / "config.yml").read_text())
    setup_filters = setup_config["workflows"]["setup"]["jobs"][0]["setup"]["filters"]
    assert "finalize-baked-" in setup_filters["tags"]["only"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all passed")
