"""Power Automate member publication uses exact remote outcome evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.security import SecretFingerprintError
from elspeth.contracts.sink_effect_http import SinkEffectHTTPPost, SinkEffectHTTPPostRequest, SinkEffectHTTPPostResponse
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
)
from elspeth.plugins.infrastructure.power_automate import PROTOCOL, PowerAutomateProtocolError, selected_data_hash
from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateOptions, ArchivedPowerAutomateSinkConfig
from elspeth.plugins.sinks.power_automate import PowerAutomateSink


def _config() -> dict[str, Any]:
    return {
        "auth": {"method": "managed_identity", "client_id": "test-client"},
        "trigger_url": "https://flows.example.org/workflows/abc/triggers/manual/paths/invoke?api-version=1",
        "allowed_origin": "https://flows.example.org",
        "fields": ["record_id", "result"],
        "schema": {"mode": "flexible", "fields": ["record_id: str", "result: str"]},
    }


def _member(ordinal: int, *, identity: str = "group") -> SinkEffectMember:
    row = {"record_id": "same", "result": "approved", "unused": "private"}
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-{ordinal}",
        row_id=f"row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json="[]",
        lineage_hash=stable_hash([]),
        payload_hash=stable_hash(row),
        row=row,
        member_effect_id=stable_hash({"identity": identity, "ordinal": ordinal}),
    )


class _Flow(SinkEffectHTTPPost):
    def __init__(self, state: str = "not_applied") -> None:
        self.state = state
        self.requests: list[dict[str, object]] = []
        self.status_code = 200
        self.content_type: str | None = "application/json; charset=utf-8"
        self.extra: dict[str, object] = {}
        self.drop_write_response = False

    def post_json(self, request: SinkEffectHTTPPostRequest) -> SinkEffectHTTPPostResponse:
        body = deep_thaw(request.json_body)
        assert isinstance(body, dict)
        self.requests.append(body)
        if body["operation"] == "write" and self.state == "not_applied":
            self.state = "applied"
        if body["operation"] == "write" and self.drop_write_response:
            self.drop_write_response = False
            raise RuntimeError("transport_failed")
        result: dict[str, object] = {
            "protocol": PROTOCOL,
            "delivery_id": body["delivery_id"],
            "payload_sha256": body["payload_sha256"],
            "state": self.state,
        }
        if self.state == "applied":
            result.update(receipt_id="receipt-original", flow_run_id="flow-original")
        if self.state == "rejected":
            result["reason_code"] = "validation_failed"
        result.update(self.extra)
        return SinkEffectHTTPPostResponse(
            status_code=self.status_code,
            content_type=self.content_type,
            body=json.dumps(result).encode(),
            call_id=f"call-{len(self.requests)}",
            request_ref="a" * 64,
            response_ref="b" * 64,
        )


def _prepared(count: int = 2) -> tuple[PowerAutomateSink, SinkEffectPipelineMembersInput, SinkEffectPlan, RestrictedSinkEffectContext]:
    sink = PowerAutomateSink(_config())
    sink._on_write_failure = "discard"
    members = tuple(_member(index) for index in range(count))
    effect_input = SinkEffectPipelineMembersInput(members, members, count)
    ctx = RestrictedSinkEffectContext("run", datetime(2026, 10, 1, tzinfo=UTC), "operation", "sink")
    inspection = sink.inspect_effect(SinkEffectInspectionRequest("a" * 64, "safe-target", None), ctx)
    plan = sink.prepare_effect(SinkEffectPrepareRequest("a" * 64, effect_input, inspection), ctx)
    return sink, effect_input, plan, ctx


def test_status_precedes_initial_write_and_uses_only_selected_data() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow()
    ctx = replace(ctx, http_post=flow)
    member = effect_input.members[0]
    assert sink.reconcile_member_effect(plan, member, effect_input, ctx).kind is SinkEffectReconcileKind.NOT_APPLIED
    result = sink.commit_member_effect(plan, member, effect_input, ctx)
    assert [request["operation"] for request in flow.requests] == ["status", "write"]
    assert flow.requests[1]["data"] == {"record_id": "same", "result": "approved"}
    assert flow.requests[1]["payload_sha256"] == selected_data_hash(flow.requests[1]["data"])
    assert result.descriptor == plan.expected_descriptor
    assert result.accepted_ordinals == (0, 1)
    assert result.diverted_ordinals == ()
    assert result.evidence["receipt_id"] == "receipt-original"
    assert result.evidence["call_id"] == "call-2"


def test_identical_rows_have_distinct_engine_delivery_ids() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("applied")
    ctx = replace(ctx, http_post=flow)
    for member in effect_input.members:
        sink.commit_member_effect(plan, member, effect_input, ctx)
    assert flow.requests[0]["delivery_id"] != flow.requests[1]["delivery_id"]
    assert flow.requests[0]["payload_sha256"] == flow.requests[1]["payload_sha256"]


def test_plan_binds_members_fields_payloads_and_safe_target() -> None:
    sink, effect_input, plan, ctx = _prepared()
    assert plan.target.startswith("power-automate://flows.example.org/binding/")
    assert "workflows" not in plan.target
    assert plan.expected_descriptor is not None
    assert plan.expected_descriptor.metadata["row_count"] == 2
    reordered = replace(effect_input, members=(_member(0, identity="changed"), effect_input.members[1]))
    with pytest.raises(ValueError, match="plan_divergent"):
        sink.commit_member_effect(plan, reordered.members[0], reordered, replace(ctx, http_post=_Flow()))
    sink.config["timeout_seconds"] = 1
    with pytest.raises(ValueError, match="configuration_changed"):
        sink.configure_for_resume()


@pytest.mark.parametrize("extra", [{"delivery_id": "d" * 64}, {"payload_sha256": "f" * 64}, {"receipt_id": ""}, {"surplus": 1}])
def test_applied_requires_exact_receipt_and_identity(extra: dict[str, object]) -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("applied")
    flow.extra = extra
    with pytest.raises(PowerAutomateProtocolError):
        sink.commit_member_effect(plan, effect_input.members[0], effect_input, replace(ctx, http_post=flow))
    assert sink._get_diversions() == ()


@pytest.mark.parametrize("status", [202, 204, 400, 401, 403, 409, 429, 500])
def test_http_errors_never_divert(status: int) -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("rejected")
    flow.status_code = status
    with pytest.raises(RuntimeError, match="http_status_refused"):
        sink.commit_member_effect(plan, effect_input.members[0], effect_input, replace(ctx, http_post=flow))
    assert sink._get_diversions() == ()


@pytest.mark.parametrize("content_type", [None, "text/html", "application/jsonp"])
def test_non_json_media_refuses_without_diversion(content_type: str | None) -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("rejected")
    flow.content_type = content_type
    with pytest.raises(RuntimeError, match="media_type_refused"):
        sink.commit_member_effect(plan, effect_input.members[0], effect_input, replace(ctx, http_post=flow))
    assert sink._get_diversions() == ()


def test_unknown_never_diverts_or_claims_success() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("unknown")
    ctx = replace(ctx, http_post=flow)
    member = effect_input.members[0]
    assert sink.reconcile_member_effect(plan, member, effect_input, ctx).kind is SinkEffectReconcileKind.UNKNOWN
    with pytest.raises(RuntimeError, match="remote_outcome_unknown"):
        sink.commit_member_effect(plan, member, effect_input, ctx)
    assert sink._get_diversions() == ()


def test_sticky_rejection_after_response_loss_diverts_exact_member() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("rejected")
    flow.drop_write_response = True
    ctx = replace(ctx, http_post=flow)
    member = effect_input.members[1]
    with pytest.raises(RuntimeError, match="transport_failed"):
        sink.commit_member_effect(plan, member, effect_input, ctx)
    assert sink._get_diversions() == ()
    status = sink.reconcile_member_effect(plan, member, effect_input, ctx)
    assert status.kind is SinkEffectReconcileKind.NOT_APPLIED
    result = sink.commit_member_effect(plan, member, effect_input, ctx)
    assert result.descriptor == plan.expected_descriptor
    assert result.accepted_ordinals == (0,)
    assert result.diverted_ordinals == (1,)
    assert result.evidence["diversion_attribution"][0]["ordinal"] == 1
    assert sink._get_diversions()[0].row_index == 1


def test_missing_attempt_authority_refuses() -> None:
    sink, effect_input, plan, ctx = _prepared()
    with pytest.raises(FrameworkBugError, match="attempt_http_missing"):
        sink.commit_member_effect(plan, effect_input.members[0], effect_input, ctx)


def test_export_group_and_legacy_write_are_refused() -> None:
    sink, _, plan, ctx = _prepared()
    assert sink._resolve_sink_effect_mode(_config(), purpose=SinkEffectExecutionPurpose.AUDIT_EXPORT) is None
    for purpose in (SinkEffectExecutionPurpose.FRESH, SinkEffectExecutionPurpose.RESUME, SinkEffectExecutionPurpose.FOLLOWER):
        assert sink._resolve_sink_effect_mode(_config(), purpose=purpose).value == "write"
    with pytest.raises(FrameworkBugError):
        sink.commit_effect(plan, ctx)
    with pytest.raises(FrameworkBugError):
        sink.reconcile_effect(plan, ctx)
    with pytest.raises(RuntimeError):
        sink.write([], ctx)
    sink.configure_for_resume()
    sink.flush()
    sink.close()
    assert sink.supported_effect_input_kinds == frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})


def test_safe_authored_config_is_detached_and_defaults_remain_absent() -> None:
    config = _config()
    sink = PowerAutomateSink(config)
    config["fields"].append("unused")
    assert sink.config["fields"] == ["record_id", "result"]
    assert "timeout_seconds" not in sink.config


def test_secret_config_requires_fingerprint_key_even_with_raw_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    config.pop("trigger_url")
    config["auth"] = {"method": "sas_url", "trigger_url_secret": "https://flows.example.org/invoke?sig=test-signature"}
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    with pytest.raises(SecretFingerprintError, match="ELSPETH_FINGERPRINT_KEY"):
        PowerAutomateSink(config)


def test_secret_rotation_refuses_original_plan_and_resume_keeps_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-only-fingerprint-key")
    config = _config()
    config.pop("trigger_url")
    endpoint = "https://flows.example.org/invoke?sig=test-signature-one"
    config["auth"] = {"method": "sas_url", "trigger_url_secret": endpoint}
    sink = PowerAutomateSink(config)
    original = sink.safe_config_fingerprint
    sink.configure_for_resume()
    assert sink.safe_config_fingerprint == original
    assert endpoint not in json.dumps(sink.config)
    assert "trigger_url_secret_fingerprint" in sink.config["auth"]
    member = _member(0)
    effect_input = SinkEffectPipelineMembersInput((member,), (member,), 1)
    ctx = RestrictedSinkEffectContext("run", datetime(2026, 10, 1, tzinfo=UTC), "operation", "sink")
    inspection = sink.inspect_effect(SinkEffectInspectionRequest("a" * 64, "safe", None), ctx)
    plan = sink.prepare_effect(SinkEffectPrepareRequest("a" * 64, effect_input, inspection), ctx)
    config["auth"]["trigger_url_secret"] = endpoint.replace("one", "two")
    rotated = PowerAutomateSink(config)
    flow = _Flow()
    with pytest.raises(ValueError, match="plan_divergent"):
        rotated.reconcile_member_effect(plan, member, effect_input, replace(ctx, http_post=flow))
    assert flow.requests == []


def test_archived_sink_requires_no_credentials_and_cannot_compose_live_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    config.pop("trigger_url")
    config["auth"] = {"method": "sas_url", "trigger_url_secret_fingerprint": "c" * 64}
    options = ArchivedPowerAutomateOptions(
        "sink", "publish", "source-run", config, stable_hash(config), ArchivedPowerAutomateSinkConfig.model_validate(config)
    )
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    sink = PowerAutomateSink.from_archived_options(options)
    assert sink.config == config
    assert sink.safe_config_fingerprint == options.safe_options_hash
    with pytest.raises(FrameworkBugError, match="nonlive_factory_refused"):
        sink.make_http_post_factory()


def test_factory_composition_is_pure_and_binds_safe_configuration() -> None:
    sink = PowerAutomateSink(_config())
    factory = sink.make_http_post_factory()
    assert factory.safe_config_fingerprint == stable_hash(sink.config)


def test_descriptor_tamper_and_member_mismatch_refuse_before_http() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow()
    ctx = replace(ctx, http_post=flow)
    assert plan.expected_descriptor is not None
    changed = replace(plan, expected_descriptor=replace(plan.expected_descriptor, content_hash="c" * 64))
    with pytest.raises(ValueError, match="plan_divergent"):
        sink.commit_member_effect(changed, effect_input.members[0], effect_input, ctx)
    with pytest.raises(ValueError, match="member_divergent"):
        sink.commit_member_effect(plan, _member(0, identity="different"), effect_input, ctx)
    assert flow.requests == []


def test_limits_fail_without_diversion_or_partial_success() -> None:
    sink, effect_input, plan, ctx = _prepared()
    flow = _Flow("rejected")
    sink._cfg = sink._cfg.model_copy(update={"max_request_body_bytes": 1})
    with pytest.raises(ValueError, match="configuration_changed"):
        sink.commit_member_effect(plan, effect_input.members[0], effect_input, replace(ctx, http_post=flow))
    assert flow.requests == []
    assert sink._get_diversions() == ()


def test_duplicate_delivery_identity_refuses_group_preparation() -> None:
    sink, effect_input, _, ctx = _prepared()
    duplicated = replace(effect_input.members[1], member_effect_id=effect_input.members[0].member_effect_id)
    effect_input = replace(effect_input, members=(effect_input.members[0], duplicated))
    inspection = sink.inspect_effect(SinkEffectInspectionRequest("a" * 64, "safe", None), ctx)
    with pytest.raises(ValueError, match="duplicate_member_identity"):
        sink.prepare_effect(SinkEffectPrepareRequest("a" * 64, effect_input, inspection), ctx)


def test_response_limit_refuses_before_remote_body_parsing() -> None:
    config = _config()
    config["max_response_body_bytes"] = 1
    sink = PowerAutomateSink(config)
    members = (_member(0),)
    effect_input = SinkEffectPipelineMembersInput(members, members, 1)
    ctx = RestrictedSinkEffectContext("run", datetime(2026, 10, 1, tzinfo=UTC), "operation", "sink")
    inspection = sink.inspect_effect(SinkEffectInspectionRequest("a" * 64, "safe", None), ctx)
    plan = sink.prepare_effect(SinkEffectPrepareRequest("a" * 64, effect_input, inspection), ctx)
    with pytest.raises(RuntimeError, match="response_too_large"):
        sink.commit_member_effect(plan, members[0], effect_input, replace(ctx, http_post=_Flow("rejected")))
    assert sink._get_diversions() == ()
