"""Public call-session admission preserves exact, reconstructable audit evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from elspeth.contracts.audit import Call, NodeStateOpen, Operation
from elspeth.contracts.enums import CallStatus, CallType, NodeStateStatus, RunMode
from elspeth.contracts.errors import AuditIntegrityError, VerificationMismatchError
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.landscape.row_data import CallDataResult, CallDataState
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from tests.unit.engine.orchestrator.test_call_mode_session import _CURRENT_TOKEN, _factory

_REQUEST = {"method": "GET", "url": "https://example.org/"}
_RESPONSE = {"status_code": 200, "transport": {"body_b64": ""}}
_ERROR = {"type": "TimeoutError", "message": "connection timed out", "category": "network"}
_NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _archive(*, status: CallStatus = CallStatus.SUCCESS, error_json: str | None = None) -> tuple[Mock, Call]:
    factory, _ = _factory()
    call = Call(
        call_id="source-call",
        call_index=0,
        call_type=CallType.HTTP,
        status=status,
        request_hash=stable_hash(_REQUEST),
        created_at=_NOW,
        state_id="source-state",
        error_json=error_json,
        latency_ms=12.0,
    )
    _calls(factory, call)
    return factory, call


def _calls(factory: Mock, call: Call) -> None:
    factory.query.get_all_calls_for_run.return_value = [call] if call.state_id is not None else []
    factory.execution.get_all_operation_calls_for_run.return_value = [call] if call.operation_id is not None else []
    factory.execution.find_call_for_current_parent.return_value = call
    factory.execution.list_source_calls_for_current_parent.return_value = [call]


def _session(factory: Mock, mode: RunMode = RunMode.REPLAY) -> AuditedCallModeSession:
    return AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=mode, coordination_token=_CURRENT_TOKEN)


def _replay(session: AuditedCallModeSession):
    return session.replay_call(
        call_type=CallType.HTTP,
        request_data=_REQUEST,
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
    )


def _admit(session: AuditedCallModeSession, *, index: int | None = 0) -> str:
    return session.admit_verify_call(
        call_type=CallType.HTTP,
        request_data=_REQUEST,
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=index,
    )


def _verify(
    session: AuditedCallModeSession,
    *,
    status: CallStatus = CallStatus.SUCCESS,
    response: dict | None = _RESPONSE,
    error: dict | None = None,
):
    return session.verify_call(
        call_type=CallType.HTTP,
        request_data=_REQUEST,
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
        current_call_id="current-call",
        live_status=status,
        live_response_data=response,
        live_error_data=error,
    )


@pytest.mark.parametrize("state", [state for state in CallDataState if state is not CallDataState.AVAILABLE])
def test_preflight_refuses_incomplete_request_without_lookup_or_verdict(state: CallDataState) -> None:
    factory, call = _archive()
    missing = CallDataResult(state=state, data=None)
    factory.execution.get_call_request_data.return_value = missing
    with pytest.raises(AuditIntegrityError, match="complete request archive"):
        _session(factory)
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.get_call_response_data.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]
    assert factory.execution.get_call_request_data.return_value is missing


@pytest.mark.parametrize("state", [CallDataState.PURGED, CallDataState.HASH_ONLY, CallDataState.CALL_NOT_FOUND])
def test_error_archive_requires_retained_response_or_genuine_absence(state: CallDataState) -> None:
    factory, call = _archive(status=CallStatus.ERROR, error_json=canonical_json(_ERROR))
    missing = CallDataResult(state=state, data=None)
    factory.execution.get_call_response_data.return_value = missing
    with pytest.raises(AuditIntegrityError, match=r"error call.*incomplete response archive"):
        _session(factory)
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]
    assert factory.execution.get_call_response_data.return_value is missing


@pytest.mark.parametrize("error_json", [None, "null", "[]", '"timeout"'])
def test_preflight_requires_structured_error_envelope(error_json: str | None) -> None:
    factory, call = _archive(status=CallStatus.ERROR, error_json=error_json)
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    with pytest.raises(AuditIntegrityError, match="structured error evidence"):
        _session(factory)
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("error", [{"message": "failed"}, {"category": "invented"}, {"category": None}])
def test_llm_preflight_requires_owned_error_category(error: dict) -> None:
    factory, call = _archive(status=CallStatus.ERROR, error_json=canonical_json(error))
    call = replace(call, call_type=CallType.LLM)
    _calls(factory, call)
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    with pytest.raises(AuditIntegrityError, match="owned error category"):
        _session(factory)
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("call_type", [CallType.HTTP, CallType.LLM])
@pytest.mark.parametrize("response_available", [False, True])
def test_operational_error_replays_once_without_live_dispatch(call_type: CallType, response_available: bool) -> None:
    factory, call = _archive(status=CallStatus.ERROR, error_json=canonical_json(_ERROR))
    call = replace(call, call_type=call_type)
    _calls(factory, call)
    response = {"status_code": 503} if response_available else None
    factory.execution.get_call_response_data.return_value = CallDataResult(
        state=CallDataState.AVAILABLE if response_available else CallDataState.NEVER_STORED, data=response
    )
    session = _session(factory)
    evidence = session.replay_call(
        call_type=call_type,
        request_data=_REQUEST,
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
    )
    assert evidence.source_call_id == call.call_id
    assert evidence.status is CallStatus.ERROR
    assert evidence.error_data == _ERROR
    assert evidence.response_data == response
    assert evidence.latency_ms == 12.0
    session.assert_complete()
    with pytest.raises(AuditIntegrityError, match="already consumed"):
        session.replay_call(
            call_type=call_type,
            request_data=_REQUEST,
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=0,
        )
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("status", [CallStatus.SUCCESS, CallStatus.ERROR])
def test_replay_rechecks_response_retention_without_consuming_refused_call(status: CallStatus) -> None:
    factory, call = _archive(status=status, error_json=canonical_json(_ERROR) if status is CallStatus.ERROR else None)
    session = _session(factory)
    retained = factory.execution.get_call_response_data.return_value
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    with pytest.raises(AuditIntegrityError, match="retained response"):
        _replay(session)
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()
    factory.execution.record_verification_decision.assert_not_called()
    factory.execution.get_call_response_data.return_value = retained
    assert _replay(session).source_call_id == call.call_id
    session.assert_complete()


@pytest.mark.parametrize("error_json", [None, "[]"])
def test_replay_rechecks_error_evidence_without_consuming_refused_call(error_json: str | None) -> None:
    factory, original = _archive(status=CallStatus.ERROR, error_json=canonical_json(_ERROR))
    session = _session(factory)
    changed = replace(original, error_json=error_json)
    factory.execution.find_call_for_current_parent.return_value = changed
    with pytest.raises(AuditIntegrityError, match="error evidence"):
        _replay(session)
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()
    factory.execution.record_verification_decision.assert_not_called()
    factory.execution.find_call_for_current_parent.return_value = original
    assert _replay(session).error_data == _ERROR
    session.assert_complete()


def test_missing_operation_refuses_preflight_before_request_archive_access() -> None:
    factory, call = _archive()
    _calls(factory, replace(call, state_id=None, operation_id="source-operation"))
    factory.execution.get_operation.return_value = None
    with pytest.raises(AuditIntegrityError, match=r"operation.*disappeared"):
        _session(factory)
    factory.execution.get_call_request_data.assert_not_called()
    factory.execution.find_call_for_current_parent.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
@pytest.mark.parametrize("operation_type", ["runtime_preflight", "source_load", "sink_write"])
def test_operation_preflight_scope_respects_replay_and_verify_authority(mode: RunMode, operation_type: str) -> None:
    factory, call = _archive()
    _calls(factory, replace(call, state_id=None, operation_id="source-operation"))
    factory.execution.get_operation.return_value = Operation(
        operation_id="source-operation",
        run_id="source",
        node_id="source-node",
        operation_type=operation_type,
        started_at=_NOW,
        status="open",
    )
    session = _session(factory, mode)
    assert session.source_run_id == "source"
    required = operation_type == "runtime_preflight" or (mode is RunMode.VERIFY and operation_type == "source_load")
    if required:
        with pytest.raises(AuditIntegrityError, match="1 unconsumed"):
            session.assert_complete()
        factory.execution.get_call_request_data.assert_called_once_with(call.call_id)
    else:
        session.assert_complete()
        factory.execution.get_call_request_data.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("parent", ["state", "operation"])
def test_source_parent_identity_preserves_nominal_audit_ownership(parent: str) -> None:
    factory, call = _archive()
    state = NodeStateOpen(
        state_id="source-state",
        token_id="source-token",
        node_id="source-node",
        step_index=0,
        attempt=0,
        status=NodeStateStatus.OPEN,
        input_hash=stable_hash({}),
        started_at=_NOW,
    )
    operation = Operation(
        operation_id="source-operation",
        run_id="source",
        node_id="source-node",
        operation_type="runtime_preflight",
        started_at=_NOW,
        status="open",
    )
    factory.execution.get_node_state.return_value = state
    factory.execution.get_operation.return_value = operation
    if parent == "operation":
        _calls(factory, replace(call, state_id=None, operation_id=operation.operation_id))
    session = _session(factory)
    identity = session.source_parent_identity(
        call_type=CallType.HTTP,
        current_state_id="current-state" if parent == "state" else None,
        current_operation_id="current-operation" if parent == "operation" else None,
    )
    assert identity.source_run_id == "source"
    assert identity.source_node_id == "source-node"
    assert identity.source_state_id == (state.state_id if parent == "state" else None)
    assert identity.source_token_id == (state.token_id if parent == "state" else None)
    assert identity.source_operation_id == (operation.operation_id if parent == "operation" else None)
    factory.execution.record_verification_decision.assert_not_called()
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()


@pytest.mark.parametrize("parent", ["state", "operation"])
def test_source_parent_disappearing_after_preflight_refuses_identity(parent: str) -> None:
    factory, call = _archive()
    if parent == "operation":
        _calls(factory, replace(call, state_id=None, operation_id="source-operation"))
        factory.execution.get_operation.return_value = Operation(
            operation_id="source-operation",
            run_id="source",
            node_id="source-node",
            operation_type="runtime_preflight",
            started_at=_NOW,
            status="open",
        )
    session = _session(factory)
    factory.execution.get_node_state.return_value = None
    factory.execution.get_operation.return_value = None
    with pytest.raises(AuditIntegrityError, match=f"{parent} disappeared"):
        session.source_parent_identity(
            call_type=CallType.HTTP,
            current_state_id="current-state" if parent == "state" else None,
            current_operation_id="current-operation" if parent == "operation" else None,
        )
    factory.execution.record_verification_decision.assert_not_called()
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_source_parent_identity_requires_one_exact_parent(ambiguous: bool) -> None:
    factory, call = _archive()
    session = _session(factory)
    factory.execution.list_source_calls_for_current_parent.return_value = (
        [call, replace(call, call_id="other-call", state_id="other-state")] if ambiguous else []
    )
    with pytest.raises(AuditIntegrityError, match="parent is missing or ambiguous"):
        session.source_parent_identity(call_type=CallType.HTTP, current_state_id="current-state", current_operation_id=None)
    factory.execution.get_node_state.assert_not_called()
    factory.execution.record_verification_decision.assert_not_called()


@pytest.mark.parametrize("index", [0, None])
def test_exact_verification_settles_once_and_refuses_repeated_admission(index: int | None) -> None:
    factory, call = _archive()
    session = _session(factory, RunMode.VERIFY)
    assert _admit(session, index=index) == call.call_id
    with pytest.raises(AuditIntegrityError, match=r"already admitted|missing or ambiguous"):
        _admit(session, index=index)
    with pytest.raises(AuditIntegrityError, match="unsettled admissions"):
        session.assert_complete()
    assert _verify(session).is_match is True
    session.assert_complete()
    with pytest.raises(AuditIntegrityError, match="without pre-dispatch"):
        _verify(session)
    with pytest.raises(AuditIntegrityError, match=r"already consumed|missing or ambiguous"):
        _admit(session, index=index)
    factory.execution.record_verification_decision.assert_called_once_with(
        current_run_id="current",
        current_call_id="current-call",
        source_run_id="source",
        source_call_id=call.call_id,
        is_match=True,
        differences_json="{}",
        coordination_token=_CURRENT_TOKEN,
    )
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("changed", [False, True])
def test_verify_refuses_source_changed_after_admission_without_persisting_verdict(changed: bool) -> None:
    factory, call = _archive()
    session = _session(factory, RunMode.VERIFY)
    assert _admit(session) == call.call_id
    factory.execution.find_call_for_current_parent.return_value = replace(call, call_id="other-call") if changed else None
    with pytest.raises(AuditIntegrityError, match="changed after pre-dispatch"):
        _verify(session)
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()


@pytest.mark.parametrize("drift", ["none", "status", "error", "purged-response"])
def test_error_verification_distinguishes_exact_failure_from_live_drift(drift: str) -> None:
    factory, call = _archive(status=CallStatus.ERROR, error_json=canonical_json(_ERROR))
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.NEVER_STORED, data=None)
    session = _session(factory, RunMode.VERIFY)
    assert _admit(session) == call.call_id
    if drift == "purged-response":
        factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    live_status = CallStatus.SUCCESS if drift == "status" else CallStatus.ERROR
    live_error = {**_ERROR, "message": "different failure"} if drift == "error" else _ERROR
    decision = _verify(session, status=live_status, response=None, error=live_error)
    expected = {}
    if drift == "status":
        expected = {"status": {"source": "error", "current": "success"}}
    elif drift == "error":
        expected = {"error_hash": {"source": stable_hash(_ERROR), "current": stable_hash(live_error)}}
    elif drift == "purged-response":
        expected = {"source_response": "purged"}
    assert decision.differences == expected
    assert decision.is_match is (True if drift == "none" else None if drift == "purged-response" else False)
    recorded = factory.execution.record_verification_decision.call_args.kwargs
    assert recorded["source_call_id"] == call.call_id
    assert recorded["is_match"] is decision.is_match
    assert json.loads(recorded["differences_json"]) == expected
    if drift == "none":
        session.assert_complete()
    else:
        with pytest.raises(VerificationMismatchError, match="1 mismatches"):
            session.assert_complete()
    with pytest.raises(AuditIntegrityError, match="already consumed"):
        _admit(session)
    factory.execution.record_verification_decision.assert_called_once()
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("changed", ["purged", "divergent"])
def test_nested_transport_preflight_rechecks_retained_request(changed: str) -> None:
    factory, call = _archive()
    session = _session(factory, RunMode.VERIFY)
    assert (
        session.preflight_verify_request(
            call_type=CallType.HTTP, request_data=_REQUEST, current_state_id="current-state", current_operation_id=None
        )
        == call.call_id
    )
    factory.execution.get_call_request_data.return_value = (
        CallDataResult(state=CallDataState.PURGED, data=None)
        if changed == "purged"
        else CallDataResult(state=CallDataState.AVAILABLE, data={**_REQUEST, "url": "https://different.example/"})
    )
    with pytest.raises(AuditIntegrityError, match=r"unavailable|disagrees"):
        session.preflight_verify_request(
            call_type=CallType.HTTP, request_data=_REQUEST, current_state_id="current-state", current_operation_id=None
        )
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]


@pytest.mark.parametrize("missing", ["call", "request"])
def test_archived_request_refuses_disappearing_call_or_payload(missing: str) -> None:
    factory, call = _archive()
    session = _session(factory)
    if missing == "call":
        factory.execution.find_call_for_current_parent.return_value = None
    else:
        factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.PURGED, data=None)
    with pytest.raises(AuditIntegrityError, match=r"missing|unavailable"):
        session.archived_call_request(
            call_type=CallType.HTTP, current_state_id="current-state", current_operation_id=None, current_call_index=0
        )
    factory.execution.record_verification_decision.assert_not_called()
    assert factory.query.get_all_calls_for_run.return_value == [call]
