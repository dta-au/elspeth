"""Actual common-loop retention through provider, detached worker and fenced SQL."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from types import CoroutineType
from uuid import UUID

import httpx
import litellm
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.web import async_workers
from elspeth.web.composer import composition_completion as completion_module
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.composer.no_tool_policy import AssistantTextSegment, TrustedSystemNoticeSegment, visible_message_segments
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostRefused
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import composer_async_worker, composer_turn
from elspeth.web.sessions import service as sessions_module
from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_async_operations_table,
    composition_proposals_table,
    composition_states_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.routes import _helpers
from tests.helpers.composer_operations import message_body, running_operation_async, submit_and_settle
from tests.unit.web.sessions.test_common_mutation_authority_workers import reachable, thread_event
from tests.unit.web.sessions.test_routes import (
    _close_app_session_engines as _close_app_session_engines,
)
from tests.unit.web.sessions.test_routes import (
    _llm_call_audit_rows,
    _make_app,
)


@dataclass
class Trace:
    completed_calls: list[ComposerLLMCall] = field(default_factory=list)
    contexts: list[object] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    budgets: list[CommonProviderCostBudget] = field(default_factory=list)
    requests: list[tuple[str, dict]] = field(default_factory=list)
    refusals: list[CommonProviderCostRefused] = field(default_factory=list)
    compose_exits: list[BaseException] = field(default_factory=list)


def observe_real_owners(monkeypatch, service, trace):
    actual_finish = service.finish_provider_attempt
    actual_turn = composer_async_worker.run_composer_turn
    actual_bind = ProviderInvocationOwner.bind_cost_budget

    async def finish(*, session_operation_context, call, required_work=None):
        result = await actual_finish(
            session_operation_context=session_operation_context,
            call=call,
            required_work=required_work,
        )
        assert type(call) is ComposerLLMCall
        trace.completed_calls.append(call)
        trace.contexts.append(session_operation_context)
        return result

    async def turn(*args, **kwargs):
        trace.turns.append(kwargs)
        try:
            return await actual_turn(*args, **kwargs)
        except BaseException as original:
            observation = kwargs["observation"]
            trace.exits.append(
                {
                    "original": original,
                    "attached": composer_turn._llm_calls_from_exception(original),
                    "pending": observation.pending_exception_llm_calls,
                    "durable": observation.audit_cohort_durable,
                    "compose_result": observation.compose_result,
                }
            )
            raise

    def bind(owner, budget):
        actual_bind(owner, budget)
        assert type(budget) is CommonProviderCostBudget
        if not any(item is budget for item in trace.budgets):
            trace.budgets.append(budget)

    monkeypatch.setattr(service, "finish_provider_attempt", finish)
    monkeypatch.setattr(composer_async_worker, "run_composer_turn", turn)
    monkeypatch.setattr(ProviderInvocationOwner, "bind_cost_budget", bind)


def observe_terminal_refusal(monkeypatch, composer, trace):
    actual_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    actual_compose = composer.compose

    async def compose(*args, **kwargs):
        try:
            return await actual_compose(*args, **kwargs)
        except BaseException as original:
            trace.compose_exits.append(original)
            raise

    async def checkpoint(**kwargs):
        try:
            return await actual_checkpoint(**kwargs)
        except CommonProviderCostRefused as original:
            trace.refusals.append(original)
            raise

    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", checkpoint)
    monkeypatch.setattr(composer, "compose", compose)


def ordinary_app(tmp_path, monkeypatch, *, call_limit=3, timeout=85.0):
    app, service = _make_app(tmp_path, quota_enabled=True)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_prompt_guard_mode": "debug-when-unconfigured",
            "composer_planner_max_provider_calls": call_limit,
            "composer_planner_max_cumulative_provider_cost": Decimal("5"),
            "composer_timeout_seconds": timeout,
        }
    )
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    app.state.composer_service = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(),
        app.state.settings,
        sessions_service=service,
        session_engine=app.state.session_engine,
    )
    return app, service


def install_sdk(monkeypatch, trace, candidate, *, end_original=None, forbid_end=False):
    async def completion(**kwargs):
        phase = "end" if "response_format" in kwargs else "primary"
        trace.requests.append((phase, copy.deepcopy(kwargs)))
        assert [role for role, _ in trace.requests] in (["primary"], ["primary", "end"])
        if phase == "end":
            assert not forbid_end, "END entered SDK after the original allowance/deadline refused it"
            assert candidate in json.dumps(kwargs["messages"], ensure_ascii=False)
            if end_original is not None:
                raise end_original
            content = json.dumps(
                {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            )
        else:
            content = candidate
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "tool_calls": None}, "index": 0, "finish_reason": "stop"}],
            model=kwargs["model"],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)


async def read_actual_stores(app, service, session_id, operation_id):
    rows = await service.get_messages(session_id, limit=None, include_private_evidence=True)
    diagnostics = []
    withheld = []
    for row in rows:
        for envelope in row.tool_calls or ():
            if envelope.get("_kind") == DIAGNOSTIC_EVIDENCE_KIND:
                diagnostics.append(ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(session_id)))
            elif envelope.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND:
                withheld.append((row, envelope))
    with app.state.session_engine.connect() as conn:
        attempts = conn.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session_id))
        ).all()
        ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session_id))).all()
        states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session_id))).all()
        proposals = conn.execute(
            select(composition_proposals_table).where(composition_proposals_table.c.session_id == str(session_id))
        ).all()
        terminal = conn.execute(
            select(composer_async_operations_table).where(
                composer_async_operations_table.c.session_id == str(session_id),
                composer_async_operations_table.c.operation_id == operation_id,
            )
        ).one()
        chat = conn.execute(
            select(chat_messages_table)
            .where(chat_messages_table.c.session_id == str(session_id))
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
    return rows, diagnostics, withheld, attempts, ledger, states, proposals, terminal, chat


def assert_physical_prefix(stores, trace, *, expected_sources, expected_statuses):
    rows, diagnostics, _withheld, attempts, ledger, _states, _proposals, terminal, _chat = stores
    calls = trace.completed_calls
    assert len(calls) == len(expected_sources)
    assert [call.status for call in calls] == expected_statuses
    ids = [call.call_id for call in calls]
    assert None not in ids and len(set(ids)) == len(ids)
    assert len(attempts) == len(ledger) == len(calls)
    attempts_by_id = {row.attempt_id: row for row in attempts}
    ledger_by_id = {row.entry_id: row for row in ledger}
    assert set(attempts_by_id) == set(ids)
    public_audits = _llm_call_audit_rows(rows)
    assert len(public_audits) == len(calls)
    assert {envelope["call"]["call_id"] for _, envelope in public_audits} == set(ids)
    for call, context, source in zip(calls, trace.contexts, expected_sources, strict=True):
        if call.status is ComposerLLMCallStatus.SUCCESS:
            assert call.provider_cost == 0.001 and call.provider_cost_source == "response_usage.cost"
        actual = attempts_by_id[call.call_id]
        assert actual.settled_at is not None and actual.ledger_entry_id in ledger_by_id
        entry = ledger_by_id[actual.ledger_entry_id]
        assert actual.source == entry.source == source
        assert actual.operation_id == terminal.session_operation_id == context.fence.operation_id
        assert actual.operation_epoch == terminal.session_operation_epoch == context.fence.operation_epoch
        assert actual.lease_token == context.fence.lease_token
        assert entry.identity_id == actual.identity_id == "alice"
        assert entry.run_id is None and actual.run_id is None
        assert entry.prompt_tokens == call.prompt_tokens and entry.completion_tokens == call.completion_tokens
        assert entry.cached_prompt_tokens == call.cached_prompt_tokens and entry.reasoning_tokens == call.reasoning_tokens
        (saved,) = [envelope for _, envelope in public_audits if envelope["call"]["call_id"] == call.call_id]
        assert deep_thaw(saved) == deep_thaw(llm_call_audit_envelope(call))
        # TITLE currently owns no private request capture; require exactly what
        # this actual call owns rather than inventing an absent title port.
        contexts = [item for item in diagnostics if item.status == "provider_request" and item.tool_call_id == call.call_id]
        if call.private_request is None:
            assert source == "auto_title" and contexts == []
        else:
            (private,) = contexts
            assert private.operation_id == actual.operation_id and private.operation_epoch == actual.operation_epoch
            assert private.arguments.disposition == call.private_request.disposition
            assert private.arguments.content == call.private_request.canonical
            if private.arguments.content is not None:
                assert private.arguments.retained_sha256 == sha256(private.arguments.content.encode("utf-8")).hexdigest()
    assert len({item.evidence_id for item in diagnostics}) == len(diagnostics)


async def assert_public_exclusion(client, service, session_id, *, forbidden, terminal=None):
    ordinary = await client.get(f"/api/sessions/{session_id}/messages")
    audit = await client.get(
        f"/api/sessions/{session_id}/messages",
        params={"include_llm_audit": "true", "include_raw_content": "true", "include_tool_rows": "true"},
    )
    assert ordinary.status_code == audit.status_code == 200
    replay_rows = await service.get_messages(session_id, limit=None, include_private_evidence=False)
    replay = json.dumps(_helpers._composer_chat_history(replay_rows), ensure_ascii=False)
    surfaces = [ordinary.text, audit.text, replay]
    if terminal is not None:
        surfaces.append(terminal.text)
    for text in forbidden:
        assert all(text not in surface for surface in surfaces)
    assert all(DIAGNOSTIC_EVIDENCE_KIND not in surface for surface in surfaces)


def assert_provider_tickets(trace, expected):
    (turn,) = trace.turns
    coordinator = turn["observation"].required_work
    assert coordinator is not None
    sql = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.PROVIDER_SETTLEMENT_SQL]
    projection = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION]
    assert len(sql) == len(projection) == expected
    assert all(ticket.complete and not ticket.errors for ticket in (*sql, *projection))
    assert all(ticket.receipts() == () for ticket in (*sql, *projection))


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["pass_audit", "end_cancel"])
async def test_original_end_fault_keeps_full_physical_private_and_observation_prefix(tmp_path, monkeypatch, site):
    app, service = ordinary_app(tmp_path, monkeypatch)
    trace = Trace()
    observe_real_owners(monkeypatch, service, trace)
    candidate = "F_PRIVATE_CANDIDATE_café_8e7392: explain available components without claiming a completed graph."
    original = (
        AuditIntegrityError("F_PASS_WRITE_ORIGINAL_3f72") if site == "pass_audit" else asyncio.CancelledError("F_END_CANCEL_ORIGINAL_4d91")
    )
    prior = ValueError("F_PASS_PRIOR_CAUSE_e1c4")
    if site == "pass_audit":
        original.__cause__ = prior
    install_sdk(monkeypatch, trace, candidate, end_original=original if site == "end_cancel" else None)
    actual_add = service.add_message
    pass_attempts = []

    async def add(*args, **kwargs):
        if site == "pass_audit" and any(slot.get("_kind") == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for slot in kwargs.get("tool_calls") or ()):
            pass_attempts.append(kwargs)
            raise original
        return await actual_add(*args, **kwargs)

    monkeypatch.setattr(service, "add_message", add)
    session = await service.create_session("alice", "Existing title", "local")
    body = message_body("Explain the available components.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=body)
        # Gather/verify actual durability before the formerly first failing
        # exception-carrier assertion, so those boundaries stay independent.
        stores = await read_actual_stores(app, service, session.id, body["operation_id"])
        expected_last = ComposerLLMCallStatus.SUCCESS if site == "pass_audit" else ComposerLLMCallStatus.CANCELLED
        assert_physical_prefix(
            stores, trace, expected_sources=["composer", "composer"], expected_statuses=[ComposerLLMCallStatus.SUCCESS, expected_last]
        )
        await assert_public_exclusion(client, service, session.id, forbidden=[candidate, original.args[0]], terminal=settled.final)
    assert [role for role, _ in trace.requests] == ["primary", "end"]
    assert len(pass_attempts) == int(site == "pass_audit")
    assert stores[7].status == settled.final.json()["status"] == "failed"
    assert stores[5] == stores[6] == []
    assert all(slot.get("_kind") != ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for row in stores[0] for slot in row.tool_calls or ())
    (exit,) = trace.exits
    assert exit["original"] is original
    if site == "pass_audit":
        assert original.__cause__ is prior
    assert exit["compose_result"] is None
    assert [call.call_id for call in exit["attached"]] == [call.call_id for call in trace.completed_calls]
    assert [call.call_id for call in exit["pending"]] == [call.call_id for call in trace.completed_calls]
    assert exit["durable"] is True
    assert_provider_tickets(trace, 2)
    status, error = settled.error()
    assert status == (500 if site == "pass_audit" else 503)
    expected_type = "audit_integrity_error" if site == "pass_audit" else "composer_operation_worker_lost"
    assert expected_type in json.dumps(error)
    # Actual END request includes the exact candidate even though no pass or
    # assistant publication committed. PRIMARY's request does not contain it.
    requests = {item.tool_call_id: item for item in stores[1] if item.status == "provider_request"}
    assert all(type(requests[call.call_id].arguments.content) is str for call in trace.completed_calls)
    assert candidate not in requests[trace.completed_calls[0].call_id].arguments.content
    assert candidate in requests[trace.completed_calls[1].call_id].arguments.content


def exact_private_candidate(stores, candidate):
    matches = [(row, envelope) for row, envelope in stores[2] if row.content == candidate]
    assert len(matches) == 1, "Expected exactly one actual private candidate row"
    row, envelope = matches[0]
    assert row.role == "audit" and row.writer_principal == "compose_loop"
    assert envelope["content_hash"] == sha256(candidate.encode("utf-8")).hexdigest()
    assert envelope["origin"] == "compose_policy_refused"
    # This optional field is the user ingress receipt, not the write fence.
    assert row.operation_id is None
    assert row.session_id == UUID(stores[7].session_id)


def exact_admitted_candidate(stores, response, candidate):
    (saved,) = [row for row in stores[8] if row.id == response["message"]["id"]]
    assert saved.role == "assistant" and saved.raw_content == candidate
    assert response["message"]["content"] == saved.content
    segments = visible_message_segments(content=saved.content, raw_content=saved.raw_content)
    assert segments[0] == AssistantTextSegment(candidate)
    assert len(segments) == 2 and type(segments[1]) is TrustedSystemNoticeSegment
    assert response["message"]["segments"] == [{"kind": part.kind, "content": part.content} for part in segments]


@pytest.mark.asyncio
@pytest.mark.parametrize("call_limit", [1, 2], ids=["no_call_headroom", "enough_headroom"])
async def test_received_candidate_survives_original_call_limit_without_public_replay(tmp_path, monkeypatch, call_limit):
    app, service = ordinary_app(tmp_path, monkeypatch, call_limit=call_limit)
    trace = Trace()
    observe_real_owners(monkeypatch, service, trace)
    observe_terminal_refusal(monkeypatch, app.state.composer_service, trace)
    candidate = "D_PRIVATE_CANDIDATE_雪_café_732e: the available components need your next choice."
    install_sdk(monkeypatch, trace, candidate, forbid_end=call_limit == 1)
    session = await service.create_session("alice", "Existing title", "local")
    body = message_body("Explain the available components.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=body)
        stores = await read_actual_stores(app, service, session.id, body["operation_id"])
        expected = 1 if call_limit == 1 else 2
        assert_physical_prefix(
            stores, trace, expected_sources=["composer"] * expected, expected_statuses=[ComposerLLMCallStatus.SUCCESS] * expected
        )
        if call_limit == 1:
            await assert_public_exclusion(client, service, session.id, forbidden=[candidate], terminal=settled.final)
    assert [role for role, _ in trace.requests] == (["primary"] if call_limit == 1 else ["primary", "end"])
    assert trace.completed_calls[0].messages_hash == stable_hash(trace.requests[0][1]["messages"])
    assert candidate not in json.dumps(trace.requests[0][1]["messages"], ensure_ascii=False)
    assert stores[5] == stores[6] == []
    (budget,) = trace.budgets
    snapshot = budget.snapshot()
    assert snapshot.policy is not None and snapshot.policy.max_provider_calls == call_limit
    assert snapshot.physical_calls == len(trace.requests) == len(snapshot.charges)
    assert {charge.call_id for charge in snapshot.charges} == {call.call_id for call in trace.completed_calls}
    assert snapshot.policy.max_cumulative_provider_cost == Decimal("5")
    if call_limit == 1:
        status, error = settled.error()
        assert status == 502 and "invalid_provider_response" in json.dumps(error) and "PROVIDER_CALLS_EXHAUSTED" in json.dumps(error)
        assert stores[7].status == "failed"
        exact_private_candidate(stores, candidate)
        (refusal,) = trace.refusals
        assert trace.compose_exits[0] is refusal
        assert trace.exits[0]["original"].__cause__ is refusal
        assert refusal.code == "PROVIDER_CALLS_EXHAUSTED"
    else:
        assert stores[7].status == "completed"
        exact_admitted_candidate(stores, settled.result(), candidate)
        assert not any(row.content == candidate for row, _ in stores[2])
    assert_provider_tickets(trace, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancellation_count", "fail_sql", "fail_projection"),
    [
        pytest.param(3, False, False, id="three_cancellations_commit"),
        pytest.param(3, True, False, id="three_cancellations_sql_rollback"),
        pytest.param(0, True, False, id="sql_rollback"),
        pytest.param(0, False, True, id="fatal_context"),
        pytest.param(3, False, True, id="three_cancellations_fatal_context"),
    ],
)
async def test_policy_refused_reply_required_write_drains_actual_sql_and_original_cancellations(
    tmp_path, monkeypatch, cancellation_count, fail_sql, fail_projection
):
    app, service = ordinary_app(tmp_path, monkeypatch, call_limit=1)
    composer = app.state.composer_service
    trace = Trace()
    observe_real_owners(monkeypatch, service, trace)
    observe_terminal_refusal(monkeypatch, composer, trace)
    candidate = "REQUIRED_POLICY_REPLY_雪_café_368b: preserve the exact received words."
    install_sdk(monkeypatch, trace, candidate, forbid_end=True)
    entered, release = threading.Event(), threading.Event()
    seen = [asyncio.Event() for _ in range(cancellation_count)]
    originals, compose_tasks, sql_inputs, physical_outcomes, in_transaction_rows = [], [], [], [], []
    fault = OperationalError("controlled withheld reply insert", {}, RuntimeError("exact retained driver fault"))
    prior_context = MemoryError("original fatal retention context")
    prior_cause = ValueError("original explicit retention cause")
    projection_failure = AuditIntegrityError("original retention projection failure")
    projection_failure.__context__ = prior_context
    projection_failure.__cause__ = prior_cause
    producer_contexts, projection_contexts = [], []
    actual_compose = composer.compose
    actual_atomic = service.add_messages_atomic
    actual_write = service._write_audit_cohort_on_connection
    actual_shield = asyncio.shield
    actual_finish = async_workers.run_required_sql_finish_once
    actual_envelope = completion_module.withheld_reply_envelope
    actual_project = sessions_module.record_settled_composer_audit_message

    def is_retention(drafts):
        return any(
            envelope.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND and envelope.get("origin") == "compose_policy_refused"
            for draft in drafts
            for envelope in draft.tool_calls or ()
        )

    async def compose(*args, **kwargs):
        task = asyncio.current_task()
        assert task is not None
        compose_tasks.append(task)
        return await actual_compose(*args, **kwargs)

    async def atomic(*args, **kwargs):
        if is_retention(args[1]):
            sql_inputs.append((args, kwargs))
            assert kwargs["audit_only"] is True and kwargs["writer_principal"] == "compose_loop"
            assert kwargs["required_work"].key.source is RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL
        return await actual_atomic(*args, **kwargs)

    def write(conn, **kwargs):
        committed = actual_write(conn, **kwargs)
        if is_retention(kwargs["drafts"]):
            assert conn.in_transaction()
            (row,) = conn.execute(select(chat_messages_table).where(chat_messages_table.c.content == candidate)).all()
            assert row.role == "audit" and row.writer_principal == "compose_loop"
            in_transaction_rows.append(row)
            entered.set()
            assert release.wait(10), "Test must release the actual retention transaction"
            if fail_sql:
                raise fault
        return committed

    async def shield(awaitable):
        try:
            return await actual_shield(awaitable)
        except asyncio.CancelledError as original:
            if compose_tasks and asyncio.current_task() is compose_tasks[0] and all(original is not prior for prior in originals):
                originals.append(original)
                seen[len(originals) - 1].set()
            raise

    async def finish(ticket, func, *args, **kwargs):
        outcome = await actual_finish(ticket, func, *args, **kwargs)
        if sql_inputs and ticket is sql_inputs[0][1]["required_work"]:
            physical_outcomes.append(outcome)
        return outcome

    def envelope(origin, content):
        if origin == "compose_policy_refused":
            # A synchronous observation at the actual producer preserves the
            # direct caller shape; an async wrapper would change that shape.
            producer_contexts.append(sys.exception())
        return actual_envelope(origin, content)

    def project(*, role, writer_principal, tool_calls):
        if any(
            slot.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND and slot.get("origin") == "compose_policy_refused"
            for slot in tool_calls or ()
        ):
            projection_contexts.append((sys.exception(), projection_failure.__context__, projection_failure.__cause__))
            raise projection_failure
        return actual_project(role=role, writer_principal=writer_principal, tool_calls=tool_calls)

    monkeypatch.setattr(composer, "compose", compose)
    monkeypatch.setattr(service, "add_messages_atomic", atomic)
    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", write)
    monkeypatch.setattr(asyncio, "shield", shield)
    monkeypatch.setattr(async_workers, "run_required_sql_finish_once", finish)
    if fail_projection:
        monkeypatch.setattr(completion_module, "withheld_reply_envelope", envelope)
        monkeypatch.setattr(sessions_module, "record_settled_composer_audit_message", project)
    session = await service.create_session("alice", "Existing title", "local")
    body = message_body("Explain the available components.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with running_operation_async(client, app, path=f"/api/sessions/{session.id}/messages", body=body) as operation:
            try:
                await thread_event(entered)
                (task,) = compose_tasks
                ((args, kwargs),) = sql_inputs
                ticket = kwargs["required_work"]
                assert kwargs["session_operation_context"] is trace.contexts[0]
                assert args[0] == session.id
                assert not ticket.complete and physical_outcomes == []
                for index in range(cancellation_count):
                    task.cancel(f"retention-original-cancel-{index}")
                    await asyncio.wait_for(seen[index].wait(), 5)
                    assert not task.done() and not operation.drive.done() and not ticket.complete
                release.set()
                await asyncio.wait_for(asyncio.shield(operation.drive), 15)
                final = await client.get(operation.poll_path)
            finally:
                release.set()
        stores = await read_actual_stores(app, service, session.id, body["operation_id"])
        assert_physical_prefix(stores, trace, expected_sources=["composer"], expected_statuses=[ComposerLLMCallStatus.SUCCESS])
        await assert_public_exclusion(client, service, session.id, forbidden=[candidate, "exact retained driver fault"], terminal=final)
        if fail_projection:
            await assert_public_exclusion(
                client,
                service,
                session.id,
                forbidden=[str(projection_failure), str(prior_context), str(prior_cause)],
                terminal=final,
            )
    assert [role for role, _ in trace.requests] == ["primary"]
    assert stores[5] == stores[6] == []
    assert len(in_transaction_rows) == len(physical_outcomes) == len(sql_inputs) == 1
    (refusal,) = trace.refusals
    assert refusal.code == "PROVIDER_CALLS_EXHAUSTED"
    (exit,) = trace.exits
    graph = reachable(exit["original"])
    assert any(item is refusal for item in graph)
    assert len(originals) == len({id(original) for original in originals}) == cancellation_count
    assert [original.args for original in originals] == [(f"retention-original-cancel-{index}",) for index in range(cancellation_count)]
    assert all(any(item is original for item in graph) for original in originals)
    assert [call.call_id for call in exit["attached"]] == [trace.completed_calls[0].call_id]
    assert exit["compose_result"] is None and exit["durable"] is True
    assert ticket.complete
    coordinator = trace.turns[0]["observation"].required_work
    coordinator.assert_completed()
    (outcome,) = physical_outcomes
    assert outcome.deferred_cancellations == (), "Outer cancellations must not enter the owned SQL/projection child"
    error = final.json()["error"]
    assert final.json()["status"] == stores[7].status == "failed" and final.json()["result"] is None
    assert stores[7].cancel_requested_at is None
    if fail_projection:
        assert type(outcome) is RequiredSQLReturned and ticket.errors == ()
        exact_private_candidate(stores, candidate)
        assert error["http_status"] == 500 and error["error_type"] == "audit_integrity_error"
        assert any(item is projection_failure for item in graph)
        assert projection_contexts == [(None, prior_context, prior_cause)]
        assert projection_failure.__cause__ is prior_cause
        assert projection_failure.__context__ is prior_context
        assert all(any(item is original for item in graph) for original in (projection_failure, prior_context, prior_cause))
    elif fail_sql:
        assert type(outcome) is RequiredSQLRaised and outcome.error is fault
        assert any(item is fault for item in graph)
        assert any(type(item) is AuditIntegrityError and item.__cause__ is fault for item in graph)
        assert ticket.errors == (fault,)
        assert error["http_status"] == 500 and error["error_type"] == "audit_integrity_error"
        assert not any(row.content == candidate for row, _ in stores[2])
        assert not any(row.content == candidate for row in stores[8]), "The actual insert must roll back"
    else:
        assert type(outcome) is RequiredSQLReturned and ticket.errors == ()
        assert error["http_status"] == 503 and error["failure_code"] == "worker_lost"
        exact_private_candidate(stores, candidate)
    assert_provider_tickets(trace, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["prepare", "create_task", "submit", "projection"])
async def test_policy_refused_reply_required_preparation_and_projection_faults_keep_originals(tmp_path, monkeypatch, site):
    app, service = ordinary_app(tmp_path, monkeypatch, call_limit=1)
    trace = Trace()
    observe_real_owners(monkeypatch, service, trace)
    observe_terminal_refusal(monkeypatch, app.state.composer_service, trace)
    candidate = "POLICY_EVIDENCE_SETUP_雪_café_671a: preserve original ownership on every required exit."
    install_sdk(monkeypatch, trace, candidate, forbid_end=True)
    original = (
        OperationalError("controlled actual executor acquisition", {}, RuntimeError("original setup driver fault"))
        if site == "submit"
        else AuditIntegrityError(f"original policy evidence {site} fault")
    )
    hits, sql_inputs = [], []
    actual_envelope = completion_module.withheld_reply_envelope
    actual_create_task = asyncio.create_task
    actual_atomic = service.add_messages_atomic
    actual_submit = async_workers._submit_shared
    actual_project = sessions_module.record_settled_composer_audit_message

    def envelope(origin, content):
        if site == "prepare" and origin == "compose_policy_refused":
            hits.append(site)
            raise original
        return actual_envelope(origin, content)

    def create_task(operation, **kwargs):
        if (
            site == "create_task"
            and type(operation) is CoroutineType
            and operation.cr_code.co_qualname == "CompositionCompletion._retain_policy_refused_reply.<locals>.persist"
        ):
            hits.append(site)
            raise original
        return actual_create_task(operation, **kwargs)

    async def atomic(*args, **kwargs):
        if any(draft.content == candidate for draft in args[1]):
            sql_inputs.append(kwargs["required_work"])
        return await actual_atomic(*args, **kwargs)

    async def submit(callable_, ticket=None, finalizer=None, **kwargs):
        if site == "submit" and sql_inputs and ticket is sql_inputs[0]:

            def unavailable():
                hits.append(site)
                raise original

            # The existing bridge, not this fixture, records its actual
            # pre-submission failure on the original SQL ticket.
            with monkeypatch.context() as scoped:
                scoped.setattr(async_workers, "_get_shared_executor", unavailable)
                return await actual_submit(callable_, ticket, finalizer, **kwargs)
        return await actual_submit(callable_, ticket, finalizer, **kwargs)

    def project(*, role, writer_principal, tool_calls):
        if site == "projection" and any(
            slot.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND and slot.get("origin") == "compose_policy_refused"
            for slot in tool_calls or ()
        ):
            hits.append(site)
            raise original
        return actual_project(role=role, writer_principal=writer_principal, tool_calls=tool_calls)

    monkeypatch.setattr(completion_module, "withheld_reply_envelope", envelope)
    monkeypatch.setattr(asyncio, "create_task", create_task)
    monkeypatch.setattr(service, "add_messages_atomic", atomic)
    monkeypatch.setattr(async_workers, "_submit_shared", submit)
    monkeypatch.setattr(sessions_module, "record_settled_composer_audit_message", project)
    session = await service.create_session("alice", "Existing title", "local")
    body = message_body("Explain the available components.")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=body)
        stores = await read_actual_stores(app, service, session.id, body["operation_id"])
        assert_physical_prefix(stores, trace, expected_sources=["composer"], expected_statuses=[ComposerLLMCallStatus.SUCCESS])
        await assert_public_exclusion(client, service, session.id, forbidden=[candidate, str(original)], terminal=settled.final)
    assert hits == [site]
    assert [role for role, _ in trace.requests] == ["primary"]
    assert stores[5] == stores[6] == []
    (refusal,) = trace.refusals
    (exit,) = trace.exits
    graph = reachable(exit["original"])
    assert any(item is refusal for item in graph) and any(item is original for item in graph)
    assert [call.call_id for call in exit["attached"]] == [trace.completed_calls[0].call_id]
    assert exit["compose_result"] is None and exit["durable"] is True
    coordinator = trace.turns[0]["observation"].required_work
    coordinator.assert_completed()
    status, error = settled.error()
    assert status == 500 and "audit_integrity_error" in json.dumps(error)
    if site == "projection":
        (ticket,) = sql_inputs
        assert ticket.complete and ticket.errors == ()
        exact_private_candidate(stores, candidate)
    else:
        assert any(any(error is original for error in ticket.errors) for ticket in coordinator.tickets)
        assert not any(row.content == candidate for row in stores[8])
    assert_provider_tickets(trace, 1)
