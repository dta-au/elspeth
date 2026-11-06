"""Actual manual-refusal P4 rollback, current suffix and route recovery custody.

The SQL faults and pre-dispatch Advisor refusal are deliberate injections. The
SDK responses, both primary attempts, metadata mutation, P4 transaction,
ComposerToolFailure consumer, required recovery SQL and terminal settlement use
their production owners. These controls are distinct from natural preparation
reachability and from a comparison failure after a committed checkpoint.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import litellm
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import ComposerToolFailure
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions._persist_payload import AuditMessageDraft, AuditOutcome
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import ChatMessageRecord, CompositionStateRecord
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.sessions.test_composer_async_worker import _worker
from tests.unit.web.sessions.test_routes import _install_required_route_prompt_shields, _make_app
from tests.unit.web.sessions.test_run_composer_turn import _row, _run, _running_job, _seed_output_only_draft

_SAVED_TOOL_ID = "saved-before-refusal"
_UNCOMMITTED_TOOL_ID = "current-uncommitted-metadata"
_REFUSED_TOOL_ID = "current-refused-advisor"
_REQUEST = "Set the pipeline name to Preserved pipeline, then try a temporary name and request an Advisor hint."


@dataclass
class _RollbackScenario:
    app: FastAPI
    sessions: SessionServiceImpl
    engine: Engine
    session_id: UUID
    baseline: CompositionStateRecord
    original_refusal: ComposerContextUnsupported
    original_p4_sql: SQLAlchemyError
    recovery_sql: OperationalError
    sdk_requests: list[dict[str, Any]] = field(default_factory=list)
    shield_requests: list[dict[str, Any]] = field(default_factory=list)
    p4_outcomes: list[AuditOutcome] = field(default_factory=list)
    p4_throws: list[AuditIntegrityError | IntegrityError] = field(default_factory=list)
    tool_failures: list[ComposerToolFailure] = field(default_factory=list)
    rolled_back_assistant_ids: list[str] = field(default_factory=list)
    recovery_rolled_back_ids: list[str] = field(default_factory=list)
    first_checkpoint_rows: tuple[Any, ...] = ()
    first_checkpoint_states: tuple[Any, ...] = ()
    postcommit_checkpoint_rows: tuple[Any, ...] = ()
    postcommit_checkpoint_states: tuple[Any, ...] = ()
    recovery_baseline_rows: tuple[ChatMessageRecord, ...] | None = None
    recovery_baseline_llm: dict[UUID, ChatMessageRecord] | None = None
    postcommit_failure: AuditIntegrityError | None = None
    postcommit_prior_cause: RuntimeError = field(default_factory=lambda: RuntimeError("PRIVATE postcommit validation cause"))
    refusal_entries: int = 0
    p4_sql_hits: int = 0
    recovery_sql_hits: int = 0


def _exception_links(error: BaseException) -> tuple[BaseException, ...]:
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is earlier for earlier in found):
            continue
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return tuple(found)


def _audit_envelopes(messages: Sequence[ChatMessageRecord]) -> tuple[tuple[ChatMessageRecord, Mapping[str, Any]], ...]:
    # Assistant rows use the same column for OpenAI function requests, which
    # have no audit discriminator. These producers put sidecars in role=audit.
    entries: list[tuple[ChatMessageRecord, Mapping[str, Any]]] = []
    for message in messages:
        if message.role != "audit":
            continue
        for envelope in message.tool_calls or ():
            assert "_kind" in envelope, "role=audit envelope lacks the owned discriminator"
            entries.append((message, envelope))
    return tuple(entries)


def _invocation_envelopes(messages: Sequence[ChatMessageRecord]) -> tuple[Mapping[str, Any], ...]:
    return tuple(envelope["invocation"] for _message, envelope in _audit_envelopes(messages) if envelope["_kind"] == "audit")


def _private_tool_evidence(messages: Sequence[ChatMessageRecord], call_id: str) -> tuple[ComposerDiagnosticEvidence, ...]:
    matching: list[ComposerDiagnosticEvidence] = []
    for message, envelope in _audit_envelopes(messages):
        if envelope["_kind"] != DIAGNOSTIC_EVIDENCE_KIND:
            continue
        evidence = ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(message.session_id))
        if evidence.tool_call_id == call_id and evidence.status == "success":
            matching.append(evidence)
    return tuple(matching)


def _physical_call_snapshot(messages: Sequence[ChatMessageRecord]) -> dict[UUID, ChatMessageRecord]:
    rows = tuple(message for message, envelope in _audit_envelopes(messages) if envelope["_kind"] == "llm_call_audit")
    assert len({row.id for row in rows}) == len(rows), "physical call rows and their envelopes occur exactly once"
    return {row.id: row for row in rows}


async def _prepare_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    p4_exit: str,
    fail_recovery: bool,
    restamp_postcommit_metadata: bool = False,
) -> _RollbackScenario:
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
        }
    )
    _mount, shield_requests = _install_required_route_prompt_shields(app, monkeypatch)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        app.state.catalog_service, app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing authored pipeline", "local")
    baseline = await _seed_output_only_draft(app, sessions, session.id)
    p4_sql = (
        OperationalError("PRIVATE second P4 SQL", {}, RuntimeError("PRIVATE rollback cause"))
        if p4_exit == "unwind-rollback"
        else IntegrityError("PRIVATE second P4 SQL", {}, RuntimeError("PRIVATE raw-integrity cause"))
        if p4_exit == "raw-sql-integrity"
        else DataError("PRIVATE second P4 SQL", {}, RuntimeError("PRIVATE direct-throw cause"))
    )
    scenario = _RollbackScenario(
        app=app,
        sessions=sessions,
        engine=app.state.session_engine,
        session_id=session.id,
        baseline=baseline,
        original_refusal=ComposerContextUnsupported("graph_resource_limit"),
        original_p4_sql=p4_sql,
        recovery_sql=OperationalError("PRIVATE recovery cohort SQL", {}, RuntimeError("PRIVATE recovery cause")),
        shield_requests=shield_requests,
    )
    actual_insert = sessions._insert_chat_message
    actual_persist = sessions.persist_compose_turn_async
    actual_application_persist = composer._persist_turn_audit
    actual_compose = composer.compose
    actual_add_messages_atomic = sessions.add_messages_atomic

    async def completion(**kwargs):
        scenario.sdk_requests.append(kwargs)
        ordinal = len(scenario.sdk_requests)
        assert ordinal in (1, 2), "a refused attempt cannot add an Advisor or later primary SDK call"
        if ordinal == 1:
            name, call_id, arguments = "set_metadata", _SAVED_TOOL_ID, {"patch": {"name": "Preserved pipeline"}}
        else:
            assert len(scenario.p4_outcomes) == 1 and scenario.p4_outcomes[0].checkpoint_disposition == "committed"
            name, call_id, arguments = (
                "request_advisor_hint",
                _REFUSED_TOOL_ID,
                {
                    "trigger": "proactive_security_safety",
                    "problem_summary": "Review the saved pipeline name.",
                    "recent_errors": [],
                    "attempted_actions": ["set_metadata"],
                },
            )
        tool_calls = [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        if ordinal == 2:
            tool_calls.insert(
                0,
                {
                    "id": _UNCOMMITTED_TOOL_ID,
                    "type": "function",
                    "function": {"name": "set_metadata", "arguments": json.dumps({"patch": {"name": "Uncommitted candidate"}})},
                },
            )
        return litellm.ModelResponse(
            id=f"actual-primary-response-{ordinal}",
            model=kwargs["model"],
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost": 0.001},
        )

    async def refuse_before_advisor_dispatch(*_args, **_kwargs):
        scenario.refusal_entries += 1
        raise scenario.original_refusal

    async def observe_before_atomic_recovery(session_id: UUID, drafts: Sequence[AuditMessageDraft], **kwargs):
        invocation_ids = tuple(
            envelope["invocation"]["tool_call_id"]
            for draft in drafts
            for envelope in draft.tool_calls or ()
            if envelope["_kind"] == "audit"
        )
        if fail_recovery and invocation_ids == (_UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID) and scenario.recovery_baseline_rows is None:
            assert session_id == scenario.session_id and kwargs["audit_only"] is True
            assert kwargs["required_work"].key.source is RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL
            # Provider finalization already committed each physical call. Read
            # before entering the recovery transaction, never through its conn.
            rows = tuple(await sessions.get_messages(session_id, limit=None))
            scenario.recovery_baseline_rows = rows
            scenario.recovery_baseline_llm = _physical_call_snapshot(rows)
            calls = tuple(envelope["call"] for _message, envelope in _audit_envelopes(rows) if envelope["_kind"] == "llm_call_audit")
            assert len(calls) == 2
            assert {call["provider_request_id"] for call in calls} == {"actual-primary-response-1", "actual-primary-response-2"}
            failure = scenario.tool_failures[0]
            assert {call["call_id"] for call in calls} == {call.call_id for call in failure.llm_calls}
            assert all(invocation.private_failure is None for invocation in failure.tool_invocations)
        return await actual_add_messages_atomic(session_id, drafts, **kwargs)

    def fail_inside_actual_sql(conn, **kwargs):
        inserted_id = actual_insert(conn, **kwargs)
        envelopes = kwargs["tool_calls"] or ()
        current_p4 = kwargs["role"] == "assistant" and any(
            "function" in envelope and envelope["function"]["name"] == "request_advisor_hint" for envelope in envelopes
        )
        current_recovery = (
            kwargs["role"] == "audit"
            and "audit_only" in kwargs
            and kwargs["audit_only"]
            and any(envelope["_kind"] == "audit" and envelope["invocation"]["tool_call_id"] == _REFUSED_TOOL_ID for envelope in envelopes)
        )
        if current_p4 and p4_exit != "postcommit-throw":
            scenario.p4_sql_hits += 1
            assert scenario.p4_sql_hits == 1
            assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.id == inserted_id)).scalar_one() == inserted_id
            scenario.rolled_back_assistant_ids.append(inserted_id)
            raise scenario.original_p4_sql
        if fail_recovery and current_recovery and not scenario.recovery_sql_hits:
            scenario.recovery_sql_hits += 1
            scenario.recovery_rolled_back_ids.append(inserted_id)
            assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.id == inserted_id)).scalar_one() == inserted_id
            raise scenario.recovery_sql
        return inserted_id

    async def observe_actual_p4(**kwargs):
        try:
            outcome = await actual_persist(**kwargs)
        except (AuditIntegrityError, IntegrityError) as failure:
            scenario.p4_throws.append(failure)
            raise
        scenario.p4_outcomes.append(outcome)
        if len(scenario.p4_outcomes) == 1:
            assert kwargs["plugin_crash_pending"] is False
            assert outcome.checkpoint_disposition == "committed" and outcome.checkpoint is not None
            with scenario.engine.connect() as connection:
                scenario.first_checkpoint_rows = tuple(
                    connection.execute(
                        select(chat_messages_table)
                        .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                        .order_by(chat_messages_table.c.sequence_no)
                    )
                )
                scenario.first_checkpoint_states = tuple(connection.execute(select(composition_states_table)))
            assert [row.role for row in scenario.first_checkpoint_rows] == ["assistant", "tool"]
            assert scenario.first_checkpoint_rows[1].tool_call_id == _SAVED_TOOL_ID
        else:
            assert kwargs["plugin_crash_pending"] is True
            if p4_exit == "postcommit-throw":
                assert outcome.checkpoint_disposition == "committed" and outcome.assistant_id is not None
                with scenario.engine.connect() as connection:
                    scenario.postcommit_checkpoint_rows = tuple(
                        connection.execute(
                            select(chat_messages_table)
                            .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                            .order_by(chat_messages_table.c.sequence_no)
                        )
                    )
                    scenario.postcommit_checkpoint_states = tuple(connection.execute(select(composition_states_table)))
            else:
                assert outcome.unwind_audit_failed and outcome.assistant_id is None
        return outcome

    async def throw_after_actual_committed_p4(**kwargs):
        result = await actual_application_persist(**kwargs)
        if p4_exit == "postcommit-throw" and len(scenario.p4_outcomes) == 2:
            assert result.failed_turn is not None and result.failed_turn.assistant_message_id is not None
            forensic_metadata = (
                FailedTurnMetadata(
                    assistant_message_id=None,
                    tool_calls_attempted=result.failed_turn.tool_calls_attempted,
                    tool_responses_persisted=0,
                )
                if restamp_postcommit_metadata
                else result.failed_turn
            )
            scenario.postcommit_failure = AuditIntegrityError("PRIVATE postcommit validation failure", failed_turn=forensic_metadata)
            raise scenario.postcommit_failure from scenario.postcommit_prior_cause
        return result

    async def observe_failure_carrier(*args, **kwargs):
        try:
            return await actual_compose(*args, **kwargs)
        except ComposerToolFailure as failure:
            scenario.tool_failures.append(failure)
            raise

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(composer._advisor_checkpoint, "_call_advisor_with_audit", refuse_before_advisor_dispatch)
    monkeypatch.setattr(sessions, "_insert_chat_message", fail_inside_actual_sql)
    monkeypatch.setattr(sessions, "add_messages_atomic", observe_before_atomic_recovery)
    monkeypatch.setattr(sessions, "persist_compose_turn_async", observe_actual_p4)
    monkeypatch.setattr(composer, "_persist_turn_audit", throw_after_actual_committed_p4)
    monkeypatch.setattr(composer, "compose", observe_failure_carrier)
    return scenario


def _assert_native_rollback_and_exact_carrier(scenario: _RollbackScenario, *, p4_exit: str) -> ComposerToolFailure:
    assert scenario.p4_sql_hits == scenario.refusal_entries == 1
    assert len(scenario.sdk_requests) == len(scenario.shield_requests) == 2
    assert len(scenario.tool_failures) == 1
    failure = scenario.tool_failures[0]
    assert type(failure) is ComposerToolFailure and type(failure.original_exception) is AuditIntegrityError
    assert tuple(invocation.tool_call_id for invocation in failure.tool_invocations) == (_UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID)
    assert all(invocation.tool_call_id != _SAVED_TOOL_ID for invocation in failure.tool_invocations)
    assert len(failure.llm_calls) == 2
    assert tuple(call.provider_request_id for call in failure.llm_calls) == (
        "actual-primary-response-1",
        "actual-primary-response-2",
    )
    assert len({call.call_id for call in failure.llm_calls}) == 2
    assert scenario.original_refusal in _exception_links(failure.original_exception)
    assert failure.original_exception.failed_turn is not None
    assert failure.original_exception.failed_turn.tool_calls_attempted == 2
    assert failure.original_exception.failed_turn.tool_responses_persisted == 0
    if p4_exit == "unwind-rollback":
        assert len(scenario.p4_outcomes) == 2 and scenario.p4_throws == []
        assert scenario.p4_outcomes[1].unwind_audit_failed
    else:
        assert len(scenario.p4_outcomes) == 1 and len(scenario.p4_throws) == 1
        if p4_exit == "raw-sql-integrity":
            assert scenario.p4_throws[0] is scenario.original_p4_sql
            assert failure.original_exception is not scenario.original_p4_sql, "the raw SQL violation has an owned integrity primary"
        else:
            assert failure.original_exception is scenario.p4_throws[0], "retain the exact throwing P4 integrity original"
        assert scenario.original_p4_sql in _exception_links(failure.original_exception)
    return failure


async def _assert_saved_effect_and_exact_recovered_cohort(scenario: _RollbackScenario, failure: ComposerToolFailure) -> None:
    rows = await scenario.sessions.get_messages(scenario.session_id, limit=None)
    with scenario.engine.connect() as connection:
        states = tuple(connection.execute(select(composition_states_table)))
        attempts = tuple(connection.execute(select(quota_provider_attempts_table)))
        ledger = tuple(connection.execute(select(token_usage_ledger_table)))
    assert states == scenario.first_checkpoint_states
    if scenario.recovery_baseline_llm is not None:
        assert _physical_call_snapshot(rows) == scenario.recovery_baseline_llm
    assert [(row.id, row.role, row.tool_call_id) for row in rows if row.role in ("assistant", "tool")] == [
        (UUID(row.id), row.role, row.tool_call_id) for row in scenario.first_checkpoint_rows
    ]
    assert len([row for row in rows if row.role == "tool" and row.tool_call_id == _SAVED_TOOL_ID]) == 1
    rolled_back_ids = tuple(UUID(raw_id) for raw_id in scenario.rolled_back_assistant_ids + scenario.recovery_rolled_back_ids)
    assert all(row.id not in rolled_back_ids for row in rows)
    recovered = _invocation_envelopes(rows)
    assert tuple(invocation["tool_call_id"] for invocation in recovered) == (_UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID)
    assert all(invocation["status"] == "success" for invocation in recovered)
    assert len(_private_tool_evidence(rows, _UNCOMMITTED_TOOL_ID)) == 1
    refusal_evidence = _private_tool_evidence(rows, _REFUSED_TOOL_ID)
    assert len(refusal_evidence) == 1
    refusal_result = refusal_evidence[0].result.content
    assert refusal_result is not None and refusal_result == failure.tool_invocations[1].result_canonical
    recovered_result = json.loads(refusal_result)
    assert recovered_result["error_type"] == "composer_context_unsupported"
    assert recovered_result["outbound_call_made"] is False and recovered_result["budget_used"] == 0
    assert len(_private_tool_evidence(rows, _SAVED_TOOL_ID)) == len(_private_tool_evidence(rows, _REFUSED_TOOL_ID)) == 1
    actual_calls = tuple(envelope["call"] for _message, envelope in _audit_envelopes(rows) if envelope["_kind"] == "llm_call_audit")
    assert len(actual_calls) == len(attempts) == len(ledger) == 2
    assert (
        {call["call_id"] for call in actual_calls}
        == {call.call_id for call in failure.llm_calls}
        == {attempt.attempt_id for attempt in attempts}
    )
    assert {call["provider_request_id"] for call in actual_calls} == {"actual-primary-response-1", "actual-primary-response-2"}
    assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
    checks = tuple(envelope["check"] for _message, envelope in _audit_envelopes(rows) if envelope["_kind"] == "prompt_guard_audit")
    assert [check["role"] for check in checks] == ["composer", "composer"]
    assert all(check["disposition"] == "allow" for check in checks)
    current = await scenario.sessions.get_current_state(scenario.session_id)
    assert current is not None and current.metadata_["name"] == "Preserved pipeline"
    assert str(current.id) == scenario.p4_outcomes[0].current_state_id


@pytest.mark.asyncio
@pytest.mark.parametrize("p4_exit", ["unwind-rollback", "direct-throw", "raw-sql-integrity"])
@pytest.mark.parametrize("fail_recovery", [False, True], ids=["recovery-commits", "recovery-rolls-back-then-retries"])
async def test_manual_refusal_p4_failure_recovers_only_current_suffix_through_real_route_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, p4_exit: str, fail_recovery: bool
) -> None:
    scenario = await _prepare_scenario(tmp_path, monkeypatch, p4_exit=p4_exit, fail_recovery=fail_recovery)
    try:
        async with _running_job(scenario.app, scenario.sessions, scenario.session_id, kind="compose_message", content=_REQUEST) as job:
            services = composer_app_services(scenario.app)
            with pytest.raises(AuditIntegrityError) as caught:
                await _run(scenario.app, job)
            failure = _assert_native_rollback_and_exact_carrier(scenario, p4_exit=p4_exit)
            assert job.observation.pending_exception_tool_invocations is failure.tool_invocations
            assert job.observation.pending_exception_llm_calls is failure.llm_calls
            if fail_recovery:
                assert scenario.recovery_sql_hits == 1
                assert job.observation.audit_cohort_durable is False
                assert scenario.recovery_sql in _exception_links(caught.value)
                before = await scenario.sessions.get_messages(scenario.session_id, limit=None)
                assert _invocation_envelopes(before) == ()
                assert _private_tool_evidence(before, _UNCOMMITTED_TOOL_ID) == _private_tool_evidence(before, _REFUSED_TOOL_ID) == ()
                assert len(_private_tool_evidence(before, _SAVED_TOOL_ID)) == 1
                assert scenario.recovery_baseline_rows is not None and scenario.recovery_baseline_llm is not None
                assert tuple(before) == scenario.recovery_baseline_rows, "the failed recovery must leave every durable row unchanged"
                assert _physical_call_snapshot(before) == scenario.recovery_baseline_llm
                await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
                assert job.observation.audit_cohort_durable is True
                after_retry = await scenario.sessions.get_messages(scenario.session_id, limit=None)
                assert len({row.id for row in after_retry}) == len(after_retry)
                assert tuple(after_retry[: len(before)]) == scenario.recovery_baseline_rows
                assert _physical_call_snapshot(after_retry) == scenario.recovery_baseline_llm
                added = after_retry[len(before) :]
                assert len(added) == 4, "retry appends only the two current private/invocation pairs"
                assert all(row.role == "audit" and row.tool_calls is not None and len(row.tool_calls) == 1 for row in added)
                assert tuple(envelope["_kind"] for _message, envelope in _audit_envelopes(added)) == (
                    DIAGNOSTIC_EVIDENCE_KIND,
                    "audit",
                    DIAGNOSTIC_EVIDENCE_KIND,
                    "audit",
                )
                assert tuple(
                    ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(scenario.session_id)).tool_call_id
                    for _message, envelope in _audit_envelopes(added)
                    if envelope["_kind"] == DIAGNOSTIC_EVIDENCE_KIND
                ) == (_UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID)
            else:
                assert scenario.recovery_sql_hits == 0
                assert caught.value is failure.original_exception
                assert job.observation.audit_cohort_durable is True
            await _assert_saved_effect_and_exact_recovered_cohort(scenario, failure)
            before_repeat = await scenario.sessions.get_messages(scenario.session_id, limit=None)
            await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
            assert await scenario.sessions.get_messages(scenario.session_id, limit=None) == before_repeat
            assert (await _row(job)).status == "running", "the turn leaves failure terminal ownership with the worker"
            # Use the same production failure owner as a detached worker. The
            # nominal integrity outcome stays primary after evidence recovery.
            terminal = await _worker(scenario.app, job.authority)._settle_failure(services, job.running, failure.original_exception)
            assert terminal.status == "failed" and terminal.result_json is not None
            public = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
            assert public.http_status == 500 and public.error_type == "audit_integrity_error"
            assert public.failure_code == "http_error"
            assert "PRIVATE" not in public.model_dump_json()
            assert "composer_context_unsupported" not in public.model_dump_json(), "a failed required checkpoint cannot settle as 422"
            after_terminal = await scenario.sessions.get_messages(scenario.session_id, limit=None)
            assert deep_thaw(_invocation_envelopes(after_terminal)) == deep_thaw(_invocation_envelopes(before_repeat))
            await _assert_saved_effect_and_exact_recovered_cohort(scenario, failure)
    finally:
        scenario.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restamp_metadata", [False, True], ids=["actual-committed-metadata", "restamped-forensic-zero-row-metadata"])
async def test_postcommit_p4_validation_failure_keeps_committed_context_rows_without_suffix_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restamp_metadata: bool
) -> None:
    """A non-SQL obligation throw cannot authorize fallback from metadata alone.

    The real P4 commits in both cases. Restamped zero-row forensic metadata
    deliberately disagrees with that commit and cannot replace SQL disposition
    authority. This independently controls the required direct-SQL-cause check.
    """
    scenario = await _prepare_scenario(
        tmp_path, monkeypatch, p4_exit="postcommit-throw", fail_recovery=False, restamp_postcommit_metadata=restamp_metadata
    )
    try:
        async with _running_job(scenario.app, scenario.sessions, scenario.session_id, kind="compose_message", content=_REQUEST) as job:
            services = composer_app_services(scenario.app)
            with pytest.raises(AuditIntegrityError) as caught:
                await _run(scenario.app, job)
            assert scenario.postcommit_failure is not None and caught.value is scenario.postcommit_failure
            assert scenario.original_refusal in _exception_links(caught.value)
            assert scenario.postcommit_prior_cause in _exception_links(caught.value)
            assert scenario.tool_failures == [], "committed failure metadata must not authorize an unpersisted tool suffix"
            assert scenario.p4_sql_hits == scenario.recovery_sql_hits == 0
            assert len(scenario.p4_outcomes) == 2 and all(outcome.checkpoint_disposition == "committed" for outcome in scenario.p4_outcomes)
            assert len(scenario.sdk_requests) == len(scenario.shield_requests) == 2 and scenario.refusal_entries == 1
            assert caught.value.failed_turn is not None
            expected_assistant_id = None if restamp_metadata else scenario.p4_outcomes[1].assistant_id
            expected_responses = 0 if restamp_metadata else 2
            assert caught.value.failed_turn.assistant_message_id == expected_assistant_id
            assert caught.value.failed_turn.tool_calls_attempted == 2
            assert caught.value.failed_turn.tool_responses_persisted == expected_responses
            assert job.observation.pending_exception_tool_invocations == ()
            assert job.observation.audit_cohort_durable is True
            rows = await scenario.sessions.get_messages(scenario.session_id, limit=None)
            assert _invocation_envelopes(rows) == (), "normal committed P4 rows must never acquire fallback invocation envelopes"
            assert [row.tool_call_id for row in rows if row.role == "tool"] == [_SAVED_TOOL_ID, _UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID]
            assert all(
                len(_private_tool_evidence(rows, call_id)) == 1 for call_id in (_SAVED_TOOL_ID, _UNCOMMITTED_TOOL_ID, _REFUSED_TOOL_ID)
            )
            assert [(row.id, row.role, row.tool_call_id) for row in rows if row.role in ("assistant", "tool")] == [
                (UUID(row.id), row.role, row.tool_call_id) for row in scenario.postcommit_checkpoint_rows
            ]
            with scenario.engine.connect() as connection:
                assert tuple(connection.execute(select(composition_states_table))) == scenario.postcommit_checkpoint_states
                attempts = tuple(connection.execute(select(quota_provider_attempts_table)))
                ledger = tuple(connection.execute(select(token_usage_ledger_table)))
            calls = tuple(envelope["call"] for _message, envelope in _audit_envelopes(rows) if envelope["_kind"] == "llm_call_audit")
            assert len(calls) == len(attempts) == len(ledger) == 2
            assert {call["call_id"] for call in calls} == {attempt.attempt_id for attempt in attempts}
            assert {call["provider_request_id"] for call in calls} == {"actual-primary-response-1", "actual-primary-response-2"}
            current = await scenario.sessions.get_current_state(scenario.session_id)
            assert current is not None and str(current.id) == scenario.p4_outcomes[1].current_state_id
            assert current.metadata_["name"] == "Uncommitted candidate", (
                "the actual second P4 committed this candidate before validation failed"
            )
            await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
            assert await scenario.sessions.get_messages(scenario.session_id, limit=None) == rows
            terminal = await _worker(scenario.app, job.authority)._settle_failure(services, job.running, caught.value)
            assert terminal.status == "failed" and terminal.result_json is not None
            public = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
            assert public.http_status == 500 and public.error_type == "audit_integrity_error"
            assert "PRIVATE" not in public.model_dump_json()
            assert public.body["failed_turn"]["assistant_message_id"] == expected_assistant_id
            assert public.body["failed_turn"]["tool_calls_attempted"] == 2
            assert public.body["failed_turn"]["tool_responses_persisted"] == expected_responses
            after = await scenario.sessions.get_messages(scenario.session_id, limit=None)
            assert _invocation_envelopes(after) == ()
            assert [row.id for row in after if row.role in ("assistant", "tool")] == [
                UUID(row.id) for row in scenario.postcommit_checkpoint_rows
            ]
    finally:
        scenario.engine.dispose()
