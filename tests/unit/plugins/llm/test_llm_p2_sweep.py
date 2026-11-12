"""Regression tests for the 20261111-plugins-review P2 LLM sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from elspeth.contracts import CallStatus
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.enums import RunMode
from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient, ContentPolicyError
from elspeth.plugins.transforms.llm.transform import LLMTransform
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


_RETRY_BUDGET_SECONDS = 30
_OPENROUTER_TIMEOUT_SECONDS = 45.0
_QUERY_FIELDS = [{"suffix": "score", "type": "integer"}]


def _connected_wait_timeout(config: dict[str, Any]) -> float:
    transform = LLMTransform(config)
    try:
        transform.connect_output(CollectorOutputPort(), max_pending=2)
        return transform.batch_wait_timeout
    finally:
        transform.close()


def _openrouter(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "provider": "openrouter",
        "api_key": "sk-test-key",
        "model": "anthropic/claude-3.5-sonnet",
        "prompt_template": "Analyze: {{ row.text }}",
        "schema": {"mode": "observed"},
        "required_input_fields": [],
        "max_capacity_retry_seconds": _RETRY_BUDGET_SECONDS,
        "timeout_seconds": _OPENROUTER_TIMEOUT_SECONDS,
    }
    config.update(overrides)
    return config


class TestFinding44BatchWaitCoversRetryBudget:
    """Sweep finding #44: the engine waiter must outlast the plugin's own retry budget.

    The plugin's retry clock starts inside the worker and is only checked
    between provider attempts, so the earliest structured ``retry_timeout``
    result lands at budget + one in-flight request. Sequential multi-query
    gives each later query at most one attempt after the deadline. The
    engine's wait must cover all of that, or a raw TimeoutError wins the race.
    """

    def test_single_query_wait_covers_budget_plus_request_timeout(self) -> None:
        wait = _connected_wait_timeout(_openrouter())

        assert wait > _RETRY_BUDGET_SECONDS + _OPENROUTER_TIMEOUT_SECONDS

    def test_sequential_multi_query_wait_covers_one_request_per_query(self) -> None:
        queries = {name: {"input_fields": {"text": "text"}, "output_fields": _QUERY_FIELDS} for name in ("q1", "q2", "q3")}
        wait = _connected_wait_timeout(_openrouter(queries=queries, pool_size=1))

        assert wait > _RETRY_BUDGET_SECONDS + 3 * _OPENROUTER_TIMEOUT_SECONDS

    def test_azure_wait_covers_the_sdk_request_timeout(self) -> None:
        import openai

        sdk_read_timeout = openai.DEFAULT_TIMEOUT.read
        assert sdk_read_timeout is not None
        wait = _connected_wait_timeout(
            {
                "provider": "azure",
                "deployment_name": "my-gpt4o-deployment",
                "endpoint": "https://my-resource.openai.azure.com",
                "api_key": "azure-api-key",
                "prompt_template": "{{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
                "max_capacity_retry_seconds": _RETRY_BUDGET_SECONDS,
            }
        )

        assert wait > _RETRY_BUDGET_SECONDS + sdk_read_timeout

    def test_bedrock_wait_covers_the_litellm_request_timeout(self) -> None:
        import litellm

        wait = _connected_wait_timeout(
            {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
                "prompt_template": "Classify: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
                "max_capacity_retry_seconds": _RETRY_BUDGET_SECONDS,
            }
        )

        assert wait > _RETRY_BUDGET_SECONDS + float(litellm.request_timeout)
