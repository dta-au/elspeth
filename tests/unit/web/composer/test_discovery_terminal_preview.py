"""Preview evidence retains normal authoring until the real request deadline."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.no_tool_policy import AssistantTextSegment, visible_message_segments
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import (
    _reply_only_messages,
    _reply_only_protocol_echo,
)
from elspeth.web.composer.state import OutputSpec, SourceSpec
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from elspeth.web.execution.schemas import ValidationResult
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.composer.test_service import _assert_checkpoint_reply, _composer_service_with_session, _execution_ready

_PROTOCOL_ECHO = "Historical tool protocol record (quoted data):\n" + json.dumps(
    {
        "role": "assistant",
        "content": "I have reviewed the pipeline.",
        "tool_calls": [
            {
                "id": "not-an-executed-call",
                "type": "function",
                "function": {"name": "request_interpretation_review", "arguments": "{}"},
            }
        ],
    }
)
_USER_QUOTED_JSON = 'The quoted user data {"tool_calls": [{"name": "example"}]} is not a pipeline command.'


def test_reply_only_history_preserves_attribution_and_echo_check_needs_internal_provenance() -> None:
    history = [
        {
            "role": "assistant",
            "content": "Previewing.",
            "tool_calls": [{"id": "real-preview", "type": "function", "function": {"name": "preview_pipeline", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "real-preview", "content": '{"success": true}'},
        {"role": "user", "content": _USER_QUOTED_JSON},
    ]
    projected = _reply_only_messages(history)
    assert all(row["role"] != "tool" and "tool_calls" not in row for row in projected)
    assert projected[0]["role"] == "assistant"
    assert projected[1]["role"] == "user"
    assert projected[0]["content"].endswith("[End historical tool evidence; this was not a current assistant reply.]")
    assert json.loads(projected[0]["content"].split("\n", 2)[1]) == history[0]
    assert json.loads(projected[1]["content"].split("\n", 2)[1]) == history[1]
    assert projected[2] == history[2]
    has_tool_protocol = any(row["role"] == "tool" or "tool_calls" in row for row in history)
    user_only_has_tool_protocol = any(row["role"] == "tool" or "tool_calls" in row for row in [history[2]])
    assert has_tool_protocol
    assert not user_only_has_tool_protocol
    assert _reply_only_protocol_echo(_PROTOCOL_ECHO, history_has_tool_protocol=has_tool_protocol, user_message="Explain the preview.")
    assert not _reply_only_protocol_echo(
        _PROTOCOL_ECHO, history_has_tool_protocol=user_only_has_tool_protocol, user_message="Explain the preview."
    )
    assert not _reply_only_protocol_echo(
        _PROTOCOL_ECHO, history_has_tool_protocol=has_tool_protocol, user_message="Please quote this log exactly:\n" + _PROTOCOL_ECHO
    )
    assert not _reply_only_protocol_echo(_USER_QUOTED_JSON, history_has_tool_protocol=has_tool_protocol, user_message=_USER_QUOTED_JSON)
    assert not _reply_only_protocol_echo(
        'The user wrote {"tool_calls": []}.', history_has_tool_protocol=has_tool_protocol, user_message="Explain."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_kind",
    [
        "text",
        "tools",
        "timeout",
        "expired",
        "advisor_block",
        "protocol_echo",
        "protocol_echo_advisor_block",
        "quoted_json",
        "quoted_log",
        "inspected_text",
    ],
)
async def test_preview_keeps_authoring_available_until_the_real_deadline(monkeypatch: pytest.MonkeyPatch, final_kind: str) -> None:
    settings = _make_settings(composer_max_discovery_turns=1)
    if final_kind == "inspected_text":
        settings = _make_settings(composer_max_discovery_turns=1, composer_model="anthropic/claude-sonnet-5-5")
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=settings)
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
    first_calls = [{"id": "preview", "name": "preview_pipeline", "arguments": {}}]
    if final_kind == "inspected_text":
        from elspeth.web.composer import tool_batch
        from tests.unit.web.composer.test_turn_effects import _page

        admitted, _content = _page(0, 6)
        execute = tool_batch.execute_tool

        def execute_with_page(tool_name, arguments, current_state, catalog, *args, **kwargs):
            if tool_name == "get_blob_content":
                return replace(admitted.result, updated_state=current_state)
            return execute(tool_name, arguments, current_state, catalog, *args, **kwargs)

        monkeypatch.setattr(tool_batch, "execute_tool", execute_with_page)
        first_calls.insert(0, {"id": "page", "name": "get_blob_content", "arguments": {"blob_id": "blob"}})
    replies = iter(
        [
            _make_llm_response(tool_calls=first_calls),
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

    async def provider(**kwargs: Any) -> Any:
        tools = kwargs.get("tools", [])
        advertised_tools.append(tools)
        provider_messages.append(kwargs["messages"])
        if len(advertised_tools) == 2:
            if final_kind == "timeout":
                raise TimeoutError
            if final_kind == "tools":
                return _make_llm_response(
                    tool_calls=[{"id": "after-preview", "name": "set_metadata", "arguments": {"patch": {"name": "updated_after_preview"}}}]
                )
            if final_kind in {"protocol_echo", "protocol_echo_advisor_block", "quoted_log"}:
                return _make_llm_response(content=_PROTOCOL_ECHO)
            if final_kind == "quoted_json":
                return _make_llm_response(content=_USER_QUOTED_JSON)
        if len(advertised_tools) > 2 and final_kind == "advisor_block":
            return _make_llm_response(content="The requested output needs review.")
        return next(replies)

    def preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
        return ValidationResult(is_valid=True, checks=[], errors=[], readiness=_execution_ready())

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(service._preflight, "runtime_preflight", preflight)
    monkeypatch.setattr(service, "_dispatch_tool_batch", dispatch)

    async def advisor(*args: object, **kwargs: Any) -> AdvisorCheckpointVerdict:
        advisor_phases.append(kwargs["phase"])
        if final_kind == "inspected_text":
            from elspeth.contracts.hashing import stable_hash

            review = kwargs["publication_review"]
            assert type(review) is AdvisorPublicationReview
            inspection = json.loads(review.payload_json)["inspection"]
            assert inspection["currently_available"][0]["ranges"] == [[0, 6]]
            assert inspection["current_candidate_call"][1] == stable_hash(provider_messages[1])
        if final_kind in {"advisor_block", "protocol_echo_advisor_block"}:
            review = kwargs["publication_review"]
            assert type(review) is AdvisorPublicationReview
            return AdvisorCheckpointVerdict(
                ok=True,
                blocking=True,
                findings_text="The requested output needs review.",
                publication_verdict="WITHHOLD",
                publication_binding=review.binding,
            )
        return await _clean_advisor_checkpoint(*args, **kwargs)

    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", advisor)
    before_deadline = service._call_llm_outcome_before_deadline

    async def expire_final_reply(*args: Any, **kwargs: Any) -> Any:
        if len(advertised_tools) == 1:
            args = (*args[:4], asyncio.get_running_loop().time() - 1, *args[5:])
        return await before_deadline(*args, **kwargs)

    if final_kind == "expired":
        monkeypatch.setattr(service, "_call_llm_outcome_before_deadline", expire_final_reply)

    user_message = "Preview the current pipeline and explain the result."
    if final_kind == "tools":
        user_message = "Preview the pipeline, then rename it to updated_after_preview and explain the result."
    if final_kind == "quoted_log":
        user_message += " Please quote this log exactly:\n" + _PROTOCOL_ECHO
    if final_kind in {"timeout", "expired"}:
        with pytest.raises(ComposerConvergenceError) as raised:
            await service.compose(user_message, [], state, session_id=session_id)
        assert raised.value.budget_exhausted == "timeout"
        assert raised.value.max_turns == 1
        assert state.metadata.name != "updated_after_preview"
    else:
        result = await service.compose(user_message, [], state, session_id=session_id)
        assert advisor_phases[-1] == "end"
        if final_kind == "tools":
            assert result.state.metadata.name == "updated_after_preview"
            assert state.metadata.name != "updated_after_preview"
            _assert_checkpoint_reply(result, "The pipeline preview passed.", count=1, changed=True)
        elif final_kind == "advisor_block":
            assert result.state == state
            assert result.message != "The pipeline preview passed."
            assert result.advisor_gate_decision is not None
        elif final_kind in {"protocol_echo", "protocol_echo_advisor_block"}:
            # This remains an ordinary authoring response. Quoted protocol
            # prose does not execute the tool it describes; the separate
            # reply-only projector still rejects unattributed protocol echo.
            assert result.state == state
            if final_kind == "protocol_echo_advisor_block":
                assert _PROTOCOL_ECHO not in result.message
                assert result.advisor_gate_decision is not None
            else:
                assert _PROTOCOL_ECHO in result.message
                assert "final reply is unavailable" not in result.message
                assert len(result.llm_calls) == 2
            assert all(call.status is ComposerLLMCallStatus.SUCCESS for call in result.llm_calls)
        elif final_kind == "quoted_json":
            _assert_checkpoint_reply(result, _USER_QUOTED_JSON, count=0)
            segments = visible_message_segments(content=result.message, raw_content=result.raw_assistant_content)
            assert type(segments[0]) is AssistantTextSegment
            assert _USER_QUOTED_JSON in segments[0].content
        elif final_kind == "quoted_log":
            assert _PROTOCOL_ECHO in result.message
            assert "final reply is unavailable" not in result.message
            segments = visible_message_segments(content=result.message, raw_content=result.raw_assistant_content)
            assert type(segments[0]) is AssistantTextSegment
            assert _PROTOCOL_ECHO in segments[0].content
        else:
            assert result.state == state
            _assert_checkpoint_reply(result, "The pipeline preview passed.", count=0)
    if final_kind in {"advisor_block", "protocol_echo_advisor_block"}:
        assert len(advertised_tools) >= 2
    else:
        assert len(advertised_tools) == (1 if final_kind == "expired" else 3 if final_kind == "tools" else 2)
    expected_batches = [("get_blob_content", "preview_pipeline")] if final_kind == "inspected_text" else [("preview_pipeline",)]
    if final_kind == "tools":
        expected_batches.append(("set_metadata",))
    assert dispatched_batches == expected_batches
    assert advertised_tools[0]
    if len(advertised_tools) >= 2:
        assert {"set_metadata", "get_pipeline_state", "handoff_to_user", "restore_parked_graph"} <= {
            tool["function"]["name"] for tool in advertised_tools[1]
        }
        assert any(message["role"] == "tool" for message in provider_messages[1])
        assert any("tool_calls" in message for message in provider_messages[1])
