"""Rootless reply orchestration without a lexical pre-END recovery call.

The existing CLEAN fixture isolates these mechanical controls. Real END
WITHHOLD/repair/ADMIT and guarded physical transitions are covered separately
by the real-worker semantic-publication tests, without this fixture.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.web.composer import composition_completion as completion_module
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from tests.helpers.composer_sdk import ComposerSDKScript
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean as _stub_advisor_end_gate_clean
from tests.unit.web.composer.test_service import (
    ValidationResult,
    _composer_service_with_session,
    _empty_state,
    _insert_user_message,
    _make_raw_llm_response,
    _make_settings,
    _mock_catalog,
)

_REQUEST = (
    "I've got a handful of customer complaints - make up 6 realistic ones in a csv "
    "with an id and a complaint field, we'll be triaging them by urgency"
)
_FALSE_COMPLETION = "The six complaints are bound as the pipeline CSV source. Please confirm the urgency rubric."
_CLARIFICATION = "Which urgency categories should the pipeline use?"


@contextmanager
def _physical_provider(service: ComposerServiceImpl, *responses: object) -> Iterator[AsyncMock]:
    script = ComposerSDKScript(primary_model=service._settings.composer_model, primary=responses)
    with patch("litellm.acompletion", new=script):
        yield script.primary


def _assert_persisted_physical_accounting(service: ComposerServiceImpl, session_id: str, call_id: str):
    assert service._session_engine is not None
    with service._session_engine.connect() as conn:
        attempts = conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == session_id)).all()
        ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == session_id)).all()
        audit_rows = conn.execute(
            select(chat_messages_table.c.tool_calls).where(
                chat_messages_table.c.session_id == session_id,
                chat_messages_table.c.role == "audit",
                chat_messages_table.c.tool_calls.is_not(None),
            )
        ).all()
    calls = [item for row in audit_rows for item in row.tool_calls if item["_kind"] == "llm_call_audit"]
    assert len(attempts) == len(ledger) == len(calls) == 1
    attempt, usage, envelope = attempts[0], ledger[0], calls[0]
    assert attempt.settled_at is not None
    assert attempt.ledger_entry_id is not None
    assert attempt.ledger_entry_id == usage.entry_id
    assert attempt.identity_id == usage.identity_id
    assert attempt.session_id == usage.session_id == session_id
    assert attempt.source == usage.source == "composer"
    assert attempt.attempt_id == envelope["call"]["call_id"] == call_id
    return usage, envelope


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Build a pipeline that reads CSV and writes JSON.",
        "Please create the requested workflow.",
        "How does CSV parsing work?",
        "Do not change the graph; explain routing.",
        'What does "source:csv" mean?',
        "neutral context " * 400 + "Please describe the available inputs.",
    ],
    ids=["build", "workflow", "explanation", "revocation", "quoted-source", "long-context"],
)
async def test_persisted_empty_request_uses_the_common_provider_entry(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    assert service._session_engine is not None
    request_id = _insert_user_message(service._session_engine, sid, user_message)
    explanation = "Here is an explanation without changing the graph."
    gateway = service._provider_gateway
    response = ModelResponse(
        model=service._settings.composer_model,
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": explanation, "tool_calls": None}}],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
    )
    with (
        _physical_provider(service, response) as provider,
        patch.object(gateway, "_call_llm", wraps=gateway._call_llm) as common_entry,
    ):
        result = await service.compose(
            user_message,
            [],
            _empty_state(),
            session_id=sid,
            user_id="test-user",
            user_message_id=request_id,
        )

    # Every physical SDK call went through the real common provider entry.
    assert common_entry.call_count == provider.call_count == 1
    assert len(result.llm_calls) == 1
    call = result.llm_calls[0]
    assert call.call_id is not None
    assert (call.prompt_tokens, call.completion_tokens, call.total_tokens) == (10, 5, 15)
    assert call.provider_cost == 0.01
    assert call.provider_cost_source == "response_usage.cost"
    usage, envelope = _assert_persisted_physical_accounting(service, sid, call.call_id)
    assert (usage.prompt_tokens, usage.completion_tokens) == (10, 5)
    assert envelope == llm_call_audit_envelope(call)
    assert result.message.startswith(explanation)
    assert result.state.version == 1
    assert not result.state.sources
    assert not result.state.nodes
    assert not result.state.outputs
    assert not result.tool_invocations
    assert result.graph_recovery_intent is None
    messages, tools = provider.call_args.kwargs["messages"], provider.call_args.kwargs["tools"]
    assert any(message["role"] == "user" and message["content"] == user_message for message in messages)
    tool_names = {tool["function"]["name"] for tool in tools}
    assert "set_pipeline" in tool_names
    assert "emit_pipeline_proposal" not in tool_names


@pytest.mark.asyncio
async def test_rootless_tool_execution_is_preserved_without_lexical_retry() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    tool_reply = _make_raw_llm_response(
        content="CLARIFY: Which categories should the pipeline use?",
        tool_calls=[
            {
                "id": "source-call",
                "name": "set_source",
                "arguments": {
                    "plugin": "csv",
                    "on_success": "t1",
                    "options": {"path": f"/data/blobs/{sid}/data.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "quarantine",
                },
            }
        ],
    )
    with (
        _physical_provider(service, tool_reply, _make_raw_llm_response(content="The source is now configured.")) as completion,
        patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
    ):
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 2
    assert result.repair_turns_used == 0
    assert result.state.sources["source"].plugin == "csv"
    assert any(call.tool_name == "set_source" and call.status.value == "success" for call in result.tool_invocations)


@pytest.mark.asyncio
async def test_truthful_rootless_reply_does_not_spend_a_generic_primary_repair() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    truthful = "No source has been configured. Please supply the CSV file."
    with _physical_provider(service, _make_raw_llm_response(content=truthful)) as completion:
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0
    assert not result.tool_invocations
    assert not result.state.sources
    assert result.message.startswith(truthful)
    assert "pipeline is still empty" not in result.message
    assert result.runtime_preflight is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Do not build anything; explain how CSV parsing works.",
        "Are you able to run pipelines yourself?",
        "How do I route rows to two different files?",
        "I make heavy use of CSV files.",
        'What does "build a pipeline" mean?',
    ],
)
async def test_zero_attempt_explanation_and_revocation_do_not_spend_lexical_repair(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    with _physical_provider(service, _make_raw_llm_response(content="I can explain that without changing anything.")) as completion:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0
    assert not result.tool_invocations
    assert not result.state.sources
    assert result.runtime_preflight is None
    assert "did not complete a valid build" not in result.message
    assert "pipeline is still empty" not in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "repair_budget"),
    [("Hello, what can you do?", 2), (_REQUEST, 0)],
    ids=["greeting", "disabled-repairs"],
)
async def test_ineligible_request_does_not_add_a_call(user_message: str, repair_budget: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion_module, "_MAX_REPAIR_TURNS", repair_budget)
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    with _physical_provider(service, _make_raw_llm_response(content="Here is my response.")) as completion:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0
    assert not result.tool_invocations


@pytest.mark.asyncio
async def test_expired_deadline_does_not_start_rootless_retry() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    terminate = service._completion._try_terminate_no_tools

    async def terminate_after_deadline(**kwargs: Any) -> Any:
        kwargs["deadline"] = 0.0
        return await terminate(**kwargs)

    with (
        _physical_provider(service, _make_raw_llm_response(content=_FALSE_COMPLETION)) as completion,
        patch.object(service._completion, "_try_terminate_no_tools", side_effect=terminate_after_deadline),
    ):
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_budget", [0, 2])
async def test_marked_clarification_preserves_question_without_build_failure(repair_budget: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion_module, "_MAX_REPAIR_TURNS", repair_budget)
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    replies = [_make_raw_llm_response(content="CLARIFY: " + _CLARIFICATION)]
    with _physical_provider(service, *replies) as completion:
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == len(replies)
    assert result.raw_assistant_content == _CLARIFICATION
    assert result.message.startswith(_CLARIFICATION)
    assert "No pipeline has been built yet" in result.message
    assert "did not complete a valid build" not in result.message
    assert "CLARIFY:" not in result.message
    assert result.runtime_preflight is None
    assert result.repair_turns_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prose", "notice"),
    [
        (_FALSE_COMPLETION, "No pipeline has been built yet"),
        ("I just configured the source. Which categories?", "No pipeline has been built yet"),
    ],
)
async def test_marked_false_completion_retains_authoritative_disclosure(prose: str, notice: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    with _physical_provider(service, _make_raw_llm_response(content="CLARIFY: " + prose)) as completion:
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.raw_assistant_content == prose
    assert notice in result.message
    assert result.message.count("[ELSPETH-SYSTEM]") == 1
    assert result.runtime_preflight is None


@pytest.mark.asyncio
async def test_marked_clarification_at_expired_deadline_keeps_normal_finalization() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    terminate = service._completion._try_terminate_no_tools

    async def terminate_after_deadline(**kwargs: Any) -> Any:
        kwargs["deadline"] = 0.0
        return await terminate(**kwargs)

    with (
        _physical_provider(service, _make_raw_llm_response(content="CLARIFY: " + _CLARIFICATION)) as completion,
        patch.object(service._completion, "_try_terminate_no_tools", side_effect=terminate_after_deadline),
    ):
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.runtime_preflight is None
    assert "No pipeline has been built yet" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["get_pipeline_state", "set_pipeline"])
async def test_marked_clarification_after_tool_preserves_receipt_policy(tool_name: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    replies = [
        _make_raw_llm_response(tool_calls=[{"id": "before-question", "name": tool_name, "arguments": {}}]),
        _make_raw_llm_response(content="CLARIFY: " + _CLARIFICATION),
    ]
    with _physical_provider(service, *replies) as completion:
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert result.tool_invocations[0].tool_name == tool_name
    if tool_name == "get_pipeline_state":
        assert completion.call_count == 2
        assert "No pipeline has been built yet" in result.message
        assert result.runtime_preflight is None
    else:
        assert completion.call_count == 2
        assert "set_pipeline failed before mutation" in result.message
        assert result.runtime_preflight is not None and not result.runtime_preflight.is_valid
        assert "No pipeline has been built yet" not in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Build a pipeline that reads CSV and writes JSON.",
        "Please assemble the requested workflow.",
        'Explain the example "build a pipeline" without changing it.',
        "Do not build anything; explain the review error.",
        "Read the complete instruction before replying. " + "neutral detail " * 400,
    ],
)
async def test_pre_state_review_error_does_not_inject_a_host_tool_sequence(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    refusal_reply = "The interpretation review could not be staged; you can ask me to retry."
    replies = [
        _make_raw_llm_response(
            tool_calls=[
                {
                    "id": "review-before-state",
                    "name": "request_interpretation_review",
                    "arguments": {
                        "affected_node_id": "rank",
                        "kind": "vague_term",
                        "user_term": "urgency",
                    },
                }
            ]
        ),
        _make_raw_llm_response(content=refusal_reply),
    ]
    with _physical_provider(service, *replies) as provider:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert provider.call_count == 2
    assert result.repair_turns_used == 0
    assert [entry.tool_name for entry in result.tool_invocations] == ["request_interpretation_review"]
    assert result.tool_invocations[0].status.value == "arg_error"
    assert result.tool_invocations[0].error_message
    assert not result.state.sources
    assert not result.state.nodes
    assert result.message.startswith(refusal_reply)
    sent_text = "\n".join(
        message["content"]
        for call in provider.call_args_list
        for message in call.kwargs["messages"]
        if isinstance(message.get("content"), str)
    )
    assert "First call set_pipeline or another state-staging tool" not in sent_text
    assert "Do not reply to the user yet" not in sent_text


@pytest.mark.asyncio
async def test_persisted_empty_request_missing_cost_refuses_at_the_physical_boundary() -> None:
    user_message = "Build a pipeline that reads CSV and writes JSON."
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    assert service._session_engine is not None
    request_id = _insert_user_message(service._session_engine, sid, user_message)
    gateway = service._provider_gateway
    with (
        _physical_provider(service, _make_raw_llm_response(content="Here is an explanation without changing the graph.")) as provider,
        patch.object(gateway, "_call_llm", wraps=gateway._call_llm) as common_entry,
        pytest.raises(CommonProviderCostRefused) as caught,
    ):
        await service.compose(
            user_message,
            [],
            _empty_state(),
            session_id=sid,
            user_id="test-user",
            user_message_id=request_id,
        )
    assert caught.value.code == "COST_UNAVAILABLE"
    assert common_entry.call_count == provider.call_count == 1
    assert caught.value.call_id is not None
    usage, envelope = _assert_persisted_physical_accounting(service, sid, caught.value.call_id)
    call = envelope["call"]
    assert call["status"] == "success"
    assert call["prompt_tokens"] is call["completion_tokens"] is call["total_tokens"] is None
    assert call["provider_cost"] is None
    assert call["provider_cost_source"] == "not_available"
    assert usage.prompt_tokens is usage.completion_tokens is None
    messages = provider.call_args.kwargs["messages"]
    assert any(message["role"] == "user" and message["content"] == user_message for message in messages)
