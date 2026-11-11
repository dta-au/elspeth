"""Regression tests for the 20261111-plugins-review P2 LLM sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from elspeth.contracts import CallStatus
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.enums import RunMode
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient, ContentPolicyError
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.clients.test_audited_llm_client import (
    FakeExecutionRepository,
    FakeOpenAIClient,
    provider_response,
)


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    try:
        call()
    except Exception as exc:
        return exc
    return None


def _content_filtered_source_call() -> dict[str, Any]:
    """Run the LIVE null-content path once and return what it recorded."""
    execution = FakeExecutionRepository()
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state_source",
        run_id="run_source",
        telemetry_emit=lambda event: None,
        underlying_client=FakeOpenAIClient(
            response=provider_response(
                content=None,
                completion_tokens=0,
                finish_reason="content_filter",
                raw_response={"id": "resp_null", "choices": [{"finish_reason": "content_filter"}]},
            )
        ),
    )
    live_error = _raised_by(lambda: client.chat_completion(model="gpt-4", messages=[ChatMessage(role="user", content="Hello")]))
    assert type(live_error) is ContentPolicyError
    execution.assert_recorded_once()
    return execution.last_record_call_kwargs


class TestFinding24ReplayContentFilteredCompletion:
    """Sweep finding #24: replay of a recorded content-filtered completion."""

    def test_replay_reproduces_content_policy_error_with_recorded_response(self) -> None:
        source = _content_filtered_source_call()
        assert source["status"] is CallStatus.ERROR
        source_response = source["response_data"].to_dict()
        source_error = source["error"].to_dict()
        assert source_error["category"] == "content_policy"

        class ReplaySession:
            mode = RunMode.REPLAY

            def replay_call(self, **kwargs: Any) -> SimpleNamespace:
                return SimpleNamespace(
                    source_call_id="source-content-filter-call",
                    status=CallStatus.ERROR,
                    response_data=source_response,
                    error_data=source_error,
                    latency_ms=4.0,
                )

        sdk = FakeOpenAIClient(exception=AssertionError("SDK must not be called in replay"))
        execution = FakeExecutionRepository()
        client = AuditedLLMClient(
            **mock_audit_authority(),
            execution=execution,
            state_id="state_replay",
            run_id="run_replay",
            telemetry_emit=lambda event: None,
            underlying_client=sdk,
            call_mode_session=ReplaySession(),
        )

        raised = _raised_by(lambda: client.chat_completion(model="gpt-4", messages=[ChatMessage(role="user", content="Hello")]))

        assert type(raised) is ContentPolicyError, f"replay raised {type(raised).__name__}: {raised}"
        assert str(raised) == source_error["message"]
        assert sdk.create_calls == []
        execution.assert_recorded_once()
        replayed = execution.last_record_call_kwargs
        assert replayed["status"] is CallStatus.ERROR
        assert replayed["source_call_id"] == "source-content-filter-call"
        assert replayed["error"].to_dict() == source_error
        assert replayed["response_data"].to_dict() == source_response
