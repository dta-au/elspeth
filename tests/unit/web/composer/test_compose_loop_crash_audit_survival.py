"""A second failure on the turn where a tool crashes: nothing is swallowed, nothing is replayed twice.

When a tool handler crashes, the compose loop raises
``ComposerPluginCrashError``. What that error carries decides what the caller
writes: invocations it carries are replayed into the audit trail, so it must
carry every invocation that is not yet durable and none that already is.
``test_compose_loop_persistence.py`` covers the crash on its own, persisted and
rolled back. These cover the advisor's early checkpoint failing on the same turn
as the crash, after an earlier turn of the same request already committed.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text

from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.service import ComposerServiceImpl
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_tool_batch_exit_contract import _raw_batch, _ScriptedProvider
from tests.unit.web.composer.test_tools import _stage1_valid_preview_state


class _SecondToolCrash(RuntimeError):
    """The handler defect: the second tool the request dispatches raises this."""


class _EarlyCheckpointDefect(RuntimeError):
    """A second, unrelated failure raised by the advisor's early checkpoint on the crashing turn."""


def _two_turns_second_crashes(monkeypatch: pytest.MonkeyPatch) -> _ScriptedProvider:
    """Turn 1 renames the pipeline and succeeds; turn 2's tool crashes inside its handler."""
    real_execute_tool = tool_batch_module.execute_tool
    dispatched = 0

    def _execute(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal dispatched
        dispatched += 1
        if dispatched == 2:
            raise _SecondToolCrash("synthetic handler defect")
        return real_execute_tool(tool_name, *args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", _execute)
    return _ScriptedProvider(
        _raw_batch(("call_applied", "set_metadata", json.dumps({"patch": {"name": "Applied before the crash"}}))),
        _raw_batch(("call_crash", "set_metadata", json.dumps({"patch": {"name": "Never applied"}}))),
    )


def _fail_early_checkpoint_on_the_crashing_turn(monkeypatch: pytest.MonkeyPatch, service: ComposerServiceImpl) -> None:
    real_checkpoint = service._advisor_checkpoint._maybe_run_early_checkpoint
    turns = 0

    async def _checkpoint(**kwargs: Any) -> Any:
        nonlocal turns
        turns += 1
        if turns == 2:
            raise _EarlyCheckpointDefect("synthetic early checkpoint defect")
        return await real_checkpoint(**kwargs)

    monkeypatch.setattr(service._advisor_checkpoint, "_maybe_run_early_checkpoint", _checkpoint)


def _causes(error: BaseException) -> list[BaseException]:
    """The failures chained behind ``error``: its cause, or every member of a cause group."""
    cause = error.__cause__
    if cause is None:
        return []
    if isinstance(cause, BaseExceptionGroup):
        return list(cause.exceptions)
    return [cause]


@pytest.mark.asyncio
@pytest.mark.parametrize("unwind_commit_fails", [False, True], ids=["turn-persisted", "turn-rolled-back"])
async def test_checkpoint_failure_on_the_crashing_turn_is_kept_beside_the_crash(
    unwind_commit_fails: bool,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    inject_commit_OperationalError: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash stays the primary failure; the checkpoint failure is neither lost nor allowed to change what is replayed."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service = composer_service_with_real_sessions
    sessions_service = service._require_sessions_service()
    provider = _two_turns_second_crashes(monkeypatch)
    _fail_early_checkpoint_on_the_crashing_turn(monkeypatch, service)
    if unwind_commit_fails:
        real_persist = sessions_service.persist_compose_turn_async
        persists = 0

        async def _fail_second_persist(**kwargs: Any) -> Any:
            nonlocal persists
            persists += 1
            if persists == 2:
                inject_commit_OperationalError(sessions_service._engine)
            return await real_persist(**kwargs)

        monkeypatch.setattr(sessions_service, "persist_compose_turn_async", _fail_second_persist)
    state = _stage1_valid_preview_state()

    with patch.object(service._provider_gateway, "_call_llm", new=provider), pytest.raises(ComposerPluginCrashError) as raised:
        await service.compose("Rename, then crash", [], state, session_id=result_session_id)

    crash = raised.value
    assert [type(cause) for cause in _causes(crash)] == [_SecondToolCrash, _EarlyCheckpointDefect]
    assert crash.partial_state is not None
    assert crash.partial_state.metadata.name == "Applied before the crash"
    with sessions_service._engine.connect() as conn:
        stored = conn.execute(
            text("SELECT tool_call_id FROM chat_messages WHERE session_id = :session_id AND role = 'tool' ORDER BY sequence_no"),
            {"session_id": result_session_id},
        ).fetchall()
    stored_ids = [row[0] for row in stored]
    carried_ids = [invocation.tool_call_id for invocation in crash.tool_invocations]
    if unwind_commit_fails:
        # Turn 1 is durable, the crashing turn rolled back: only the crashing turn is replayed.
        assert stored_ids == ["call_applied"]
        assert carried_ids == ["call_crash"]
    else:
        # Both turns are durable: nothing is replayed.
        assert stored_ids == ["call_applied", "call_crash"]
        assert carried_ids == []


@pytest.mark.asyncio
async def test_crash_on_a_turn_that_was_neither_persisted_nor_rolled_back_is_an_invariant_failure(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed tool turn is always persisted or recorded as rolled back; anything else must not pass as an ordinary crash.

    The persist step is made to report neither, which current code cannot
    produce. The loop refuses to raise a crash whose audit standing is
    unknown, and keeps the handler's exception as the cause.
    """
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service = composer_service_with_real_sessions
    provider = _two_turns_second_crashes(monkeypatch)
    real_persist = service._persist_turn_audit
    persists = 0

    async def _persist_reporting_neither(**kwargs: Any) -> Any:
        nonlocal persists
        persists += 1
        outcome = await real_persist(**kwargs)
        if persists == 2:
            return dataclasses.replace(
                outcome,
                persisted_tool_call_turn=False,
                persisted_assistant_matches_current_dispatch=False,
                unwind_audit_failed=False,
            )
        return outcome

    monkeypatch.setattr(service, "_persist_turn_audit", _persist_reporting_neither)

    with patch.object(service._provider_gateway, "_call_llm", new=provider), pytest.raises(InvariantError) as raised:
        await service.compose("Rename, then crash", [], _stage1_valid_preview_state(), session_id=result_session_id)

    assert type(raised.value.__cause__) is _SecondToolCrash
