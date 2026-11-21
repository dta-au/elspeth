"""The compose loop never uses a refused result's validation as the next delta base.

A successful change reports ``validation_delta``: what it added and resolved,
compared with the base the compose loop passes as ``prior_validation``. A
refused result's validation describes the refused call (a ``rejected_mutation``
entry, and for a refused full replacement nothing else), not the saved graph,
so it must not become the base of the next change.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.audit import finish_success
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.session_tool import _SessionAwareDispatchOutcome
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationEntry, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_compose_loop_context_budget import _GENEROUS_TOKENS, _RecordingProvider, _service, _tool_turn
from tests.unit.web.composer.test_compose_loop_llm_audit import _empty_state, _make_llm_response, _passing_preflight

_STANDING_ERROR = ValidationEntry("node:a", "Node 'a' has a standing error.", "high", "schema_contract_violation")
_STANDING_WARNING = ValidationEntry("node:b", "Node 'b' has a standing warning.", "medium")
_REJECTION = ValidationEntry("rejected_mutation", "The call was refused.", "high", "plugin_options_invalid")
_STANDING = ValidationSummary(is_valid=False, errors=(_STANDING_ERROR,), warnings=(_STANDING_WARNING,))
_NARROW_REFUSAL = ValidationSummary(is_valid=False, errors=(_REJECTION, _STANDING_ERROR), warnings=(_STANDING_WARNING,))
_FULL_REPLACEMENT_REFUSAL = ValidationSummary(is_valid=False, errors=(_REJECTION,))
_EMPTY_DELTA: dict[str, list[dict[str, Any]]] = {"new_errors": [], "resolved_errors": [], "new_warnings": [], "resolved_warnings": []}


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


def _apply_rename(current: CompositionState, prior_validation: ValidationSummary | None) -> ToolResult:
    """A successful change that touches no validation entry, attaching its base as the dispatcher does."""
    base = prior_validation if prior_validation is not None else _STANDING
    updated = replace(current, metadata=PipelineMetadata(name="renamed"), version=current.version + 1)
    return ToolResult(success=True, updated_state=updated, validation=_STANDING, affected_nodes=(), prior_validation=base)


def _sent_tool_results(provider: _RecordingProvider) -> dict[str, dict[str, Any]]:
    return {m["tool_call_id"]: json.loads(m["content"]) for m in provider.requests[2]["messages"] if m["role"] == "tool"}


async def _delta_after_refusal(refusal: ValidationSummary) -> dict[str, Any]:
    """Refuse call-1, apply call-2 (both ordinary tools); return the validation_delta the model was sent for call-2."""
    service, session_id = _service(_GENEROUS_TOKENS)
    provider = _RecordingProvider(_tool_turn("call-1"), _tool_turn("call-2"), _make_llm_response(content="Done."))

    def _execute(tool_name: str, arguments: dict[str, Any], current: CompositionState, *_args: Any, **kwargs: Any) -> ToolResult:
        if arguments["patch"]["name"] == "call-1":
            return ToolResult(success=False, updated_state=current, validation=refusal, affected_nodes=())
        return _apply_rename(current, kwargs["prior_validation"])

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_execute),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        await service.compose("Rename the pipeline", [], _empty_state(), session_id=session_id)

    sent = _sent_tool_results(provider)
    assert sent["call-1"]["success"] is False  # control: the refusal reached the model as a refusal
    delta: dict[str, Any] = sent["call-2"]["validation_delta"]
    return delta


async def _delta_after_session_aware_refusal(refusal: ValidationSummary) -> dict[str, Any]:
    """As above, but the refused call takes the session-aware dispatch path (the loop's second base site).

    The session-aware dispatcher is replaced by a fake that returns a refused
    result carrying ``refusal``. The shipped ``request_interpretation_review``
    handler does not produce a ``rejected_mutation`` entry today; this pins the
    loop's rule for that path, not the handler's current output.
    """
    service, session_id = _service(_GENEROUS_TOKENS)
    review_call = _make_llm_response(
        tool_calls=[
            {
                "id": "call-1",
                "name": "request_interpretation_review",
                "arguments": {"affected_node_id": "a", "kind": "vague_term", "user_term": "cool", "llm_draft": "Well organized."},
            }
        ],
        request_id="chatcmpl-review",
    )
    provider = _RecordingProvider(review_call, _tool_turn("call-2"), _make_llm_response(content="Done."))

    async def _dispatch_session_aware(**kwargs: Any) -> _SessionAwareDispatchOutcome:
        # Mirrors the real dispatcher's result tail: audit record, then the tool message.
        state: CompositionState = kwargs["state"]
        refused = ToolResult(
            success=False,
            updated_state=state,
            validation=refusal,
            affected_nodes=(),
            data={"_kind": "interpretation_review_blocked", "message": "The review was refused."},
        )
        kwargs["recorder"].record(finish_success(kwargs["audit"], result_payload=refused.to_dict(), version_after=state.version))
        kwargs["llm_messages"].append({"role": "tool", "tool_call_id": kwargs["tool_call_id"], "content": serialize_tool_result(refused)})
        return _SessionAwareDispatchOutcome(result=refused, is_discovery=False, post_version=state.version)

    def _execute(tool_name: str, arguments: dict[str, Any], current: CompositionState, *_args: Any, **kwargs: Any) -> ToolResult:
        return _apply_rename(current, kwargs["prior_validation"])

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_execute),
        patch.object(service._session_tools, "_dispatch_session_aware_tool", side_effect=_dispatch_session_aware) as dispatched,
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        await service.compose("Rename the pipeline", [], _empty_state(), session_id=session_id)

    assert dispatched.call_count == 1  # control: call-1 took the session-aware path
    sent = _sent_tool_results(provider)
    assert sent["call-1"]["success"] is False
    delta: dict[str, Any] = sent["call-2"]["validation_delta"]
    return delta


@pytest.mark.asyncio
async def test_delta_after_a_refused_call_does_not_list_the_rejection_as_resolved() -> None:
    delta = await _delta_after_refusal(_NARROW_REFUSAL)
    # The change touched no validation entry: nothing is new and nothing is resolved.
    assert delta == _EMPTY_DELTA


@pytest.mark.asyncio
async def test_delta_after_a_refused_full_replacement_lists_no_standing_entry_as_new() -> None:
    delta = await _delta_after_refusal(_FULL_REPLACEMENT_REFUSAL)
    assert delta["new_errors"] == []  # must not list the standing error as new
    assert delta["new_warnings"] == []  # must not list the standing warning as new
    assert delta["resolved_errors"] == []  # must not list the rejection as resolved


@pytest.mark.asyncio
async def test_delta_after_a_failed_call_that_carries_only_the_saved_graphs_validation_is_empty() -> None:
    """Control: with no rejection entry in the failed result, the base is right either way."""
    delta = await _delta_after_refusal(_STANDING)
    assert delta == _EMPTY_DELTA


@pytest.mark.asyncio
async def test_delta_after_a_refused_session_aware_call_does_not_list_the_rejection_as_resolved() -> None:
    delta = await _delta_after_session_aware_refusal(_NARROW_REFUSAL)
    assert delta == _EMPTY_DELTA


@pytest.mark.asyncio
async def test_delta_after_a_refused_session_aware_full_replacement_lists_no_standing_entry_as_new() -> None:
    delta = await _delta_after_session_aware_refusal(_FULL_REPLACEMENT_REFUSAL)
    assert delta == _EMPTY_DELTA
