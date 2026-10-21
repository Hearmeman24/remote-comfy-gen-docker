#!/usr/bin/env python3
"""Regression tests for the runtime manifest's pinned base-image contract."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from write_runtime_manifest import read_base_image


def _pins_file(value: object) -> Path:
    tmp = TemporaryDirectory()
    # Keep the directory alive for the duration of the test via the path's
    # parent reference held in this module-level list.
    _TEMP_DIRS.append(tmp)
    path = Path(tmp.name) / "pins.json"
    path.write_text(json.dumps({"base_image": value}), encoding="utf-8")
    return path


_TEMP_DIRS: list[TemporaryDirectory[str]] = []


def test_accepts_supported_base_tags() -> None:
    for value in (
        "hearmeman/comfyui-base:cu130-comfy0.32.0-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9b-torch2.11.0",
    ):
        assert read_base_image(_pins_file(value)) == value


def test_rejects_malformed_or_unapproved_base_tags() -> None:
    for value in (
        "docker.io/hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9b-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-badtag-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-63BE2F9B-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9b-extra-torch2.11.0",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-torch2.11.0;echo bad",
        "hearmeman/comfyui-base:cu130-comfy0.42.1-63be2f9b-torch",
    ):
        try:
            read_base_image(_pins_file(value))
        except RuntimeError:
            continue
        raise AssertionError(f"accepted malformed base image {value!r}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all passed")
