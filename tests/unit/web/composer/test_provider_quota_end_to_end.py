"""Physical Composer dispatch through real fenced quota admission and settlement."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import structlog
from litellm.exceptions import APIError, ServiceUnavailableError
from litellm.litellm_core_utils import logging_worker
from litellm.llms.openai.openai import OpenAIChatCompletion
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.capability_skill import load_pipeline_capability_core
from elspeth.web.composer.diagnostic_evidence import (
    DIAGNOSTIC_EVIDENCE_KIND,
    ComposerDiagnosticEvidence,
    diagnostic_evidence_identity,
)
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.pipeline_proposal import AbsentBase
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.composer.provider_quota import (
    ProviderInvocationFamily,
    composer_quota_scope,
    provider_call_scope,
    quota_provider_calls,
)
from elspeth.web.coordination import chargeable_admission_authority, quota_authority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import QuotaExceeded
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions._auto_title import maybe_auto_title_session
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.fenced_session import IDENTITY_TOKENS_PER_DAY, seed_token_policies
from tests.helpers.quota_guard import GuardedQuotaTurn, assert_provider_audit_companion, guarded_quota_turn
from tests.unit.web.composer.test_pipeline_planner import (
    _budget,
    _custody,
    _empty_state,
    _lifecycle,
    _model,
    _origin,
    _response,
    _ScriptedCompletion,
)


@pytest.fixture
def quota_service(tmp_path: Path) -> Iterator[tuple[Engine, SessionServiceImpl, list[QuotaExceeded]]]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'quota-dispatch.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        grant_test_pipeline_user(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")
    refusals: list[QuotaExceeded] = []
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.quota_dispatch"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
        ),
        quota_exceeded_recorder=refusals.append,
    )
    yield engine, service, refusals
    engine.dispose()


async def _lease(service: SessionServiceImpl, session_id) -> SessionOperationLease:
    return await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )


async def _audited_dispatch(turn: GuardedQuotaTurn, *, tokens: int | None, finished_at: datetime | None = None) -> None:
    recorder = BufferingRecorder()
    custody = turn.owner.mint(ProviderInvocationFamily.PRIMARY)
    messages = [{"role": "user", "content": turn.user_prompt}]
    started_at = finished_at if finished_at is not None else datetime.now(UTC)
    started_ns = time.monotonic_ns()

    def record(status: ComposerLLMCallStatus) -> None:
        call = build_llm_call_record(
            model_requested="test/model",
            messages=messages,
            tools=None,
            status=status,
            started_at=started_at,
            started_ns=started_ns,
            temperature=None,
            seed=None,
            error_class="TimeoutError" if status is ComposerLLMCallStatus.TIMEOUT else None,
            error_message="timed out" if status is ComposerLLMCallStatus.TIMEOUT else None,
            provider_custody=custody,
        )
        recorder.record_llm_call(
            dataclasses.replace(
                call,
                prompt_tokens=tokens,
                completion_tokens=0 if tokens is not None else None,
                finished_at=finished_at if finished_at is not None else call.finished_at,
            ),
            provider_custody=custody,
        )

    async with provider_call_scope(custody):
        try:
            await _litellm_acompletion(
                model="test/model",
                messages=messages,
                prompt_guard_mount=turn.mount,
                prompt_guard_mode="required",
                prompt_guard_role="composer",
                provider_custody=custody,
            )
        except TimeoutError:
            # A refusal or guard timeout before SDK entry owes no LLM record.
            if custody.needs_terminal_audit():
                record(ComposerLLMCallStatus.TIMEOUT)
            raise
        else:
            record(ComposerLLMCallStatus.SUCCESS)


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens", [IDENTITY_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY + 1])
async def test_real_admission_settles_spend_before_refusing_next_dispatch(
    quota_service, monkeypatch: pytest.MonkeyPatch, tokens: int
) -> None:
    engine, service, refusals = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        with engine.connect() as connection:
            pending = connection.execute(select(quota_provider_attempts_table)).all()
        assert len(pending) == 1 and pending[0].settled_at is None
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with guarded_quota_turn(engine, service, lease.context) as turn:
            await _audited_dispatch(turn, tokens=tokens)
            with engine.connect() as connection:
                usage = connection.execute(select(token_usage_ledger_table)).one()
            assert usage.prompt_tokens == tokens and usage.completion_tokens == 0
            with pytest.raises(ChargeableAdmissionRefused) as caught:
                await _audited_dispatch(turn, tokens=1)
    turn.assert_audit(guards=2, calls=1, private=0)
    assert len(turn.requests) == 2
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert dispatches == ["sent"]
    assert len(refusals) == 1
    assert (refusals[0].dimension, refusals[0].cap, refusals[0].ceiling, refusals[0].usage) == ("tokens", 1000, 5000, tokens)


@pytest.mark.asyncio
async def test_timeout_with_unknown_usage_blocks_next_physical_call(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, refusals = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")
        raise TimeoutError("provider response lost")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with guarded_quota_turn(engine, service, lease.context) as turn:
            with pytest.raises(TimeoutError):
                await _audited_dispatch(turn, tokens=None)
            with pytest.raises(ChargeableAdmissionRefused) as caught:
                await _audited_dispatch(turn, tokens=1)
    turn.assert_audit(guards=2, calls=1, private=0)
    assert len(turn.requests) == 2
    with engine.connect() as connection:
        usage = connection.execute(select(token_usage_ledger_table)).one()
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert dispatches == ["sent"]
    assert refusals == []


async def _run_quota_planner(
    *,
    turn: GuardedQuotaTurn,
    completion,
    recorder: BufferingRecorder,
    originating_message,
    custody_config,
):
    """Use the real planner with this test's exact configured service and owner."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    # Match the original _plan fixture's complete discovery palette. Only the
    # SDK response is scripted; guard, admission, planner and audit are real.
    full_policy = pipeline_planner.PlannerDiscoveryPolicy(
        manifest=pipeline_planner.PlannerInformationManifest(supplied=frozenset()),
        discovery_tool_names=pipeline_planner.PLANNER_DISCOVERY_TOOL_NAMES,
        unresolved_classes=(),
    )
    state = _empty_state()
    with (
        patch.object(pipeline_planner.PlannerDiscoveryPolicy, "initial", autospec=True, return_value=full_policy),
        patch("litellm.acompletion", new=completion),
    ):
        return await pipeline_planner.plan_pipeline(
            intent=turn.user_prompt,
            current_state=state,
            provider_current_state=state.to_dict(),
            schemas_loaded=frozenset(),
            mark_schema_loaded=None,
            policy_catalog=policy_catalog,
            plugin_snapshot=snapshot,
            originating_message=originating_message,
            base=AbsentBase(),
            model_config=_model(completion, max_api_attempts=2, prompt_guard_mode="required", prompt_guard_mount=turn.mount),
            rendered_skill=f"{load_pipeline_capability_core()}\n\nYou are the bounded ELSPETH pipeline planner.",
            repair_budget=1,
            budget_policy=_budget(),
            custody_config=custody_config,
            lifecycle=_lifecycle(),
            recorder=recorder,
            candidate_finalizer=lambda candidate: candidate,
            provider_service=turn.owner.service,
            provider_owner=turn.owner,
        )


@pytest.mark.asyncio
async def test_second_planning_call_503_settles_before_quota_refuses_retry(
    quota_service,
    tmp_path: Path,
    tool_context,
) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Planner quota", auth_provider_type="local")
    secret = "UPSTREAM-RESPONSE-BODY-SECRET"

    script = _ScriptedCompletion(
        _response(("list_sources", {})),
        ServiceUnavailableError(message=secret, llm_provider="test-provider", model="test/planner"),
    )

    recorder = BufferingRecorder()
    origin = dataclasses.replace(_origin(), session_id=str(session.id), user_id="alice")

    async with await _lease(service, session.id) as lease:
        custody = dataclasses.replace(
            _custody(tmp_path),
            session_engine=engine,
            session_operation_context=lease.context,
            session_operation_authority=service.session_operation_authority,
        )
        with guarded_quota_turn(engine, service, lease.context) as turn, pytest.raises(ChargeableAdmissionRefused) as caught:
            await _run_quota_planner(
                turn=turn,
                completion=script,
                recorder=recorder,
                originating_message=origin,
                custody_config=custody,
            )

    turn.assert_audit(guards=3, calls=2, private=4)
    rows = await service.get_messages(session.id, limit=None)
    assert [
        envelope["evidence"]["status"]
        for row in rows
        if row.role == "audit" and row.tool_calls
        for envelope in row.tool_calls
        if envelope["_kind"] == "composer_diagnostic_evidence"
    ] == ["provider_request", "received_response", "provider_request", "provider_failure"]
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert len(script.requests) == 2
    assert [call.status for call in recorder.llm_calls] == [ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.API_ERROR]
    assert [call.planner_call_ordinal for call in recorder.llm_calls] == [1, 2]
    assert recorder.llm_calls[1].error_class == "ServiceUnavailableError"
    assert secret not in repr(recorder.llm_calls)
    with engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        usage = connection.execute(select(token_usage_ledger_table)).all()
    assert len(attempts) == len(usage) == 2
    assert all(attempt.settled_at is not None for attempt in attempts)
    assert {call.call_id for call in recorder.llm_calls} == {attempt.attempt_id for attempt in attempts}
    attempts_by_id = {attempt.attempt_id: attempt for attempt in attempts}
    usage_by_id = {entry.entry_id: entry for entry in usage}
    success_usage = usage_by_id[attempts_by_id[recorder.llm_calls[0].call_id].ledger_entry_id]
    failure_usage = usage_by_id[attempts_by_id[recorder.llm_calls[1].call_id].ledger_entry_id]
    assert (success_usage.prompt_tokens, success_usage.completion_tokens) == (10, 5)
    assert failure_usage.prompt_tokens is None and failure_usage.completion_tokens is None

    # The settled unknown usage remains authoritative for a fresh operation.
    async with await _lease(service, session.id) as fresh:
        fresh_custody = dataclasses.replace(custody, session_operation_context=fresh.context)
        with guarded_quota_turn(engine, service, fresh.context) as fresh_turn, pytest.raises(ChargeableAdmissionRefused) as fresh_caught:
            await _run_quota_planner(
                turn=fresh_turn,
                completion=script,
                recorder=BufferingRecorder(),
                originating_message=origin,
                custody_config=fresh_custody,
            )
    fresh_turn.assert_audit(guards=1, calls=0, private=0)
    assert fresh_caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert len(script.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [sqlite3.OperationalError("settlement write failed"), TimeoutError("settlement timed out")])
@pytest.mark.parametrize("cancel", [False, True])
async def test_legacy_retry_settlement_failure_preserves_original_provider_evidence(
    quota_service, monkeypatch: pytest.MonkeyPatch, failure: Exception, cancel: bool
) -> None:
    """Legacy span integration with a synthetic callback, not a guarded SDK claim."""
    import threading

    from elspeth.web.composer.provider_context import capture_provider_context
    from elspeth.web.composer.provider_quota import (
        admit_provider_attempt,
        provider_attempt_needs_terminal_audit,
        quota_provider_calls,
    )

    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Legacy retry settlement", auth_provider_type="local")
    dispatches: list[str] = []
    messages = [{"role": "user", "content": "Build the requested pipeline."}]
    provider_error = APIError(status_code=500, message="provider failed", llm_provider="test", model="test/planner")
    budget = _budget()

    async def provider() -> None:
        # Deliberately below the physical gateway. No LiteLLM/guard callback is patched.
        dispatches.append("sent")
        raise provider_error

    settlement_calls: list[ComposerLLMCall] = []
    settlement_started = asyncio.Event()
    settlement_release = threading.Event()
    loop = asyncio.get_running_loop()
    finish = service.finish_provider_attempt
    write = service._write_audit_cohort_on_connection
    sql_calls = []

    async def observe_settlement(*, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
        settlement_calls.append(call)
        await finish(session_operation_context=session_operation_context, call=call)

    def fail_first_sql(conn, **kwargs):
        drafts = kwargs["drafts"]
        assert [draft.tool_calls[0]["_kind"] for draft in drafts] == [
            "llm_call_audit",
            DIAGNOSTIC_EVIDENCE_KIND,
            DIAGNOSTIC_EVIDENCE_KIND,
        ]
        assert all(draft.role == "audit" for draft in drafts)
        for draft in drafts:
            assert_provider_audit_companion(draft.tool_calls, kwargs["session_operation_context"])
        assert dispatches == ["sent"]
        assert drafts[0].tool_calls[0]["call"]["call_id"] == settlement_calls[-1].call_id
        sql_calls.append(settlement_calls[-1])
        if len(sql_calls) == 1:
            loop.call_soon_threadsafe(settlement_started.set)
            if cancel and not settlement_release.wait(timeout=15):
                raise AssertionError("legacy settlement pause was not released")
            raise failure
        return write(conn, **kwargs)

    monkeypatch.setattr(service, "finish_provider_attempt", observe_settlement)
    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_first_sql)
    recorder = BufferingRecorder()

    @quota_provider_calls
    async def legacy_retry_span() -> None:
        # Two bounded synthetic attempts drive the real settle-before-admit seam.
        # No quota/settlement implementation is reproduced here.
        for ordinal in range(1, 3):
            started_at = datetime.now(UTC)
            started_ns = time.monotonic_ns()
            await admit_provider_attempt(model="test/planner")
            try:
                await provider()
            except APIError as original:
                assert original is provider_error
                assert provider_attempt_needs_terminal_audit()
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested="test/planner",
                        messages=messages,
                        tools=None,
                        status=ComposerLLMCallStatus.API_ERROR,
                        started_at=started_at,
                        started_ns=started_ns,
                        temperature=None,
                        seed=None,
                        error_class="APIError",
                        error_message="provider failed",
                        max_completion_tokens_requested=budget.max_completion_tokens,
                        planner_policy_hash=budget.audit_hash,
                        planner_call_ordinal=ordinal,
                        failure=original,
                        private_request=capture_provider_context(messages=messages, tools=None),
                    )
                )
                if ordinal == 2:
                    raise

    async with await _lease(service, session.id) as lease:
        with composer_quota_scope(service, lease.context):
            task = asyncio.create_task(legacy_retry_span())
            waiter = asyncio.create_task(settlement_started.wait())
            with pytest.raises(asyncio.CancelledError if cancel else type(failure)) as caught:
                body = None
                child = None
                try:
                    done, _ = await asyncio.wait((task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED)
                    if waiter not in done and task not in done:
                        raise TimeoutError("legacy settlement phase timeout")
                    if cancel and waiter in done:
                        task.cancel("cancel during retry settlement")
                except BaseException as error:
                    body = error
                finally:
                    settlement_release.set()
                    waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await waiter
                    # Await the child directly once, retaining its original cancellation
                    # object/message while joining the real shielded settlement worker.
                    try:
                        await task
                    except BaseException as error:
                        child = error
                if body is not None and child is not None and body is not child:
                    raise BaseExceptionGroup("Legacy test body and joined child outcomes", [body, child]) from None
                if body is not None:
                    raise body
                if child is not None:
                    raise child

    if cancel:
        assert caught.value.args == ("cancel during retry settlement",)
    else:
        assert caught.value is failure
    assert dispatches == ["sent"]
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        usage = connection.execute(select(token_usage_ledger_table)).one()
        messages = connection.execute(
            select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id), chat_messages_table.c.role == "audit")
        ).all()
    assert len(messages) == 3
    assert all(message.writer_principal == "compose_loop" for message in messages)
    for message in messages:
        assert_provider_audit_companion(message.tool_calls, lease.context)
    (message,) = [row for row in messages if row.tool_calls[0]["_kind"] == "llm_call_audit"]
    private_rows = [row for row in messages if row.tool_calls[0]["_kind"] == DIAGNOSTIC_EVIDENCE_KIND]
    assert len(private_rows) == 2
    private_evidence = [
        ComposerDiagnosticEvidence.from_envelope(
            row.tool_calls[0],
            expected_session_id=str(session.id),
            expected_operation_id=lease.context.fence.operation_id,
            expected_tool_call_id=attempt.attempt_id,
        )
        for row in private_rows
    ]
    assert {item.status for item in private_evidence} == {"provider_request", "provider_failure"}
    by_status = {item.status: item for item in private_evidence}
    evidence = by_status["provider_failure"]
    request_evidence = by_status["provider_request"]
    assert evidence.tool_name == "composer_failure"
    assert evidence.evidence_id == diagnostic_evidence_identity("provider-failure", attempt.attempt_id)
    assert request_evidence.tool_name == "composer_provider_context"
    assert request_evidence.evidence_id == diagnostic_evidence_identity("provider-request", attempt.attempt_id)
    assert all(item.operation_epoch == lease.context.fence.operation_epoch for item in private_evidence)
    assert all(row.composition_state_id is None for row in private_rows)
    visible = await service.get_messages(session.id, limit=None, include_private_evidence=False)
    assert {str(row.id) for row in private_rows}.isdisjoint(str(row.id) for row in visible)
    persisted_call = message.tool_calls[0]["call"]
    assert persisted_call["planner_call_ordinal"] == 1
    assert persisted_call["error_class"] == "APIError"
    assert persisted_call["call_id"] == attempt.attempt_id
    assert attempt.settled_at is not None
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert len(settlement_calls) == 2
    assert settlement_calls[0] == settlement_calls[1]
    assert recorder.llm_calls == (settlement_calls[0],)
    assert sql_calls == settlement_calls


@pytest.mark.asyncio
async def test_unsettled_attempt_blocks_a_fresh_operation(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    async with await _lease(service, session.id) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
    # Persisted state left by death after dispatch; a new operation cannot
    # interpret its absent terminal response as an empty day's spend.
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as fresh:
        with guarded_quota_turn(engine, service, fresh.context) as turn, pytest.raises(ChargeableAdmissionRefused) as caught:
            await _audited_dispatch(turn, tokens=1)
    turn.assert_audit(guards=1, calls=0, private=0)
    assert len(turn.requests) == 1
    with engine.connect() as connection:
        pending = connection.execute(select(quota_provider_attempts_table)).one()
    assert pending.attempt_id == attempt.attempt_id and pending.settled_at is None
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert dispatches == []


@pytest.mark.asyncio
async def test_utc_midnight_reopens_daily_allowance_without_retiming_old_usage(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    before_midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    clock = [before_midnight]
    monkeypatch.setattr(quota_authority, "database_now", lambda _: clock[0])
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _: clock[0])
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with guarded_quota_turn(engine, service, lease.context) as turn:
            await _audited_dispatch(turn, tokens=IDENTITY_TOKENS_PER_DAY, finished_at=before_midnight)
            with pytest.raises(ChargeableAdmissionRefused):
                await _audited_dispatch(turn, tokens=1)
            clock[0] += timedelta(seconds=1)
            await _audited_dispatch(turn, tokens=1, finished_at=before_midnight + timedelta(seconds=1))
    turn.assert_audit(guards=3, calls=2, private=0)
    assert len(turn.requests) == 3
    with engine.connect() as connection:
        usage = connection.execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.recorded_at)).all()
    assert [row.prompt_tokens for row in usage] == [1000, 1]
    assert usage[0].recorded_at.replace(tzinfo=UTC) == before_midnight
    assert usage[1].recorded_at.replace(tzinfo=UTC) == before_midnight + timedelta(seconds=1)
    assert dispatches == ["sent", "sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replica_skew", [timedelta(days=-1), timedelta(days=1)])
async def test_provider_chronology_uses_database_origin_and_monotonic_duration(
    quota_service, monkeypatch: pytest.MonkeyPatch, replica_skew: timedelta
) -> None:
    """A skewed replica neither strands spent work nor moves it to another UTC day."""
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Clock domains", auth_provider_type="local")
    database_start = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(quota_authority, "database_now", lambda _: database_start)
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with guarded_quota_turn(engine, service, lease.context) as turn:
            custody = turn.owner.mint(ProviderInvocationFamily.PRIMARY)
            messages = [{"role": "user", "content": turn.user_prompt}]
            async with provider_call_scope(custody):
                await _litellm_acompletion(
                    model="test/model",
                    messages=messages,
                    prompt_guard_mount=turn.mount,
                    prompt_guard_mode="required",
                    prompt_guard_role="composer",
                    provider_custody=custody,
                )
                with patch("elspeth.web.composer.llm_response_parsing.time.monotonic_ns", return_value=custody._admitted_ns + 2_500_000):
                    call = build_llm_call_record(
                        model_requested="test/model",
                        messages=messages,
                        tools=None,
                        status=ComposerLLMCallStatus.SUCCESS,
                        started_at=database_start + replica_skew,
                        # Guard/admission latency precedes the database attempt.
                        started_ns=custody._admitted_ns - 1_000_000_000,
                        temperature=None,
                        seed=None,
                        provider_custody=custody,
                    )
                # Bind explicitly, as caller-side usage admission may refine the call.
                call = custody.bind_call(dataclasses.replace(call, prompt_tokens=17, completion_tokens=3))
            turn.assert_audit(guards=1, calls=1, private=0)
            assert call.started_at == database_start
            assert call.finished_at == database_start + timedelta(microseconds=2500)
            assert call.latency_ms == 1002
            # Exact replay retains the authoritative timestamp, rather than reading a new clock.
            await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        usage = connection.execute(select(token_usage_ledger_table)).one()
    assert attempt.settled_at is not None
    assert usage.recorded_at.replace(tzinfo=UTC) == call.finished_at
    assert (usage.prompt_tokens, usage.completion_tokens) == (17, 3)
    assert dispatches == ["sent"]


@pytest.mark.asyncio
async def test_custody_free_gateway_refuses_before_admission_or_callback(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired gateway fallback cannot strand quota intent behind a callback."""
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="No legacy gateway", auth_provider_type="local")
    events: list[str] = []

    def refuse() -> None:
        events.append("callback")
        raise RuntimeError("continuation budget exhausted")

    async def provider(**kwargs: Any) -> None:
        events.append("provider")

    monkeypatch.setattr("litellm.acompletion", provider)

    @quota_provider_calls
    async def invoke() -> None:
        await _litellm_acompletion(
            model="test/model",
            messages=[{"role": "user", "content": "Build a pipeline"}],
            prompt_guard_mount=None,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            on_provider_dispatch=refuse,
        )

    async with await _lease(service, session.id) as lease:
        with composer_quota_scope(service, lease.context), pytest.raises(AuditIntegrityError, match="requires exact explicit custody"):
            await invoke()
    assert events == []
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        assert connection.execute(select(token_usage_ledger_table)).all() == []


@pytest.mark.asyncio
async def test_callback_refusal_settles_owned_undispatched_attempt_at_zero(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real callback rejection writes an undispatched audit, never a fictitious call."""
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Callback refusal", auth_provider_type="local")
    events: list[str] = []
    refusal = RuntimeError("continuation budget exhausted")

    def refuse() -> None:
        events.append("callback")
        raise refusal

    async def provider(**kwargs: Any) -> None:
        events.append("provider")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with guarded_quota_turn(engine, service, lease.context) as turn:
            custody = turn.owner.mint(ProviderInvocationFamily.ADVISOR)
            with pytest.raises(RuntimeError) as caught:
                async with provider_call_scope(custody):
                    await _litellm_acompletion(
                        model="test/model",
                        messages=[{"role": "user", "content": turn.user_prompt}],
                        prompt_guard_mount=turn.mount,
                        prompt_guard_mode="required",
                        prompt_guard_role="composer",
                        provider_custody=custody,
                        on_provider_dispatch=refuse,
                    )
            assert caught.value is refusal
            assert custody.attempt is None
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        usage = connection.execute(select(token_usage_ledger_table)).one()
        messages = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
    assert events == ["callback"]
    assert attempt.settled_at is not None and attempt.ledger_entry_id == usage.entry_id
    assert (usage.prompt_tokens, usage.completion_tokens) == (0, 0)
    assert len(messages) == 2
    assert messages[0].tool_calls[0]["_kind"] == "prompt_guard_audit"
    assert messages[1].tool_calls is None
    assert '"kind":"provider_attempt.cancelled_before_dispatch"' in messages[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("retryable_failure", [False, True])
async def test_auto_title_physical_transport_attempt_has_one_quota_outcome(
    quota_service, title_logging_worker, monkeypatch: pytest.MonkeyPatch, required: bool, retryable_failure: bool
) -> None:
    """Owned title sends once; unsupported custody-free entry refuses before HTTP."""
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Untitled", auth_provider_type="local")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if retryable_failure and len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "Synthetic outage", "type": "server_error"}})
        return httpx.Response(
            200,
            json={
                "id": "synthetic-title-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "CSV Pipeline"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:

        def synthetic_http_client(shared_session: object = None) -> httpx.AsyncClient:
            return http_client

        monkeypatch.setattr(OpenAIChatCompletion, "_get_async_http_client", staticmethod(synthetic_http_client))
        async with await _lease(service, session.id) as lease:
            if not required:
                with pytest.raises(AuditIntegrityError, match="requires real fenced prompt-guard custody"):
                    await maybe_auto_title_session(
                        service=service,
                        session_id=session.id,
                        user_message="Build a CSV pipeline",
                        model="openai/gpt-4o-mini",
                        temperature=None,
                        seed=None,
                        session_operation_context=lease.context,
                        prompt_guard_mount=None,
                        prompt_guard_mode="required",
                        api_base=f"https://title-{uuid4()}.example.test/v1",
                        api_key="synthetic-title-key",
                        provider_custody=None,
                    )
                assert requests == []
                with engine.connect() as connection:
                    assert connection.execute(select(quota_provider_attempts_table)).all() == []
                    assert connection.execute(select(token_usage_ledger_table)).all() == []
                    assert connection.execute(select(chat_messages_table)).all() == []
                assert (await service.get_session(session.id)).title == "Untitled"
                return
            with guarded_quota_turn(engine, service, lease.context, user_prompt="Build a CSV pipeline") as turn:
                custody = turn.owner.mint(ProviderInvocationFamily.TITLE)
                await maybe_auto_title_session(
                    service=service,
                    session_id=session.id,
                    user_message="Build a CSV pipeline",
                    model="openai/gpt-4o-mini",
                    temperature=None,
                    seed=None,
                    session_operation_context=lease.context,
                    api_base=f"https://title-{uuid4()}.example.test/v1",
                    api_key="synthetic-title-key",
                    prompt_guard_mount=turn.mount,
                    prompt_guard_mode="required",
                    provider_custody=custody,
                )
                assert len(requests) == 1
                assert turn.coordinator.all_completed
                turn.assert_audit(guards=1, calls=1, private=0)
                assert len(turn.requests) == 1
                with engine.connect() as connection:
                    attempt = connection.execute(select(quota_provider_attempts_table)).one()
                    usage = connection.execute(select(token_usage_ledger_table)).one()
                assert attempt.source == usage.source == "auto_title"
                assert attempt.settled_at is not None and attempt.ledger_entry_id == usage.entry_id
                updated = await service.get_session(session.id)
                if retryable_failure:
                    assert (usage.prompt_tokens, usage.completion_tokens) == (None, None)
                    assert updated.title == "Untitled"
                    # A midnight rollover legitimately opens a new allowance.
                    charged_at = usage.recorded_at.replace(tzinfo=UTC)
                    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _: charged_at)
                    with pytest.raises(ChargeableAdmissionRefused) as caught:
                        await service.begin_provider_attempt(session_operation_context=lease.context, source="auto_title")
                    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
                    assert len(requests) == 1
                else:
                    assert (usage.prompt_tokens, usage.completion_tokens) == (11, 3)
                    assert updated.title == "CSV Pipeline"


@pytest_asyncio.fixture
async def title_logging_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Keep real provider callbacks owned by, and bounded to, this test loop."""
    worker = logging_worker.LoggingWorker(timeout=1)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        yield
    finally:
        await worker.stop()
