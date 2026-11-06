"""Common audit-cohort settlement and exactly-once draining.

A tool turn already persisted inline must not drain again through a route
handler. Buffered public/private audit rows settle in one transaction.

A cancelled cohort drains through the shield and lands whole; a mid-cohort
failure leaves no partial rows while the original cancellation propagates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes import _handle_convergence_error
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean  # noqa: F401  (autouse end-gate CLEAN stub)

_CATALOG = create_catalog_service()
_PLUGIN_SNAPSHOT = PluginAvailabilitySnapshot.for_trained_operator(_CATALOG)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text_response(content: str) -> Any:
    from types import SimpleNamespace

    return _admit_composer_llm_completion(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])
    )


def _metadata_tool_response(call_id: str, name: str) -> Any:
    from types import SimpleNamespace

    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": name}})),
    )
    return _admit_composer_llm_completion(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])
    )


def _discovery_invocation(call_id: str) -> ComposerToolInvocation:
    arguments_canonical = canonical_json({})
    result_canonical = canonical_json({"status": "SUCCESS", "call": call_id})
    return ComposerToolInvocation(
        tool_call_id=call_id,
        tool_name="get_pipeline_state",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode("utf-8")).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode("utf-8")).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=1,
        version_after=1,
        started_at=datetime(2026, 8, 16, tzinfo=UTC),
        finished_at=datetime(2026, 8, 16, tzinfo=UTC),
        latency_ms=3,
        actor="composer-web:user-test",
    )


def _chat_rows(sessions_service: Any, session_id: str) -> list[Mapping[str, Any]]:
    with sessions_service._engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    chat_messages_table.c.role,
                    chat_messages_table.c.tool_calls,
                    chat_messages_table.c.tool_call_id,
                    chat_messages_table.c.sequence_no,
                    chat_messages_table.c.created_at,
                )
                .where(chat_messages_table.c.session_id == session_id)
                .where(chat_messages_table.c.role != "user")
                .order_by(chat_messages_table.c.sequence_no)
            ).mappings()
        )


def _envelope_kind(row: Mapping[str, Any]) -> str | None:
    tool_calls = row["tool_calls"]
    if not tool_calls or not isinstance(tool_calls[0], Mapping):
        return None
    kind = tool_calls[0].get("_kind")
    return kind if isinstance(kind, str) else None


def _invocation_occurrences(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """Count how many durable rows carry each tool_call_id.

    A tool turn persisted inline lands as a ``role="tool"`` row keyed by
    ``tool_call_id``; a handler-drained breadcrumb lands as an ``audit`` row
    whose envelope carries the invocation. Exactly-once means every id
    appears once across BOTH shapes.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if row["role"] == "tool" and row["tool_call_id"] is not None:
            counts[row["tool_call_id"]] = counts.get(row["tool_call_id"], 0) + 1
        elif _envelope_kind(row) == "audit":
            invocation = row["tool_calls"][0]["invocation"]
            call_id = invocation["tool_call_id"]
            counts[call_id] = counts.get(call_id, 0) + 1
    return counts


def _llm_audit_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if _envelope_kind(row) == "llm_call_audit"]


# ---------------------------------------------------------------------------
# 1. exactly-once across a mid-request provider timeout
# ---------------------------------------------------------------------------


async def _run_convergence_handler(service: ComposerServiceImpl, session_id: str, exc: ComposerConvergenceError) -> dict[str, object]:
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    async with acquire_compose_context(sessions_service, session_id) as compose_context:
        return await _handle_convergence_error(
            exc,
            service=sessions_service,
            session_id=UUID(session_id),
            user_id="phase3-test-user",
            log_prefix="cohort_test",
            llm_composition_state_id=None,
            settings=service._settings,  # type: ignore[attr-defined]
            secret_service=None,
            plugin_snapshot=_PLUGIN_SNAPSHOT,
            profile_registry=MagicMock(spec=OperatorProfileRegistry),
            catalog=_CATALOG,
            session_operation_context=compose_context,
        )


@pytest.mark.asyncio
async def test_handler_drains_carried_invocations_only_when_no_turn_was_persisted(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """Direct pin of the ``failed_turn`` suppression, with its control arm.

    Two carriers, identical ``tool_invocations``: one stamped ``failed_turn``
    (the loop already committed the turn), one without (a pre-cutover carrier
    that never persisted inline). Only the second may be drained. Mutating the
    handler's ``exc.tool_invocations if exc.failed_turn is None else ()`` to an
    unconditional drain turns the first arm red.
    """
    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    invocation = _discovery_invocation("call_carried")

    suppressed = ComposerConvergenceError(
        1,
        budget_exhausted="timeout",
        tool_invocations=(invocation,),
        failed_turn=FailedTurnMetadata(assistant_message_id=None, tool_calls_attempted=1, tool_responses_persisted=1),
    )
    await _run_convergence_handler(service, result_session_id, suppressed)
    assert _invocation_occurrences(_chat_rows(sessions_service, result_session_id)) == {}, (
        "a carrier stamped failed_turn was drained: the inline-persisted turn would now be duplicated"
    )

    unpersisted = ComposerConvergenceError(1, budget_exhausted="timeout", tool_invocations=(invocation,), failed_turn=None)
    await _run_convergence_handler(service, result_session_id, unpersisted)
    assert _invocation_occurrences(_chat_rows(sessions_service, result_session_id)) == {"call_carried": 1}, (
        "control: a carrier with no persisted turn must still be drained exactly once"
    )


# ---------------------------------------------------------------------------
# 3. cancellation timing on the settling cohort
# ---------------------------------------------------------------------------


@dataclass
class _GatedInsert:
    """Block the sync worker inside the cohort transaction until the test releases it."""

    sessions_service: Any
    fail_after_release_at: int | None = None
    loop: asyncio.AbstractEventLoop | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    fired: bool = False
    _original: Any = field(default=None, repr=False)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.loop = asyncio.get_running_loop()
        self._original = self.sessions_service._insert_chat_message

        def _gated_insert(conn: Any, **kwargs: Any) -> str:
            index = self.calls
            self.calls += 1
            if index == 0:
                assert self.loop is not None
                self.loop.call_soon_threadsafe(self.entered.set)
                if not self.release.wait(timeout=5.0):
                    raise TimeoutError("test worker was never released")
            if self.fail_after_release_at is not None and index == self.fail_after_release_at:
                self.fired = True
                raise OperationalError("injected chat-message insert failure", {}, Exception("db unavailable"))
            return self._original(conn, **kwargs)

        monkeypatch.setattr(self.sessions_service, "_insert_chat_message", _gated_insert)


_COHORT_DRAFTS = tuple(AuditMessageDraft(role="audit", content=f"row-{index}", tool_calls=({"_kind": "audit_test"},)) for index in range(3))


@pytest.mark.asyncio
async def test_cancel_mid_cohort_drains_the_shield_and_lands_the_whole_cohort(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the awaiter while row 0 is inside the transaction: all rows land, cancel propagates."""
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    gate = _GatedInsert(sessions_service)
    gate.install(monkeypatch)

    task = asyncio.create_task(
        sessions_service.add_messages_atomic(UUID(result_session_id), _COHORT_DRAFTS, writer_principal="compose_loop")
    )
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation escaped while the cohort transaction was still open"
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert task.cancelled(), "the awaiting task must finish as genuinely cancelled"

    rows = _chat_rows(sessions_service, result_session_id)
    assert [row["tool_calls"][0]["_kind"] for row in rows] == ["audit_test"] * 3, (
        "a cancelled cohort must still land WHOLE, never as a prefix"
    )
    assert gate.calls == 3
    sequence_numbers = [row["sequence_no"] for row in rows]
    assert sequence_numbers == list(range(sequence_numbers[0], sequence_numbers[0] + 3))


@pytest.mark.asyncio
async def test_cancel_then_mid_cohort_failure_lands_nothing_and_keeps_the_cancel(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel during row 0, then row 1 fails: zero rows, and CancelledError (not the DB error) wins."""
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    gate = _GatedInsert(sessions_service, fail_after_release_at=1)
    gate.install(monkeypatch)

    task = asyncio.create_task(
        sessions_service.add_messages_atomic(UUID(result_session_id), _COHORT_DRAFTS, writer_principal="compose_loop")
    )
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await asyncio.wait_for(task, timeout=5.0)
    assert gate.fired, "the mid-cohort failure never fired"
    assert isinstance(exc_info.value.__cause__, OperationalError), "the DB failure must stay diagnosable as the cancellation's __cause__"
    assert _chat_rows(sessions_service, result_session_id) == [], "a cancelled-and-failed cohort left a durable prefix"
