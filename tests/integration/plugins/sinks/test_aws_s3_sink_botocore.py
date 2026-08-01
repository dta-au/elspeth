"""Offline real-botocore proof for conditional S3 effect requests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from hashlib import sha256

import boto3
import pytest
from botocore.stub import ANY, Stubber

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectDescriptorMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink, S3ConditionalWriteRejectedError

_CTX = RestrictedSinkEffectContext(
    run_id="run-botocore",
    run_started_at=datetime(2026, 7, 16, tzinfo=UTC),
    operation_id="operation-botocore",
    sink_node_id="sink-botocore",
)


def _member(ordinal: int, identity: int, row: dict[str, object]) -> SinkEffectMember:
    row_bytes = canonical_json(row).encode()
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-{identity}",
        row_id=f"row-{identity}",
        ingest_sequence=identity,
        lineage_json="[]",
        lineage_hash=sha256(b"[]").hexdigest(),
        payload_hash=sha256(row_bytes).hexdigest(),
        row=row,
        member_effect_id=sha256(f"member-{identity}".encode()).hexdigest(),
    )


def _prepare(
    sink: AWSS3Sink,
    *,
    effect_id: str,
    current: tuple[SinkEffectMember, ...],
    snapshot: tuple[SinkEffectMember, ...],
    predecessor=None,
):
    inspection = sink.inspect_effect(
        SinkEffectInspectionRequest(
            effect_id=effect_id,
            target="{}",
            predecessor_descriptor=predecessor,
        ),
        _CTX,
    )
    return sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            effect_input=SinkEffectPipelineMembersInput(members=current, target_snapshot_members=snapshot),
            inspection=inspection,
        ),
        _CTX,
    )


def test_real_botocore_stubber_emits_conditional_create_then_etag_successor() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    sink_config = {
        "bucket": "example-bucket",
        "key": "output.json",
        "format": "json",
        "overwrite": True,
        "schema": {"mode": "observed"},
    }
    first = _member(0, 1, {"id": 1})
    second_current = _member(0, 2, {"id": 2})
    second_snapshot = _member(1, 2, {"id": 2})

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object",
            service_error_code="NoSuchKey",
            service_message="missing",
            http_status_code=404,
            expected_params={"Bucket": "example-bucket", "Key": "output.json", "ChecksumMode": "ENABLED"},
        )
        first_sink = AWSS3Sink(sink_config)
        first_sink._s3_client = client
        first_plan = _prepare(
            first_sink,
            effect_id="a" * 64,
            current=(first,),
            snapshot=(first,),
        )
        stubber.add_response(
            "put_object",
            {"ETag": '"etag-1"'},
            {
                "Bucket": "example-bucket",
                "Key": "output.json",
                "Body": ANY,
                "ContentLength": first_plan.expected_descriptor.size_bytes,
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(first_plan.payload_hash)).decode("ascii"),
                "IfNoneMatch": "*",
                "Metadata": {
                    "elspeth-content-sha256": first_plan.payload_hash,
                    "elspeth-effect-id": first_plan.effect_id,
                    "elspeth-plan-hash": first_plan.plan_hash,
                    "elspeth-protocol-version": "sink-effect-v1",
                },
            },
        )
        first_result = first_sink.commit_effect(first_plan, _CTX)

        stubber.add_response(
            "head_object",
            {
                "ContentLength": first_plan.expected_descriptor.size_bytes,
                "ETag": '"etag-1"',
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(first_plan.payload_hash)).decode("ascii"),
                "Metadata": {
                    "elspeth-content-sha256": first_plan.payload_hash,
                    "elspeth-effect-id": first_plan.effect_id,
                    "elspeth-plan-hash": first_plan.plan_hash,
                    "elspeth-protocol-version": "sink-effect-v1",
                },
            },
            {"Bucket": "example-bucket", "Key": "output.json", "ChecksumMode": "ENABLED"},
        )
        second_sink = AWSS3Sink(sink_config)
        second_sink._s3_client = client
        second_plan = _prepare(
            second_sink,
            effect_id="b" * 64,
            current=(second_current,),
            snapshot=(first, second_snapshot),
            predecessor=first_result.descriptor,
        )
        stubber.add_response(
            "put_object",
            {"ETag": '"etag-2"'},
            {
                "Bucket": "example-bucket",
                "Key": "output.json",
                "Body": ANY,
                "ContentLength": second_plan.expected_descriptor.size_bytes,
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(second_plan.payload_hash)).decode("ascii"),
                "IfMatch": '"etag-1"',
                "Metadata": {
                    "elspeth-content-sha256": second_plan.payload_hash,
                    "elspeth-effect-id": second_plan.effect_id,
                    "elspeth-plan-hash": second_plan.plan_hash,
                    "elspeth-protocol-version": "sink-effect-v1",
                },
            },
        )
        second_sink.commit_effect(second_plan, _CTX)

        stubber.assert_no_pending_responses()


def test_real_botocore_stubber_reaffirms_identical_content_and_rejects_true_collision() -> None:
    """elspeth-9a78b3a02f: overwrite=False must no-op on an idempotent
    re-drive of identical content, and still reject a genuine collision —
    proven against real botocore head_object/PutObject response shapes."""
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    sink_config = {
        "bucket": "example-bucket",
        "key": "output.json",
        "format": "json",
        "overwrite": False,
        "schema": {"mode": "observed"},
    }
    member = _member(0, 1, {"id": 1})

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object",
            service_error_code="NoSuchKey",
            service_message="missing",
            http_status_code=404,
            expected_params={"Bucket": "example-bucket", "Key": "output.json", "ChecksumMode": "ENABLED"},
        )
        primary_sink = AWSS3Sink(sink_config)
        primary_sink._s3_client = client
        primary_plan = _prepare(primary_sink, effect_id="c" * 64, current=(member,), snapshot=(member,))
        stubber.add_response(
            "put_object",
            {"ETag": '"etag-1"'},
            {
                "Bucket": "example-bucket",
                "Key": "output.json",
                "Body": ANY,
                "ContentLength": primary_plan.expected_descriptor.size_bytes,
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(primary_plan.payload_hash)).decode("ascii"),
                "IfNoneMatch": "*",
                "Metadata": {
                    "elspeth-content-sha256": primary_plan.payload_hash,
                    "elspeth-effect-id": primary_plan.effect_id,
                    "elspeth-plan-hash": primary_plan.plan_hash,
                    "elspeth-protocol-version": "sink-effect-v1",
                },
            },
        )
        primary_sink.commit_effect(primary_plan, _CTX)

        existing_head_response = {
            "ContentLength": primary_plan.expected_descriptor.size_bytes,
            "ETag": '"etag-1"',
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(primary_plan.payload_hash)).decode("ascii"),
            "Metadata": {
                "elspeth-content-sha256": primary_plan.payload_hash,
                "elspeth-effect-id": primary_plan.effect_id,
                "elspeth-plan-hash": primary_plan.plan_hash,
                "elspeth-protocol-version": "sink-effect-v1",
            },
        }
        expected_head_params = {"Bucket": "example-bucket", "Key": "output.json", "ChecksumMode": "ENABLED"}

        # Second, independent sink instance re-drives the SAME content with
        # no declared predecessor: reaffirmed no-op, no put_object issued —
        # if the fix regressed, the Stubber would raise on an unexpected
        # put_object call instead of the assertions below ever running.
        stubber.add_response("head_object", existing_head_response, expected_head_params)
        reaffirming_sink = AWSS3Sink(sink_config)
        reaffirming_sink._s3_client = client
        reaffirming_member = _member(0, 2, {"id": 1})
        reaffirmed_plan = _prepare(
            reaffirming_sink,
            effect_id="d" * 64,
            current=(reaffirming_member,),
            snapshot=(reaffirming_member,),
        )
        assert reaffirmed_plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
        assert reaffirmed_plan.safe_evidence["publication_kind"] == "reaffirmed"
        assert reaffirmed_plan.expected_descriptor == primary_plan.expected_descriptor

        # Third, independent sink instance re-drives DIFFERENT content
        # against the same key with no overwrite/predecessor authority:
        # a genuine collision, rejected at prepare with no put_object.
        stubber.add_response("head_object", existing_head_response, expected_head_params)
        colliding_sink = AWSS3Sink(sink_config)
        colliding_sink._s3_client = client
        colliding_member = _member(0, 3, {"id": 999})
        inspection = colliding_sink.inspect_effect(
            SinkEffectInspectionRequest(effect_id="e" * 64, target="{}", predecessor_descriptor=None),
            _CTX,
        )
        with pytest.raises(S3ConditionalWriteRejectedError):
            colliding_sink.prepare_effect(
                SinkEffectPrepareRequest(
                    effect_id="e" * 64,
                    effect_input=SinkEffectPipelineMembersInput(members=(colliding_member,), target_snapshot_members=(colliding_member,)),
                    inspection=inspection,
                ),
                _CTX,
            )

        stubber.assert_no_pending_responses()
