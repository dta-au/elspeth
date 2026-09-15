"""Task I1 through the real SessionServiceImpl: the Composer ledger adapters and the R14 audit seam.

Every Composer provider call is charged in the transaction that makes its audit
row durable; auto-title and run spend come through ``record_token_usage``; and a
quota refusal reaches the ``quota_exceeded_recorder`` exactly once, before the
caller learns of it.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableOperation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination import run_diagnostics_authority as diagnostics_module
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.coordination.quota_authority import QuotaExceeded, TokenUsageEntry
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows
from elspeth.web.sessions.models import (
    chat_messages_table,
    quota_policies_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import CompositionStateData, RunDiagnosticsAuditAuthority, RunDiagnosticsAuditDraft
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, seed_token_policies
from tests.unit.web.conftest import _make_session as _make_session_row
from tests.unit.web.coordination.test_durable_run_admission import _admission
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

DAY = datetime(2026, 9, 13, tzinfo=UTC)
_REQUIRED = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def _call(
    *, status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS, prompt: int | None = None, completion: int | None = None
) -> ComposerLLMCall:
    failed = status is not ComposerLLMCallStatus.SUCCESS
    call = build_llm_call_record(
        model_requested="test/model",
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=status,
        started_at=DAY,
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        error_class="TimeoutError" if failed else None,
        error_message="timed out" if failed else None,
    )
    return dataclasses.replace(call, prompt_tokens=prompt, completion_tokens=completion)


def _compose_context(session_id: str) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id, operation_id=f"ledger-{session_id}", lease_token=f"ledger-token-{session_id}", operation_epoch=1
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def _seed_compose_session(engine: Engine) -> tuple[str, SessionOperationContext]:
    session_id = str(uuid4())
    context = _compose_context(session_id)
    with engine.begin() as conn:
        _make_session_row(conn, session_id=session_id)
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=session_id,
                operation_id=context.fence.operation_id,
                lease_token=context.fence.lease_token,
                operation_kind=context.operation_kind.value,
                owner_instance_id="ledger-test-owner",
                operation_epoch=context.fence.operation_epoch,
                lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
    return session_id, context


def _ledger(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(select(token_usage_ledger_table)).all()
    return sorted(
        ((row.identity_id, row.session_id, row.source, row.run_id, row.model, row.prompt_tokens, row.completion_tokens) for row in rows),
        key=repr,
    )


@pytest.fixture
def harness(engine: Engine) -> DualFencedSessionServiceHarness:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return DualFencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


# ── the Composer adapters ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_checkpoint_settles_once_and_later_cohort_reuses_evidence(harness, engine) -> None:
    session_id, context = _seed_compose_session(engine)
    attempt = await harness.begin_provider_attempt(session_operation_context=context, source="composer")
    with engine.connect() as conn:
        assert conn.execute(select(quota_provider_attempts_table.c.settled_at)).scalar_one() is None
        assert conn.execute(select(token_usage_ledger_table.c.entry_id)).all() == []
    call = dataclasses.replace(_call(prompt=4, completion=2), call_id=attempt.attempt_id)
    await harness.finish_provider_attempt(session_operation_context=context, call=call)
    await harness.finish_provider_attempt(session_operation_context=context, call=call)
    await harness.add_messages_atomic(
        UUID(session_id),
        (AuditMessageDraft(role="audit", content="later cohort", tool_calls=(llm_call_audit_envelope(call),)),),
        writer_principal="compose_loop",
        session_operation_context=context,
    )
    with engine.connect() as conn:
        assert len(conn.execute(select(chat_messages_table.c.id)).all()) == 1
        assert len(conn.execute(select(token_usage_ledger_table.c.entry_id)).all()) == 1
        assert conn.execute(select(quota_provider_attempts_table.c.settled_at)).scalar_one() is not None
    with pytest.raises(AuditIntegrityError, match="contradicts"):
        await harness.finish_provider_attempt(session_operation_context=context, call=dataclasses.replace(call, prompt_tokens=999))


@pytest.mark.asyncio
async def test_composer_cohort_charges_its_provider_calls_with_the_audit_rows(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    session_id, context = _seed_compose_session(engine)
    await harness.add_messages_atomic(
        UUID(session_id),
        (
            AuditMessageDraft(role="audit", content="measured", tool_calls=(llm_call_audit_envelope(_call(prompt=7, completion=3)),)),
            AuditMessageDraft(
                role="audit", content="timed out", tool_calls=(llm_call_audit_envelope(_call(status=ComposerLLMCallStatus.TIMEOUT)),)
            ),
            AuditMessageDraft(role="audit", content="tool", tool_calls=({"_kind": "audit", "invocation": {}},)),
        ),
        writer_principal="compose_loop",
        session_operation_context=context,
    )
    assert _ledger(engine) == [
        ("test_user", session_id, "composer", None, "test/model", 7, 3),
        ("test_user", session_id, "composer", None, "test/model", None, None),
    ]


@pytest.mark.asyncio
async def test_composer_cohort_that_fails_after_charging_leaves_no_ledger_row(
    harness: DualFencedSessionServiceHarness, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger row rolls back with the audit cohort: accounting never outlives the evidence it was derived from."""
    session_id, context = _seed_compose_session(engine)

    def _fail_after_the_ledger_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("session bump failed after the ledger insert")

    monkeypatch.setattr(harness, "_session_mutations", _fail_after_the_ledger_write)
    with pytest.raises(RuntimeError, match="session bump failed after the ledger insert"):
        await harness.add_messages_atomic(
            UUID(session_id),
            (AuditMessageDraft(role="audit", content="measured", tool_calls=(llm_call_audit_envelope(_call(prompt=7, completion=3)),)),),
            writer_principal="compose_loop",
            session_operation_context=context,
        )
    assert _ledger(engine) == []
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == session_id)).all() == []


@pytest.mark.asyncio
async def test_composer_charges_completion_day_across_midnight_not_settlement_day(harness, engine) -> None:
    session_id, context = _seed_compose_session(engine)
    completion_time = DAY + timedelta(days=1, seconds=2)
    call = dataclasses.replace(_call(prompt=7, completion=3), started_at=DAY + timedelta(hours=23, minutes=59), finished_at=completion_time)
    await harness.add_messages_atomic(
        UUID(session_id),
        (AuditMessageDraft(role="audit", content="delayed", tool_calls=(llm_call_audit_envelope(call),)),),
        writer_principal="compose_loop",
        session_operation_context=context,
    )
    with engine.connect() as conn:
        recorded_at = conn.execute(select(token_usage_ledger_table.c.recorded_at)).scalar_one()
    assert recorded_at.replace(tzinfo=UTC) == completion_time


def test_guided_ledger_failure_rolls_back_audit_and_accounting(harness, engine, monkeypatch) -> None:
    session_id, context = _seed_compose_session(engine)
    rows = prepare_guided_audit_rows(invocations=(), llm_calls=(_call(prompt=5, completion=6),), chat_turns=())
    original = service_module.record_token_usage_on_connection

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after ledger insert")

    monkeypatch.setattr(service_module, "record_token_usage_on_connection", fail_after_write)
    with (
        pytest.raises(RuntimeError, match="after ledger insert"),
        harness._session_process_locked_begin(session_id) as conn,
        harness._session_write_lock(conn, session_id),
    ):
        harness._insert_prepared_guided_audit_rows_on_connection(
            conn,
            session_id=session_id,
            composition_state_id=None,
            audit_rows=rows,
            sequence_no=harness._reserve_sequence_range(conn, session_id, count=len(rows)),
            created_at=datetime.now(UTC),
            session_operation_context=context,
        )
    assert _ledger(engine) == []
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == session_id)).all() == []


@pytest.mark.asyncio
async def test_diagnostics_ledger_failure_rolls_back_audit_and_accounting(harness, engine, monkeypatch) -> None:
    session = await harness.create_session("alice", "Pipeline", "local")
    state = await harness.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
    run = await harness.create_run(session.id, state.id)
    authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=session.id, state_id=state.id)
    with engine.connect() as conn:
        before = conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == str(session.id))).all()
    original = diagnostics_module.record_token_usage_on_connection

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after ledger insert")

    monkeypatch.setattr(diagnostics_module, "record_token_usage_on_connection", fail_after_write)
    with pytest.raises(RuntimeError, match="after ledger insert"):
        await harness.add_run_diagnostics_audit_messages_atomic(
            authority,
            (
                RunDiagnosticsAuditDraft(
                    content="explained", tool_calls=({**llm_call_audit_envelope(_call(prompt=12, completion=8)), "run_id": str(run.id)},)
                ),
            ),
        )
    assert _ledger(engine) == []
    with engine.connect() as conn:
        assert conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.session_id == str(session.id))).all() == before


def test_guided_cohort_charges_its_llm_rows(harness: DualFencedSessionServiceHarness, engine: Engine) -> None:
    session_id, context = _seed_compose_session(engine)
    rows = prepare_guided_audit_rows(
        invocations=(), llm_calls=(_call(prompt=5, completion=6), _call(status=ComposerLLMCallStatus.TIMEOUT)), chat_turns=()
    )
    with harness._session_process_locked_begin(session_id) as conn, harness._session_write_lock(conn, session_id):
        harness._insert_prepared_guided_audit_rows_on_connection(
            conn,
            session_id=session_id,
            composition_state_id=None,
            audit_rows=rows,
            sequence_no=harness._reserve_sequence_range(conn, session_id, count=len(rows)),
            created_at=datetime.now(UTC),
            session_operation_context=context,
        )
    assert _ledger(engine) == [
        ("test_user", session_id, "composer", None, "test/model", 5, 6),
        ("test_user", session_id, "composer", None, "test/model", None, None),
    ]


@pytest.mark.asyncio
async def test_run_diagnostics_cohort_charges_its_llm_rows(harness: DualFencedSessionServiceHarness, engine: Engine) -> None:
    session = await harness.create_session("alice", "Pipeline", "local")
    state = await harness.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
    run = await harness.create_run(session.id, state.id)
    authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=session.id, state_id=state.id)
    await harness.add_run_diagnostics_audit_messages_atomic(
        authority,
        (
            RunDiagnosticsAuditDraft(
                content="explained", tool_calls=({**llm_call_audit_envelope(_call(prompt=12, completion=8)), "run_id": str(run.id)},)
            ),
        ),
    )
    assert _ledger(engine) == [("alice", str(session.id), "composer", None, "test/model", 12, 8)]


@pytest.mark.asyncio
async def test_record_token_usage_charges_auto_title_under_compose_authority(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    session_id, context = _seed_compose_session(engine)
    entry = TokenUsageEntry(model="openai/title", prompt_tokens=30, completion_tokens=6, cached_prompt_tokens=None, reasoning_tokens=None)
    entry_ids = await harness.record_token_usage(session_operation_context=context, source="auto_title", run_id=None, entries=(entry,))
    assert len(entry_ids) == 1
    assert _ledger(engine) == [("test_user", session_id, "auto_title", None, "openai/title", 30, 6)]


@pytest.mark.asyncio
async def test_record_token_usage_refuses_run_spend_under_compose_authority(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    _session_id, context = _seed_compose_session(engine)
    entry = TokenUsageEntry(model="openai/run", prompt_tokens=1, completion_tokens=1, cached_prompt_tokens=None, reasoning_tokens=None)
    with pytest.raises(ValueError, match="source='run' token usage requires execute authority"):
        await harness.record_token_usage(session_operation_context=context, source="run", run_id=uuid4(), entries=(entry,))
    assert _ledger(engine) == []


# ── R14's audit seam ──────────────────────────────────────────────────────


def _quota_service(tmp_path: Path, **recorder: Any) -> tuple[Engine, SQLiteLocalSessionOperationAuthority, SessionServiceImpl]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    authority = SQLiteLocalSessionOperationAuthority(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
        session_operation_authority=authority,
        chargeable_admission_policy=_REQUIRED,
        **recorder,
    )
    return engine, authority, service


def _spend(engine: Engine, *, session_id: str, tokens: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id=str(uuid4()),
                identity_id="alice",
                source="composer",
                session_id=session_id,
                run_id=None,
                model="test/model",
                prompt_tokens=tokens,
                completion_tokens=0,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                recorded_at=DAY + timedelta(hours=1),
            )
        )


def _exceeded(operation: str) -> QuotaExceeded:
    return QuotaExceeded(
        identity_id="alice",
        provider="local",
        operation=operation,
        dimension="tokens",
        cap=IDENTITY_TOKENS_PER_DAY,
        ceiling=CONTAINER_TOKENS_PER_DAY,
        usage=IDENTITY_TOKENS_PER_DAY,
        identity_policy_id="quota-identity",
        container_policy_id="quota-container",
    )


@pytest.mark.asyncio
async def test_composer_quota_refusal_writes_quota_exceeded_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire test for R14's audit half (spec :1161-1163): the refusal writes quota_exceeded with dimension=tokens."""
    recorded: list[QuotaExceeded] = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    decision = await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert recorded == [_exceeded("composer")]


@pytest.mark.asyncio
async def test_composer_quota_audit_derives_from_the_policy_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation-derivation: one more token of allowance on the policy row and nothing is refused or audited."""
    recorded: list[QuotaExceeded] = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    with engine.begin() as conn:
        conn.execute(
            update(quota_policies_table)
            .where(quota_policies_table.c.policy_id == "quota-identity")
            .values(tokens_per_day=IDENTITY_TOKENS_PER_DAY + 1)
        )
    decision = await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)
    assert decision.allowed
    assert recorded == []


@pytest.mark.asyncio
async def test_an_unwired_quota_recorder_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    with pytest.raises(AuditIntegrityError, match="quota_exceeded refusal for identity alice has no auth audit writer wired"):
        await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)


@pytest.mark.asyncio
async def test_run_permit_quota_refusal_is_audited_once_across_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[QuotaExceeded] = []
    engine, _authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    _admission_authority, context, run, _envelope = _admission(engine)
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(run.session_id), tokens=IDENTITY_TOKENS_PER_DAY)
    first = await service.issue_run_start_permit(run.id, session_operation_context=context)
    replay = await service.issue_run_start_permit(run.id, session_operation_context=context)
    assert first.state is StartPermitState.REFUSED
    assert first.admission_decision is not None
    assert first.admission_decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert replay == first
    assert recorded == [_exceeded("run")]
