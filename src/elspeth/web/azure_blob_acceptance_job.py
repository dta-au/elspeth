"""Run the two Azure Blob plugin lifecycles inside the acceptance environment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict
from uuid import uuid4

from azure.core.exceptions import ResourceExistsError

from elspeth.plugins.infrastructure.azure_auth import AzureAuthConfig


class BlobProbeReport(TypedDict):
    cases_total: int
    cases_passed: int
    blob_sha256: str
    collision_rejected: bool
    cleanup_succeeded: bool


def _pipeline(document: str, directory: Path, name: str) -> None:
    """Execute the installed production CLI; raw output stays in the private Job scratch directory."""

    settings = directory / f"{name}.yaml"
    settings.write_text(document, encoding="utf-8")
    with (directory / f"{name}.log").open("wb") as log:
        completed = subprocess.run(
            [sys.executable, "-m", "elspeth.cli", "run", "--settings", str(settings), "--execute"],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=600,
        )
    if completed.returncode != 0:
        raise RuntimeError("blob_plugin_lifecycle_failed")


def run_probe(*, account_url: str, container: str, directory: Path) -> BlobProbeReport:
    """Publish through the sink, consume through the source, and independently check the remote object."""

    if os.environ.get("AZURE_TOKEN_CREDENTIALS") != "ManagedIdentityCredential":
        raise ValueError("managed_identity_credential_required")
    auth = AzureAuthConfig(auth_mode="managed_identity", use_managed_identity=True, account_url=account_url)
    blob_path = f"elspeth-acceptance/{uuid4().hex}/probe.jsonl"
    row = {"id": 1, "value": "azure-blob-managed-identity"}
    source_path = directory / "input.jsonl"
    output_path = directory / "output.jsonl"
    source_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    local_source = {
        "plugin": "json",
        "on_success": "output",
        "options": {
            "path": str(source_path),
            "format": "jsonl",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    }
    remote_options = {
        "auth_mode": "managed_identity",
        "use_managed_identity": True,
        "account_url": account_url,
        "container": container,
        "blob_path": blob_path,
        "format": "jsonl",
        "schema": {"mode": "observed"},
    }
    runtime = {
        "landscape": {"url": f"sqlite:///{directory / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(directory / "payloads")},
    }
    service = auth.create_blob_service_client()
    blob = service.get_blob_client(container=container, blob=blob_path)
    cleanup_succeeded = False
    try:
        _pipeline(
            json.dumps(
                {
                    **runtime,
                    "sources": {"input": local_source},
                    "sinks": {"output": {"plugin": "azure_blob", "on_write_failure": "discard", "options": remote_options}},
                }
            ),
            directory,
            "sink",
        )
        published = blob.download_blob().readall()
        if [json.loads(line) for line in published.splitlines() if line.strip()] != [row]:
            raise RuntimeError("blob_sink_bytes_mismatch")
        _pipeline(
            json.dumps(
                {
                    **runtime,
                    "sources": {
                        "input": {
                            "plugin": "azure_blob",
                            "on_success": "output",
                            "options": {**remote_options, "on_validation_failure": "discard"},
                        }
                    },
                    "sinks": {
                        "output": {
                            "plugin": "json",
                            "on_write_failure": "discard",
                            "options": {
                                "path": str(output_path),
                                "format": "jsonl",
                                "schema": {"mode": "observed"},
                                "collision_policy": "fail_if_exists",
                            },
                        }
                    },
                }
            ),
            directory,
            "source",
        )
        if [json.loads(line) for line in output_path.read_bytes().splitlines() if line.strip()] != [row]:
            raise RuntimeError("blob_source_bytes_mismatch")
        collision_rejected = False
        try:
            blob.upload_blob(b"conflicting-content", overwrite=False)
        except ResourceExistsError:
            collision_rejected = True
        if not collision_rejected or blob.download_blob().readall() != published:
            raise RuntimeError("blob_collision_not_rejected")
        digest = hashlib.sha256(published).hexdigest()
    finally:
        try:
            # A failed sink can leave no object; the absence is itself observed.
            if blob.exists():
                blob.delete_blob()
            cleanup_succeeded = not blob.exists()
        finally:
            blob.close()
            service.close()
    if not cleanup_succeeded:
        raise RuntimeError("blob_cleanup_failed")
    return {
        "cases_total": 2,
        "cases_passed": 2,
        "blob_sha256": digest,
        "collision_rejected": collision_rejected,
        "cleanup_succeeded": cleanup_succeeded,
    }


def main() -> int:
    try:
        with TemporaryDirectory(prefix="elspeth-blob-acceptance-") as directory:
            report = run_probe(
                account_url=os.environ["ELSPETH_ACCEPTANCE_BLOB_ACCOUNT_URL"],
                container=os.environ["ELSPETH_ACCEPTANCE_BLOB_CONTAINER"],
                directory=Path(directory),
            )
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0
    except Exception:
        sys.stderr.write('{"error":"blob_managed_identity_probe_failed"}\n')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
