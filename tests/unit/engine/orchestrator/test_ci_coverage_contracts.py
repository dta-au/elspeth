"""Fail-closed replay admission and immutable source evidence regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from elspeth.contracts.call_mode import RuntimeRunMode
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import CallStatus, CallType, RunMode, RunStatus, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, VerificationMismatchError
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.row_data import CallDataResult, CallDataState
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession, _mi_non_auth_request
from elspeth.engine.orchestrator.run_modes import admit_nonlive_settings, admit_source_run, resolve_runtime_run_mode
from elspeth.engine.orchestrator.schema_reconstruction import reconstruct_schema_from_json
from elspeth.engine.orchestrator.source_replay import _verified_rows, prepare_audited_sources, prepare_verified_sources
from tests.fixtures.factories import make_context
from tests.unit.engine.orchestrator.test_call_mode_session import _CURRENT_TOKEN, _factory
from tests.unit.engine.orchestrator.test_run_modes import _pipeline, _settings
from tests.unit.engine.orchestrator.test_source_replay import _bound_source_load, _failed_source_state, _source_audit

_PARENT = {"current_state_id": "current-state", "current_operation_id": None}
_REQUEST = {"method": "GET", "url": "https://example.org/"}
_FINGERPRINT = "<fingerprint:" + "a" * 64 + ">"


def _session(factory: Mock, mode: RunMode = RunMode.VERIFY) -> AuditedCallModeSession:
    return AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=mode, coordination_token=_CURRENT_TOKEN)


def _http_factory(*, operation: bool = False) -> tuple[Mock, SimpleNamespace, dict[str, object]]:
    factory, call = _factory()
    request = {**_REQUEST, "headers": {"Authorization": _FINGERPRINT, "Accept": "application/json"}}
    if operation:
        call.state_id = None
        call.operation_id = "source-operation"
        factory.execution.get_operation.return_value = SimpleNamespace(operation_type="source_load", node_id="source-node")
    else:
        request["resolved_ip"] = "93.184.216.34"
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    call.request_hash = stable_hash(request)
    return factory, call, request


def test_call_session_rejects_live_mode_and_foreign_fencing_token_before_archive_reads() -> None:
    factory, _ = _factory()
    with pytest.raises(ValueError, match="replay or verify"):
        _session(factory, RunMode.LIVE)
    with pytest.raises(AuditIntegrityError, match="token does not belong"):
        AuditedCallModeSession(
            factory,
            current_run_id="current",
            source_run_id="source",
            mode=RunMode.REPLAY,
            coordination_token=CoordinationToken(run_id="other", worker_id="worker:other:test", leader_epoch=1),
        )
    factory.query.get_all_calls_for_run.assert_not_called()


@pytest.mark.parametrize(
    ("error_json", "call_type", "reason"),
    [
        (None, CallType.HTTP, "structured error evidence"),
        ("[]", CallType.HTTP, "structured error evidence"),
        ('{"message":"provider failed"}', CallType.LLM, "owned error category"),
        ('{"category":"invented"}', CallType.LLM, "owned error category"),
    ],
)
def test_error_archives_need_reconstructable_owned_error_evidence(error_json: str | None, call_type: CallType, reason: str) -> None:
    factory, call = _factory()
    call.status = CallStatus.ERROR
    call.error_json = error_json
    call.call_type = call_type
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    with pytest.raises(AuditIntegrityError, match=reason):
        _session(factory, RunMode.REPLAY)
    factory.execution.find_call_for_current_parent.assert_not_called()


def test_replay_error_without_response_retains_category_and_latency() -> None:
    factory, call = _factory()
    call.call_type = CallType.LLM
    call.status = CallStatus.ERROR
    call.error_json = '{"category":"network","message":"connection failed"}'
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    session = _session(factory, RunMode.REPLAY)
    evidence = session.replay_call(call_type=CallType.LLM, request_data=_REQUEST, current_call_index=0, **_PARENT)
    assert evidence.status is CallStatus.ERROR
    assert evidence.error_data == {"category": "network", "message": "connection failed"}
    assert evidence.response_data is None
    assert evidence.latency_ms == 12.0
    session.assert_complete()


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
def test_disappeared_operation_is_never_permission_to_skip_archive_admission(mode: RunMode) -> None:
    factory, call = _factory()
    call.operation_id = "source-operation"
    factory.execution.get_operation.return_value = None
    with pytest.raises(AuditIntegrityError, match=r"operation.*disappeared"):
        _session(factory, mode)


@pytest.mark.parametrize("state", [CallDataState.PURGED, CallDataState.HASH_ONLY])
def test_error_archive_rejects_lost_response_before_plugin_lifecycle(state: CallDataState) -> None:
    factory, call = _factory()
    call.status = CallStatus.ERROR
    call.error_json = '{"message":"failed"}'
    factory.execution.get_call_response_data.return_value = CallDataResult(state=state, data=None)
    with pytest.raises(AuditIntegrityError, match="incomplete response archive"):
        _session(factory)


@pytest.mark.parametrize(
    ("status", "response_state", "error_json", "reason"),
    [
        (CallStatus.SUCCESS, CallDataState.PURGED, None, "complete retained response"),
        (CallStatus.ERROR, CallDataState.PURGED, '{"message":"failed"}', "incomplete retained response"),
        (CallStatus.ERROR, CallDataState.NEVER_STORED, "[]", "error evidence is malformed"),
        (CallStatus.ERROR, CallDataState.NEVER_STORED, None, "reconstructable error evidence"),
    ],
)
def test_replay_checks_retention_again_at_consumption(
    status: CallStatus, response_state: CallDataState, error_json: str | None, reason: str
) -> None:
    factory, call = _factory()
    session = _session(factory, RunMode.REPLAY)
    call.status = status
    call.error_json = error_json
    factory.execution.get_call_response_data.return_value = CallDataResult(state=response_state, data=None)
    with pytest.raises(AuditIntegrityError, match=reason):
        session.replay_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=0, **_PARENT)
    with pytest.raises(AuditIntegrityError, match="1 unconsumed"):
        session.assert_complete()


def test_parent_scoped_admission_requires_a_unique_unsettled_source_occurrence() -> None:
    factory, call = _factory()
    call.request_hash = stable_hash(_REQUEST)
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    session = _session(factory)
    assert session.admit_verify_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=None, **_PARENT) == call.call_id
    with pytest.raises(AuditIntegrityError, match="missing or ambiguous"):
        session.admit_verify_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=None, **_PARENT)
    with pytest.raises(AuditIntegrityError, match="unsettled admissions"):
        session.assert_complete()
    decision = session.verify_call(
        call_type=CallType.HTTP,
        request_data=_REQUEST,
        current_call_index=0,
        current_call_id="current-call",
        live_status=CallStatus.SUCCESS,
        live_response_data={"status_code": 200, "transport": {"body_b64": ""}},
        live_error_data=None,
        **_PARENT,
    )
    assert decision.is_match is True
    assert decision.source_call_id == call.call_id
    session.assert_complete()


def test_indexed_admission_cannot_be_repeated_or_rebound_after_dispatch() -> None:
    factory, call = _factory()
    session = _session(factory)
    session.admit_verify_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=0, **_PARENT)
    with pytest.raises(AuditIntegrityError, match="already admitted"):
        session.admit_verify_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=0, **_PARENT)
    call.call_id = "different-source-call"
    with pytest.raises(AuditIntegrityError, match="changed after pre-dispatch"):
        session.verify_call(
            call_type=CallType.HTTP,
            request_data=_REQUEST,
            current_call_index=0,
            current_call_id="current-call",
            live_status=CallStatus.SUCCESS,
            live_response_data={},
            live_error_data=None,
            **_PARENT,
        )
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("corruption", ["missing", "hash-drift"])
def test_nested_transport_preflight_revalidates_retained_request(corruption: str) -> None:
    factory, call = _factory()
    call.request_hash = stable_hash(_REQUEST)
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    session = _session(factory)
    if corruption == "missing":
        factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    else:
        factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data={"url": "other"})
    with pytest.raises(AuditIntegrityError, match="payload"):
        session.preflight_verify_request(call_type=CallType.HTTP, request_data=_REQUEST, **_PARENT)
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("response_state", [CallDataState.NEVER_STORED, CallDataState.PURGED])
def test_verify_error_records_absent_or_lost_evidence_without_claiming_success(response_state: CallDataState) -> None:
    factory, call = _factory()
    call.status = CallStatus.ERROR
    call.error_json = '{"message":"old failure"}'
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    session = _session(factory)
    session.admit_verify_call(call_type=CallType.HTTP, request_data=_REQUEST, current_call_index=0, **_PARENT)
    factory.execution.get_call_response_data.return_value = CallDataResult(state=response_state, data=None)
    decision = session.verify_call(
        call_type=CallType.HTTP,
        request_data=_REQUEST,
        current_call_index=0,
        current_call_id="current-call",
        live_status=CallStatus.SUCCESS,
        live_response_data={"status_code": 200, "transport": {"body_b64": ""}},
        live_error_data=None,
        **_PARENT,
    )
    if response_state is CallDataState.NEVER_STORED:
        assert decision.is_match is False
        assert set(decision.differences) == {"status", "response_hash", "error_hash"}
    else:
        assert decision.is_match is None
        assert decision.differences == {"source_response": "purged"}
    recorded = factory.execution.record_verification_decision.call_args.kwargs
    assert recorded["coordination_token"] is _CURRENT_TOKEN
    assert json.loads(recorded["differences_json"]) == decision.differences
    with pytest.raises(VerificationMismatchError, match="1 mismatches"):
        session.assert_complete()


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        (None, "structured headers"),
        ({"Authorization": _FINGERPRINT, "authorization": _FINGERPRINT}, "ambiguous Authorization"),
        ({"Authorization": "Bearer plaintext"}, "not a fingerprint"),
    ],
)
def test_managed_identity_comparison_refuses_unstructured_or_ambiguous_auth(headers: object, reason: str) -> None:
    with pytest.raises(AuditIntegrityError, match=reason):
        _mi_non_auth_request({"headers": headers}, before_dns=False)


@pytest.mark.parametrize("operation", [False, True])
@pytest.mark.parametrize("drift", ["source-id", "no-preflight", "non-auth", "already-admitted"])
def test_managed_identity_admission_binds_pre_token_request_and_source(operation: bool, drift: str) -> None:
    factory, call, request = _http_factory(operation=operation)
    session = _session(factory)
    current_request = {**request, "headers": {"Authorization": "<fingerprint:" + "b" * 64 + ">", "Accept": "application/json"}}
    parent = {"current_operation_id": "current-operation"} if operation else _PARENT
    preflight = session.preflight_verify_operation_http_managed_identity if operation else session.preflight_verify_http_managed_identity
    admit = session.admit_verify_operation_http_managed_identity if operation else session.admit_verify_http_managed_identity
    if drift != "no-preflight":
        preflight(request_data=current_request, **parent)
    source_id = "other-call" if drift == "source-id" else call.call_id
    if drift == "non-auth":
        current_request["method"] = "POST"
    if drift == "already-admitted":
        assert admit(request_data=current_request, current_call_index=0, source_call_id=source_id, **parent) == call.call_id
        preflight(request_data=current_request, **parent)
    reason = {
        "source-id": "changed after preflight",
        "no-preflight": "no pre-token source admission",
        "non-auth": "non-auth request fields differ",
        "already-admitted": "already admitted",
    }[drift]
    with pytest.raises(AuditIntegrityError, match=reason):
        admit(request_data=current_request, current_call_index=0, source_call_id=source_id, **parent)
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("operation", [False, True])
def test_managed_identity_preflight_refuses_retention_loss(operation: bool) -> None:
    factory, _, request = _http_factory(operation=operation)
    session = _session(factory)
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    with pytest.raises(AuditIntegrityError, match="unavailable"):
        if operation:
            session.preflight_verify_operation_http_managed_identity(request_data=request, current_operation_id="current-operation")
        else:
            session.preflight_verify_http_managed_identity(request_data=request, **_PARENT)


def test_operation_managed_identity_refuses_dns_pins_and_missing_url_before_tokens() -> None:
    factory, _, request = _http_factory(operation=True)
    session = _session(factory)
    factory.execution.get_call_request_data.return_value = CallDataResult(
        state=CallDataState.AVAILABLE, data={**request, "resolved_ip": "93.184.216.34"}
    )
    with pytest.raises(AuditIntegrityError, match="unexpected DNS pin"):
        session.preflight_verify_operation_http_managed_identity(request_data=request, current_operation_id="current-operation")
    request.pop("url")
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    with pytest.raises(AuditIntegrityError, match="requires a URL"):
        session.preflight_verify_operation_http_managed_identity(request_data=request, current_operation_id="current-operation")


@pytest.mark.parametrize(
    ("url", "expected_host", "expected_path", "expected_port"),
    [
        ("https://[2001:db8::1]:8443/x?query=yes#fragment", "[2001:db8::1]:8443", "/x?query=yes#fragment", 8443),
        ("http://example.org", "example.org", "/", 80),
    ],
)
def test_replay_dns_reconstructs_authority_and_path_from_original_url(
    url: str, expected_host: str, expected_path: str, expected_port: int
) -> None:
    factory, _, request = _http_factory()
    request["url"] = "redacted-url"
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    session = _session(factory, RunMode.REPLAY)
    replay = session.replay_ssrf_request(original_url=url, audited_url="redacted-url", call_type=CallType.HTTP, **_PARENT)
    assert replay.host_header == expected_host
    assert replay.path == expected_path
    assert replay.port == expected_port
    assert replay.resolved_ip == "93.184.216.34"
    assert replay.original_url == url


@pytest.mark.parametrize("corruption", ["retention", "pin", "url"])
def test_replay_dns_refuses_corrupt_evidence_and_hostname(corruption: str) -> None:
    factory, _, request = _http_factory()
    session = _session(factory, RunMode.REPLAY)
    if corruption == "retention":
        factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    else:
        if corruption == "pin":
            request.pop("resolved_ip")
        else:
            request["url"] = "relative/path"
        factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    with pytest.raises(
        AuditIntegrityError, match={"retention": "retained request", "pin": "lacks a DNS pin", "url": "no hostname"}[corruption]
    ):
        session.replay_ssrf_request(original_url=str(request["url"]), call_type=CallType.HTTP, **_PARENT)


@pytest.mark.parametrize("operation", [False, True])
def test_source_parent_identity_recovers_durable_owner_and_refuses_disappearance(operation: bool) -> None:
    factory, call, _ = _http_factory(operation=operation)
    factory.execution.get_node_state.return_value = SimpleNamespace(node_id="source-node", token_id="source-token")
    session = _session(factory)
    identity = session.source_parent_identity(call_type=CallType.HTTP, **_PARENT)
    assert identity.source_run_id == "source"
    assert identity.source_node_id == "source-node"
    assert identity.source_state_id == (None if operation else "source-state")
    assert identity.source_operation_id == ("source-operation" if operation else None)
    assert identity.source_token_id == (None if operation else "source-token")
    if operation:
        factory.execution.get_operation.return_value = None
    else:
        factory.execution.get_node_state.return_value = None
    with pytest.raises(AuditIntegrityError, match="disappeared"):
        session.source_parent_identity(call_type=CallType.HTTP, **_PARENT)
    call.state_id = None
    call.operation_id = None
    with pytest.raises(AuditIntegrityError, match="no operation parent"):
        session.source_parent_identity(call_type=CallType.HTTP, **_PARENT)


@pytest.mark.parametrize(
    ("config_change", "reason"),
    [
        ({"run_mode": 7}, "must be a string"),
        ({"run_mode": "invented"}, "Unsupported"),
        ({"replay_from": " "}, "non-empty run ID"),
        ({"replay_from": None}, "required for replay"),
        ({"replay_from": "different-run"}, "disagree"),
    ],
)
def test_programmatic_mode_configuration_never_silently_downgrades(config_change: dict[str, object], reason: str) -> None:
    settings = _settings(RunMode.REPLAY)
    pipeline = _pipeline(settings)
    pipeline = replace(pipeline, config={**pipeline.config, **config_change})
    with pytest.raises(OrchestrationInvariantError, match=reason):
        resolve_runtime_run_mode(pipeline, settings)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("depends_on", [object()], "depends_on"),
        ("collection_probes", [object()], "collection_probes"),
        ("commencement_gates", [object()], "commencement_gates"),
        ("landscape", SimpleNamespace(export=SimpleNamespace(enabled=True)), "Landscape export"),
        ("telemetry", SimpleNamespace(enabled=True), "telemetry exporters"),
    ],
)
def test_nonlive_admission_refuses_every_unmodeled_side_channel(field: str, value: object, reason: str) -> None:
    settings = _settings(RunMode.REPLAY).model_copy(update={field: value})
    with pytest.raises(OrchestrationInvariantError, match=reason):
        admit_nonlive_settings(settings)


def test_live_admission_does_not_query_replay_evidence() -> None:
    db = Mock(spec=LandscapeDB)
    admit_nonlive_settings(_settings(RunMode.LIVE))
    with patch("elspeth.engine.orchestrator.run_modes.RecorderFactory.read_only") as read_only:
        admit_source_run(db, RuntimeRunMode(mode=RunMode.LIVE, replay_from=None))
    read_only.assert_not_called()


@pytest.mark.parametrize("failure", ["self", "moving", "timestamp"])
def test_source_run_admission_refuses_mutable_or_uncommitted_history(failure: str) -> None:
    db = Mock(spec=LandscapeDB)
    settings = _settings(RunMode.REPLAY)
    mode = resolve_runtime_run_mode(_pipeline(settings), settings)
    source = SimpleNamespace(status=RunStatus.RUNNING if failure == "moving" else RunStatus.COMPLETED, completed_at=None)
    with patch("elspeth.engine.orchestrator.run_modes.RecorderFactory.read_only") as read_only:
        read_only.return_value.run_lifecycle.get_run.return_value = source
        with pytest.raises(
            OrchestrationInvariantError,
            match={"self": "against itself", "moving": "not a completed", "timestamp": "completion timestamp"}[failure],
        ):
            admit_source_run(db, mode, current_run_id=mode.replay_from if failure == "self" else None)
        if failure == "self":
            read_only.assert_not_called()


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("lifecycle", "not exhausted"),
        ("node", "node is missing"),
        ("plugin", "plugin identity differs"),
        ("config-hash", "config is corrupt"),
        ("config-shape", "config is corrupt"),
        ("source-name", "audited name is missing"),
        ("version", "implementation or configuration differs"),
        ("schema-none", "no stored schema"),
        ("schema-json", "malformed schema JSON"),
        ("schema-list", "schema is not an object"),
        ("row-owner", "undeclared source node"),
        ("contract-none", "source contract missing"),
        ("contract-json", "malformed source contract"),
        ("contract-list", "malformed source contract"),
        ("payload-hash", "restored payload hash mismatch"),
    ],
)
def test_audited_source_snapshot_refuses_corruption_before_source_code(corruption: str, reason: str) -> None:
    factory, source, row = _source_audit()
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records.return_value["source-old"]
    node = factory.data_flow.get_nodes.return_value[0]
    if corruption == "lifecycle":
        lifecycle.lifecycle_state = "loading"
    elif corruption == "node":
        factory.data_flow.get_nodes.return_value = []
    elif corruption == "plugin":
        node.plugin_name = "other"
    elif corruption == "config-hash":
        node.config_hash = "0" * 64
    elif corruption in {"config-shape", "source-name"}:
        config = [] if corruption == "config-shape" else source.config
        node.config_json = json.dumps(config)
        node.config_hash = stable_hash(config)
    elif corruption == "version":
        node.plugin_version = "changed"
    elif corruption == "schema-none":
        lifecycle.source_schema_json = None
    elif corruption == "schema-json":
        lifecycle.source_schema_json = "invalid-json"
    elif corruption == "schema-list":
        lifecycle.source_schema_json = "[]"
    elif corruption == "row-owner":
        row.source_node_id = "unknown-node"
    elif corruption == "contract-none":
        row.source_contract_json = None
    elif corruption == "contract-json":
        row.source_contract_json = "invalid-json"
    elif corruption == "contract-list":
        row.source_contract_json = "[]"
    elif corruption == "payload-hash":
        row.source_data_hash = "0" * 64
    with pytest.raises(AuditIntegrityError, match=reason):
        prepare_audited_sources(factory, "source-run", {"primary": source})
    source.load.assert_not_called()


@pytest.mark.parametrize("order", [None, 0])
def test_multisource_snapshot_requires_audited_source_order(order: int | None) -> None:
    factory, source, _ = _source_audit()
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records.return_value["source-old"]
    node = factory.data_flow.get_nodes.return_value[0]
    other_lifecycle = SimpleNamespace(
        **vars(lifecycle),
    )
    other_lifecycle.source_node_id = "second-source"
    other_lifecycle.source_name = "secondary"
    factory.run_lifecycle.get_run_source_lifecycle_records.return_value["second-source"] = other_lifecycle
    other_node = SimpleNamespace(**vars(node))
    other_node.node_id = "second-source"
    other_node.config_json = json.dumps({**source.config, "source_name": "secondary"})
    other_node.config_hash = stable_hash({**source.config, "source_name": "secondary"})
    node.sequence_in_pipeline = order
    other_node.sequence_in_pipeline = 1
    factory.data_flow.get_nodes.return_value.append(other_node)
    sources = {"secondary": source, "primary": source}
    with pytest.raises(AuditIntegrityError, match="order"):
        prepare_audited_sources(factory, "source-run", sources)
    source.load.assert_not_called()


@pytest.mark.parametrize("additional", [True, {"type": "integer"}])
def test_reconstructed_mapping_fields_preserve_declared_value_types(additional: object) -> None:
    schema = reconstruct_schema_from_json(
        {"properties": {"values": {"type": "object", "additionalProperties": additional}}, "required": ["values"]}
    )
    assert schema.model_validate({"values": {"first": 7}}).to_row() == {"values": {"first": 7}}
    if type(additional) is dict:
        with pytest.raises(ValidationError):
            schema.model_validate({"values": {"first": "not-an-integer"}})
    else:
        assert schema.model_validate({"values": {"first": "text"}}).to_row() == {"values": {"first": "text"}}


@pytest.mark.parametrize(
    ("field", "definitions", "reason"),
    [
        ({"$ref": "https://outside/schema"}, {}, "Only local refs"),
        ({"$ref": "#/$defs/missing"}, None, "no \\$defs section"),
        ({"$ref": "#/$defs/missing"}, {}, "missing schema def"),
        ({"type": "object", "additionalProperties": 42}, {}, "invalid additionalProperties"),
    ],
)
def test_schema_reconstruction_refuses_unresolvable_or_malformed_definitions(field: dict, definitions: dict | None, reason: str) -> None:
    schema = {"properties": {"value": field}}
    if definitions is not None:
        schema["$defs"] = definitions
    with pytest.raises(AuditIntegrityError, match=reason):
        reconstruct_schema_from_json(schema)


@pytest.mark.parametrize("field_name", ["123name", "---"])
def test_schema_reconstruction_handles_external_nested_field_names(field_name: str) -> None:
    schema = reconstruct_schema_from_json(
        {"properties": {field_name: {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}}}
    )
    assert schema.model_validate({field_name: {"count": 4}}).to_row() == {field_name: {"count": 4}}


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("duplicate-state", "validation error evidence missing"),
        ("missing-error", "validation error evidence missing"),
        ("duplicate-outcome", "ambiguous quarantine outcomes"),
        ("missing-sink", "quarantine destination missing"),
        ("error-list", "malformed source validation error"),
        ("error-empty", "malformed source validation error"),
        ("hash-drift", "quarantine payload hash mismatch"),
        ("missing-original", "ambiguous _raw quarantine payload"),
        ("wrong-original", "disagrees with validation evidence"),
    ],
)
def test_quarantine_replay_requires_one_bound_original_payload_and_destination(corruption: str, reason: str) -> None:
    factory, source, row = _source_audit(payload={"_raw": "bad"})
    failed = _failed_source_state()
    outcome = SimpleNamespace(path=TerminalPath.QUARANTINED_AT_SOURCE, token_id="source-token", sink_name="quarantine")
    factory.query.get_tokens.return_value = [SimpleNamespace(token_id="source-token")]
    factory.query.get_node_states_for_token.return_value = [failed]
    factory.data_flow.get_token_outcomes_for_row.return_value = [outcome]
    factory.data_flow.get_validation_errors_for_row.return_value = [SimpleNamespace(row_data_json='"bad"')]
    if corruption == "duplicate-state":
        factory.query.get_node_states_for_token.return_value = [failed, failed]
    elif corruption == "missing-error":
        factory.query.get_node_states_for_token.return_value = [replace(failed, error_json=None)]
    elif corruption == "duplicate-outcome":
        factory.data_flow.get_token_outcomes_for_row.return_value = [outcome, outcome]
    elif corruption == "missing-sink":
        outcome.sink_name = None
    elif corruption == "error-list":
        factory.query.get_node_states_for_token.return_value = [replace(failed, error_json="[]")]
    elif corruption == "error-empty":
        factory.query.get_node_states_for_token.return_value = [replace(failed, error_json='{"exception":""}')]
    elif corruption == "hash-drift":
        row.source_data_hash = "0" * 64
    elif corruption == "missing-original":
        factory.data_flow.get_validation_errors_for_row.return_value = []
    elif corruption == "wrong-original":
        factory.data_flow.get_validation_errors_for_row.return_value = [SimpleNamespace(row_data_json='"different"')]
    with pytest.raises(AuditIntegrityError, match=reason):
        prepare_audited_sources(factory, "source-run", {"primary": source})
    source.load.assert_not_called()


def test_quarantine_replay_preserves_real_raw_dict_instead_of_unwrapping_it() -> None:
    factory, source, _ = _source_audit(payload={"_raw": "bad"})
    factory.query.get_tokens.return_value = [SimpleNamespace(token_id="source-token")]
    factory.query.get_node_states_for_token.return_value = [_failed_source_state()]
    factory.data_flow.get_token_outcomes_for_row.return_value = [
        SimpleNamespace(path=TerminalPath.QUARANTINED_AT_SOURCE, token_id="source-token", sink_name="quarantine")
    ]
    factory.data_flow.get_validation_errors_for_row.return_value = [SimpleNamespace(row_data_json='{"_raw":"bad"}')]
    snapshot = prepare_audited_sources(factory, "source-run", {"primary": source})["primary"]
    assert snapshot.rows[0].row == {"_raw": "bad"}
    assert snapshot.rows[0].quarantine_destination == "quarantine"
    assert snapshot.rows[0].quarantine_error == "bad value"
    source.load.assert_not_called()


@pytest.mark.parametrize("corruption", ["run-id", "mode", "declarations", "snapshot-type", "node-id"])
def test_verify_source_preflight_rejects_invalid_context_before_loading(corruption: str) -> None:
    factory, source, _ = _source_audit()
    audited = prepare_audited_sources(factory, "source-run", {"primary": source})
    ctx = make_context(run_id="current", node_id="original-node")
    ctx.operation_id = "original-operation"
    ctx.run_mode = RunMode.VERIFY
    ctx.replay_from = "source-run"
    ctx.call_mode_session = _session(_factory()[0])
    source.node_id = "source-current"
    token = _CURRENT_TOKEN
    if corruption == "run-id":
        token = CoordinationToken(run_id="other", worker_id="worker:other:test", leader_epoch=1)
    elif corruption == "mode":
        ctx.run_mode = RunMode.LIVE
    elif corruption == "declarations":
        audited = {}
    elif corruption == "snapshot-type":
        audited = {"primary": SimpleNamespace()}
    elif corruption == "node-id":
        source.node_id = None
    with patch("elspeth.engine.orchestrator.source_replay.track_operation") as tracked:
        with pytest.raises((AuditIntegrityError, OrchestrationInvariantError)):
            prepare_verified_sources(factory, "current", audited, {"primary": source}, ctx, token)
        tracked.assert_not_called()
    source.load.assert_not_called()
    assert ctx.node_id == "original-node"
    assert ctx.operation_id == "original-operation"


@pytest.mark.parametrize("live_rows", [[], [object()]])
def test_verify_source_stream_never_accepts_missing_or_untyped_rows(live_rows: list[object]) -> None:
    factory, source, _ = _source_audit()
    audited = prepare_audited_sources(factory, "source-run", {"primary": source})["primary"]
    source.load = Mock(spec=_bound_source_load, return_value=iter(live_rows))
    ctx = make_context(run_id="current")
    expected = VerificationMismatchError if not live_rows else OrchestrationInvariantError
    with pytest.raises(expected, match="row count differs" if not live_rows else "non-SourceRow"):
        _verified_rows(source, ctx, audited)
    source.load.assert_called_once_with(ctx)


@pytest.mark.parametrize(
    ("entry", "kwargs"),
    [
        (
            AuditedCallModeSession.admit_verify_call,
            {"call_type": CallType.HTTP, "request_data": _REQUEST, "current_call_index": 0, **_PARENT},
        ),
        (AuditedCallModeSession.preflight_verify_request, {"call_type": CallType.HTTP, "request_data": _REQUEST, **_PARENT}),
        (AuditedCallModeSession.preflight_verify_http_managed_identity, {"request_data": _REQUEST, **_PARENT}),
        (AuditedCallModeSession.preflight_verify_http_request, {"request_data": _REQUEST, **_PARENT}),
        (
            AuditedCallModeSession.admit_verify_http_managed_identity,
            {"request_data": _REQUEST, "current_call_index": 0, "source_call_id": "source-call", **_PARENT},
        ),
        (
            AuditedCallModeSession.preflight_verify_operation_http_managed_identity,
            {"request_data": _REQUEST, "current_operation_id": "current-operation"},
        ),
        (
            AuditedCallModeSession.admit_verify_operation_http_managed_identity,
            {
                "request_data": _REQUEST,
                "current_operation_id": "current-operation",
                "current_call_index": 0,
                "source_call_id": "source-call",
            },
        ),
    ],
)
def test_replay_cannot_grant_live_verification_dispatch(entry: object, kwargs: dict) -> None:
    factory, _ = _factory()
    session = _session(factory, RunMode.REPLAY)
    with pytest.raises(AuditIntegrityError, match="verify mode"):
        entry(session, **kwargs)
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.list_source_calls_for_current_parent.assert_not_called()


def test_archived_request_lookup_refuses_missing_and_purged_parent_local_occurrences() -> None:
    factory, _ = _factory()
    session = _session(factory)
    factory.execution.find_call_for_current_parent.return_value = None
    with pytest.raises(AuditIntegrityError, match="Exact source request is missing"):
        session.archived_call_request(call_type=CallType.HTTP, current_call_index=0, **_PARENT)
    factory.execution.find_call_for_current_parent.return_value = SimpleNamespace(call_id="source-call")
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    with pytest.raises(AuditIntegrityError, match="unavailable"):
        session.archived_call_request(call_type=CallType.HTTP, current_call_index=0, **_PARENT)
