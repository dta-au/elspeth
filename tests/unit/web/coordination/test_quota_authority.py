"""QuotaAuthority: the token ledger, the UTC-day aggregate and R14 admission (Task I1).

Every numbered refusal has a fire test and a mutation-derivation test: the
authority row (a policy's ``tokens_per_day``, a ledger measure, the database
clock) is changed, never the guard.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, inspect, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, FencedSessionWithPolicy

from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionPolicy,
    QuotaDisposition,
)
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import (
    ActiveQuotaPolicies,
    QuotaPolicyRow,
    RepositoryQuotaAuthority,
    TokenUsageEntry,
    TokenUsageSource,
    begin_provider_attempt_on_connection,
    llm_call_usage_entries,
    settle_provider_attempt_on_connection,
    utc_day_start,
)
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import quota_policies_table, sessions_table, token_usage_ledger_table

DAY = datetime(2026, 9, 13, tzinfo=UTC)
NO_REQUIRED_POLICY = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def test_container_quota_scans_have_timestamp_leading_indexes(fenced_session: FencedSession) -> None:
    indexes = {
        table_name: {index["name"]: tuple(index["column_names"]) for index in inspect(fenced_session.engine).get_indexes(table_name)}
        for table_name in ("quota_provider_attempts", "token_usage_ledger")
    }

    assert indexes["quota_provider_attempts"]["ix_quota_provider_attempts_started_identity"] == ("started_at", "identity_id")
    assert indexes["token_usage_ledger"]["ix_token_usage_ledger_recorded_identity"] == ("recorded_at", "identity_id")


def _entry(
    prompt: int | None = 100, completion: int | None = 50, *, model: str = "openai/gpt-test", call_id: str | None = None
) -> TokenUsageEntry:
    return TokenUsageEntry(
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        call_id=call_id,
        recorded_at=DAY if call_id is not None else None,
    )


def _record(
    fenced: FencedSession | FencedSessionWithPolicy, *entries: TokenUsageEntry, at: datetime, source: TokenUsageSource = "composer"
) -> tuple[str, ...]:
    return RepositoryQuotaAuthority.record_token_usage(
        fenced.connection_token,
        session_id=fenced.session_id,
        source=source,
        run_id=None,
        entries=entries,
        recorded_at=at,
    )


def _pin_database_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """R14 reads the sessions database clock; the test names the instant instead of racing midnight."""
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: now)


def _assess(fenced: FencedSessionWithPolicy, policy: ChargeableAdmissionPolicy = NO_REQUIRED_POLICY):
    return RepositoryChargeableAdmissionAuthority.assess(fenced.connection_token, session_id=fenced.session_id, policy=policy)


def _set_tokens_per_day(fenced: FencedSessionWithPolicy, policy_id: str, tokens_per_day: int) -> None:
    _resolve_mutation_connection(fenced.connection_token).execute(
        update(quota_policies_table).where(quota_policies_table.c.policy_id == policy_id).values(tokens_per_day=tokens_per_day)
    )


# ── the ledger ────────────────────────────────────────────────────────────


def test_record_token_usage_attributes_every_row_to_the_session_owner(fenced_session: FencedSession) -> None:
    entry_ids = _record(fenced_session, _entry(7, 3), _entry(None, None, model="openai/other"), at=DAY + timedelta(hours=1))
    rows = (
        _resolve_mutation_connection(fenced_session.connection_token)
        .execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.prompt_tokens.is_(None)))
        .all()
    )
    assert [row.entry_id for row in rows] == list(entry_ids)
    assert {(row.identity_id, row.session_id, row.source, row.run_id) for row in rows} == {
        (fenced_session.identity_id, fenced_session.session_id, "composer", None)
    }
    assert [(row.model, row.prompt_tokens, row.completion_tokens) for row in rows] == [
        ("openai/gpt-test", 7, 3),
        ("openai/other", None, None),
    ]


def test_record_token_usage_follows_the_session_owner_not_a_caller_argument(fenced_session: FencedSession) -> None:
    """Mutation-derivation: re-own the session and the same call is charged to the new owner."""
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(update(sessions_table).where(sessions_table.c.id == fenced_session.session_id).values(user_id="bob"))
    _record(fenced_session, _entry(), at=DAY)
    assert conn.execute(select(token_usage_ledger_table.c.identity_id)).scalar_one() == "bob"


def test_record_token_usage_requires_run_id_exactly_for_run_source(fenced_session: FencedSession) -> None:
    with pytest.raises(ValueError, match="run_id is required for, and only for, source='run'"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="run",
            run_id=None,
            entries=(_entry(),),
            recorded_at=DAY,
        )
    with pytest.raises(ValueError, match="run_id is required for, and only for, source='run'"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="composer",
            run_id="run-1",
            entries=(_entry(),),
            recorded_at=DAY,
        )


def test_run_usage_appends_only_the_calls_not_yet_recorded(fenced_session: FencedSession) -> None:
    """A resumed run re-reads its whole call list; the ledger charges each call once."""
    first = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(1, 1, call_id="first"),),
        recorded_at=DAY,
    )
    second = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(1, 1, call_id="first"), _entry(2, 2, call_id="second")),
        recorded_at=DAY,
    )
    assert len(first) == 1 and len(second) == 1
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    assert sorted(conn.execute(select(token_usage_ledger_table.c.prompt_tokens)).scalars()) == [1, 2]


def test_run_usage_refuses_a_ledger_longer_than_the_call_list(fenced_session: FencedSession) -> None:
    RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(call_id="first"), _entry(call_id="second")),
        recorded_at=DAY,
    )
    from elspeth.contracts.errors import AuditIntegrityError

    with pytest.raises(AuditIntegrityError, match="more rows than the run has LLM calls"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="run",
            run_id="run-1",
            entries=(_entry(call_id="first"),),
            recorded_at=DAY,
        )


def test_llm_call_envelopes_preserve_unknown_usage_after_failed_calls() -> None:
    base = build_llm_call_record(
        model_requested="openai/requested",
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=DAY,
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
    )
    base = dataclasses.replace(base, finished_at=DAY)
    measured = dataclasses.replace(base, model_returned="openai/returned", prompt_tokens=11, completion_tokens=4, reasoning_tokens=2)
    unreported_success = base
    failed_unreported = dataclasses.replace(base, status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="t")
    failed_reported = dataclasses.replace(failed_unreported, prompt_tokens=9, completion_tokens=0)
    envelopes = (
        llm_call_audit_envelope(measured),
        {"_kind": "audit", "invocation": {}},
        {"id": "call_1", "type": "function", "function": {"name": "set_source", "arguments": "{}"}},
        llm_call_audit_envelope(unreported_success),
        llm_call_audit_envelope(failed_unreported),
        llm_call_audit_envelope(failed_reported),
    )
    assert llm_call_usage_entries(envelopes) == (
        TokenUsageEntry(
            model="openai/returned", prompt_tokens=11, completion_tokens=4, cached_prompt_tokens=None, reasoning_tokens=2, recorded_at=DAY
        ),
        TokenUsageEntry(
            model="openai/requested",
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY,
        ),
        TokenUsageEntry(
            model="openai/requested",
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY,
        ),
        TokenUsageEntry(
            model="openai/requested",
            prompt_tokens=9,
            completion_tokens=0,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY,
        ),
    )


# ── the UTC-day aggregate ─────────────────────────────────────────────────


def test_daily_total_sums_only_the_identity_and_the_utc_day(fenced_session: FencedSession) -> None:
    """The day boundary is UTC midnight (spec :1167), named here explicitly."""
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    _record(fenced_session, _entry(1000, 1000), at=DAY - timedelta(microseconds=1))
    _record(fenced_session, _entry(100, 50), at=DAY)
    _record(fenced_session, _entry(10, 5), at=DAY + timedelta(hours=23, minutes=59, seconds=59))
    _record(fenced_session, _entry(1000, 1000), at=DAY + timedelta(days=1))
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(
        insert(token_usage_ledger_table).values(
            entry_id="bob-entry",
            identity_id="bob",
            source="composer",
            session_id=None,
            run_id=None,
            model="m",
            prompt_tokens=500,
            completion_tokens=500,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY + timedelta(hours=2),
        )
    )
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 165
    )


def test_daily_total_over_a_day_with_no_rows_is_measured_zero(fenced_session: FencedSession) -> None:
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 0
    )


def test_unknown_usage_makes_the_total_unknown_not_zero(fenced_session: FencedSession) -> None:
    _record(fenced_session, _entry(100, 50), at=DAY)
    _record(fenced_session, _entry(None, 50), at=DAY + timedelta(hours=1))
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        is None
    )


def test_unknown_total_derives_from_the_null_measure(fenced_session: FencedSession) -> None:
    """Mutation-derivation: supply the missing measure on the same row and the day is measured again."""
    (unknown_id,) = _record(fenced_session, _entry(None, 50), at=DAY)
    _resolve_mutation_connection(fenced_session.connection_token).execute(
        update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == unknown_id).values(prompt_tokens=25)
    )
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 75
    )


def test_daily_total_refuses_a_day_start_that_is_not_utc_midnight(fenced_session: FencedSession) -> None:
    with pytest.raises(ValueError, match="day_start_utc must be a UTC midnight"):
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY + timedelta(hours=1)
        )


def test_utc_day_start_converts_before_truncating() -> None:
    from datetime import timezone

    sydney_morning = datetime(2026, 9, 14, 9, 30, tzinfo=timezone(timedelta(hours=10)))
    assert utc_day_start(sydney_morning) == datetime(2026, 9, 13, tzinfo=UTC)


def test_active_policy_returns_only_unrevoked_rows(fenced_session_with_policy: FencedSessionWithPolicy) -> None:
    fenced = fenced_session_with_policy
    assert RepositoryQuotaAuthority.active_policy(fenced.connection_token, identity_id=fenced.identity_id) == ActiveQuotaPolicies(
        identity=QuotaPolicyRow(policy_id=fenced.identity_policy_id, tokens_per_day=IDENTITY_TOKENS_PER_DAY, storage_bytes=1_000_000),
        container=QuotaPolicyRow(policy_id=fenced.container_policy_id, tokens_per_day=CONTAINER_TOKENS_PER_DAY, storage_bytes=10_000_000),
    )
    _resolve_mutation_connection(fenced.connection_token).execute(
        update(quota_policies_table).where(quota_policies_table.c.policy_id == fenced.identity_policy_id).values(revoked_at=DAY)
    )
    assert RepositoryQuotaAuthority.active_policy(fenced.connection_token, identity_id=fenced.identity_id).identity is None


# ── R14 ───────────────────────────────────────────────────────────────────


def test_r14_refuses_when_the_day_total_reaches_the_identity_cap(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire test. sso-design.md:1161: refuse work that would take the identity over tokens_per_day."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(600, 400), at=DAY + timedelta(hours=1))
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert decision.evidence.quota_disposition is QuotaDisposition.EXCEEDED
    assert (decision.evidence.dimension, decision.evidence.cap, decision.evidence.ceiling, decision.evidence.usage) == (
        "tokens",
        IDENTITY_TOKENS_PER_DAY,
        CONTAINER_TOKENS_PER_DAY,
        1000,
    )
    assert (decision.evidence.identity_policy_id, decision.evidence.container_policy_id) == (
        fenced.identity_policy_id,
        fenced.container_policy_id,
    )


@pytest.mark.parametrize(("tokens_per_day", "refused"), [(1001, False), (1000, True), (999, True)])
def test_r14_derives_from_the_identity_policy_row_not_a_constant(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch, tokens_per_day: int, refused: bool
) -> None:
    """Mutation-derivation: the same 1000-token day flips on the policy row's tokens_per_day alone."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(600, 400), at=DAY + timedelta(hours=1))
    _set_tokens_per_day(fenced, fenced.identity_policy_id, tokens_per_day)
    decision = _assess(fenced)
    assert (decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED) is refused
    assert decision.evidence.quota_disposition is (QuotaDisposition.EXCEEDED if refused else QuotaDisposition.WITHIN_CAP)
    assert decision.evidence.cap == tokens_per_day


def test_r14_container_ceiling_binds_when_it_is_the_lower_bound(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-derivation for the ceiling: lower the container row below the usage and the identity cap no longer admits."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(300, 300), at=DAY + timedelta(hours=1))
    assert _assess(fenced).allowed
    _set_tokens_per_day(fenced, fenced.container_policy_id, 600)
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert (decision.evidence.cap, decision.evidence.ceiling, decision.evidence.usage) == (IDENTITY_TOKENS_PER_DAY, 600, 600)


def test_r14_container_ceiling_aggregates_usage_across_identities(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(300, 300), at=DAY + timedelta(hours=1))
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(
        insert(token_usage_ledger_table).values(
            entry_id="bob-container-entry",
            identity_id="bob",
            source="composer",
            session_id=None,
            run_id=None,
            model="openai/gpt-test",
            prompt_tokens=300,
            completion_tokens=300,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY + timedelta(hours=2),
        )
    )
    _set_tokens_per_day(fenced, fenced.container_policy_id, 1000)

    decision = _assess(fenced)

    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert decision.evidence.usage == 1200


@pytest.mark.parametrize(("clock", "refused"), [(DAY - timedelta(microseconds=1), True), (DAY, False)])
def test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch, clock: datetime, refused: bool
) -> None:
    """Rollover (spec §Testing :1297-1298): a full day at 23:59:59.999999 stops counting at 00:00:00 UTC."""
    fenced = fenced_session_with_policy
    _record(fenced, _entry(800, 200), at=DAY - timedelta(hours=1))
    _pin_database_clock(monkeypatch, clock)
    decision = _assess(fenced)
    assert (decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED) is refused
    assert decision.evidence.usage == (1000 if refused else 0)


def test_r14_empty_ledger_admits_within_the_cap_as_measured_zero(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    decision = _assess(fenced_session_with_policy)
    assert decision.allowed
    assert decision.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
    assert decision.evidence.usage == 0


def test_r14_unknown_usage_refuses_as_accounting_unavailable(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(None, None), at=DAY + timedelta(hours=1))
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert decision.evidence.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE
    assert decision.evidence.usage is None


def test_r14_required_policy_missing_still_refuses_before_measuring(fenced_session: FencedSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    required = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    decision = RepositoryChargeableAdmissionAuthority.assess(
        fenced_session.connection_token, session_id=fenced_session.session_id, policy=required
    )
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_POLICY_MISSING
    assert decision.evidence.usage is None


def test_run_replay_keeps_call_day_and_deduplicates_reordering(fenced_session: FencedSession) -> None:
    entries = (
        dataclasses.replace(_entry(10, 5, call_id="before-midnight"), recorded_at=DAY - timedelta(microseconds=1)),
        dataclasses.replace(_entry(20, 5, call_id="after-midnight"), recorded_at=DAY),
    )
    first = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="midnight-run",
        entries=entries,
        recorded_at=DAY + timedelta(days=1),
    )
    replay = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="midnight-run",
        entries=tuple(reversed(entries)),
        recorded_at=DAY + timedelta(days=2),
    )
    assert len(first) == 2
    assert replay == ()
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 25
    )
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY - timedelta(days=1)
        )
        == 15
    )


@pytest.mark.parametrize("change", ["measure", "id", "owner", "timestamp"])
def test_run_replay_refuses_changed_evidence(fenced_session: FencedSession, change: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    entry = _entry(10, 5, call_id="call-1")
    RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="replay-run",
        entries=(entry,),
        recorded_at=DAY,
    )
    if change == "measure":
        entry = dataclasses.replace(entry, prompt_tokens=11)
    elif change == "id":
        entry = dataclasses.replace(entry, call_id="call-2")
    elif change == "timestamp":
        entry = dataclasses.replace(entry, recorded_at=DAY + timedelta(hours=1))
    else:
        conn = _resolve_mutation_connection(fenced_session.connection_token)
        ensure_test_identity(conn, identity_id="bob")
        conn.execute(update(sessions_table).where(sessions_table.c.id == fenced_session.session_id).values(user_id="bob"))
    with pytest.raises(AuditIntegrityError):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="run",
            run_id="replay-run",
            entries=(entry,),
            recorded_at=DAY,
        )


@pytest.mark.parametrize("usage", [0, 999, 1000, 1001])
def test_contract_measured_disposition_must_agree_with_limit(usage: int) -> None:
    from pydantic import ValidationError

    from elspeth.contracts.chargeable_admission import AdmissionPolicyEvidence

    incorrect = QuotaDisposition.WITHIN_CAP if usage >= 1000 else QuotaDisposition.EXCEEDED
    with pytest.raises(ValidationError, match="contradicts measured usage"):
        AdmissionPolicyEvidence(
            quota_disposition=incorrect,
            identity_policy_id="policy",
            dimension="tokens",
            cap=1000,
            usage=usage,
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
        )


def _operation(fenced: FencedSessionWithPolicy, *, epoch: int = 1) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(session_id=fenced.session_id, operation_id="operation", lease_token="lease", operation_epoch=epoch),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def test_pending_attempt_blocks_new_operation_but_allows_same_live_fence(fenced_session_with_policy: FencedSessionWithPolicy) -> None:
    from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused

    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    context = _operation(fenced)
    first = begin_provider_attempt_on_connection(conn, session_operation_context=context, source="composer", policy=NO_REQUIRED_POLICY)
    assert _assess(fenced).refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    second = begin_provider_attempt_on_connection(conn, session_operation_context=context, source="composer", policy=NO_REQUIRED_POLICY)
    assert first.attempt_id != second.attempt_id
    with pytest.raises(ChargeableAdmissionRefused):
        begin_provider_attempt_on_connection(
            conn, session_operation_context=_operation(fenced, epoch=2), source="composer", policy=NO_REQUIRED_POLICY
        )


@pytest.mark.parametrize("unknown", [False, True])
def test_settled_attempt_removes_pending_and_preserves_unknown_usage(
    fenced_session_with_policy: FencedSessionWithPolicy, unknown: bool
) -> None:
    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    attempt = begin_provider_attempt_on_connection(
        conn, session_operation_context=_operation(fenced), source="composer", policy=NO_REQUIRED_POLICY
    )
    entry = dataclasses.replace(
        _entry(None if unknown else 10, None if unknown else 5), call_id=attempt.attempt_id, recorded_at=attempt.started_at
    )
    settle_provider_attempt_on_connection(conn, session_id=fenced.session_id, attempt_id=attempt.attempt_id, entry=entry)
    settle_provider_attempt_on_connection(conn, session_id=fenced.session_id, attempt_id=attempt.attempt_id, entry=entry)
    decision = _assess(fenced)
    assert decision.allowed is not unknown
    if unknown:
        assert decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    else:
        assert decision.evidence.usage == 15
    assert len(conn.execute(select(token_usage_ledger_table)).all()) == 1


def test_attempt_terminal_replay_cannot_change_recorded_usage(fenced_session_with_policy: FencedSessionWithPolicy) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    attempt = begin_provider_attempt_on_connection(
        conn, session_operation_context=_operation(fenced), source="composer", policy=NO_REQUIRED_POLICY
    )
    entry = dataclasses.replace(_entry(10, 5), call_id=attempt.attempt_id, recorded_at=attempt.started_at)
    settle_provider_attempt_on_connection(conn, session_id=fenced.session_id, attempt_id=attempt.attempt_id, entry=entry)
    with pytest.raises(AuditIntegrityError, match="contradicts recorded usage"):
        settle_provider_attempt_on_connection(
            conn, session_id=fenced.session_id, attempt_id=attempt.attempt_id, entry=dataclasses.replace(entry, prompt_tokens=11)
        )


def test_abandoned_attempt_day_rolls_over_at_utc_midnight(fenced_session_with_policy: FencedSessionWithPolicy) -> None:
    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    attempt = begin_provider_attempt_on_connection(
        conn, session_operation_context=_operation(fenced), source="composer", policy=NO_REQUIRED_POLICY
    )
    day = utc_day_start(attempt.started_at)
    assert RepositoryQuotaAuthority.daily_token_total(fenced.connection_token, identity_id=fenced.identity_id, day_start_utc=day) is None
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced.connection_token, identity_id=fenced.identity_id, day_start_utc=day + timedelta(days=1)
        )
        == 0
    )


@pytest.mark.parametrize("changed", ["session", "owner", "call"])
def test_attempt_settlement_rejects_changed_custody(fenced_session_with_policy: FencedSessionWithPolicy, changed: str) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    fenced = fenced_session_with_policy
    conn = _resolve_mutation_connection(fenced.connection_token)
    attempt = begin_provider_attempt_on_connection(
        conn, session_operation_context=_operation(fenced), source="composer", policy=NO_REQUIRED_POLICY
    )
    entry = dataclasses.replace(_entry(10, 5), call_id=attempt.attempt_id, recorded_at=attempt.started_at)
    session_id = fenced.session_id
    if changed == "session":
        session_id = "another-session"
    elif changed == "owner":
        ensure_test_identity(conn, identity_id="bob")
        conn.execute(update(sessions_table).where(sessions_table.c.id == session_id).values(user_id="bob"))
    else:
        settle_provider_attempt_on_connection(conn, session_id=session_id, attempt_id=attempt.attempt_id, entry=entry)
        entry = dataclasses.replace(entry, call_id="different-actual-call")
    with pytest.raises(AuditIntegrityError):
        settle_provider_attempt_on_connection(conn, session_id=session_id, attempt_id=attempt.attempt_id, entry=entry)
