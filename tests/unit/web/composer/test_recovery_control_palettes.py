"""Independent recovery protocol checks using scripted, offline providers."""

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import service as service_module
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.protocol import ComposerServiceError
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.state import CompositionState, OutputSpec, SourceSpec
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import (
    _build_composer,
    _seed_session_and_state,
    _state_with_llm_node,
)
from tests.unit.web.composer.test_service import (
    _composer_service_with_session,
    _empty_state,
    _execution_ready,
    _make_llm_response,
    _make_raw_llm_response,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.sessions.test_token_usage_adapters import _call as _ledger_call
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service

_CONTROL_NAMES = {"handoff_to_user", "restore_parked_graph"}
_QUESTION = "The output instructions disagree. Which report should we create?"
_CONTROLS = [
    pytest.param("handoff_to_user", {"action": "park", "message": _QUESTION}, "park", id="park"),
    pytest.param(
        "handoff_to_user",
        {"action": "reset_to_turn_start", "message": _QUESTION},
        "reset_to_turn_start",
        id="turn-start",
    ),
    pytest.param(
        "handoff_to_user",
        {"action": "reset_to_safe_checkpoint", "message": _QUESTION},
        "reset_to_safe_checkpoint",
        id="safe-checkpoint",
    ),
    pytest.param(
        "restore_parked_graph",
        {"park_id": "previous-draft", "message": _QUESTION},
        "restore_parked_graph",
        id="restore",
    ),
]


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
def test_recovery_palettes_preserve_explicit_target_choices(dialect: ToolContractDialect) -> None:
    tools = recovery_tool_definitions(dialect)
    definitions = {tool["function"]["name"]: tool["function"] for tool in tools}
    assert set(definitions) == _CONTROL_NAMES
    handoff = definitions["handoff_to_user"]
    assert set(handoff["parameters"]["properties"]["action"]["enum"]) == {
        "park",
        "reset_to_turn_start",
        "reset_to_safe_checkpoint",
    }
    for definition in definitions.values():
        Draft202012Validator.check_schema(definition["parameters"])
        assert definition["parameters"]["additionalProperties"] is False
        assert set(definition["parameters"]["required"]) == set(definition["parameters"]["properties"])
        if dialect is ToolContractDialect.OPENAI_STRICT:
            assert definition["strict"] is True
        else:
            assert "strict" not in definition


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,action", _CONTROLS)
async def test_primary_control_exits_before_dispatch_repair_and_advisor(name: str, arguments: dict[str, str], action: str) -> None:
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    state = _empty_state()
    response = _make_raw_llm_response(tool_calls=[{"id": "handoff-1", "name": name, "arguments": arguments}])
    with (
        patch("litellm.acompletion", side_effect=[response]) as provider,
        patch.object(service, "_dispatch_tool_batch", side_effect=AssertionError("Recovery reached ordinary dispatch")) as dispatch,
        patch.object(
            service._completion, "_try_terminate_no_tools", side_effect=AssertionError("Recovery reached no-tool repair")
        ) as finalize,
        patch.object(
            service._advisor_checkpoint, "_run_advisor_checkpoint", side_effect=AssertionError("Recovery reached advisor")
        ) as advisor,
    ):
        result = await service.compose(
            "Build the report from the conflicting instructions; I cannot tell which output is intended.",
            [],
            state,
            session_id=session_id,
        )
    assert provider.call_count == 1
    advertised = {tool["function"]["name"] for tool in provider.call_args.kwargs["tools"]}
    assert advertised >= _CONTROL_NAMES, "Provider palette omitted recovery controls"
    dispatch.assert_not_called()
    finalize.assert_not_called()
    advisor.assert_not_called()
    assert result.graph_recovery_intent is not None
    assert result.graph_recovery_intent.action == action
    assert result.graph_recovery_intent.message == _QUESTION
    assert result.graph_recovery_intent.tool_call_id == "handoff-1"
    assert result.state is state
    assert result.runtime_preflight is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["control", "prose", "mixed_batch"])
async def test_malformed_head_entry_never_hydrates_or_enters_authoring(tmp_path: Path, reply_kind: str) -> None:
    engine, authority, sessions = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Malformed graph recovery", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    operation = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, operation, invocation_id=str(uuid4()))
    )
    required_work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    owner = ProviderInvocationOwner(service=sessions, required_work=required_work)
    service = _build_composer(tmp_path, sessions)
    state_id = str(uuid4())
    context = GraphRecoveryContext(state_id, 4, state_id, 4, True)
    calls = [
        {
            "id": "recover-unreadable",
            "name": "handoff_to_user",
            "arguments": {"action": "reset_to_safe_checkpoint", "message": _QUESTION},
        }
    ]
    if reply_kind == "mixed_batch":
        calls.append({"id": "disallowed-read", "name": "get_pipeline_state", "arguments": {}})
    response = _make_llm_response(content="The graph has been restored.") if reply_kind == "prose" else _make_llm_response(tool_calls=calls)
    audit_call = _ledger_call(prompt=3, completion=2)

    async def recorded_gateway(*args: Any, **kwargs: Any) -> Any:
        kwargs["recorder"].record_llm_call(audit_call)
        return response

    try:
        with (
            patch.object(service._provider_gateway, "_call_llm_with_audit", side_effect=recorded_gateway) as gateway,
            patch.object(CompositionState, "from_dict", side_effect=AssertionError("Recovery hydrated graph")) as hydrate,
            patch.object(service, "compose", side_effect=AssertionError("Recovery entered normal authoring")) as compose,
        ):
            if reply_kind == "control":
                result = await service.compose_recovery(
                    message="This graph is broken. Go back to an editable checkpoint and ask me what to do.",
                    recovery_context=context,
                    session_operation_context=operation,
                    required_work=required_work,
                    provider_owner=owner,
                )
                assert result.intent.action == "reset_to_safe_checkpoint"
                assert result.intent.message == _QUESTION
                assert result.llm_calls == (audit_call,)
            else:
                expected_error = ComposerServiceError if reply_kind == "prose" else ValueError
                expected_message = "no graph was restored" if reply_kind == "prose" else "sole tool call"
                with pytest.raises(expected_error, match=expected_message) as refused:
                    await service.compose_recovery(
                        message="This graph is broken. Go back to an editable checkpoint and ask me what to do.",
                        recovery_context=context,
                        session_operation_context=operation,
                        required_work=required_work,
                        provider_owner=owner,
                    )
                assert refused.value.__dict__["llm_calls"] == (audit_call,)
        assert gateway.call_count == 1
        messages, advertised = gateway.call_args.args
        assert {tool["function"]["name"] for tool in advertised} == _CONTROL_NAMES
        assert "must not be inferred" in messages[0]["content"]
        assert state_id in messages[0]["content"]
        hydrate.assert_not_called()
        compose.assert_not_called()
    finally:
        authority.release(operation)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["hidden_control", "disabled_terminal"])
async def test_primary_recovery_probe_detects_protocol_regressions(defect: str) -> None:
    """Control the actual request/termination probe with in-memory mutations."""
    if defect == "hidden_control":
        original = service_module.composer_loop_tool_definitions

        def without_handoff(dialect: ToolContractDialect) -> list[dict[str, Any]]:
            return [tool for tool in original(dialect) if tool["function"]["name"] != "handoff_to_user"]

        mutation = patch.object(service_module, "composer_loop_tool_definitions", side_effect=without_handoff)
        expected_message = "Provider palette omitted recovery controls"
    else:
        mutation = patch.object(service_module.ComposerServiceImpl, "_terminal_recovery_result", return_value=None)
        expected_message = "Recovery reached ordinary dispatch"
    with mutation, pytest.raises(AssertionError, match=expected_message):
        await test_primary_control_exits_before_dispatch_repair_and_advisor(
            "handoff_to_user", {"action": "park", "message": _QUESTION}, "park"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("control_first", [True, False])
async def test_mixed_primary_control_batch_executes_neither_call(control_first: bool) -> None:
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    calls = [
        {"id": "stop", "name": "handoff_to_user", "arguments": {"action": "park", "message": _QUESTION}},
        {"id": "mutate", "name": "set_metadata", "arguments": {"patch": {"name": "must-not-apply"}}},
    ]
    if not control_first:
        calls.reverse()
    state = _empty_state()
    with (
        patch("litellm.acompletion", side_effect=[_make_raw_llm_response(tool_calls=calls)]) as provider,
        patch.object(service, "_dispatch_tool_batch", side_effect=AssertionError("Mixed control batch dispatched")) as dispatch,
        pytest.raises(ValueError, match="sole tool call"),
    ):
        await service.compose("Please pause and ask me about the report.", [], state, session_id=session_id)
    assert provider.call_count == 1
    dispatch.assert_not_called()
    assert state.metadata.name != "must-not-apply"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,action", _CONTROLS)
@pytest.mark.parametrize("after_advisor_repair", [False, True])
async def test_final_preview_reply_still_accepts_recovery_without_advisor_reentry(
    name: str, arguments: dict[str, str], action: str, after_advisor_repair: bool
) -> None:
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(composer_max_discovery_turns=1))
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
    scripted_replies = [
        _make_raw_llm_response(tool_calls=[{"id": "preview", "name": "preview_pipeline", "arguments": {}}]),
        _make_raw_llm_response(tool_calls=[{"id": "reply-recovery", "name": name, "arguments": arguments}]),
    ]
    if after_advisor_repair:
        scripted_replies.insert(0, _make_raw_llm_response(content="The draft is ready for your review."))
    replies = iter(scripted_replies)
    advertised: list[list[dict[str, Any]]] = []

    async def provider(messages: Any, tools: Any, **kwargs: Any) -> Any:
        advertised.append(deepcopy(tools))
        return next(replies)

    with (
        patch("litellm.acompletion", side_effect=provider) as completion,
        patch.object(
            service._preflight,
            "runtime_preflight",
            return_value=ValidationResult(is_valid=True, checks=[], errors=[], readiness=_execution_ready()),
        ),
        patch.object(
            service._advisor_checkpoint,
            "_run_advisor_checkpoint",
            new_callable=AsyncMock,
            side_effect=(
                [AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="The output requirements need clarification.")]
                if after_advisor_repair
                else AssertionError("Accepted reply recovery reentered advisor")
            ),
        ) as advisor,
        patch.object(
            service._completion,
            "_qualified_advisor_repair_public_result",
            side_effect=AssertionError("Accepted recovery reentered advisor repair qualification"),
        ) as requalify,
    ):
        result = await service.compose("Preview the current pipeline and explain the result.", [], state, session_id=session_id)
    assert completion.call_count == 2 + int(after_advisor_repair)
    assert {tool["function"]["name"] for tool in advertised[0]} >= _CONTROL_NAMES
    assert {tool["function"]["name"] for tool in advertised[-1]} == _CONTROL_NAMES
    assert advisor.call_count == int(after_advisor_repair)
    requalify.assert_not_called()
    assert result.graph_recovery_intent is not None
    assert result.graph_recovery_intent.action == action
    assert result.graph_recovery_intent.message == _QUESTION
    assert result.state == state


@pytest.mark.asyncio
async def test_pending_review_reply_accepts_recovery_without_completion_gate(tmp_path: Path) -> None:
    seeded_service, _ = _composer_service_with_session(_mock_catalog(), _make_settings())
    sessions = seeded_service._require_sessions_service()
    state = _state_with_llm_node()
    session_id, state_id = await _seed_session_and_state(sessions, state=state, user_id="test-user")
    service = _build_composer(tmp_path, sessions)
    replies = iter(
        [
            _make_raw_llm_response(
                tool_calls=[
                    {
                        "id": "review",
                        "name": "request_interpretation_review",
                        "arguments": {
                            "affected_node_id": "rate_node",
                            "kind": "vague_term",
                            "user_term": "cool",
                            "llm_draft": "modern, useful, engaging, and clear for the public",
                        },
                    }
                ]
            ),
            _make_raw_llm_response(
                tool_calls=[
                    {
                        "id": "review-recovery",
                        "name": "handoff_to_user",
                        "arguments": {"action": "reset_to_safe_checkpoint", "message": _QUESTION},
                    }
                ]
            ),
        ]
    )
    advertised: list[list[dict[str, Any]]] = []

    async def provider(messages: Any, tools: Any, **kwargs: Any) -> Any:
        advertised.append(deepcopy(tools))
        return next(replies)

    with (
        patch("litellm.acompletion", side_effect=provider) as completion,
        patch.object(
            service._completion,
            "_surface_and_finalize_no_tools",
            side_effect=AssertionError("Accepted review-stage recovery entered completion"),
        ) as finalize,
        patch.object(
            service._advisor_checkpoint, "_run_advisor_checkpoint", side_effect=AssertionError("Recovery entered advisor")
        ) as advisor,
    ):
        result = await service.compose(
            "Ask me to review the interpretation and explain what remains.",
            [],
            state,
            session_id=str(session_id),
            current_state_id=str(state_id),
        )
    assert completion.call_count == 2
    assert {tool["function"]["name"] for tool in advertised[1]} == _CONTROL_NAMES
    pending_reviews = await sessions.list_interpretation_events(session_id, status="pending")
    assert any(review.tool_call_id == "review" for review in pending_reviews)
    assert result.tool_invocations == (), "Already-persisted review audit must not be replayed at recovery settlement"
    assert result.graph_recovery_intent is not None
    assert result.graph_recovery_intent.action == "reset_to_safe_checkpoint"
    assert result.graph_recovery_intent.message == _QUESTION
    finalize.assert_not_called()
    advisor.assert_not_called()
