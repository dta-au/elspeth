"""The call session refuses incomplete or divergent source evidence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.row_data import CallDataResult, CallDataState
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession


def _factory() -> tuple[Mock, SimpleNamespace]:
    factory = Mock()
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
    session = AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=RunMode.REPLAY)
    with pytest.raises(AuditIntegrityError, match="unconsumed"):
        session.finalize()

    evidence = session.replay_call(
        call_type=CallType.HTTP,
        request_data={"method": "GET", "url": "https://example.org/"},
        current_state_id="current-state",
        current_operation_id=None,
        current_call_index=0,
    )
    assert evidence.source_call_id == "source-call"
    session.finalize()
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
        AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=RunMode.REPLAY)
    factory.execution.find_call_for_current_parent.assert_not_called()


def test_verify_persists_mismatch_and_refuses_success() -> None:
    factory, _ = _factory()
    session = AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY)
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
        session.finalize()


def test_verify_refuses_unmatched_request_before_live_dispatch() -> None:
    factory, _ = _factory()
    factory.execution.find_call_for_current_parent.return_value = None
    factory.execution.list_source_calls_for_current_parent.return_value = []
    session = AuditedCallModeSession(factory, current_run_id="current", source_run_id="source", mode=RunMode.VERIFY)
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
