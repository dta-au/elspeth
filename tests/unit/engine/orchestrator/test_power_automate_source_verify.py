"""Canonical page verification is admitted only for reviewed source reads."""

import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts.call_data import HTTPCallResponse, HTTPDecodedBodyEvidence
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import CallStatus, CallType, NodeType, RunMode
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, VerificationMismatchError
from elspeth.contracts.payload_store import PayloadStore
from elspeth.contracts.source_read_verification import CanonicalJSONSourceReadCapability, SourceReadVerificationPolicy
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.config import ElspethSettings, resolve_config
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataResult, CallDataState
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from elspeth.engine.orchestrator.source_read_verification import (
    canonical_source_read_response,
    source_load_input_data,
    source_read_verification_policy,
)
from elspeth.engine.orchestrator.types import PipelineConfig
from elspeth.plugins.infrastructure.run_mode_capabilities import admit_nonlive_runtime_plugin_instances


def _session_fixture() -> tuple[AuditedCallModeSession, Mock, object, dict]:
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    source = PowerAutomateSource(
        {
            "auth": {"method": "managed_identity", "client_id": "approved-principal"},
            "trigger_url": "https://example.org/flow",
            "allowed_origin": "https://example.org",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
    )
    source.node_id = "node-current"
    source.source_read_verification_policy = SourceReadVerificationPolicy.POWER_AUTOMATE_CANONICAL_JSON_V1
    request = {"method": "POST", "url": "https://example.org/flow", "json": {"protocol": "elspeth.power-automate.v1", "operation": "read"}}
    call = SimpleNamespace(
        call_id="source-call",
        state_id=None,
        operation_id="op-source",
        call_type=CallType.HTTP,
        call_index=0,
        status=CallStatus.SUCCESS,
        error_json=None,
        request_hash=stable_hash(request),
    )
    factory = Mock(spec=RecorderFactory)
    factory.query.get_all_calls_for_run.return_value = []
    factory.execution.get_all_operation_calls_for_run.return_value = [call]
    factory.execution.find_call_for_current_parent.return_value = call
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    operations = {
        "op-source": SimpleNamespace(operation_type="source_load", run_id="source", node_id="node-source"),
        "op-current": SimpleNamespace(operation_type="source_load", run_id="current", node_id="node-current"),
    }
    metadata = {"source_plugin": "power_automate", "source_read_verification_policy": "power-automate-canonical-json-v1"}
    for operation in operations.values():
        operation.input_data_hash = stable_hash(metadata)
        operation.input_data_ref = stable_hash(metadata)
    factory.execution.get_operation.side_effect = operations.__getitem__
    factory.data_flow.get_node.return_value = SimpleNamespace(node_type=NodeType.SOURCE, plugin_name="power_automate")
    factory.payload_store = Mock(spec=PayloadStore)
    factory.payload_store.retrieve_bounded.return_value = canonical_json(metadata).encode("utf-8")
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=_response(b'{"rows":[]}'))
    session = AuditedCallModeSession(
        factory,
        current_run_id="current",
        source_run_id="source",
        mode=RunMode.VERIFY,
        coordination_token=CoordinationToken(run_id="current", worker_id="worker", leader_epoch=1),
    )
    return session, factory, source, request


def _settle(session: AuditedCallModeSession, request: dict, live: dict, *, state_id: str | None = None):
    session.admit_verify_call(
        call_type=CallType.HTTP,
        request_data=request,
        current_state_id=state_id,
        current_operation_id=None if state_id else "op-current",
        current_call_index=0,
    )
    return session.verify_call(
        call_type=CallType.HTTP,
        request_data=request,
        current_state_id=state_id,
        current_operation_id=None if state_id else "op-current",
        current_call_index=0,
        current_call_id="call-current",
        live_status=CallStatus.SUCCESS,
        live_response_data=live,
        live_error_data=None,
    )


def test_scoped_policy_matches_headers_and_preserves_required_consumption() -> None:
    session, factory, source, request = _session_fixture()
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()
    with session.source_read_scope(source=source, current_operation_id="op-current"):
        decision = _settle(
            session, request, _response(b' {"rows": []} ', headers={"content-type": "application/json", "x-request-id": "second"})
        )
    assert decision.is_match is True
    assert decision.differences == {}
    assert factory.execution.record_verification_decision.call_args.kwargs["differences_json"] == "{}"
    session.assert_complete()


@pytest.mark.parametrize("operation", ["write", "status"])
def test_policy_cannot_authorize_transform_or_write(operation: str) -> None:
    session, factory, source, request = _session_fixture()
    request["json"]["operation"] = operation
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=request)
    with (
        pytest.raises(AuditIntegrityError, match="source read"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def test_scope_unregisters_even_after_failure_and_generic_comparison_stays_strict() -> None:
    session, _factory, source, request = _session_fixture()
    with pytest.raises(RuntimeError, match="abort"), session.source_read_scope(source=source, current_operation_id="op-current"):
        raise RuntimeError("abort")
    assert (
        _settle(
            session, request, _response(b'{"rows":[]}', headers={"content-type": "application/json", "x-request-id": "changed"})
        ).is_match
        is False
    )


def test_missing_capture_refuses_before_source_work() -> None:
    session, factory, source, _request = _session_fixture()
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data={"body": {"rows": []}})
    with (
        pytest.raises(AuditIntegrityError, match="canonical source response"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def test_missing_required_source_call_fails_scope_completion_before_downstream() -> None:
    session, _factory, source, _request = _session_fixture()
    with (
        pytest.raises(AuditIntegrityError, match="unconsumed source reads"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        pass


def test_live_invalid_capture_records_failure_before_downstream() -> None:
    session, factory, source, request = _session_fixture()
    with (
        pytest.raises(VerificationMismatchError, match="source read results differ"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        decision = _settle(session, request, {"status_code": 200, "body": {"rows": []}})
        assert decision.is_match is False
        assert decision.differences == {"canonical_response": "invalid_evidence"}
    assert factory.execution.record_verification_decision.call_args.kwargs["is_match"] is False


@pytest.mark.parametrize("changed", [{"run_id": "different"}, {"node_id": "different"}, {"operation_type": "sink_write"}])
def test_scope_binds_source_run_node_and_operation(changed: dict) -> None:
    session, factory, source, _request = _session_fixture()
    operation = factory.execution.get_operation("op-current")
    if "run_id" in changed:
        operation.run_id = changed["run_id"]
    if "node_id" in changed:
        operation.node_id = changed["node_id"]
    if "operation_type" in changed:
        operation.operation_type = changed["operation_type"]
    with (
        pytest.raises(AuditIntegrityError, match="identity differs"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def test_source_policy_metadata_is_required_and_integrity_checked() -> None:
    session, factory, source, _request = _session_fixture()
    factory.payload_store.retrieve_bounded.return_value = b'{"source_plugin":"power_automate"}'
    with (
        pytest.raises(AuditIntegrityError, match="operation evidence differs"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def test_policy_operation_metadata_refuses_duplicate_keys() -> None:
    session, factory, source, _request = _session_fixture()
    factory.payload_store.retrieve_bounded.return_value = (
        b'{"source_plugin":"unreviewed","source_plugin":"power_automate",'
        b'"source_read_verification_policy":"power-automate-canonical-json-v1"}'
    )
    with (
        pytest.raises(AuditIntegrityError, match="operation evidence is invalid"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def _response(body: bytes, **changes: object) -> dict:
    result = HTTPCallResponse(
        status_code=200,
        headers={"Content-Type": "application/json; charset=utf-8", "x-request-id": "first"},
        body_size=len(body),
        body={"untrusted_parsed": "ignored"},
        decoded_body=HTTPDecodedBodyEvidence(base64.b64encode(body).decode("ascii"), len(body), True),
    ).to_dict()
    result.update(changes)
    return result


def test_header_only_change_verifies_but_raw_evidence_is_retained() -> None:
    source = _response(b'{"snapshot_id":"snapshot","rows":[{"a":1,"b":2}],"next_cursor":null}')
    live = _response(
        b'{ "rows": [ { "b": 2, "a": 1 } ], "next_cursor": null, "snapshot_id": "snapshot" }',
        headers={"content-type": "APPLICATION/JSON", "x-request-id": "second"},
    )
    assert canonical_source_read_response(source, max_body_bytes=4096) == canonical_source_read_response(live, max_body_bytes=4096)
    assert source["headers"]["x-request-id"] == "first"
    assert live["headers"]["x-request-id"] == "second"
    assert source["decoded_body"] != live["decoded_body"]


@pytest.mark.parametrize("body", [b'{"rows":[{"a":2}]}', b'{"snapshot_id":"other"}', b'{"next_cursor":"other"}'])
def test_entire_page_data_snapshot_and_cursor_drift_fails(body: bytes) -> None:
    source = _response(b'{"rows":[{"a":1}],"snapshot_id":"snapshot","next_cursor":null}')
    assert canonical_source_read_response(source, max_body_bytes=4096) != canonical_source_read_response(
        _response(body), max_body_bytes=4096
    )


@pytest.mark.parametrize(
    "change",
    [
        {"decoded_body": None},
        {"decoded_body": {"body_b64": "e30=", "decoded_size": 99, "complete": True}},
        {"decoded_body": {"body_b64": "e30=", "decoded_size": 2, "complete": False, "incomplete_reason": "transport_failed"}},
        {"decoded_body": {"body_b64": "e30=", "decoded_size": 2, "complete": True, "extra": True}},
        {"headers": {"Content-Type": "text/plain"}},
        {"headers": {"Content-Type": "application/json", "content-type": "application/json"}},
        {"redirect_count": 1},
        {"redirect_count": True},
    ],
)
def test_missing_or_corrupt_capture_fails_canonical_verification(change: dict) -> None:
    with pytest.raises(AuditIntegrityError, match="canonical source response"):
        canonical_source_read_response(_response(b"{}", **change), max_body_bytes=4096)


@pytest.mark.parametrize("body", [b'{"secret":1,"secret":2}', b'{"a":NaN}', b'{"a":1e999}', b"not-json", b'"\xff"'])
def test_canonical_verification_strictly_parses_exact_decoded_bytes(body: bytes) -> None:
    with pytest.raises(AuditIntegrityError, match="canonical source response") as error:
        canonical_source_read_response(_response(body), max_body_bytes=4096)
    assert error.value.__cause__ is None
    assert "secret" not in str(error.value)


def test_canonical_verification_refuses_capture_above_bound() -> None:
    with pytest.raises(AuditIntegrityError, match="canonical source response"):
        canonical_source_read_response(_response(b"{}"), max_body_bytes=1)


def test_canonical_verification_checks_invalid_suffix_beyond_legacy_preview() -> None:
    body = b'{"rows":[],"padding":"' + b"x" * 15000 + b'","rows":[1]}'
    with pytest.raises(AuditIntegrityError, match="canonical source response"):
        canonical_source_read_response(_response(body), max_body_bytes=32768)


def test_canonical_verification_enforces_shared_depth_bound() -> None:
    shallow = b"[" * 64 + b"0" + b"]" * 64
    assert canonical_source_read_response(_response(shallow), max_body_bytes=4096)
    deep = b"[" * 65 + b"0" + b"]" * 65
    with pytest.raises(AuditIntegrityError, match="canonical source response"):
        canonical_source_read_response(_response(deep), max_body_bytes=4096)


def test_source_read_capability_is_nominal_and_policy_is_closed() -> None:
    class Source(CanonicalJSONSourceReadCapability):
        pass

    assert isinstance(Source(), CanonicalJSONSourceReadCapability)
    assert not isinstance(object(), CanonicalJSONSourceReadCapability)
    assert list(SourceReadVerificationPolicy) == [SourceReadVerificationPolicy.POWER_AUTOMATE_CANONICAL_JSON_V1]


def test_policy_selection_requires_exact_builtin_and_persists_ordinary_input_metadata() -> None:
    _session, _factory, source, _request = _session_fixture()
    assert source_load_input_data(source) == {
        "source_plugin": "power_automate",
        "source_read_verification_policy": "power-automate-canonical-json-v1",
    }

    class Impostor(CanonicalJSONSourceReadCapability):
        pass

    with pytest.raises(AuditIntegrityError, match="reviewed builtin source"):
        source_read_verification_policy(Impostor())


def test_snapshot_live_and_verify_operation_parent_metadata_agree() -> None:
    _session, _factory, source, _request = _session_fixture()
    source.config["snapshot_for_resume"] = True
    assert source_load_input_data(source) == {
        "source_plugin": "power_automate",
        "source_read_verification_policy": "power-automate-canonical-json-v1",
        "snapshot_for_resume": True,
    }


def test_policy_refuses_unassigned_source_identity() -> None:
    session, _factory, source, _request = _session_fixture()
    source.node_id = None
    with (
        pytest.raises(AuditIntegrityError, match="source node identity"),
        session.source_read_scope(source=source, current_operation_id="op-current"),
    ):
        raise AssertionError("must refuse before source SDK or DNS")


def test_forged_marker_policy_cannot_pass_nonlive_runtime_class_admission() -> None:
    class Impostor(CanonicalJSONSourceReadCapability):
        source_read_verification_policy = SourceReadVerificationPolicy.POWER_AUTOMATE_CANONICAL_JSON_V1

    settings = ElspethSettings(
        sources={"primary": {"plugin": "power_automate", "on_success": "output"}},
        sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        run_mode=RunMode.VERIFY,
        replay_from="source",
    )
    config = PipelineConfig(sources={"primary": Impostor()}, transforms=[], sinks={"output": object()}, config=resolve_config(settings))
    with pytest.raises(OrchestrationInvariantError, match="not the reviewed built-in class"):
        admit_nonlive_runtime_plugin_instances(config, settings)


def test_scope_refuses_unreviewed_source_before_any_archive_access() -> None:
    factory = Mock(spec=RecorderFactory)
    factory.query.get_all_calls_for_run.return_value = []
    factory.execution.get_all_operation_calls_for_run.return_value = []
    session = AuditedCallModeSession(
        factory,
        current_run_id="current",
        source_run_id="source",
        mode=RunMode.VERIFY,
        coordination_token=CoordinationToken(run_id="current", worker_id="worker", leader_epoch=1),
    )
    with (
        pytest.raises(AuditIntegrityError, match="reviewed builtin source"),
        session.source_read_scope(source=object(), current_operation_id="op-current"),
    ):
        raise AssertionError("impostor admitted")
    factory.execution.get_operation.assert_not_called()
