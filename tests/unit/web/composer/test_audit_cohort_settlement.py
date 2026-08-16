"""Cohort settlement close-out coverage for elspeth-90231248dc.

The fix chose the ATOMICITY branch of the ticket's contract: a buffered audit
cohort (tool breadcrumbs, LLM-call sidecars, planner evidence) settles in one
``add_messages_atomic`` transaction — fully durable or cleanly absent — and a
tool turn the compose loop already persisted inline is never drained again by
a route handler. These tests pin the three mechanisms the 2026-08-09 sweep
found unpinned:

* exactly-once across a mid-request provider timeout — the loop-side carrier
  still carries the already-persisted invocations, so the handler's
  ``failed_turn`` suppression is the load-bearing guard;
* per-write-index fault injection on the planner cohort, on the success AND
  the decline path, with the injection proven to have fired;
* cancellation timing — a cohort cancelled mid-flight is drained through the
  shield and lands whole (or, with a mid-cohort failure, not at all), while the
  caller's ``CancelledError`` still propagates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlanResult, PlannerDeclined
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.models import chat_messages_table, composition_proposals_table
from elspeth.web.sessions.routes import _handle_convergence_error
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean  # noqa: F401  (autouse end-gate CLEAN stub)

_CATALOG = create_catalog_service()
_PLUGIN_SNAPSHOT = PluginAvailabilitySnapshot.for_trained_operator(_CATALOG)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text_response(content: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


def _metadata_tool_response(call_id: str, name: str) -> Any:
    from types import SimpleNamespace

    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": name}})),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])


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


async def _seed_user_message(sessions_service: Any, session_id: str) -> UUID:
    """The proposal row's ``user_message_id`` is a real FK — seed the originating user row."""
    record = await sessions_service.add_message(UUID(session_id), "user", "build me a pipeline", writer_principal="route_user_message")
    return UUID(str(record.id))


def _proposal_count(sessions_service: Any, session_id: str) -> int:
    with sessions_service._engine.connect() as conn:
        return len(
            conn.execute(select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == session_id)).fetchall()
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


@dataclass
class _InsertInjector:
    """Fault-inject ``_insert_chat_message`` at one zero-based cohort index.

    ``fail_at=None`` is the control arm. ``fired`` records whether the injected
    failure was actually raised — every rejecting assertion checks it so a
    wrong-reason pass (the failure never fired) cannot masquerade as coverage.
    """

    sessions_service: Any
    fail_at: int | None
    calls: int = 0
    fired: bool = False
    _original: Any = field(default=None, repr=False)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._original = self.sessions_service._insert_chat_message

        def _flaky_insert(conn: Any, **kwargs: Any) -> str:
            index = self.calls
            self.calls += 1
            if self.fail_at is not None and index == self.fail_at:
                self.fired = True
                raise OperationalError("INSERT INTO chat_messages", {}, Exception("db unavailable"))
            return self._original(conn, **kwargs)

        monkeypatch.setattr(self.sessions_service, "_insert_chat_message", _flaky_insert)


# ---------------------------------------------------------------------------
# 1. exactly-once across a mid-request provider timeout
# ---------------------------------------------------------------------------


async def _drive_turn_then_provider_timeout(
    service: ComposerServiceImpl,
    session_id: str,
) -> ComposerConvergenceError:
    """Turn 1 commits a tool turn inline; turn 2's provider call times out."""
    responses = [_metadata_tool_response("call_turn1_metadata", "Committed inline")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        if responses:
            return responses.pop(0)
        raise TimeoutError

    driver = cast(Any, service)
    with pytest.raises(ComposerConvergenceError) as exc_info:
        await driver._run_one_turn_for_test(llm=_fake_llm, session_id=session_id)
    return exc_info.value


async def _run_convergence_handler(service: ComposerServiceImpl, session_id: str, exc: ComposerConvergenceError) -> dict[str, object]:
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
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
    )


@pytest.mark.asyncio
async def test_provider_timeout_after_inline_tool_turn_persists_each_record_exactly_once(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """Loop → carrier → route handler → DB: one tool row per invocation, one sidecar per LLM call.

    ``_call_llm_before_deadline`` captures ``recorder.invocations`` on the
    timeout carrier WITHOUT the loop's ``persisted_tool_call_turn`` filter, so
    the invocation that turn 1 already committed inline arrives at the handler
    a second time. The handler's ``failed_turn`` suppression is therefore the
    only thing between the audit trail and a duplicated breadcrumb.
    """
    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]

    exc = await _drive_turn_then_provider_timeout(service, result_session_id)

    # The carrier's own shape — the premise the handler guard rests on.
    assert exc.budget_exhausted == "timeout"
    assert exc.failed_turn is not None
    assert exc.failed_turn.tool_responses_persisted == 1
    assert [inv.tool_call_id for inv in exc.tool_invocations] == ["call_turn1_metadata"], (
        "the timeout carrier still carries the inline-persisted invocation; the handler must not drain it again"
    )
    assert [call.status.name for call in exc.llm_calls] == ["SUCCESS", "TIMEOUT"]

    before = _chat_rows(sessions_service, result_session_id)
    assert _invocation_occurrences(before) == {"call_turn1_metadata": 1}
    assert _llm_audit_rows(before) == [], "LLM sidecars are buffered until the handler drains them"

    body = await _run_convergence_handler(service, result_session_id, exc)

    assert body["failed_turn"] == {
        "assistant_message_id": exc.failed_turn.assistant_message_id,
        "tool_calls_attempted": 1,
        "tool_responses_persisted": 1,
        "transcript_url": None,
    }
    after = _chat_rows(sessions_service, result_session_id)
    assert _invocation_occurrences(after) == {"call_turn1_metadata": 1}, "the inline-persisted tool turn was drained a second time"
    llm_rows = _llm_audit_rows(after)
    assert len(llm_rows) == 2, "each buffered LLM call must be durable exactly once"
    sequence_numbers = [row["sequence_no"] for row in llm_rows]
    assert sequence_numbers == list(range(sequence_numbers[0], sequence_numbers[0] + 2)), (
        "the sidecar cohort settles as one contiguous block"
    )
    assert len({row["created_at"] for row in llm_rows}) == 1, "one transaction stamps one created_at across the cohort"


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
# 2. per-write-index fault injection on the planner cohort
# ---------------------------------------------------------------------------

_PLANNER_COHORT = tuple(_discovery_invocation(f"call_planner_{index}") for index in range(3))


def _green_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


def _plan() -> PipelinePlanResult:
    proposal = PipelineProposal.create(
        pipeline={"sources": {}, "nodes": [], "edges": [], "outputs": []},
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.FREEFORM,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    return PipelinePlanResult(
        proposal=proposal,
        tool_call_id="call_pipeline",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
        candidate_state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
    )


@dataclass(frozen=True, slots=True)
class _FakePreferences:
    trust_mode: str


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [None, 0, 1, 2], ids=["control", "index0", "index1", "index2"])
async def test_planner_success_path_cohort_is_all_or_nothing_at_every_write_index(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int | None,
) -> None:
    """``_stage_pipeline_plan``: a failure at any cohort row leaves zero rows AND no proposal."""
    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    user_message_id = await _seed_user_message(sessions_service, result_session_id)
    injector = _InsertInjector(sessions_service, fail_at)
    injector.install(monkeypatch)
    preflight = AsyncMock(spec=service._cached_runtime_preflight, return_value=_green_preflight())  # type: ignore[attr-defined]

    async def _stage() -> Any:
        return await service._stage_pipeline_plan(  # type: ignore[attr-defined]
            plan=_plan(),
            state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
            session_id=UUID(result_session_id),
            current_state_id=None,
            user_message_id=user_message_id,
            user_id="phase3-test-user",
            preferences=_FakePreferences(trust_mode="explicit_approve"),
            recorder=MagicMock(spec=BufferingRecorder, llm_calls=(), invocations=()),
            planner_llm_calls=(),
            planner_attempts=(),
            planner_invocations=_PLANNER_COHORT,
            plugin_snapshot=None,
        )

    with patch.object(service, "_cached_runtime_preflight", preflight):
        if fail_at is None:
            result = await _stage()
            assert result.message
            rows = _chat_rows(sessions_service, result_session_id)
            assert _invocation_occurrences(rows) == {inv.tool_call_id: 1 for inv in _PLANNER_COHORT}
            assert _proposal_count(sessions_service, result_session_id) == 1
            assert injector.calls == len(_PLANNER_COHORT)
            return

        with pytest.raises(AuditIntegrityError, match="pipeline planner audit persistence failed before proposal creation"):
            await _stage()

    assert injector.fired, "the injected failure never fired — this arm would pass for the wrong reason"
    assert injector.calls == fail_at + 1, "the cohort must stop at the failing row, not keep inserting"
    assert _chat_rows(sessions_service, result_session_id) == [], f"a partial planner cohort survived a failure at index {fail_at}"
    assert _proposal_count(sessions_service, result_session_id) == 0, "no proposal may exist without its evidence cohort"


async def _plan_and_stage_empty(service: ComposerServiceImpl, session_id: str, recorder: BufferingRecorder) -> Any:
    driver = cast(Any, service)
    plugin_snapshot, policy_catalog = driver._plugin_policy_context(None)
    return await driver._plan_and_stage_empty_pipeline(
        message="build me a pipeline",
        messages=[],
        state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        session_id=session_id,
        current_state_id=None,
        user_id="phase3-test-user",
        progress=None,
        user_message_id=str(uuid4()),
        recorder=recorder,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [None, 0, 1, 2], ids=["control", "index0", "index1", "index2"])
async def test_planner_decline_path_cohort_is_all_or_nothing_at_every_write_index(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int | None,
) -> None:
    """The ``PlannerDeclined`` arm mirrors the success path: zero rows on any failure, and the
    failure surfaces as Tier-1 audit corruption rather than being swallowed into a decline reply."""
    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    injector = _InsertInjector(sessions_service, fail_at)
    injector.install(monkeypatch)
    recorder = BufferingRecorder()

    async def _declining_planner(**kwargs: Any) -> Any:
        planner_recorder = kwargs["recorder"]
        for invocation in _PLANNER_COHORT:
            planner_recorder.record(invocation)
        raise PlannerDeclined("declined", decline_text="I cannot build that from the available components.")

    monkeypatch.setattr("elspeth.web.composer.service.plan_pipeline", _declining_planner)

    if fail_at is None:
        result = await _plan_and_stage_empty(service, result_session_id, recorder)
        assert result.message == "I cannot build that from the available components."
        rows = _chat_rows(sessions_service, result_session_id)
        assert _invocation_occurrences(rows) == {inv.tool_call_id: 1 for inv in _PLANNER_COHORT}
        assert injector.calls == len(_PLANNER_COHORT)
        return

    with pytest.raises(AuditIntegrityError, match="pipeline planner audit persistence failed before proposal creation"):
        await _plan_and_stage_empty(service, result_session_id, recorder)

    assert injector.fired, "the injected failure never fired — this arm would pass for the wrong reason"
    assert injector.calls == fail_at + 1
    assert _chat_rows(sessions_service, result_session_id) == [], f"a partial decline-path cohort survived a failure at index {fail_at}"
    assert _proposal_count(sessions_service, result_session_id) == 0


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
                raise OperationalError("INSERT INTO chat_messages", {}, Exception("db unavailable"))
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


@pytest.mark.asyncio
async def test_planner_cohort_cancelled_mid_settlement_lands_whole_and_creates_no_proposal(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on the planner success path: evidence lands whole, cancel wins, no proposal follows."""
    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    user_message_id = await _seed_user_message(sessions_service, result_session_id)
    gate = _GatedInsert(sessions_service)
    gate.install(monkeypatch)
    preflight = AsyncMock(spec=service._cached_runtime_preflight, return_value=_green_preflight())  # type: ignore[attr-defined]

    async def _stage() -> Any:
        return await service._stage_pipeline_plan(  # type: ignore[attr-defined]
            plan=_plan(),
            state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
            session_id=UUID(result_session_id),
            current_state_id=None,
            user_message_id=user_message_id,
            user_id="phase3-test-user",
            preferences=_FakePreferences(trust_mode="explicit_approve"),
            recorder=MagicMock(spec=BufferingRecorder, llm_calls=(), invocations=()),
            planner_llm_calls=(),
            planner_attempts=(),
            planner_invocations=_PLANNER_COHORT,
            plugin_snapshot=None,
        )

    with patch.object(service, "_cached_runtime_preflight", preflight):
        task = asyncio.create_task(_stage())
        await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation escaped while the planner cohort transaction was still open"
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

    rows = _chat_rows(sessions_service, result_session_id)
    assert _invocation_occurrences(rows) == {inv.tool_call_id: 1 for inv in _PLANNER_COHORT}, "the evidence cohort must land whole"
    assert _proposal_count(sessions_service, result_session_id) == 0, (
        "evidence-without-proposal is the safe direction; a proposal must not follow a cancelled request"
    )
