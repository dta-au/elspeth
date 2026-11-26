"""What every exit of the tool-batch loop owes the provider and the audit trail.

``run_tool_batch`` (``web/composer/tool_batch.py``) closes each tool call at
one of about seventeen exits. Whatever the exit, three things must hold:

* every tool call the model made gets exactly one audit invocation and
  exactly one ``role: tool`` reply, in call order. A call with no reply is
  rejected by the provider on the next request; a call with no invocation, or
  with two, breaks the one-invocation-per-dispatch audit contract;
* a failure after a successful mutation does not lose the mutated state;
* a cancellation that lands before any tool started publishes nothing.

These run the real compose loop against a real SQLite sessions service; only
the provider call is scripted.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardToolArgumentParts
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.sessions.models import chat_messages_table
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.composer.test_tools import _stage1_valid_preview_state

from .conftest import _FakeChoice, _FakeFunction, _FakeLLMResponse, _FakeMessage, _FakeToolCall


class _ScriptedProvider:
    """Replays admitted completions in order and keeps a copy of every request's messages."""

    def __init__(self, *completions: _AdmittedLLMCompletion) -> None:
        self._completions = list(completions)
        self.requests: list[list[dict[str, Any]]] = []

    async def __call__(
        self,
        messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> _AdmittedLLMCompletion:
        del provider_custody, credential_state, prompt_guard_trusted_message_indexes
        del prompt_guard_content_parts, prompt_guard_tool_argument_parts
        self.requests.append(deepcopy(list(messages)))
        if not self._completions:
            return _admit_composer_llm_completion(_make_llm_response(content="Done."))
        return self._completions.pop(0)


def _raw_batch(*calls: tuple[str, str, str]) -> _AdmittedLLMCompletion:
    """One assistant turn of ``(call id, tool name, RAW argument string)`` calls.

    The argument string is passed through untouched so a call can carry
    arguments that are not JSON, or not an object.
    """
    return _admit_composer_llm_completion(
        _FakeLLMResponse(
            choices=[
                _FakeChoice(
                    message=_FakeMessage(
                        content=None,
                        tool_calls=[
                            _FakeToolCall(id=call_id, function=_FakeFunction(name=name, arguments=arguments))
                            for call_id, name, arguments in calls
                        ],
                    )
                )
            ]
        )
    )


def _tool_replies_to(request: list[dict[str, Any]], call_ids: set[str]) -> list[str]:
    """The ``tool_call_id`` of every ``role: tool`` message in ``request`` that answers one of ``call_ids``."""
    return [message["tool_call_id"] for message in request if message["role"] == "tool" and message["tool_call_id"] in call_ids]


# One call per way the loop can close a call without the tool succeeding, plus
# the ordinary successes between them. Order matters: the test asserts it.
_EVERY_EXIT_BATCH: tuple[tuple[str, str, str], ...] = (
    ("call_not_json", "get_pipeline_state", "{not valid json"),
    ("call_not_an_object", "get_pipeline_state", "[1, 2]"),
    ("call_missing_required", "set_metadata", "{}"),
    ("call_non_finite", "set_metadata", '{"patch": {"name": NaN}}'),
    ("call_unknown_tool", "no_such_tool", "{}"),
    ("call_discovery", "list_sources", "{}"),
    ("call_discovery_again", "list_sources", "{}"),
    ("call_mutation", "set_metadata", json.dumps({"patch": {"name": "Named by the batch"}})),
    ("call_failed_result", "remove_node", json.dumps({"id": "no_such_node"})),
    ("call_advisor_bad_arguments", "request_advisor_hint", json.dumps({"trigger": "not_a_trigger"})),
    ("call_after_everything", "get_pipeline_state", "{}"),
)


@pytest.mark.asyncio
async def test_every_tool_call_gets_one_invocation_and_one_reply_in_call_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever exit closes a call, the provider and the audit trail each see it exactly once."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    provider = _ScriptedProvider(_raw_batch(*_EVERY_EXIT_BATCH))
    call_ids = [call_id for call_id, _name, _arguments in _EVERY_EXIT_BATCH]

    with patch.object(service._provider_gateway, "_call_llm", new=provider):
        result = await service.compose("Exercise every exit", [], _stage1_valid_preview_state(), session_id=session_id)

    # Audit: one invocation per call, in call order.
    assert [invocation.tool_call_id for invocation in result.tool_invocations] == call_ids
    statuses = {invocation.tool_call_id: invocation.status for invocation in result.tool_invocations}
    # The batch really did leave by different exits: refusals and successes both occurred.
    assert statuses["call_not_json"] is ComposerToolStatus.ARG_ERROR
    assert statuses["call_missing_required"] is ComposerToolStatus.ARG_ERROR
    assert statuses["call_mutation"] is ComposerToolStatus.SUCCESS
    assert statuses["call_after_everything"] is ComposerToolStatus.SUCCESS

    # Provider: the next request answers every call exactly once, in call order,
    # directly after the assistant message that made them.
    assert len(provider.requests) >= 2
    next_request = provider.requests[1]
    assistant_index = next(
        index for index, message in enumerate(next_request) if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert [call["id"] for call in next_request[assistant_index]["tool_calls"]] == call_ids
    assert _tool_replies_to(next_request, set(call_ids)) == call_ids
    following = next_request[assistant_index + 1 : assistant_index + 1 + len(call_ids)]
    assert [message["role"] for message in following] == ["tool"] * len(call_ids)

    # Stored transcript: the same one-reply-per-call shape is what a later turn reads back.
    sessions = service._require_sessions_service()
    with sessions._engine.connect() as conn:
        stored_tool_rows = list(
            conn.execute(
                select(chat_messages_table.c.tool_call_id)
                .where(chat_messages_table.c.session_id == session_id)
                .where(chat_messages_table.c.role == "tool")
                .order_by(chat_messages_table.c.sequence_no)
            ).scalars()
        )
    assert [tool_call_id for tool_call_id in stored_tool_rows if tool_call_id in set(call_ids)] == call_ids


@pytest.mark.asyncio
async def test_preflight_failure_inside_dispatch_keeps_the_state_an_earlier_call_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal preflight failure must not drop a mutation an earlier call in the batch applied.

    ``set_metadata`` succeeds, then ``diff_pipeline`` checks whether the
    request-start state is a usable baseline and the runtime preflight fails
    internally. The error the route receives carries ``partial_state``: the
    state it persists so the applied mutation is not lost from the history.
    """
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    state = _stage1_valid_preview_state()

    def _failing_runtime_preflight(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("synthetic runtime preflight worker failure")

    monkeypatch.setattr(service._preflight, "runtime_preflight", _failing_runtime_preflight)
    provider = _ScriptedProvider(
        _raw_batch(
            ("call_rename", "set_metadata", json.dumps({"patch": {"name": "Renamed before the diff"}})),
            ("call_diff", "diff_pipeline", "{}"),
        )
    )

    with (
        patch.object(service._provider_gateway, "_call_llm", new=provider),
        pytest.raises(ComposerRuntimePreflightError) as raised,
    ):
        await service.compose("Rename, then diff", [], state, session_id=session_id)

    failure = raised.value
    assert isinstance(failure.original_exc, RuntimeError)
    assert failure.partial_state is not None
    assert failure.partial_state.version == state.version + 1
    assert failure.partial_state.metadata.name == "Renamed before the diff"
    # Both calls are in the audit trail the failure carries: the applied
    # mutation as a success, the diff as the call that crashed.
    assert [(invocation.tool_call_id, invocation.status) for invocation in failure.tool_invocations] == [
        ("call_rename", ComposerToolStatus.SUCCESS),
        ("call_diff", ComposerToolStatus.PLUGIN_CRASH),
    ]


@pytest.mark.asyncio
async def test_cancellation_before_the_first_tool_publishes_no_assistant_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel that lands after the model replied but before any tool started leaves no trace of the batch.

    The batch is held at its opening progress event, which is before the first
    tool call is looked at. The caller cancels there. No tool may start, no
    further model call may be made, and no assistant or tool row may be
    stored: an assistant turn with tool calls and no results would be a
    fabricated turn.
    """
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))

    batch_opened = asyncio.Event()
    release_batch = asyncio.Event()
    real_emit_progress = tool_batch_module.emit_progress
    held_once = False

    async def _hold_at_first_progress_event(*args: Any, **kwargs: Any) -> Any:
        nonlocal held_once
        if not held_once:
            held_once = True
            batch_opened.set()
            await release_batch.wait()
        return await real_emit_progress(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "emit_progress", _hold_at_first_progress_event)

    tools_started = 0
    real_execute_tool = tool_batch_module.execute_tool

    def _counting_execute_tool(*args: Any, **kwargs: Any) -> Any:
        nonlocal tools_started
        tools_started += 1
        return real_execute_tool(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", _counting_execute_tool)
    provider = _ScriptedProvider(
        _raw_batch(
            ("call_never_started", "set_metadata", json.dumps({"patch": {"name": "Must not be applied"}})),
            ("call_never_started_either", "get_pipeline_state", "{}"),
        )
    )

    with patch.object(service._provider_gateway, "_call_llm", new=provider):
        compose_task = asyncio.create_task(
            service.compose("Cancel before any tool", [], _stage1_valid_preview_state(), session_id=session_id)
        )
        await asyncio.wait_for(batch_opened.wait(), timeout=5.0)
        compose_task.cancel()
        await asyncio.sleep(0)
        release_batch.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(compose_task, timeout=5.0)

    assert tools_started == 0
    assert len(provider.requests) == 1
    sessions = service._require_sessions_service()
    with sessions._engine.connect() as conn:
        stored_roles = list(
            conn.execute(
                select(chat_messages_table.c.role)
                .where(chat_messages_table.c.session_id == session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).scalars()
        )
    assert stored_roles == []
