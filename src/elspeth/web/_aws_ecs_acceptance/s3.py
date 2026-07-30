"""S3 plugin acceptance for the AWS ECS deployment lane."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from elspeth.contracts import ArtifactDescriptor
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectDescriptorMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
)
from elspeth.plugins.aws_s3_common import build_s3_client
from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink, S3ConditionalWriteRejectedError
from elspeth.plugins.sources.aws_s3_source import AWSS3Source

from .contracts import (
    _SHA256_PATTERN,
    FORBIDDEN_AWS_OVERRIDE_ENV,
    AcceptanceCheckError,
    _resolve_acceptance_s3_location,
    _sha256,
)

_S3_ACCEPTANCE_ROW: dict[str, object] = {"id": 1, "name": "elspeth-s3-acceptance"}
_S3_ACCEPTANCE_BYTES = b'{"id":1,"name":"elspeth-s3-acceptance"}\n'
_S3_COLLISION_PROBE_ROW: dict[str, object] = {"id": 1, "name": "elspeth-s3-acceptance-collision-probe"}
_S3_MAX_OBJECT_BYTES = 4096
_S3_MAX_RECORD_CHARS = 256


class _S3EffectFailure(Exception):
    """Carry only cleanup ownership across the static acceptance boundary."""

    def __init__(self, *, cleanup_owned: bool) -> None:
        super().__init__()
        self.cleanup_owned = cleanup_owned


class _S3AcceptanceContext:
    """Minimal ordinary plugin context with an in-memory safe audit projection."""

    run_id = "764dd764-c265-40d7-a907-390255dccb64"
    node_id = "verify-s3"
    operation_id = "verify-s3"
    contract = None
    landscape = None
    telemetry_emit = staticmethod(lambda _event: None)

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_call(self, *args: object, **kwargs: object) -> None:
        del args
        self.calls.append(dict(kwargs))

    def record_validation_error(self, *_args: object, **_kwargs: object) -> None:
        raise AcceptanceCheckError("s3_source_rows")


def _resolve_s3_acceptance_inputs(env: Mapping[str, str]) -> tuple[str, str, str]:
    if any(name in env for name in FORBIDDEN_AWS_OVERRIDE_ENV):
        raise AcceptanceCheckError("s3_aws_override")
    bucket, prefix, region = _resolve_acceptance_s3_location(env, check="s3_input")
    key = f"{prefix}/verify-s3.jsonl"
    if len(key.encode("utf-8")) > 1024:
        raise AcceptanceCheckError("s3_input")
    return bucket, key, region


def _s3_not_found(error: BaseException) -> bool:
    if not isinstance(error, ClientError):
        return False
    response = error.response
    error_payload = response.get("Error")
    code = error_payload.get("Code") if isinstance(error_payload, Mapping) else None
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _s3_source_hash(context: _S3AcceptanceContext) -> str | None:
    hashes: list[str] = []
    for call in context.calls:
        response = call.get("response_data")
        if not isinstance(response, Mapping):
            continue
        candidate = response.get("content_hash")
        if type(candidate) is str and _SHA256_PATTERN.fullmatch(candidate) is not None:
            hashes.append(candidate)
    return hashes[0] if len(hashes) == 1 else None


def _s3_acceptance_effect_id(*, bucket: str, key: str, region: str, content_hash: str) -> str:
    """Derive the stable identity used by both acceptance contenders."""
    encoded = json.dumps(
        {
            "bucket": bucket,
            "content_hash": content_hash,
            "key": key,
            "protocol": "sink-effect-v1",
            "region": region,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _drive_s3_acceptance_effect(
    sink: object,
    *,
    bucket: str,
    key: str,
    region: str,
    expected_hash: str,
    require_existing: bool,
    row_payload: Mapping[str, object] = _S3_ACCEPTANCE_ROW,
) -> tuple[ArtifactDescriptor, bool]:
    """Exercise only the effect protocol; never call legacy write/flush."""
    effect_id = _s3_acceptance_effect_id(bucket=bucket, key=key, region=region, content_hash=expected_hash)
    row = dict(row_payload)
    lineage_json = canonical_json([{"row_id": "verify-s3-row", "token_id": "verify-s3-token"}])
    member = SinkEffectMember(
        ordinal=0,
        token_id="verify-s3-token",
        row_id="verify-s3-row",
        ingest_sequence=0,
        lineage_json=lineage_json,
        lineage_hash=_sha256(lineage_json.encode("utf-8")),
        payload_hash=_sha256(canonical_json(row).encode("utf-8")),
        row=row,
    )
    effect_input = SinkEffectPipelineMembersInput(members=(member,), target_snapshot_members=(member,))
    context = RestrictedSinkEffectContext(
        run_id=_S3AcceptanceContext.run_id,
        run_started_at=datetime(2026, 7, 16, tzinfo=UTC),
        operation_id=f"verify-s3-{effect_id[:16]}",
        sink_node_id=_S3AcceptanceContext.node_id,
    )
    target = f"s3://{bucket}/{key}"
    inspection = sink.inspect_effect(  # type: ignore[attr-defined]
        SinkEffectInspectionRequest(effect_id=effect_id, target=target, predecessor_descriptor=None),
        context,
    )
    prepare_request = SinkEffectPrepareRequest(effect_id=effect_id, effect_input=effect_input, inspection=inspection)
    plan = sink.prepare_effect(prepare_request, context)  # type: ignore[attr-defined]
    prepare_request.validate_plan(plan)
    if plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION:
        # Mirror the real orchestrator's short-circuit (executors/sink_effects.py):
        # a virtual/inherited/reaffirmed plan never reaches lease, commit, or
        # reconcile. Driving reconcile_effect against it here would be a
        # harness-only deviation from production behavior — and for a
        # reaffirmed plan specifically, it would mis-observe the effect_id/
        # plan_hash-scoped exact-match branch as a fresh "still absent" or
        # "predecessor_unchanged" NOT_APPLIED result instead of the no-op it is.
        if plan.expected_descriptor is None:
            raise AcceptanceCheckError("s3_collision" if require_existing else "s3_sink_write")
        if not require_existing:
            # The primary lane must prove this invocation published the
            # object. A reaffirmed/pre-existing descriptor is not owned by
            # the harness and must never enable cleanup.
            raise _S3EffectFailure(cleanup_owned=False)
        return plan.expected_descriptor, True
    reconciliation = sink.reconcile_effect(plan, context)  # type: ignore[attr-defined]

    if reconciliation.kind is SinkEffectReconcileKind.UNKNOWN:
        raise AcceptanceCheckError("s3_collision" if require_existing else "s3_sink_write")
    if reconciliation.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR:
        descriptor = reconciliation.descriptor
        if descriptor is None:
            raise AcceptanceCheckError("s3_collision" if require_existing else "s3_sink_write")
        if not require_existing:
            raise _S3EffectFailure(cleanup_owned=False)
        return descriptor, True
    if require_existing:
        raise AcceptanceCheckError("s3_collision")

    try:
        result = sink.commit_effect(plan, context)  # type: ignore[attr-defined]
    except Exception:
        cleanup_owned = False
        try:
            post_commit = sink.reconcile_effect(plan, context)  # type: ignore[attr-defined]
        except Exception:
            pass
        else:
            cleanup_owned = (
                post_commit.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
                and post_commit.descriptor == plan.expected_descriptor
            )
        raise _S3EffectFailure(cleanup_owned=cleanup_owned) from None
    if tuple(result.accepted_ordinals) != (0,) or result.diverted_ordinals:
        raise _S3EffectFailure(cleanup_owned=True)
    return result.descriptor, False


def verify_s3(
    env: Mapping[str, str],
    *,
    sink_factory: Callable[[dict[str, Any]], Any] = AWSS3Sink,
    source_factory: Callable[[dict[str, Any]], Any] = AWSS3Source,
    s3_client_factory: Callable[[str | None, str | None], Any] = build_s3_client,
) -> dict[str, object]:
    """Exercise the shipped S3 plugins with the ECS task-role default chain."""

    bucket, key, region = _resolve_s3_acceptance_inputs(env)
    common_config: dict[str, Any] = {
        "bucket": bucket,
        "key": key,
        "format": "jsonl",
        "schema": {"mode": "fixed", "fields": ["id: int", "name: str"]},
        "region_name": region,
        "endpoint_url": None,
        "max_object_bytes": _S3_MAX_OBJECT_BYTES,
        "max_record_chars": _S3_MAX_RECORD_CHARS,
    }
    sink_config = {**common_config, "overwrite": False}
    source_config = {**common_config, "on_validation_failure": "discard"}
    expected_hash = _sha256(_S3_ACCEPTANCE_BYTES)
    divergent_hash = _sha256(canonical_json(_S3_COLLISION_PROBE_ROW).encode("utf-8"))
    primary_sink: Any | None = None
    source: Any | None = None
    collision_sink: Any | None = None
    probe_sink: Any | None = None
    failure_check: str | None = None
    resource_close_failed = False
    cleanup_failed = False
    cleanup_owned = False
    collision_rejected = False
    source_hash: str | None = None

    try:
        try:
            primary_sink = sink_factory(dict(sink_config))
            primary_descriptor, _ = _drive_s3_acceptance_effect(
                primary_sink,
                bucket=bucket,
                key=key,
                region=region,
                expected_hash=expected_hash,
                require_existing=False,
            )
            cleanup_owned = True
        except _S3EffectFailure as exc:
            cleanup_owned = exc.cleanup_owned
            failure_check = "s3_sink_write"
        except Exception:
            failure_check = "s3_sink_write"
        if failure_check is None:
            sink_hash = primary_descriptor.content_hash
            if sink_hash != expected_hash:
                failure_check = "s3_integrity"

        if failure_check is None:
            source_context = _S3AcceptanceContext()
            try:
                source = source_factory(dict(source_config))
                rows = list(source.load(source_context))
            except AcceptanceCheckError as exc:
                failure_check = exc.check
            except Exception:
                failure_check = "s3_source_read"
            else:
                source_hash = _s3_source_hash(source_context)
                materialized = [row.row for row in rows]
                if materialized != [_S3_ACCEPTANCE_ROW]:
                    failure_check = "s3_source_rows"
                elif source_hash != expected_hash or source_hash != sink_hash:
                    failure_check = "s3_integrity"

        if failure_check is None:
            try:
                collision_sink = sink_factory(dict(sink_config))
                collision_descriptor, reconciled = _drive_s3_acceptance_effect(
                    collision_sink,
                    bucket=bucket,
                    key=key,
                    region=region,
                    expected_hash=expected_hash,
                    require_existing=True,
                )
            except Exception:
                failure_check = "s3_collision"
            else:
                if not reconciled or collision_descriptor != primary_descriptor:
                    failure_check = "s3_collision"

        if failure_check is None:
            # A genuine collision probe: same key, different content, still
            # no overwrite/predecessor authority. This must be rejected at
            # prepare, not silently accepted — today's acceptance harness
            # never exercised a real collision without this probe.
            try:
                probe_sink = sink_factory(dict(sink_config))
                _drive_s3_acceptance_effect(
                    probe_sink,
                    bucket=bucket,
                    key=key,
                    region=region,
                    expected_hash=divergent_hash,
                    require_existing=True,
                    row_payload=_S3_COLLISION_PROBE_ROW,
                )
            except S3ConditionalWriteRejectedError:
                collision_rejected = True
            except Exception:
                failure_check = "s3_collision"
            else:
                failure_check = "s3_collision"
    finally:
        for resource in (source, collision_sink, probe_sink, primary_sink):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                resource_close_failed = True

        cleanup_client: Any | None = None
        if cleanup_owned:
            try:
                cleanup_client = s3_client_factory(region, None)
            except Exception:
                cleanup_failed = True
        if cleanup_client is not None:
            try:
                cleanup_client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                cleanup_failed = True
            try:
                cleanup_client.head_object(Bucket=bucket, Key=key)
            except Exception as exc:
                if not _s3_not_found(exc):
                    cleanup_failed = True
            else:
                cleanup_failed = True
            try:
                cleanup_client.close()
            except Exception:
                cleanup_failed = True

    if cleanup_failed:
        raise AcceptanceCheckError("s3_cleanup")
    if resource_close_failed:
        raise AcceptanceCheckError("s3_resource_close")
    if failure_check is not None:
        raise AcceptanceCheckError(failure_check)
    assert source_hash is not None
    return {
        "object_count": 1,
        "source_sha256": source_hash,
        "sink_sha256": expected_hash,
        "collision_rejected": collision_rejected,
        "cleanup_succeeded": True,
    }
