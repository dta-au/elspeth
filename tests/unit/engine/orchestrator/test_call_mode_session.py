"""The call session refuses incomplete or divergent source evidence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataResult, CallDataState
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession

_CURRENT_TOKEN = CoordinationToken(run_id="current", worker_id="worker:current:test", leader_epoch=1)


def _factory() -> tuple[Mock, SimpleNamespace]:
    factory = Mock(spec=RecorderFactory)
    call = SimpleNamespace(
        call_id="source-call",
        state_id="source-state",
        operation_id=None,
        call_type=CallType.HTTP,
        call_index=0,
        status=CallStatus.SUCCESS,
        error_json=None,
        latency_ms=12.0,
    )
    factory.query.get_all_calls_for_run.return_value = [call]
    factory.execution.get_all_operation_calls_for_run.return_value = []
    factory.execution.find_call_for_current_parent.return_value = call
    factory.execution.get_call_request_data.return_value = CallDataResult(
        state=CallDataState.AVAILABLE, data={"method": "GET", "url": "https://example.org/"}
    )
    factory.execution.get_call_response_data.return_value = CallDataResult(
        state=CallDataState.AVAILABLE, data={"status_code": 200, "transport": {"body_b64": ""}}
    )
    return factory, call


def test_replay_consumes_exact_source_call_and_fails_on_missing_occurrence() -> None:
    factory, _ = _factory()
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.REPLAY, coordination_token=_CURRENT_TOKEN
    )
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()

    evidence = session.replay_call(
        call_type=CallType.HTTP,
        request_data={"method": "GET", "url": "https://example.org/"},
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
    )
    assert evidence.source_call_id == "source-call"
    session.assert_complete()
    with pytest.raises(AuditIntegrityError, match="already consumed"):
        session.replay_call(
            call_type=CallType.HTTP,
            request_data={"method": "GET", "url": "https://example.org/"},
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=0,
        )
    lookup = factory.execution.find_call_for_current_parent.call_args.kwargs
    assert lookup["source_run_id"] == "source"
    assert lookup["current_state_id"] == "current-state"
    assert lookup["current_call_index"] == 0


def test_missing_archived_response_refuses_before_replay() -> None:
    factory, _ = _factory()
    factory.execution.get_call_response_data.return_value = CallDataResult(state=CallDataState.HASH_ONLY, data=None)
    with pytest.raises(AuditIntegrityError, match="complete response archive"):
        AuditedCallModeSession(
            factory, current_run_id="current", source_run_id="source", mode=RunMode.REPLAY, coordination_token=_CURRENT_TOKEN
        )
    factory.execution.find_call_for_current_parent.assert_not_called()


def test_verify_persists_mismatch_and_refuses_success() -> None:
    factory, _ = _factory()
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    assert (
        session.admit_verify_call(
            call_type=CallType.HTTP,
            request_data={"method": "GET", "url": "https://example.org/"},
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=0,
        )
        == "source-call"
    )
    decision = session.verify_call(
        call_type=CallType.HTTP,
        request_data={"method": "GET", "url": "https://example.org/"},
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
        current_call_id="current-call",
        live_status=CallStatus.SUCCESS,
        live_response_data={"status_code": 503},
        live_error_data=None,
    )
    assert decision.is_match is False
    recorded = factory.execution.record_verification_decision.call_args.kwargs
    assert recorded["current_call_id"] == "current-call"
    assert recorded["source_call_id"] == "source-call"
    assert recorded["is_match"] is False
    with pytest.raises(AuditIntegrityError, match="failed decisions"):
        session.assert_complete()


def test_verify_refuses_unmatched_request_before_live_dispatch() -> None:
    factory, _ = _factory()
    factory.execution.find_call_for_current_parent.return_value = None
    factory.execution.list_source_calls_for_current_parent.return_value = []
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    with pytest.raises(AuditIntegrityError, match="No exact source"):
        session.admit_verify_call(
            call_type=CallType.HTTP,
            request_data={"method": "GET", "url": "https://unrecorded.example/"},
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=0,
        )
    with pytest.raises(AuditIntegrityError, match="missing or ambiguous"):
        session.preflight_verify_request(
            call_type=CallType.LLM,
            request_data={"model": "unrecorded"},
            current_state_id="current-state",
            current_operation_id=None,
        )
    factory.execution.record_verification_decision.assert_not_called()


def test_managed_identity_verify_accepts_rotating_auth_after_non_auth_preflight() -> None:
    factory, call = _factory()
    archived = {
        "method": "GET",
        "url": "https://example.org/",
        "resolved_ip": "93.184.216.34",
        "headers": {"Host": "example.org", "Authorization": f"<fingerprint:{'0' * 64}>"},
        "params": None,
    }
    current = {
        **archived,
        "headers": {"Host": "example.org", "Authorization": f"<fingerprint:{'1' * 64}>"},
    }
    call.request_hash = "unused-for-semantic-match"
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    partial = {key: value for key, value in current.items() if key != "resolved_ip"}
    partial["headers"] = {"Host": "example.org"}
    evidence = session.preflight_verify_http_managed_identity(
        request_data=partial, current_state_id="current-state", current_operation_id=None
    )
    assert evidence.source_call_id == "source-call"
    assert (
        session.admit_verify_http_managed_identity(
            request_data=current,
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=0,
            source_call_id=evidence.source_call_id,
        )
        == "source-call"
    )
    decision = session.verify_call(
        call_type=CallType.HTTP,
        request_data=current,
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
        current_call_id="current-call",
        live_status=CallStatus.SUCCESS,
        live_response_data={"status_code": 200, "transport": {"body_b64": ""}},
        live_error_data=None,
    )
    assert decision.is_match is True
    session.assert_complete()
    assert factory.execution.find_call_for_current_parent.call_args.kwargs["request_hash"] is None


def test_managed_identity_verify_refuses_missing_auth_and_ambiguous_parent() -> None:
    factory, call = _factory()
    archived = {
        "method": "GET",
        "url": "https://example.org/",
        "resolved_ip": "93.184.216.34",
        "headers": {"Host": "example.org"},
        "params": None,
    }
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    partial = {key: value for key, value in archived.items() if key != "resolved_ip"}
    with pytest.raises(AuditIntegrityError, match="fingerprinted Authorization"):
        session.preflight_verify_http_managed_identity(request_data=partial, current_state_id="current-state", current_operation_id=None)

    archived["headers"] = {"Host": "example.org", "Authorization": f"<fingerprint:{'0' * 64}>"}
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    factory.execution.list_source_calls_for_current_parent.return_value = [call, SimpleNamespace(**vars(call))]
    with pytest.raises(AuditIntegrityError, match="missing or ambiguous"):
        session.preflight_verify_http_managed_identity(request_data=partial, current_state_id="current-state", current_operation_id=None)


@pytest.mark.parametrize("managed_identity", [False, True])
@pytest.mark.parametrize("archived_ip", [None, "not-an-ip"])
def test_http_verify_refuses_missing_or_invalid_archived_dns_pin_before_egress(managed_identity: bool, archived_ip: str | None) -> None:
    factory, call = _factory()
    archived: dict[str, object] = {
        "method": "GET",
        "url": "https://example.org/",
        "headers": {"Host": "example.org", "Authorization": f"<fingerprint:{'0' * 64}>"},
        "params": None,
    }
    if archived_ip is not None:
        archived["resolved_ip"] = archived_ip
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    request = {key: value for key, value in archived.items() if key != "resolved_ip"}
    if managed_identity:
        request["headers"] = {"Host": "example.org"}
        preflight = session.preflight_verify_http_managed_identity
    else:
        preflight = session.preflight_verify_http_request
    with pytest.raises(AuditIntegrityError, match="archived DNS pin"):
        preflight(request_data=request, current_state_id="current-state", current_operation_id=None)


def test_source_load_managed_identity_requires_pre_token_admission_and_verdict() -> None:
    factory, call = _factory()
    call.state_id = None
    call.operation_id = "source-operation"
    factory.query.get_all_calls_for_run.return_value = []
    factory.execution.get_all_operation_calls_for_run.return_value = [call]
    factory.execution.get_operation.return_value = SimpleNamespace(operation_type="source_load")
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    archived = {
        "method": "GET",
        "url": "https://example.org/source/page",
        "headers": {"Authorization": f"<fingerprint:{'0' * 64}>", "Accept": "application/json"},
    }
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.assert_complete()
    partial = {**archived, "headers": {"Accept": "application/json"}}
    evidence = session.preflight_verify_operation_http_managed_identity(request_data=partial, current_operation_id="current-operation")
    current = {**archived, "headers": {"Authorization": f"<fingerprint:{'1' * 64}>", "Accept": "application/json"}}
    assert (
        session.admit_verify_operation_http_managed_identity(
            request_data=current,
            current_operation_id="current-operation",
            current_call_index=0,
            source_call_id=evidence.source_call_id,
        )
        == "source-call"
    )
    decision = session.verify_call(
        call_type=CallType.HTTP,
        request_data=current,
        current_state_id=None,
        current_operation_id="current-operation",
        current_call_index=0,
        current_call_id="current-call",
        live_status=CallStatus.SUCCESS,
        live_response_data={"status_code": 200, "transport": {"body_b64": ""}},
        live_error_data=None,
    )
    assert decision.is_match is True
    session.assert_complete()


def test_source_load_managed_identity_refuses_missing_auth_before_token() -> None:
    factory, call = _factory()
    call.state_id = None
    call.operation_id = "source-operation"
    factory.query.get_all_calls_for_run.return_value = []
    factory.execution.get_all_operation_calls_for_run.return_value = [call]
    factory.execution.get_operation.return_value = SimpleNamespace(operation_type="source_load")
    factory.execution.list_source_calls_for_current_parent.return_value = [call]
    archived = {"method": "GET", "url": "https://example.org/source/page", "headers": {"Accept": "application/json"}}
    factory.execution.get_call_request_data.return_value = CallDataResult(state=CallDataState.AVAILABLE, data=archived)
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY, coordination_token=_CURRENT_TOKEN
    )
    with pytest.raises(AuditIntegrityError, match="fingerprinted Authorization"):
        session.preflight_verify_operation_http_managed_identity(request_data=archived, current_operation_id="current-operation")
