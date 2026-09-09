"""Explicit real-AWS acceptance for conditional S3 sink writes.

Drives the recoverable sink-effect protocol directly (inspect/prepare/
commit) — AWSS3Sink.write() now raises unconditionally
("publication requires the recoverable sink effect coordinator"), so a
real acceptance test must exercise inspect_effect/prepare_effect/
commit_effect the same way the orchestrator and the AWS ECS acceptance
harness do.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectDescriptorMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.aws_s3_common import build_s3_client
from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink, S3ConditionalWriteRejectedError
from tests.fixtures.base_classes import inject_write_failure

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.live_aws, pytest.mark.live_provider]

_CTX = RestrictedSinkEffectContext(
    run_id="live-run",
    run_started_at=datetime(2026, 7, 16, tzinfo=UTC),
    operation_id="live-operation",
    sink_node_id="live-sink",
)


def _member(operation_id: str, row: dict[str, object]) -> SinkEffectMember:
    row_bytes = canonical_json(row).encode()
    return SinkEffectMember(
        ordinal=0,
        token_id=f"{operation_id}-token",
        row_id=f"{operation_id}-row",
        ingest_sequence=0,
        lineage_json="[]",
        lineage_hash=hashlib.sha256(b"[]").hexdigest(),
        payload_hash=hashlib.sha256(row_bytes).hexdigest(),
        row=row,
        member_effect_id=hashlib.sha256(f"{operation_id}-member".encode()).hexdigest(),
    )


def _prepare_effect(
    sink: AWSS3Sink,
    row: dict[str, object],
    *,
    effect_id: str,
    predecessor: ArtifactDescriptor | None = None,
) -> Any:
    member = _member(effect_id, row)
    inspection = sink.inspect_effect(
        SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=predecessor),
        _CTX,
    )
    return sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            effect_input=SinkEffectPipelineMembersInput(
                members=(member,), target_snapshot_members=(member,), target_delivered_member_count=1
            ),
            inspection=inspection,
        ),
        _CTX,
    )


@pytest.mark.parametrize("format", ["csv", "json", "jsonl"])
def test_real_s3_conditional_write_idempotent_reaffirm_and_stale_etag_non_clobber(format: str) -> None:
    """elspeth-9a78b3a02f against real AWS S3: overwrite=False must
    recognize an idempotent re-drive of identical content as a no-op
    rather than a hard collision, while a genuine collision — differing
    content, or content written by a foreign writer with no elspeth
    identity evidence — is still rejected. A declared predecessor still
    authorizes a real conditional replace."""
    bucket = os.environ.get("ELSPETH_TEST_S3_BUCKET")
    if not bucket:
        pytest.fail("ELSPETH_TEST_S3_BUCKET is required for the real AWS S3 acceptance", pytrace=False)
    key = f"elspeth-plan07/{uuid.uuid4()}/output.{format}"
    config = {
        "bucket": bucket,
        "key": key,
        "format": format,
        "overwrite": False,
        "schema": {"mode": "observed"},
    }
    client = build_s3_client(None, None)
    primary = inject_write_failure(AWSS3Sink(config))
    reaffirming = inject_write_failure(AWSS3Sink(config))
    colliding = inject_write_failure(AWSS3Sink(config))
    successor = inject_write_failure(AWSS3Sink(config))
    foreign_writer = inject_write_failure(AWSS3Sink(config))
    row = {"id": 1, "name": "Ada"}
    try:
        primary_plan = _prepare_effect(primary, row, effect_id="a" * 64)
        primary_result = primary.commit_effect(primary_plan, _CTX)
        first_remote = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert primary_result.descriptor.content_hash == hashlib.sha256(first_remote).hexdigest()

        # Independent, identical-content re-drive: idempotent no-op, never
        # touches the remote object (the bug this issue reports).
        reaffirmed_plan = _prepare_effect(reaffirming, row, effect_id="b" * 64)
        assert reaffirmed_plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
        assert reaffirmed_plan.safe_evidence["publication_kind"] == "reaffirmed"
        assert reaffirmed_plan.expected_descriptor == primary_result.descriptor
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == first_remote

        # Genuine collision: different content, no predecessor, no overwrite.
        with pytest.raises(S3ConditionalWriteRejectedError):
            _prepare_effect(colliding, {"id": 9, "name": "collision"}, effect_id="c" * 64)
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == first_remote

        # A declared predecessor still authorizes a real conditional replace.
        successor_plan = _prepare_effect(
            successor,
            {"id": 2, "name": "Grace"},
            effect_id="d" * 64,
            predecessor=primary_result.descriptor,
        )
        successor_result = successor.commit_effect(successor_plan, _CTX)
        second_remote = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert successor_result.descriptor.content_hash == hashlib.sha256(second_remote).hexdigest()
        assert successor_result.descriptor.content_hash != primary_result.descriptor.content_hash

        # An external writer without elspeth identity evidence is
        # unverifiable: never credited as a safe reaffirm, never clobbered.
        external = b"external-writer-sentinel"
        client.put_object(Bucket=bucket, Key=key, Body=external)
        with pytest.raises(S3ConditionalWriteRejectedError):
            _prepare_effect(foreign_writer, {"id": 3, "name": "stale"}, effect_id="e" * 64)
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == external
    finally:
        primary.close()
        reaffirming.close()
        colliding.close()
        successor.close()
        foreign_writer.close()
        client.delete_object(Bucket=bucket, Key=key)
        client.close()
