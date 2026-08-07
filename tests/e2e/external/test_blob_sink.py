"""Azurite-backed E2E tests for the Azure Blob Storage sink plugin."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.sinks._remote_object_effects import RemoteObjectCollisionError
from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSink
from tests.fixtures.base_classes import inject_write_failure

pytestmark = pytest.mark.e2e


def _blob_client(container_info: dict[str, str], blob_path: str):
    from azure.storage.blob import BlobServiceClient

    service_client = BlobServiceClient.from_connection_string(container_info["connection_string"])
    return service_client.get_container_client(container_info["container"]).get_blob_client(blob_path)


def _read_blob(container_info: dict[str, str], blob_path: str) -> bytes:
    return _blob_client(container_info, blob_path).download_blob().readall()


def _sink_config(container_info: dict[str, str], *, blob_path: str, format_: str = "csv", **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "connection_string": container_info["connection_string"],
        "container": container_info["container"],
        "blob_path": blob_path,
        "format": format_,
        "schema": {"mode": "observed"},
        "overwrite": True,
    }
    config.update(overrides)
    return config


def _effect_member(operation_id: str, ordinal: int, row: dict[str, Any]) -> SinkEffectMember:
    lineage_json = canonical_json([])
    encoded = canonical_json(row).encode("utf-8")
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"{operation_id}-token-{ordinal}",
        row_id=f"{operation_id}-row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json=lineage_json,
        lineage_hash=sha256(lineage_json.encode("utf-8")).hexdigest(),
        payload_hash=sha256(encoded).hexdigest(),
        row=row,
        member_effect_id=sha256(f"{operation_id}-member-{ordinal}".encode()).hexdigest(),
    )


def _prepare_effect(
    sink: AzureBlobSink,
    rows: list[dict[str, Any]],
    *,
    operation_id: str,
    predecessor: ArtifactDescriptor | None = None,
) -> SinkEffectPlan:
    """Drive inspect+prepare only, against real Azurite, without committing."""
    members = tuple(_effect_member(operation_id, ordinal, row) for ordinal, row in enumerate(rows))
    effect_id = sha256(canonical_json({"operation_id": operation_id, "rows": rows}).encode()).hexdigest()
    ctx = RestrictedSinkEffectContext(
        run_id="azure-e2e-run",
        run_started_at=datetime(2026, 7, 17, tzinfo=UTC),
        operation_id=operation_id,
        sink_node_id="azure-sink",
    )
    inspection = sink.inspect_effect(
        SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=predecessor),
        ctx,
    )
    return sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            effect_input=SinkEffectPipelineMembersInput(
                members=members, target_snapshot_members=members, target_delivered_member_count=len(members)
            ),
            inspection=inspection,
        ),
        ctx,
    )


def _publish_effect(
    sink: AzureBlobSink,
    rows: list[dict[str, Any]],
    *,
    operation_id: str,
    predecessor: ArtifactDescriptor | None = None,
) -> SinkEffectCommitResult:
    """Exercise the public recoverable-effect protocol against real Azurite."""
    ctx = RestrictedSinkEffectContext(
        run_id="azure-e2e-run",
        run_started_at=datetime(2026, 7, 17, tzinfo=UTC),
        operation_id=operation_id,
        sink_node_id="azure-sink",
    )
    plan = _prepare_effect(sink, rows, operation_id=operation_id, predecessor=predecessor)
    return sink.commit_effect(plan, ctx)


class TestBlobSink:
    """Azure Blob Storage sink plugin E2E tests."""

    def test_blob_sink_writes_csv_data(self, azurite_blob_container: dict[str, str]) -> None:
        blob_path = "outputs/customers.csv"
        sink = inject_write_failure(AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="csv")))

        result = _publish_effect(
            sink,
            [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}],
            operation_id="csv-write",
        )
        content = _read_blob(azurite_blob_container, blob_path).decode("utf-8")

        assert result.descriptor.path_or_uri == f"azure://{azurite_blob_container['container']}/{blob_path}"
        assert result.descriptor.size_bytes == len(content.encode("utf-8"))
        assert list(csv.DictReader(io.StringIO(content))) == [
            {"id": "1", "name": "Ada"},
            {"id": "2", "name": "Grace"},
        ]

    def test_blob_sink_writes_json_data(self, azurite_blob_container: dict[str, str]) -> None:
        blob_path = "outputs/customers.json"
        sink = inject_write_failure(AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="json")))

        _publish_effect(
            sink,
            [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
            operation_id="json-write",
        )

        assert json.loads(_read_blob(azurite_blob_container, blob_path)) == [
            {"id": 1, "name": "Ada"},
            {"id": 2, "name": "Grace"},
        ]

    def test_blob_sink_overwrites_existing_blob(self, azurite_blob_container: dict[str, str]) -> None:
        blob_path = "outputs/replace.csv"
        sink = inject_write_failure(AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="csv")))

        predecessor = _publish_effect(sink, [{"id": "0", "name": "Original"}], operation_id="overwrite-original")
        _publish_effect(
            sink,
            [{"id": "1", "name": "Replacement"}],
            operation_id="overwrite-replacement",
            predecessor=predecessor.descriptor,
        )

        assert "Replacement" in _read_blob(azurite_blob_container, blob_path).decode("utf-8")

    def test_blob_sink_creates_new_blob(self, azurite_blob_container: dict[str, str]) -> None:
        blob_path = "outputs/new.jsonl"
        sink = inject_write_failure(AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="jsonl")))

        result = _publish_effect(sink, [{"id": 1, "name": "Ada"}], operation_id="jsonl-write")

        assert result.descriptor.path_or_uri == f"azure://{azurite_blob_container['container']}/{blob_path}"
        assert _read_blob(azurite_blob_container, blob_path).decode("utf-8") == '{"id": 1, "name": "Ada"}'

    def test_blob_sink_reaffirms_identical_content_and_rejects_true_collision_without_overwrite(
        self, azurite_blob_container: dict[str, str]
    ) -> None:
        """elspeth-9a78b3a02f against real Azurite: overwrite=False must
        no-op on an idempotent re-drive of identical content (never issuing
        a second upload_blob), and still reject a genuine collision."""
        blob_path = "outputs/idempotent.jsonl"
        rows = [{"id": 1, "name": "Ada"}]

        primary = inject_write_failure(
            AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="jsonl", overwrite=False))
        )
        primary_result = _publish_effect(primary, rows, operation_id="idempotent-primary")

        reaffirming = inject_write_failure(
            AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="jsonl", overwrite=False))
        )
        reaffirmed_plan = _prepare_effect(reaffirming, rows, operation_id="idempotent-reaffirm")

        assert reaffirmed_plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
        assert reaffirmed_plan.safe_evidence["publication_kind"] == "reaffirmed"
        assert reaffirmed_plan.expected_descriptor == primary_result.descriptor
        # Content on the blob is unchanged — no second upload was ever issued.
        assert _read_blob(azurite_blob_container, blob_path).decode("utf-8") == '{"id": 1, "name": "Ada"}'

        colliding = inject_write_failure(
            AzureBlobSink(_sink_config(azurite_blob_container, blob_path=blob_path, format_="jsonl", overwrite=False))
        )
        with pytest.raises(RemoteObjectCollisionError):
            _prepare_effect(colliding, [{"id": 2, "name": "Grace"}], operation_id="idempotent-collision")
        assert _read_blob(azurite_blob_container, blob_path).decode("utf-8") == '{"id": 1, "name": "Ada"}'
