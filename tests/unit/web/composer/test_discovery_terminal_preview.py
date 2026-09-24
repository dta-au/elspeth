"""A final verified preview may spend the discovery budget before replying."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from elspeth.web.composer._compose_loop_carriers import _ToolOutcome
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import (
    AdvisorCheckpointVerdict,
    _admit_composer_llm_completion,
    _MalformedLLMResponseError,
    _tool_batch_ends_with_valid_current_preview,
)
from elspeth.web.composer.state import OutputSpec, SourceSpec, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.execution.schemas import ValidationResult
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.composer.test_service import _composer_service_with_session, _execution_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("final_kind", ["text", "tools", "timeout", "expired", "advisor_block"])
async def test_last_discovery_preview_gets_one_provider_reply(monkeypatch: pytest.MonkeyPatch, final_kind: str) -> None:
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings(composer_max_discovery_turns=1))
    state = (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="result",
                options={"path": "/data/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        .with_output(
            OutputSpec(
                name="result",
                plugin="csv",
                options={"path": "/data/result.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    )
    replies = iter(
        [
            _make_llm_response(tool_calls=[{"id": "preview", "name": "preview_pipeline", "arguments": {}}]),
            _make_llm_response(content="The pipeline preview passed."),
        ]
    )
    advertised_tools: list[list[dict[str, Any]]] = []
    provider_messages: list[list[dict[str, Any]]] = []
    advisor_phases: list[str] = []
    dispatched_batches: list[tuple[str, ...]] = []
    original_dispatch = service._dispatch_tool_batch

    async def dispatch(*args: Any, **kwargs: Any) -> Any:
        dispatched_batches.append(tuple(call.function.name for call in kwargs["call_model"].completion.tool_batch.calls))
        return await original_dispatch(*args, **kwargs)

    async def provider(_messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        advertised_tools.append(tools)
        provider_messages.append(_messages)
        if len(advertised_tools) == 2:
            if final_kind == "timeout":
                raise TimeoutError
            if final_kind == "tools":
                return _make_llm_response(
                    tool_calls=[{"id": "forbidden", "name": "set_metadata", "arguments": {"patch": {"name": "must not apply"}}}]
                )
        return next(replies)

    def preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
        return ValidationResult(is_valid=True, checks=[], errors=[], readiness=_execution_ready())

    monkeypatch.setattr(service, "_call_llm", provider)
    monkeypatch.setattr(service, "_runtime_preflight", preflight)
    monkeypatch.setattr(service, "_dispatch_tool_batch", dispatch)

    async def advisor(*args: object, **kwargs: Any) -> AdvisorCheckpointVerdict:
        advisor_phases.append(kwargs["phase"])
        if final_kind == "advisor_block":
            return AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="The requested output needs review.")
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr(service, "_run_advisor_checkpoint", advisor)
    before_deadline = service._call_llm_before_deadline

    async def expire_final_reply(*args: Any, **kwargs: Any) -> Any:
        if len(advertised_tools) == 1:
            args = (*args[:4], asyncio.get_running_loop().time() - 1, *args[5:])
        return await before_deadline(*args, **kwargs)

    if final_kind == "expired":
        monkeypatch.setattr(service, "_call_llm_before_deadline", expire_final_reply)

    if final_kind == "tools":
        with pytest.raises(_MalformedLLMResponseError, match="Reply-only completion must contain text and no tool calls"):
            await service.compose("Preview the current pipeline and explain the result.", [], state, session_id=session_id)
        assert state.metadata.name != "must not apply"
    elif final_kind in {"timeout", "expired"}:
        with pytest.raises(ComposerConvergenceError) as raised:
            await service.compose("Preview the current pipeline and explain the result.", [], state, session_id=session_id)
        assert raised.value.budget_exhausted == "timeout"
        assert raised.value.max_turns == 1
        assert state.metadata.name != "must not apply"
    else:
        result = await service.compose("Preview the current pipeline and explain the result.", [], state, session_id=session_id)
        assert result.state == state
        assert advisor_phases == ["end"]
        if final_kind == "advisor_block":
            assert result.message != "The pipeline preview passed."
            assert result.advisor_gate_decision is not None
        else:
            assert result.message == "The pipeline preview passed."
    assert len(advertised_tools) == (1 if final_kind == "expired" else 2)
    assert dispatched_batches == [("preview_pipeline",)]
    assert advertised_tools[0]
    if len(advertised_tools) == 2:
        assert advertised_tools[1] == []
        assert all(message["role"] != "tool" and "tool_calls" not in message for message in provider_messages[1])
        assert any("Historical tool protocol record" in message["content"] for message in provider_messages[1])


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "empty_batch",
        "tool_error",
        "failed_result",
        "invalid_stage1",
        "missing_preflight",
        "invalid_preflight",
        "stale",
        "later_read",
        "prior_error",
    ],
)
def test_terminal_preview_requires_current_successful_evidence(defect: str) -> None:
    state = _empty_state()
    call = _admit_composer_llm_completion(
        _make_llm_response(tool_calls=[{"id": "preview", "name": "preview_pipeline", "arguments": {}}])
    ).tool_batch.calls[0]
    preflight = ValidationResult(is_valid=True, checks=[], errors=[], readiness=_execution_ready())
    result = ToolResult(
        success=True,
        updated_state=state,
        validation=ValidationSummary(is_valid=True, errors=()),
        affected_nodes=(),
        runtime_preflight=preflight,
    )
    outcome = _ToolOutcome(
        call=call,
        response=result,
        error_class=None,
        error_category=None,
        error_message=None,
        pre_version=state.version,
        post_version=state.version,
    )
    if defect == "tool_error":
        outcome = replace(outcome, error_class="ToolArgumentError", response=None)
    elif defect == "failed_result":
        outcome = replace(outcome, response=replace(result, success=False))
    elif defect == "invalid_stage1":
        outcome = replace(outcome, response=replace(result, validation=ValidationSummary(is_valid=False, errors=())))
    elif defect == "missing_preflight":
        outcome = replace(outcome, response=replace(result, runtime_preflight=None))
    elif defect == "invalid_preflight":
        outcome = replace(outcome, response=replace(result, runtime_preflight=preflight.model_copy(update={"is_valid": False})))
    elif defect == "stale":
        outcome = replace(outcome, response=replace(result, updated_state=replace(state, version=state.version + 1)))
    outcomes = (outcome,)
    if defect == "empty_batch":
        outcomes = ()
    elif defect == "later_read":
        read = _admit_composer_llm_completion(
            _make_llm_response(tool_calls=[{"id": "read", "name": "get_pipeline_state", "arguments": {}}])
        ).tool_batch.calls[0]
        outcomes = (outcome, replace(outcome, call=read))
    elif defect == "prior_error":
        outcomes = (replace(outcome, response=None, error_class="ToolArgumentError"), outcome)
    assert _tool_batch_ends_with_valid_current_preview(outcomes, state) is (defect == "none")
