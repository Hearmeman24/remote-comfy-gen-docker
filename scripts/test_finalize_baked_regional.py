#!/usr/bin/env python3
"""Offline functional tests for the metadata-only regional finalizer."""
from __future__ import annotations

import gzip
import hashlib
import http.server
import importlib.util
import io
import json
import os
import threading
import urllib.parse
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("finalize_baked_regional.py")
SPEC = importlib.util.spec_from_file_location("finalize_baked_regional", SCRIPT)
assert SPEC and SPEC.loader
finalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalizer)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fixture_source(root: Path) -> tuple[str, str]:
    (root / "notices/apache").mkdir(parents=True)
    (root / "notices/krea").mkdir(parents=True)
    (root / "notices/rife").mkdir(parents=True)
    (root / "notices/sam").mkdir(parents=True)
    (root / "notices/scail").mkdir(parents=True)
    (root / "extra_model_paths.baked-regional.yaml").write_text("models: []\n")
    (root / "start_script.baked-regional.sh").write_text("#!/bin/sh\necho start\n")
    (root / "log_forwarder.py").write_text("print('forward')\n")
    (root / "MODEL-NOTICES.md").write_text("licenses are model-specific\n")
    for rel in ("apache/LICENSE", "krea/LICENSE.pdf", "krea/NOTICE", "rife/LICENSE", "sam/LICENSE", "scail/LICENSE"):
        (root / "notices" / rel).write_bytes(("license:" + rel).encode())
    assets = [{"asset_id": f"asset-{i:02d}", "bytes": i + 1, "sha256": f"{i:064x}"} for i in range(14)]
    raw = json.dumps(assets, separators=(",", ":")).encode()
    (root / "baked-models.manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), assets[-1]["sha256"]


class FakeRegistry(http.server.BaseHTTPRequestHandler):
    repo = "hearmeman/comfyui-serverless"
    base_config: dict
    base_config_bytes: bytes
    base_manifest: dict
    base_manifest_bytes: bytes
    index_bytes: bytes
    index_digest: str
    blobs: dict[str, bytes]
    manifests: dict[str, tuple[dict, bytes, str]]
    calls: list[tuple[str, str]]
    upload_bytes: dict[str, bytes]
    manifest_puts: int
    next_upload: int
    redirect_blob_digest: str | None
    redirect_url: str | None

    def log_message(self, *_args):
        pass

    def send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self.calls.append(("GET", self.path))
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/token":
            return self.send(200, b'{"token":"test-token"}', {"Content-Type": "application/json"})
        if parsed.path.endswith("/manifests/" + self.index_digest):
            return self.send(200, self.index_bytes, {
                "Content-Type": "application/vnd.oci.image.index.v1+json",
                "Docker-Content-Digest": self.index_digest,
            })
        if parsed.path.endswith("/manifests/" + digest(self.base_manifest_bytes)):
            return self.send(200, self.base_manifest_bytes, {
                "Content-Type": "application/vnd.docker.distribution.manifest.v2+json",
                "Docker-Content-Digest": digest(self.base_manifest_bytes),
            })
        if "/manifests/" in parsed.path:
            ref = urllib.parse.unquote(parsed.path.rsplit("/manifests/", 1)[1])
            found = self.manifests.get(ref)
            if not found:
                return self.send(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', {"Content-Type": "application/json"})
            obj, body, media = found
            return self.send(200, body, {"Content-Type": media, "Docker-Content-Digest": digest(body)})
        if "/blobs/" in parsed.path:
            blob_digest = parsed.path.rsplit("/blobs/", 1)[1]
            if blob_digest == self.redirect_blob_digest and self.redirect_url:
                return self.send(302, headers={"Location": self.redirect_url})
            body = self.blobs.get(blob_digest)
            if body is None:
                return self.send(404, b"")
            return self.send(200, body, {"Docker-Content-Digest": blob_digest})
        return self.send(404, b"")

    def do_HEAD(self):
        self.calls.append(("HEAD", self.path))
        blob_digest = self.path.rsplit("/blobs/", 1)[-1]
        body = self.blobs.get(blob_digest)
        if body is None:
            return self.send(404)
        return self.send(200, headers={"Docker-Content-Digest": blob_digest})

    def do_POST(self):
        self.calls.append(("POST", self.path))
        self.__class__.next_upload += 1
        return self.send(202, headers={"Location": f"/v2/{self.repo}/blobs/uploads/u{self.next_upload}"})

    def do_PUT(self):
        self.calls.append(("PUT", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        parsed = urllib.parse.urlsplit(self.path)
        if "/blobs/uploads/" in parsed.path:
            blob_digest = urllib.parse.parse_qs(parsed.query)["digest"][0]
            self.blobs[blob_digest] = body
            return self.send(201, headers={"Docker-Content-Digest": blob_digest})
        if "/manifests/" in parsed.path:
            ref = urllib.parse.unquote(parsed.path.rsplit("/manifests/", 1)[1])
            media = self.headers.get("Content-Type", "")
            self.manifests[ref] = (json.loads(body), body, media)
            self.__class__.manifest_puts += 1
            return self.send(201, headers={"Docker-Content-Digest": digest(body)})
        return self.send(404)


class FinalizerTests(unittest.TestCase):
    def test_exact_release_reference_parsing(self):
        self.assertEqual(
            finalizer.digest_ref(
                "docker.io/hearmeman/comfyui-serverless@sha256:" + "a" * 64
            ),
            ("registry-1.docker.io", "hearmeman/comfyui-serverless", "sha256:" + "a" * 64),
        )
        self.assertEqual(
            finalizer.digest_ref("docker.io/hearmeman/comfyui-serverless:scail-krea-v4"),
            ("registry-1.docker.io", "hearmeman/comfyui-serverless", "scail-krea-v4"),
        )

    def make_registry(self):
        cfg = {
            "architecture": "amd64", "os": "linux", "created": "2025-01-02T03:04:05Z",
            "config": {
                "Env": ["PATH=/usr/bin", "AUX_ANNOTATOR_CKPTS_PATH=old"],
                "Entrypoint": ["/entrypoint.sh"], "Cmd": ["old-command"],
                "WorkingDir": "/ComfyUI", "User": "1000:1000", "Volumes": {"/runpod-volume": {}},
                "Shell": ["/bin/bash", "-c"],
                "Labels": {
                    finalizer.LABEL_PREFIX + "layer": "baked-model",
                    finalizer.LABEL_PREFIX + "model-manifest-sha256": "manifest-sha",
                    finalizer.LABEL_PREFIX + "model-asset-id": "asset-13",
                    finalizer.LABEL_PREFIX + "model-asset-sha256": "f" * 64,
                    finalizer.LABEL_PREFIX + "layer-recipe-sha256": "e" * 64,
                    "custom.keep": "value",
                },
            },
            "rootfs": {"type": "layers", "diff_ids": [f"sha256:{i:064x}" for i in range(81)]},
            "history": [{"created_by": f"base-{i}"} for i in range(81)],
        }
        cfg_bytes = json.dumps(cfg, separators=(",", ":")).encode()
        cfg_digest = digest(cfg_bytes)
        layers = [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                   "size": 100 + i, "digest": f"sha256:{i + 1000:064x}"} for i in range(81)]
        base_manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                       "size": len(cfg_bytes), "digest": cfg_digest},
            "layers": layers,
        }
        manifest_bytes = json.dumps(base_manifest, separators=(",", ":")).encode()
        manifest_digest = digest(manifest_bytes)
        arm_bytes = b'{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json","config":{"digest":"sha256:' + b"0"*64 + b'","size":1,"mediaType":"application/vnd.docker.container.image.v1+json"},"layers":[]}'
        arm_digest = digest(arm_bytes)
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"mediaType": "application/vnd.docker.distribution.manifest.v2+json", "digest": arm_digest,
             "size": len(arm_bytes), "platform": {"os": "linux", "architecture": "arm64"}},
            {"mediaType": "application/vnd.docker.distribution.manifest.v2+json", "digest": manifest_digest,
             "size": len(manifest_bytes), "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        index_bytes = json.dumps(index, separators=(",", ":")).encode()
        FakeRegistry.base_config = cfg
        FakeRegistry.base_config_bytes = cfg_bytes
        FakeRegistry.base_manifest = base_manifest
        FakeRegistry.base_manifest_bytes = manifest_bytes
        FakeRegistry.index_bytes = index_bytes
        FakeRegistry.index_digest = digest(index_bytes)
        FakeRegistry.blobs = {cfg_digest: cfg_bytes}
        FakeRegistry.manifests = {}
        FakeRegistry.calls = []
        FakeRegistry.upload_bytes = {}
        FakeRegistry.manifest_puts = 0
        FakeRegistry.next_upload = 0
        FakeRegistry.redirect_blob_digest = None
        FakeRegistry.redirect_url = None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def invoke(source: Path, digest_sha: str, manifest_sha: str, receipt: Path,
               api: str, *, publish: bool, tag: str = "scail-krea-v4", credentials: bool = True) -> int:
        argv = ["finalize_baked_regional.py", "--base-image",
                f"docker.io/hearmeman/comfyui-serverless@{FakeRegistry.index_digest}",
                "--tag", f"docker.io/hearmeman/comfyui-serverless:{tag}",
                "--expected-model-manifest-sha256", manifest_sha,
                "--source-root", str(source), "--receipt", str(receipt),
                "--registry-url", api, "--token-url", api + "/token"]
        if publish:
            argv.append("--publish")
        env_patch = patch.dict(os.environ, {"DOCKERHUB_USER": "unit", "DOCKERHUB_PAT": "not-a-real-secret"}) if credentials else patch.dict(
            os.environ, {}, clear=True
        )
        with patch("sys.argv", argv), env_patch:
            return finalizer.main()

    def test_deterministic_overlay_and_destination_modes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha, _ = fixture_source(root)
            first = finalizer.create_overlay_layer(root)
            second = finalizer.create_overlay_layer(root)
            self.assertEqual(first[:3], second[:3])
            compressed, diffid, layer_digest, files = first
            raw = gzip.decompress(compressed)
            self.assertEqual(digest(raw), diffid)
            self.assertEqual(digest(compressed), layer_digest)
            with tarfile_open(raw) as contents:
                names = {m.name for m in contents.getmembers()}
                self.assertIn("usr/share/comfygen-model-licenses/apache/LICENSE", names)
                self.assertNotIn("usr/share/comfygen-model-licenses/notices/apache/LICENSE", names)
                start = contents.getmember("start_script.sh")
                self.assertEqual(start.mode, 0o755)
                self.assertEqual(len(names), len(files))

    def test_fake_registry_dry_run_publish_and_idempotent_retry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha, final_asset_sha = fixture_source(root)
            # Bind fake base labels to this fixture manifest.
            server, thread, api = self.make_registry()
            try:
                cfg = FakeRegistry.base_config
                cfg["config"]["Labels"][finalizer.LABEL_PREFIX + "model-manifest-sha256"] = manifest_sha
                cfg["config"]["Labels"][finalizer.LABEL_PREFIX + "model-asset-sha256"] = final_asset_sha
                raw_cfg = json.dumps(cfg, separators=(",", ":")).encode()
                config_descriptor = FakeRegistry.base_manifest["config"]
                config_descriptor["digest"] = digest(raw_cfg)
                config_descriptor["size"] = len(raw_cfg)
                FakeRegistry.base_config_bytes = raw_cfg
                FakeRegistry.blobs = {digest(raw_cfg): raw_cfg}
                # Recompute amd64 child/index references with the updated config descriptor.
                amd = json.dumps(FakeRegistry.base_manifest, separators=(",", ":")).encode()
                amd_digest = digest(amd)
                index = json.loads(FakeRegistry.index_bytes)
                amd_desc = next(x for x in index["manifests"] if x["platform"]["architecture"] == "amd64")
                amd_desc["digest"] = amd_digest; amd_desc["size"] = len(amd)
                FakeRegistry.base_manifest_bytes = amd
                FakeRegistry.index_bytes = json.dumps(index, separators=(",", ":")).encode()
                FakeRegistry.index_digest = digest(FakeRegistry.index_bytes)
                # Recheck base's final-asset label from fixture manifest ordering.
                FakeRegistry.base_config = cfg
                base = f"docker.io/hearmeman/comfyui-serverless@{FakeRegistry.index_digest}"

                receipt = root / "dry.json"
                status = self.invoke(root, "", manifest_sha, receipt, api, publish=False, credentials=False)
                self.assertEqual(status, 0)
                dry = json.loads(receipt.read_text())
                self.assertEqual(dry["status"], "DRY_RUN_READY")
                self.assertEqual(dry["final_filesystem_layer_count"], 82)
                self.assertFalse(any("/blobs/sha256:000" in path for _method, path in FakeRegistry.calls))
                token_query = urllib.parse.parse_qs(urllib.parse.urlsplit(next(
                    path for method, path in FakeRegistry.calls if method == "GET" and path.startswith("/token?")
                )).query)
                self.assertEqual(token_query["scope"], [f"repository:{FakeRegistry.repo}:pull"])
                self.assertFalse(any(method == "PUT" for method, _path in FakeRegistry.calls))
                self.assertEqual(FakeRegistry.manifest_puts, 0)

                # Use the new index ref produced from the fixture during the test.
                with patch.object(finalizer, "BASE_LAYER_COUNT", 81):
                    receipt = root / "published.json"
                    self.invoke(root, "", manifest_sha, receipt, api, publish=True)
                    published_receipt = json.loads(receipt.read_text())
                    self.assertEqual(published_receipt["status"], "PUBLISHED")
                    self.assertEqual(published_receipt["final_filesystem_layer_count"], 82)
                    self.assertEqual(FakeRegistry.manifest_puts, 1)
                    published, _body, _media = FakeRegistry.manifests["scail-krea-v4"]
                    self.assertEqual(len(published["layers"]), 82)
                    base_digests = [x["digest"] for x in FakeRegistry.base_manifest["layers"]]
                    self.assertEqual([x["digest"] for x in published["layers"][:81]], base_digests)
                    final_config = json.loads(FakeRegistry.blobs[published["config"]["digest"]])
                    old_config = FakeRegistry.base_config
                    for key in ("Entrypoint", "WorkingDir", "User", "Volumes", "Shell"):
                        self.assertEqual(final_config["config"][key], old_config["config"][key])
                    self.assertEqual(final_config["config"]["Cmd"], ["/start_script.sh"])
                    self.assertEqual(final_config["config"]["Env"].count("AUX_ANNOTATOR_CKPTS_PATH=/runpod-volume/ComfyUI/models/annotator_ckpts"), 1)
                    self.assertEqual(final_config["config"]["Labels"]["custom.keep"], "value")
                    self.assertEqual(final_config["architecture"], "amd64")
                    self.assertEqual(len(final_config["rootfs"]["diff_ids"]), 82)
                    self.assertEqual(FakeRegistry.manifest_puts, 1)
                    self.invoke(root, "", manifest_sha, root / "retry.json", api, publish=True)
                    self.assertEqual(FakeRegistry.manifest_puts, 1)
                self.assertFalse(any(path.endswith("/blobs/" + layer["digest"]) for method, path in FakeRegistry.calls
                                     for layer in FakeRegistry.base_manifest["layers"] if method == "GET"))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_authenticated_config_redirect_drops_auth_for_signed_storage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha, final_asset_sha = fixture_source(root)
            source_server, source_thread, api = self.make_registry()
            storage_state = {"authorization": None, "path": None}

            class SignedStorage(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    storage_state["authorization"] = self.headers.get("Authorization")
                    storage_state["path"] = self.path
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(FakeRegistry.base_config_bytes)))
                    self.end_headers()
                    self.wfile.write(FakeRegistry.base_config_bytes)

            storage_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SignedStorage)
            storage_thread = threading.Thread(target=storage_server.serve_forever, daemon=True)
            storage_thread.start()
            try:
                cfg = FakeRegistry.base_config
                cfg["config"]["Labels"][finalizer.LABEL_PREFIX + "model-manifest-sha256"] = manifest_sha
                cfg["config"]["Labels"][finalizer.LABEL_PREFIX + "model-asset-sha256"] = final_asset_sha
                config_body = json.dumps(cfg, separators=(",", ":")).encode()
                config_digest = digest(config_body)
                FakeRegistry.base_config = cfg
                FakeRegistry.base_config_bytes = config_body
                FakeRegistry.base_manifest["config"]["digest"] = config_digest
                FakeRegistry.base_manifest["config"]["size"] = len(config_body)
                child_body = json.dumps(FakeRegistry.base_manifest, separators=(",", ":")).encode()
                child_digest = digest(child_body)
                index = json.loads(FakeRegistry.index_bytes)
                amd = next(x for x in index["manifests"] if x["platform"]["architecture"] == "amd64")
                amd["digest"] = child_digest
                amd["size"] = len(child_body)
                FakeRegistry.base_manifest_bytes = child_body
                FakeRegistry.index_bytes = json.dumps(index, separators=(",", ":")).encode()
                FakeRegistry.index_digest = digest(FakeRegistry.index_bytes)
                FakeRegistry.blobs[config_digest] = config_body
                FakeRegistry.redirect_blob_digest = config_digest
                FakeRegistry.redirect_url = f"http://127.0.0.1:{storage_server.server_port}/config?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=placeholder"

                result = self.invoke(root, "", manifest_sha, root / "redirect.json", api, publish=False, credentials=True)
                self.assertEqual(result, 0)
                self.assertIsNone(storage_state["authorization"])
                self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", storage_state["path"])
                self.assertTrue(any(path.endswith("/blobs/" + config_digest) for method, path in FakeRegistry.calls if method == "GET"))
                receipt_text = (root / "redirect.json").read_text()
                self.assertNotIn("X-Amz-Signature", receipt_text)
                self.assertNotIn("not-a-real-secret", receipt_text)
            finally:
                source_server.shutdown(); source_server.server_close(); source_thread.join(timeout=2)
                storage_server.shutdown(); storage_server.server_close(); storage_thread.join(timeout=2)

    def test_refuses_unsafe_tags_and_digest_collision(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha, _ = fixture_source(root)
            server, thread, api = self.make_registry()
            try:
                before = len(FakeRegistry.calls)
                with self.assertRaises(SystemExit):
                    self.invoke(root, "", manifest_sha, root / "blocked.json", api, publish=False, tag="v35")
                self.assertEqual(len(FakeRegistry.calls), before)
                # Existing target with an unrelated digest is never overwritten.
                existing = {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                            "config": {}, "layers": []}
                body = json.dumps(existing, separators=(",", ":")).encode()
                FakeRegistry.manifests["scail-krea-v4"] = (existing, body, existing["mediaType"])
                # The base labels/manifest in this server need a matching local manifest hash.
                FakeRegistry.base_config["config"]["Labels"][finalizer.LABEL_PREFIX + "model-manifest-sha256"] = manifest_sha
                raw_cfg = json.dumps(FakeRegistry.base_config, separators=(",", ":")).encode()
                FakeRegistry.base_manifest["config"]["digest"] = digest(raw_cfg)
                FakeRegistry.base_manifest["config"]["size"] = len(raw_cfg)
                FakeRegistry.blobs[digest(raw_cfg)] = raw_cfg
                amd = json.dumps(FakeRegistry.base_manifest, separators=(",", ":")).encode()
                index = json.loads(FakeRegistry.index_bytes)
                amd_desc = next(x for x in index["manifests"] if x["platform"]["architecture"] == "amd64")
                amd_desc["digest"] = digest(amd); amd_desc["size"] = len(amd)
                FakeRegistry.index_bytes = json.dumps(index, separators=(",", ":")).encode()
                FakeRegistry.index_digest = digest(FakeRegistry.index_bytes)
                with self.assertRaises(finalizer.FinalizeError):
                    self.invoke(root, "", manifest_sha, root / "collision.json", api, publish=True)
                self.assertEqual(FakeRegistry.manifest_puts, 0)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


def tarfile_open(raw: bytes):
    import tarfile
    return tarfile.open(fileobj=io.BytesIO(raw), mode="r:")


if __name__ == "__main__":
    unittest.main(verbosity=2)
