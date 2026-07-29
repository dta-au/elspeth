"""Contract tests for the AWS ECS S3 acceptance lane."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from elspeth.contracts import ArtifactDescriptor, CallType
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectPlan,
    SinkEffectReconcileResult,
)
from elspeth.plugins.sinks.aws_s3_sink import S3ConditionalWriteRejectedError
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import s3
from elspeth.web._aws_ecs_acceptance.contracts import FORBIDDEN_AWS_OVERRIDE_ENV, AcceptanceCheckError


def test_facade_reexports_s3_owner_by_identity() -> None:
    assert acceptance.verify_s3 is s3.verify_s3


def test_s3_not_found_rejects_exception_objects_that_only_pretend_to_be_client_errors() -> None:
    class Pretender(RuntimeError):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    assert s3._s3_not_found(Pretender()) is False


_S3_PREFIX = "plan10/764dd764-c265-40d7-a907-390255dccb64"
_S3_HASH = "1ee9a4c7f487fc1b2413aea5272537ff1e5985dd14344eb268b69e83da7245a7"


def _s3_env(**updates: str) -> dict[str, str]:
    values = {
        "ELSPETH_ACCEPTANCE_S3_BUCKET": "acceptance-bucket",
        "ELSPETH_ACCEPTANCE_S3_PREFIX": _S3_PREFIX,
        "AWS_REGION": "ap-southeast-2",
    }
    values.update(updates)
    return values


class _S3NotFound(ClientError):
    def __init__(self) -> None:
        super().__init__(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )


class _S3CleanupClient:
    def __init__(self, events: list[str], *, delete_error: BaseException | None = None) -> None:
        self.events = events
        self.delete_error = delete_error

    def delete_object(self, **_kwargs: object) -> None:
        self.events.append("delete")
        if self.delete_error is not None:
            raise self.delete_error

    def head_object(self, **_kwargs: object) -> None:
        self.events.append("head")
        raise _S3NotFound

    def close(self) -> None:
        self.events.append("cleanup-close")


class _EffectS3SinkBase:
    effect_call_type = CallType.HTTP
    """Protocol-faithful shared target used by the acceptance controller tests."""

    def __init__(
        self,
        *,
        index: int,
        events: list[str],
        target: dict[str, ArtifactDescriptor],
        failure: str | None = None,
    ) -> None:
        self.index = index
        self.events = events
        self.target = target
        self.failure = failure

    def inspect_effect(self, request: object, _ctx: object) -> SinkEffectInspection:
        self.events.append(f"sink-{self.index}-inspect")
        if self.failure == "sink" and self.index == 1:
            raise RuntimeError("raw credential provider URL ARN sentinel")
        return SinkEffectInspection(
            mode=SinkEffectInspectionMode.INSPECTED,
            reference=f"inspection:{self.index}",
            evidence={"exists": bool(self.target)},
        )

    def prepare_effect(self, request: object, _ctx: object) -> SinkEffectPlan:
        self.events.append(f"sink-{self.index}-prepare")
        if self.index == 3:
            # The third drive is the genuine-collision probe (different
            # content, same key): the real sink rejects it at prepare.
            raise S3ConditionalWriteRejectedError from None
        effect_id = request.effect_id  # type: ignore[attr-defined]
        target = f"s3://acceptance-bucket/{_S3_PREFIX}/verify-s3.jsonl"
        digest = "b" * 64 if self.failure == "integrity" and self.index == 1 else _S3_HASH
        descriptor = ArtifactDescriptor(
            artifact_type="file",
            path_or_uri=target,
            content_hash=digest,
            size_bytes=len(s3._S3_ACCEPTANCE_BYTES),
        )
        return SinkEffectPlan(
            effect_id=effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            descriptor_mode=SinkEffectDescriptorMode.PRECOMPUTED,
            inspection_mode=SinkEffectInspectionMode.INSPECTED,
            target=target,
            plan_hash="a" * 64,
            payload_hash=digest,
            expected_descriptor=descriptor,
            safe_evidence={"test": True},
        )

    def reconcile_effect(self, plan: SinkEffectPlan, _ctx: object) -> SinkEffectReconcileResult:
        self.events.append(f"sink-{self.index}-reconcile")
        if self.index == 2 and self.failure == "collision":
            return SinkEffectReconcileResult.not_applied(evidence={"missing": True})
        existing = self.target.get("descriptor")
        if existing is None:
            return SinkEffectReconcileResult.not_applied(evidence={"missing": True})
        return SinkEffectReconcileResult.applied(existing, evidence={"matched": True})

    def commit_effect(self, plan: SinkEffectPlan, _ctx: object) -> SinkEffectCommitResult:
        self.events.append(f"sink-{self.index}-commit")
        assert plan.expected_descriptor is not None
        self.target["descriptor"] = plan.expected_descriptor
        return SinkEffectCommitResult(
            descriptor=plan.expected_descriptor,
            evidence={"committed": True},
            accepted_ordinals=(0,),
            diverted_ordinals=(),
        )


def test_verify_s3_round_trips_with_bounded_default_chain_plugin_configs_and_cleans_up() -> None:
    events: list[str] = []
    sink_configs: list[dict[str, object]] = []
    source_configs: list[dict[str, object]] = []
    target: dict[str, ArtifactDescriptor] = {}

    class Sink(_EffectS3SinkBase):
        def __init__(self, index: int) -> None:
            super().__init__(index=index, events=events, target=target)

        def close(self) -> None:
            events.append(f"sink-{self.index}-close")

    def sink_factory(config: dict[str, object]) -> Sink:
        sink_configs.append(config)
        return Sink(len(sink_configs))

    class Source:
        def load(self, ctx: object) -> list[object]:
            events.append("source-load")
            ctx.record_call(  # type: ignore[attr-defined]
                call_type="http",
                status="success",
                request_data={},
                response_data={"size_bytes": 42, "content_hash": _S3_HASH},
            )
            return [SimpleNamespace(row={"id": 1, "name": "elspeth-s3-acceptance"})]

        def close(self) -> None:
            events.append("source-close")

    def source_factory(config: dict[str, object]) -> Source:
        source_configs.append(config)
        return Source()

    receipt = s3.verify_s3(
        _s3_env(),
        sink_factory=sink_factory,
        source_factory=source_factory,
        s3_client_factory=lambda region, endpoint: events.append(f"cleanup-client:{region}:{endpoint}") or _S3CleanupClient(events),
    )

    expected_common = {
        "bucket": "acceptance-bucket",
        "key": f"{_S3_PREFIX}/verify-s3.jsonl",
        "format": "jsonl",
        "schema": {"mode": "fixed", "fields": ["id: int", "name: str"]},
        "region_name": "ap-southeast-2",
        "endpoint_url": None,
        "max_object_bytes": 4096,
        "max_record_chars": 256,
    }
    assert sink_configs == [
        {**expected_common, "overwrite": False},
        {**expected_common, "overwrite": False},
        {**expected_common, "overwrite": False},
    ]
    assert source_configs == [{**expected_common, "on_validation_failure": "discard"}]
    assert receipt == {
        "object_count": 1,
        "source_sha256": _S3_HASH,
        "sink_sha256": _S3_HASH,
        "collision_rejected": True,
        "cleanup_succeeded": True,
    }
    assert events == [
        "sink-1-inspect",
        "sink-1-prepare",
        "sink-1-reconcile",
        "sink-1-commit",
        "source-load",
        "sink-2-inspect",
        "sink-2-prepare",
        "sink-2-reconcile",
        "sink-3-inspect",
        "sink-3-prepare",
        "source-close",
        "sink-2-close",
        "sink-3-close",
        "sink-1-close",
        "cleanup-client:ap-southeast-2:None",
        "delete",
        "head",
        "cleanup-close",
    ]


@pytest.mark.parametrize(
    "updates",
    [
        {"ELSPETH_ACCEPTANCE_S3_BUCKET": ""},
        {"ELSPETH_ACCEPTANCE_S3_PREFIX": "plan10/not-a-uuid"},
        {"ELSPETH_ACCEPTANCE_S3_PREFIX": "/plan10/764dd764-c265-40d7-a907-390255dccb64"},
        {"AWS_REGION": ""},
        {"AWS_DEFAULT_REGION": "us-east-1"},
    ],
)
def test_verify_s3_requires_closed_bucket_uuid_prefix_and_unambiguous_region(updates: dict[str, str]) -> None:
    with pytest.raises(AcceptanceCheckError, match="s3_input"):
        s3.verify_s3(_s3_env(**updates), sink_factory=pytest.fail)


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_AWS_OVERRIDE_ENV))
def test_verify_s3_rejects_credential_endpoint_profile_and_role_overrides_by_presence(forbidden: str) -> None:
    raw_sentinel = "raw-secret-provider-url-arn-sentinel"

    with pytest.raises(AcceptanceCheckError, match="s3_aws_override") as raised:
        s3.verify_s3(_s3_env(**{forbidden: raw_sentinel}), sink_factory=pytest.fail)

    assert raw_sentinel not in str(raised.value)


def test_verify_s3_does_not_delete_when_primary_effect_fails_before_ownership() -> None:
    events: list[str] = []
    target: dict[str, ArtifactDescriptor] = {}

    class Sink(_EffectS3SinkBase):
        def __init__(self) -> None:
            super().__init__(index=1, events=events, target=target, failure="sink")

        def close(self) -> None:
            events.append("sink-close")

    with pytest.raises(AcceptanceCheckError, match="s3_sink_write") as raised:
        s3.verify_s3(
            _s3_env(),
            sink_factory=lambda _config: Sink(),
            s3_client_factory=lambda _region, _endpoint: pytest.fail("unowned key must not be deleted"),
        )

    assert events == ["sink-1-inspect", "sink-close"]
    assert "sentinel" not in str(raised.value)


@pytest.mark.parametrize("post_commit_observation", ["exact", "mismatched-exact", "unknown"])
def test_verify_s3_deletes_after_ambiguous_commit_only_with_exact_reconciliation(
    post_commit_observation: str,
) -> None:
    events: list[str] = []
    target: dict[str, ArtifactDescriptor] = {}

    class Sink(_EffectS3SinkBase):
        reconcile_calls = 0

        def __init__(self) -> None:
            super().__init__(index=1, events=events, target=target)

        def reconcile_effect(self, plan: SinkEffectPlan, _ctx: object) -> SinkEffectReconcileResult:
            self.reconcile_calls += 1
            events.append(f"sink-1-reconcile-{self.reconcile_calls}")
            if self.reconcile_calls == 1:
                return SinkEffectReconcileResult.not_applied(evidence={"missing": True})
            if post_commit_observation == "exact":
                assert plan.expected_descriptor is not None
                return SinkEffectReconcileResult.applied(plan.expected_descriptor, evidence={"matched": True})
            if post_commit_observation == "mismatched-exact":
                assert plan.expected_descriptor is not None
                return SinkEffectReconcileResult.applied(
                    ArtifactDescriptor(
                        artifact_type=plan.expected_descriptor.artifact_type,
                        path_or_uri=plan.expected_descriptor.path_or_uri,
                        content_hash="f" * 64,
                        size_bytes=plan.expected_descriptor.size_bytes,
                    ),
                    evidence={"matched": False},
                )
            return SinkEffectReconcileResult.unknown(evidence={"ambiguous": True})

        def commit_effect(self, _plan: SinkEffectPlan, _ctx: object) -> SinkEffectCommitResult:
            events.append("sink-1-commit")
            raise RuntimeError("raw ambiguous commit sentinel")

        def close(self) -> None:
            events.append("sink-close")

    def cleanup_factory(_region: str | None, _endpoint: str | None) -> _S3CleanupClient:
        if post_commit_observation == "exact":
            return _S3CleanupClient(events)
        pytest.fail("unproven key must not be deleted")

    with pytest.raises(AcceptanceCheckError, match="s3_sink_write") as raised:
        s3.verify_s3(
            _s3_env(),
            sink_factory=lambda _config: Sink(),
            s3_client_factory=cleanup_factory,
        )

    assert ("delete" in events) is (post_commit_observation == "exact")
    assert "sentinel" not in str(raised.value)


@pytest.mark.parametrize("failure", ["sink", "source", "integrity", "rows", "collision"])
def test_verify_s3_provider_and_integrity_failures_are_static_and_still_delete(failure: str) -> None:
    events: list[str] = []
    sink_count = 0
    target: dict[str, ArtifactDescriptor] = {}

    class Sink(_EffectS3SinkBase):
        def __init__(self, index: int) -> None:
            super().__init__(index=index, events=events, target=target, failure=failure)

        def close(self) -> None:
            events.append(f"sink-{self.index}-close")

    def sink_factory(_config: dict[str, object]) -> Sink:
        nonlocal sink_count
        sink_count += 1
        return Sink(sink_count)

    class Source:
        def load(self, ctx: object) -> list[object]:
            if failure == "source":
                raise RuntimeError("raw provider response request-id sentinel")
            ctx.record_call(  # type: ignore[attr-defined]
                call_type="http",
                status="success",
                request_data={},
                response_data={"content_hash": _S3_HASH},
            )
            rows = [] if failure == "rows" else [SimpleNamespace(row={"id": 1, "name": "elspeth-s3-acceptance"})]
            return rows

        def close(self) -> None:
            events.append("source-close")

    expected = {
        "sink": "s3_sink_write",
        "source": "s3_source_read",
        "integrity": "s3_integrity",
        "rows": "s3_source_rows",
        "collision": "s3_collision",
    }[failure]
    with pytest.raises(AcceptanceCheckError, match=expected) as raised:
        s3.verify_s3(
            _s3_env(),
            sink_factory=sink_factory,
            source_factory=lambda _config: Source(),
            s3_client_factory=lambda _region, _endpoint: _S3CleanupClient(events),
        )

    assert "sentinel" not in str(raised.value)
    assert ("delete" in events) is (failure != "sink")
    assert ("cleanup-close" in events) is (failure != "sink")


def test_verify_s3_cleanup_continues_after_resource_close_failure_and_fails_closed() -> None:
    events: list[str] = []
    target: dict[str, ArtifactDescriptor] = {}
    sink_count = 0

    class Sink(_EffectS3SinkBase):
        def __init__(self, index: int) -> None:
            super().__init__(index=index, events=events, target=target)

        def close(self) -> None:
            events.append("sink-close")
            raise RuntimeError("raw close sentinel")

    class Source:
        def load(self, ctx: object) -> list[object]:
            ctx.record_call(  # type: ignore[attr-defined]
                call_type="http",
                status="success",
                request_data={},
                response_data={"content_hash": _S3_HASH},
            )
            return [SimpleNamespace(row={"id": 1, "name": "elspeth-s3-acceptance"})]

        def close(self) -> None:
            events.append("source-close")

    def sink_factory(_config: dict[str, object]) -> Sink:
        nonlocal sink_count
        sink_count += 1
        return Sink(sink_count)

    with pytest.raises(AcceptanceCheckError, match="s3_resource_close") as raised:
        s3.verify_s3(
            _s3_env(),
            sink_factory=sink_factory,
            source_factory=lambda _config: Source(),
            s3_client_factory=lambda _region, _endpoint: _S3CleanupClient(events),
        )

    assert events.count("sink-close") == 3
    assert "source-close" in events
    assert "delete" in events
    assert "head" in events
    assert "raw close sentinel" not in str(raised.value)


def test_verify_s3_cleanup_failure_is_no_go_and_redacted() -> None:
    events: list[str] = []
    target: dict[str, ArtifactDescriptor] = {}
    sink_count = 0

    class Sink(_EffectS3SinkBase):
        def __init__(self, index: int) -> None:
            super().__init__(index=index, events=events, target=target)

        def close(self) -> None:
            pass

    class Source:
        def load(self, ctx: object) -> list[object]:
            ctx.record_call(  # type: ignore[attr-defined]
                call_type="http",
                status="success",
                request_data={},
                response_data={"content_hash": _S3_HASH},
            )
            return [SimpleNamespace(row={"id": 1, "name": "elspeth-s3-acceptance"})]

        def close(self) -> None:
            pass

    def sink_factory(_config: dict[str, object]) -> Sink:
        nonlocal sink_count
        sink_count += 1
        return Sink(sink_count)

    with pytest.raises(AcceptanceCheckError, match="s3_cleanup") as raised:
        s3.verify_s3(
            _s3_env(),
            sink_factory=sink_factory,
            source_factory=lambda _config: Source(),
            s3_client_factory=lambda _region, _endpoint: _S3CleanupClient(
                events, delete_error=RuntimeError("raw bucket provider response sentinel")
            ),
        )

    assert "raw bucket" not in str(raised.value)
    assert "cleanup-close" in events
