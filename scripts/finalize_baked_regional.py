#!/usr/bin/env python3
"""Finalize a verified baked model image without pulling its model layers.

The tool reads only registry manifests/config and the files copied into the
small regional overlay. It creates one deterministic gzip layer, then updates
the remote image config and manifest through the registry API. Publishing is
explicit via --publish; no Docker daemon, BuildKit, Hugging Face, or model blob
download is used.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_LAYER_COUNT = 81
RUNTIME_COMMIT = "60fdc14d6b3a4bccf181f8aa8e6983d0311766da"
LABEL_PREFIX = "org.hearmemanai.comfygen."
OCI_TITLE = "Krea 2 and SCAIL 2 ComfyGen runtime"
OCI_DESCRIPTION = (
    "ComfyGen runtime image with model-specific license notices; "
    "Krea 2 assets require recipient agreement to the Krea 2 Community License."
)
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class FinalizeError(RuntimeError):
    pass


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80 if parsed.scheme.lower() == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip registry credentials before following a redirect to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            sensitive = {"authorization", "proxy-authorization"}
            for attribute in ("headers", "unredirected_hdrs"):
                current = getattr(redirected, attribute, {})
                for key in list(current):
                    if key.lower() in sensitive:
                        del current[key]
        return redirected


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compact_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest_ref(value: str) -> tuple[str, str, str]:
    """Return registry host, repository, and immutable digest or tag."""
    value = value.removeprefix("docker://")
    if "/" not in value:
        raise FinalizeError("image reference must include a repository")
    first, rest = value.split("/", 1)
    if "." in first or ":" in first or first == "localhost":
        registry, name = first, rest
        if registry in {"docker.io", "index.docker.io"}:
            registry = "registry-1.docker.io"
    else:
        registry, name = "registry-1.docker.io", value
    if "@" in name:
        repo, ref = name.rsplit("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
            raise FinalizeError("base image must use a sha256 digest")
    elif ":" in name.rsplit("/", 1)[-1]:
        repo, ref = name.rsplit(":", 1)
    else:
        repo, ref = name, "latest"
    if not repo or not ref:
        raise FinalizeError("invalid image reference")
    return registry, repo, ref


def select_platform(manifest: dict[str, Any], body: bytes) -> tuple[dict[str, Any], bytes, str]:
    media = manifest.get("mediaType", "")
    if media not in {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }:
        return manifest, body, "sha256:" + sha256(body)
    choices = [
        descriptor for descriptor in manifest.get("manifests", [])
        if descriptor.get("platform", {}).get("os") == "linux"
        and descriptor.get("platform", {}).get("architecture") == "amd64"
        and descriptor.get("platform", {}).get("variant") in (None, "")
    ]
    if len(choices) != 1:
        raise FinalizeError(f"expected exactly one linux/amd64 manifest, found {len(choices)}")
    return manifest, b"", choices[0]["digest"]


def overlay_files(source_root: Path) -> list[tuple[str, Path, int]]:
    fixed = [
        ("ComfyUI/extra_model_paths.yaml", source_root / "extra_model_paths.baked-regional.yaml", 0o644),
        ("start_script.sh", source_root / "start_script.baked-regional.sh", 0o755),
        ("log_forwarder.py", source_root / "log_forwarder.py", 0o644),
        ("usr/share/comfygen-model-licenses/MODEL-NOTICES.md", source_root / "MODEL-NOTICES.md", 0o644),
    ]
    for target, source, _mode in fixed:
        if not source.is_file():
            raise FinalizeError(f"required overlay source is missing: {source.name}")
    notices_root = source_root / "notices"
    if not notices_root.is_dir():
        raise FinalizeError("notices directory is missing")
    notices = [
        ("usr/share/comfygen-model-licenses/" + path.relative_to(notices_root).as_posix(), path, 0o644)
        for path in notices_root.rglob("*") if path.is_file() and not path.is_symlink()
    ]
    required = {
        "apache/LICENSE", "krea/LICENSE.pdf", "krea/NOTICE", "rife/LICENSE", "sam/LICENSE", "scail/LICENSE",
    }
    found = {path.relative_to(notices_root).as_posix() for path in notices_root.rglob("*") if path.is_file()}
    if not required <= found:
        raise FinalizeError(f"required license files missing: {sorted(required - found)}")
    return [*fixed, *sorted(notices, key=lambda x: x[0])]


def create_overlay_layer(source_root: Path) -> tuple[bytes, str, str, list[dict[str, Any]]]:
    """Create deterministic gzip(tar) and return blob digest, diffID, and file inventory."""
    files = overlay_files(source_root)
    tar_bytes = BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for target, source, mode in sorted(files, key=lambda item: item[0]):
            info = tarfile.TarInfo(target)
            content = source.read_bytes()
            info.size = len(content)
            info.mode = mode
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, BytesIO(content))
    uncompressed = tar_bytes.getvalue()
    compressed_io = BytesIO()
    with gzip.GzipFile(fileobj=compressed_io, filename="", mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(uncompressed)
    compressed = compressed_io.getvalue()
    files_info = [{"path": target, "bytes": source.stat().st_size, "mode": oct(mode)}
                  for target, source, mode in sorted(files, key=lambda item: item[0])]
    return compressed, "sha256:" + sha256(uncompressed), "sha256:" + sha256(compressed), files_info


def update_config(base: dict[str, Any], *, diff_id: str, base_ref: str,
                  manifest_sha: str, recipe_sha: str, created: str) -> dict[str, Any]:
    config = json.loads(json.dumps(base))
    rootfs = config.get("rootfs") or {}
    if rootfs.get("type") != "layers" or not isinstance(rootfs.get("diff_ids"), list):
        raise FinalizeError("base image config has no valid rootfs diff_ids")
    if len(rootfs["diff_ids"]) != BASE_LAYER_COUNT:
        raise FinalizeError(f"base config has {len(rootfs['diff_ids'])} diff_ids; expected {BASE_LAYER_COUNT}")
    rootfs["diff_ids"].append(diff_id)
    config["rootfs"] = rootfs

    image_config = config.setdefault("config", {})
    env = list(image_config.get("Env") or [])
    key = "AUX_ANNOTATOR_CKPTS_PATH="
    env = [entry for entry in env if not entry.startswith(key)]
    env.append(key + "/runpod-volume/ComfyUI/models/annotator_ckpts")
    image_config["Env"] = env
    image_config["Cmd"] = ["/start_script.sh"]
    labels = dict(image_config.get("Labels") or {})
    labels.update({
        LABEL_PREFIX + "image-variant": "baked-regional",
        LABEL_PREFIX + "base-image": base_ref,
        LABEL_PREFIX + "runtime-commit": RUNTIME_COMMIT,
        LABEL_PREFIX + "model-manifest-sha256": manifest_sha,
        LABEL_PREFIX + "layer-recipe-sha256": recipe_sha,
        "org.opencontainers.image.title": OCI_TITLE,
        "org.opencontainers.image.description": OCI_DESCRIPTION,
    })
    image_config["Labels"] = labels

    history = list(config.get("history") or [])
    history.extend([
        {"created": created, "created_by": "COPY regional config, startup script, log forwarder, and model notices"},
        {"created": created, "created_by": "ENV AUX_ANNOTATOR_CKPTS_PATH=/runpod-volume/ComfyUI/models/annotator_ckpts", "empty_layer": True},
        {"created": created, "created_by": "CMD [\"/start_script.sh\"]", "empty_layer": True},
    ])
    config["history"] = history
    return config


class Registry:
    def __init__(self, host: str, repo: str, username: str, password: str, *, can_push: bool,
                 registry_url: str | None = None, token_url: str | None = None):
        self.host = host
        self.repo = repo
        self.api = (registry_url or f"https://{host}").rstrip("/")
        self.opener = urllib.request.build_opener(SafeRedirectHandler())
        if host == "registry-1.docker.io":
            auth_url = token_url or "https://auth.docker.io/token"
            service = "registry.docker.io"
        else:
            auth_url = token_url or f"https://{host}/oauth2/token"
            service = host
        actions = "pull,push" if can_push else "pull"
        query = urllib.parse.urlencode({"service": service, "scope": f"repository:{repo}:{actions}"})
        headers = {}
        if username and password:
            credential = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {credential}"
        req = urllib.request.Request(f"{auth_url}?{query}", headers=headers)
        try:
            with self.opener.open(req, timeout=30) as response:
                token_data = json.loads(response.read())
        except Exception as exc:
            raise FinalizeError(f"registry token request failed ({type(exc).__name__})") from None
        self.token = token_data.get("token") or token_data.get("access_token")
        if not self.token:
            raise FinalizeError("registry token response contained no bearer token")

    def request(self, method: str, path: str, *, data: bytes | None = None,
                content_type: str | None = None, accept: str | None = None,
                expected: tuple[int, ...] = (200,)) -> tuple[bytes, dict[str, str]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=120) as response:
                body = response.read()
                status = response.status
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
            response_headers = {k.lower(): v for k, v in exc.headers.items()}
        except Exception as exc:
            raise FinalizeError(f"registry {method} request failed ({type(exc).__name__})") from None
        if status not in expected:
            detail = body[:256].decode("utf-8", "replace")
            raise FinalizeError(f"registry {method} returned HTTP {status}: {detail}")
        response_headers["x-finalizer-http-status"] = str(status)
        return body, response_headers

    def manifest(self, reference: str, *, optional: bool = False) -> tuple[dict[str, Any], bytes, str, str] | None:
        path = f"/v2/{self.repo}/manifests/{urllib.parse.quote(reference, safe=':@') }"
        body, headers = self.request("GET", path, accept=ACCEPT, expected=(200, 404) if optional else (200,))
        if optional and headers.get("x-finalizer-http-status") == "404":
            return None
        digest = "sha256:" + sha256(body)
        if headers.get("docker-content-digest") and headers["docker-content-digest"] != digest:
            raise FinalizeError("registry manifest digest header did not match its body")
        try:
            manifest = json.loads(body)
        except json.JSONDecodeError as exc:
            raise FinalizeError(f"registry returned invalid manifest JSON: {exc}") from None
        return manifest, body, digest, headers.get("content-type", "").split(";", 1)[0]

    def blob(self, digest: str) -> bytes:
        body, _ = self.request("GET", f"/v2/{self.repo}/blobs/{digest}")
        return body

    def upload_blob(self, data: bytes, digest: str) -> None:
        _body, headers = self.request("HEAD", f"/v2/{self.repo}/blobs/{digest}", expected=(200, 404))
        if _body or "docker-content-digest" in headers:
            return
        _body, headers = self.request("POST", f"/v2/{self.repo}/blobs/uploads/", expected=(202,))
        location = headers.get("location")
        if not location:
            raise FinalizeError("registry did not return an upload location")
        url = urllib.parse.urljoin(self.api + "/", location)
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("digest", digest))
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
        request = urllib.request.Request(url, data=data, method="PUT", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/octet-stream",
        })
        try:
            with self.opener.open(request, timeout=180) as response:
                if response.status not in (201, 202):
                    raise FinalizeError(f"registry blob upload returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise FinalizeError(f"registry blob upload returned HTTP {exc.code}") from None

    def put_manifest(self, reference: str, body: bytes, media_type: str) -> str:
        path = f"/v2/{self.repo}/manifests/{urllib.parse.quote(reference, safe=':@') }"
        _body, headers = self.request("PUT", path, data=body, content_type=media_type,
                                      accept=ACCEPT, expected=(201,))
        digest = "sha256:" + sha256(body)
        if headers.get("docker-content-digest") and headers["docker-content-digest"] != digest:
            raise FinalizeError("published manifest digest differs from computed digest")
        return digest


def locate_amd64(registry: Registry, base_ref: str) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    top, top_body, top_digest, _ = registry.manifest(base_ref)
    if top_digest != base_ref:
        raise FinalizeError("base manifest digest differs from requested immutable digest")
    selected, selected_body, selected_digest = select_platform(top, top_body)
    if selected_body:
        child, body, digest, media_type = selected, selected_body, selected_digest, selected.get("mediaType", "")
    else:
        child, body, digest, media_type = registry.manifest(selected_digest)
        if digest != selected_digest:
            raise FinalizeError("linux/amd64 child manifest digest did not match index descriptor")
    if child.get("schemaVersion") != 2 or not child.get("config") or not child.get("layers"):
        raise FinalizeError("base is not a supported schema-2 image manifest")
    return top, child, digest, media_type


def read_config(registry: Registry, manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    desc = manifest["config"]
    body = registry.blob(desc["digest"])
    if len(body) != desc.get("size") or "sha256:" + sha256(body) != desc["digest"]:
        raise FinalizeError("base config blob failed size/digest verification")
    config = json.loads(body)
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise FinalizeError("selected base config is not linux/amd64")
    labels = config.get("config", {}).get("Labels") or {}
    if labels.get(LABEL_PREFIX + "layer") != "baked-model":
        raise FinalizeError("base is not a baked model layer image")
    return config, desc["digest"]


def verify_model_chain(config: dict[str, Any], manifest: dict[str, Any], source_root: Path,
                       expected_manifest_sha: str) -> str:
    manifest_path = source_root / "baked-models.manifest.json"
    raw = manifest_path.read_bytes()
    actual_manifest_sha = sha256(raw)
    if actual_manifest_sha != expected_manifest_sha:
        raise FinalizeError("local baked model manifest SHA-256 does not match expected value")
    assets = json.loads(raw)
    ordered = sorted(assets, key=lambda item: (item["bytes"], item["asset_id"]))
    final_asset = ordered[-1]
    labels = config.get("config", {}).get("Labels") or {}
    if labels.get(LABEL_PREFIX + "model-manifest-sha256") != expected_manifest_sha:
        raise FinalizeError("model layer chain manifest label differs from expected manifest")
    if labels.get(LABEL_PREFIX + "model-asset-id") != final_asset["asset_id"]:
        raise FinalizeError("base image is not the final asset layer for the expected manifest")
    if labels.get(LABEL_PREFIX + "model-asset-sha256") != final_asset["sha256"]:
        raise FinalizeError("base image final asset SHA-256 differs from the expected manifest")
    recipe_sha = labels.get(LABEL_PREFIX + "layer-recipe-sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", recipe_sha or ""):
        raise FinalizeError("model layer chain has no valid recipe SHA-256 label")
    if len(manifest["layers"]) != BASE_LAYER_COUNT:
        raise FinalizeError(f"base image has {len(manifest['layers'])} manifest layers; expected {BASE_LAYER_COUNT}")
    if len(config.get("rootfs", {}).get("diff_ids", [])) != BASE_LAYER_COUNT:
        raise FinalizeError(f"base image config does not contain {BASE_LAYER_COUNT} diff IDs")
    return recipe_sha


def parse_descriptor_body(body: bytes, media_type: str) -> dict[str, Any]:
    return {"mediaType": media_type, "digest": "sha256:" + sha256(body), "size": len(body)}


def final_manifest(base_manifest: dict[str, Any], config_desc: dict[str, Any],
                   layer_desc: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base_manifest))
    result["config"] = config_desc
    result["layers"].append(layer_desc)
    return result


def build_receipt(*, args: argparse.Namespace, dry: bool, base_digest: str,
                  base_manifest_digest: str, config_digest: str, layer_digest: str,
                  diff_id: str, layer_count: int, manifest_sha: str,
                  recipe_sha: str, files: list[dict[str, Any]],
                  final_digest: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "DRY_RUN_READY" if dry else "PUBLISHED",
        "source_image_digest": base_digest,
        "source_platform_manifest_digest": base_manifest_digest,
        "target": args.tag,
        "published_manifest_digest": final_digest,
        "platform": "linux/amd64",
        "base_filesystem_layer_count": BASE_LAYER_COUNT,
        "final_filesystem_layer_count": layer_count,
        "base_config_sha256": config_digest.removeprefix("sha256:"),
        "overlay_blob_sha256": layer_digest.removeprefix("sha256:"),
        "overlay_diff_id": diff_id,
        "model_manifest_sha256": manifest_sha,
        "inherited_layer_recipe_sha256": recipe_sha,
        "overlay_files": files,
        "base_layers_downloaded": False,
        "huggingface_downloads": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True, help="immutable sha256 image reference for model14")
    parser.add_argument("--tag", required=True, help="same-repository target tag for finalized image")
    parser.add_argument("--expected-model-manifest-sha256", required=True)
    parser.add_argument("--expected-base-recipe-sha256", help="optional strict model14 recipe label check")
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--receipt", type=Path, default=Path("baked-regional-finalize-receipt.json"))
    parser.add_argument("--expected-base-layer-count", type=int, default=BASE_LAYER_COUNT)
    parser.add_argument("--publish", action="store_true", help="upload overlay/config/manifest; default is metadata-only dry run")
    parser.add_argument("--registry-url", help=argparse.SUPPRESS)
    parser.add_argument("--token-url", help=argparse.SUPPRESS)
    args = parser.parse_args()

    base_host, base_repo, base_ref = digest_ref(args.base_image)
    tag_host, tag_repo, target_tag = digest_ref(args.tag)
    if not base_ref.startswith("sha256:"):
        parser.error("--base-image must be pinned by immutable sha256 digest")
    if (base_host, base_repo) != (tag_host, tag_repo):
        parser.error("base and output must use the same registry repository to avoid base blob copies")
    if base_repo != "hearmeman/comfyui-serverless" or not re.fullmatch(r"scail-krea-v[0-9]+", target_tag):
        parser.error("target must be scail-krea-v<N> in hearmeman/comfyui-serverless")
    if args.expected_base_layer_count != BASE_LAYER_COUNT:
        parser.error(f"the verified model14 chain has exactly {BASE_LAYER_COUNT} filesystem layers")

    username = os.environ.get("DOCKERHUB_USER", "")
    password = os.environ.get("DOCKERHUB_PAT", "")
    if args.publish and (not username or not password):
        raise FinalizeError("DOCKERHUB_USER and DOCKERHUB_PAT must be provided through the CI context for publishing")
    registry = Registry(base_host, base_repo, username, password, can_push=args.publish,
                        registry_url=args.registry_url, token_url=args.token_url)
    top, manifest, platform_digest, manifest_media = locate_amd64(registry, base_ref)
    base_manifest_digest = platform_digest
    config, config_digest = read_config(registry, manifest)
    recipe_sha = verify_model_chain(config, manifest, args.source_root, args.expected_model_manifest_sha256)
    if args.expected_base_recipe_sha256 and recipe_sha != args.expected_base_recipe_sha256:
        raise FinalizeError("base layer recipe label differs from expected recipe SHA-256")
    overlay, diff_id, layer_digest, files = create_overlay_layer(args.source_root)
    # Use an inherited timestamp so identical inputs make an identical final
    # config/manifest digest, which makes retries safe and idempotent.
    now = config.get("created") or "1970-01-01T00:00:00Z"
    updated_config = update_config(config, diff_id=diff_id, base_ref=args.base_image,
                                  manifest_sha=args.expected_model_manifest_sha256,
                                  recipe_sha=recipe_sha, created=now)
    config_body = compact_json(updated_config)
    config_desc = parse_descriptor_body(config_body, manifest["config"]["mediaType"])
    old_layer_media = manifest["layers"][-1]["mediaType"]
    layer_desc = parse_descriptor_body(overlay, old_layer_media)
    updated_manifest = final_manifest(manifest, config_desc, layer_desc)
    manifest_body = compact_json(updated_manifest)
    manifest_media = manifest.get("mediaType") or manifest_media
    if not manifest_media:
        raise FinalizeError("base image manifest did not declare a media type")

    final_digest = None
    if args.publish:
        expected_final_digest = "sha256:" + sha256(manifest_body)
        existing_target = registry.manifest(target_tag, optional=True)
        if existing_target:
            if existing_target[2] != expected_final_digest:
                raise FinalizeError(f"target tag already exists with a different digest; refusing overwrite: {target_tag}")
            final_digest = existing_target[2]
        else:
            registry.upload_blob(overlay, layer_desc["digest"])
            registry.upload_blob(config_body, config_desc["digest"])
            final_digest = registry.put_manifest(target_tag, manifest_body, manifest_media)
        # Verify the published tag's manifest/config identity and layer count.
        published, _body, verified_digest, _media = registry.manifest(target_tag)
        if verified_digest != final_digest:
            raise FinalizeError("post-publish tag points at a different manifest digest")
        if len(published.get("layers", [])) != BASE_LAYER_COUNT + 1:
            raise FinalizeError("published manifest has an unexpected layer count")
        verified_config, _ = read_config(registry, published)
        if verified_config.get("rootfs", {}).get("diff_ids", [])[-1:] != [diff_id]:
            raise FinalizeError("published config does not end with the expected overlay diff ID")

    receipt = build_receipt(args=args, dry=not args.publish, base_digest=args.base_image,
                            base_manifest_digest=base_manifest_digest,
                            config_digest=config_digest, layer_digest=layer_digest,
                            diff_id=diff_id, layer_count=BASE_LAYER_COUNT + 1,
                            manifest_sha=args.expected_model_manifest_sha256,
                            recipe_sha=recipe_sha, files=files, final_digest=final_digest)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, FinalizeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
