"""QuotaAuthority: the sole writer of ``token_usage_ledger`` and the reader of applicable quota policy.

R14 (sso-design.md:1161-1167) refuses chargeable work that would take an
identity over its ``tokens_per_day`` or the container ceiling. The measurement
lives here and nowhere else:

* ``record_token_usage_on_connection`` appends one ledger row per provider call,
  attributed to the session's owning identity, on the CALLER's connection so the
  accounting row commits with the audit row it was derived from. The three
  adapters are the Composer audit cohorts (``SessionServiceImpl.add_messages_atomic``,
  ``_insert_prepared_guided_audit_rows_on_connection``,
  ``RepositoryRunDiagnosticsAuditAuthority.append_audit_messages``), auto-title
  (``SessionServiceImpl.record_token_usage`` from ``_auto_title.py``) and run
  finalisation (the same service method from ``execution/service.py``).
* ``RepositoryQuotaAuthority.daily_token_total`` sums one identity's UTC day,
  while ``container_daily_token_total`` sums all identity-attributed usage in
  the container. NULL measures mean the provider reported no usage: unknown,
  never zero, so a day containing one returns ``None`` and admission refuses
  with ``token_accounting_unavailable``. Durable pending attempts also make
  the applicable day unknown, except concurrent attempts under the same
  validated live fence. Completed calls are charged on their audit timestamp's
  UTC day.
* ``RepositoryQuotaAuthority.active_policy`` locks and returns the identity
  policy row and the container ceiling row in force.

Sessions-side code never opens the Landscape. A refusal is reported to the
caller as a typed ``QuotaExceeded`` which the caller's ``record`` callback turns
into the Landscape ``quota_exceeded`` row (``AuthAuditWriter.record_quota_exceeded``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, final

from sqlalchemy import BigInteger, and_, case, cast, func, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import IdentityStorageQuotaExceededError, StorageAccountingUnavailableError
from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.sessions.models import (
    blobs_table,
    quota_policies_table,
    quota_provider_attempts_table,
    runs_table,
    sessions_table,
    token_usage_ledger_table,
)

TokenUsageSource = Literal["composer", "run", "auto_title"]
_TOKEN_USAGE_SOURCES: frozenset[str] = frozenset({"composer", "run", "auto_title"})


@final
@dataclass(frozen=True, slots=True)
class TokenUsageEntry:
    """One provider call's usage. ``None`` is an unreported measure, never zero."""

    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None
    recorded_at: datetime | None = None
    call_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("TokenUsageEntry.model must be a non-blank exact string")
        for name, value in (
            ("prompt_tokens", self.prompt_tokens),
            ("completion_tokens", self.completion_tokens),
            ("cached_prompt_tokens", self.cached_prompt_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"TokenUsageEntry.{name} must be a non-negative exact int or None")
        if self.recorded_at is not None and (type(self.recorded_at) is not datetime or self.recorded_at.tzinfo is None):
            raise ValueError("TokenUsageEntry.recorded_at must be an aware datetime or None")
        if self.call_id is not None and (type(self.call_id) is not str or not self.call_id):
            raise ValueError("TokenUsageEntry.call_id must be a non-empty exact string or None")


@final
@dataclass(frozen=True, slots=True)
class QuotaPolicyRow:
    policy_id: str
    tokens_per_day: int
    storage_bytes: int


@final
@dataclass(frozen=True, slots=True)
class ActiveQuotaPolicies:
    """The identity's unrevoked policy row and the unrevoked container ceiling row, either possibly absent."""

    identity: QuotaPolicyRow | None
    container: QuotaPolicyRow | None


@final
@dataclass(frozen=True, slots=True)
class QuotaExceeded:
    """A committed quota refusal, handed to the caller's ``record`` callback (spec :834)."""

    identity_id: str
    provider: AuthProviderType
    operation: str
    dimension: Literal["tokens", "storage"]
    cap: int | None
    ceiling: int | None
    usage: int
    identity_policy_id: str | None
    container_policy_id: str | None


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    attempt_id: str
    started_at: datetime


def begin_provider_attempt_on_connection(
    connection: Connection,
    *,
    session_operation_context: SessionOperationContext,
    source: TokenUsageSource,
    policy: ChargeableAdmissionPolicy,
    run_id: str | None = None,
) -> ProviderAttempt:
    """Record dispatch intent after the caller validates the live operation fence."""
    from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority

    if source not in _TOKEN_USAGE_SOURCES or (source == "run") != (run_id is not None):
        raise ValueError("A provider attempt requires a valid source and run_id exactly for run work")
    fence = session_operation_context.fence
    if run_id is not None:
        run_session = connection.execute(select(runs_table.c.session_id).where(runs_table.c.id == run_id)).scalar_one()
        if run_session != fence.session_id:
            raise AuditIntegrityError("Provider attempt run belongs to another session")
    token = _register_mutation_connection(connection)
    try:
        decision = RepositoryChargeableAdmissionAuthority.assess(
            token, session_id=fence.session_id, policy=policy, live_operation_context=session_operation_context
        )
    finally:
        _unregister_mutation_connection(token)
    if not decision.allowed:
        raise ChargeableAdmissionRefused(decision)
    owner = connection.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == fence.session_id)).scalar_one()
    attempt = ProviderAttempt(attempt_id=str(uuid.uuid4()), started_at=database_now(connection))
    connection.execute(
        insert(quota_provider_attempts_table).values(
            attempt_id=attempt.attempt_id,
            identity_id=owner,
            session_id=fence.session_id,
            source=source,
            started_at=attempt.started_at,
            settled_at=None,
            operation_id=fence.operation_id,
            operation_epoch=fence.operation_epoch,
            lease_token=fence.lease_token,
            run_id=run_id,
        )
    )
    return attempt


def settle_provider_attempt_on_connection(connection: Connection, *, session_id: str, attempt_id: str, entry: TokenUsageEntry) -> None:
    """Settle a durable dispatch attempt and its usage in the caller's audit transaction."""
    attempt = connection.execute(
        select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == attempt_id).with_for_update()
    ).one()
    if attempt.session_id != session_id or entry.call_id is None or entry.recorded_at is None:
        raise AuditIntegrityError("Provider attempt settlement requires its session and actual call identity")
    owner = connection.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id)).scalar_one()
    if owner != attempt.identity_id:
        raise AuditIntegrityError("Provider attempt settlement changed its owning identity")
    entry_id = _usage_entry_id(session_id=session_id, source=attempt.source, run_id=attempt.run_id, call_id=entry.call_id)
    previous = connection.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == entry_id)).one_or_none()
    if previous is not None:
        previous_time = previous.recorded_at
        if previous_time.tzinfo is None:
            previous_time = previous_time.replace(tzinfo=UTC)
        if (
            attempt.settled_at is None
            or attempt.ledger_entry_id != entry_id
            or previous.identity_id != attempt.identity_id
            or previous.session_id != session_id
            or previous.prompt_tokens != entry.prompt_tokens
            or previous.completion_tokens != entry.completion_tokens
            or previous.cached_prompt_tokens != entry.cached_prompt_tokens
            or previous.reasoning_tokens != entry.reasoning_tokens
            or previous.model != entry.model
            or previous_time != entry.recorded_at
        ):
            raise AuditIntegrityError("Provider attempt settlement contradicts recorded usage")
        return
    if attempt.settled_at is not None:
        raise AuditIntegrityError("Settled provider attempt has no usage record")
    connection.execute(
        insert(token_usage_ledger_table).values(
            entry_id=entry_id,
            identity_id=attempt.identity_id,
            session_id=session_id,
            source=attempt.source,
            run_id=attempt.run_id,
            model=entry.model,
            prompt_tokens=entry.prompt_tokens,
            completion_tokens=entry.completion_tokens,
            cached_prompt_tokens=entry.cached_prompt_tokens,
            reasoning_tokens=entry.reasoning_tokens,
            recorded_at=entry.recorded_at.astimezone(UTC),
        )
    )
    connection.execute(
        update(quota_provider_attempts_table)
        .where(quota_provider_attempts_table.c.attempt_id == attempt_id)
        .values(settled_at=database_now(connection), ledger_entry_id=entry_id)
    )


def _usage_entry_id(*, session_id: str, source: TokenUsageSource, run_id: str | None, call_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"elspeth:token-usage:{session_id}:{source}:{run_id}:{call_id}"))


def settled_provider_attempt_ids_on_connection(connection: Connection, *, session_id: str, attempt_ids: tuple[str, ...]) -> frozenset[str]:
    if not attempt_ids:
        return frozenset()
    return frozenset(
        connection.execute(
            select(quota_provider_attempts_table.c.attempt_id).where(
                quota_provider_attempts_table.c.session_id == session_id,
                quota_provider_attempts_table.c.attempt_id.in_(attempt_ids),
                quota_provider_attempts_table.c.settled_at.is_not(None),
            )
        ).scalars()
    )


def utc_day_start(now: datetime) -> datetime:
    """The UTC midnight that opens ``now``'s day: R14's day boundary (spec :1167)."""
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("utc_day_start requires an aware datetime")
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _daily_token_total_on_connection(
    connection: Connection,
    *,
    identity_id: str | None,
    day_start_utc: datetime,
    live_operation_context: SessionOperationContext | None,
) -> int | None:
    """Measure one identity or all identity-attributed usage for a UTC day."""
    pending = quota_provider_attempts_table.c
    pending_conditions = [
        pending.started_at >= day_start_utc,
        pending.started_at < day_start_utc + timedelta(days=1),
        pending.settled_at.is_(None),
    ]
    if identity_id is None:
        pending_conditions.append(pending.identity_id.is_not(None))
    else:
        pending_conditions.append(pending.identity_id == identity_id)
    pending_query = select(pending.attempt_id).where(*pending_conditions)
    if live_operation_context is not None:
        fence = live_operation_context.fence
        pending_query = pending_query.where(
            ~and_(
                pending.session_id == fence.session_id,
                pending.operation_id == fence.operation_id,
                pending.operation_epoch == fence.operation_epoch,
                pending.lease_token == fence.lease_token,
            )
        )
    if connection.execute(pending_query.limit(1)).first() is not None:
        return None

    ledger = token_usage_ledger_table.c
    measured = cast(func.coalesce(ledger.prompt_tokens, 0), BigInteger) + cast(func.coalesce(ledger.completion_tokens, 0), BigInteger)
    unknown = case((or_(ledger.prompt_tokens.is_(None), ledger.completion_tokens.is_(None)), 1), else_=0)
    ledger_conditions = [
        ledger.recorded_at >= day_start_utc,
        ledger.recorded_at < day_start_utc + timedelta(days=1),
    ]
    if identity_id is None:
        # NULL ledger identities are boot-probe rows and spend on no identity's
        # behalf; a container ceiling covers identity-attributed usage only.
        ledger_conditions.append(ledger.identity_id.is_not(None))
    else:
        ledger_conditions.append(ledger.identity_id == identity_id)
    row = connection.execute(
        select(
            func.count().label("row_count"),
            func.coalesce(func.sum(unknown), 0).label("unknown_count"),
            func.coalesce(func.sum(measured), 0).label("measured_total"),
        ).where(*ledger_conditions)
    ).one()
    if row.row_count == 0:
        return 0
    if row.unknown_count:
        return None
    return int(row.measured_total)


def token_usage_entry_from_llm_call_envelope(envelope: Mapping[str, Any]) -> TokenUsageEntry | None:
    """Derive the ledger entry for one persisted ``llm_call_audit`` envelope.

    ELSPETH wrote the envelope (``composer/audit.py`` ``llm_call_audit_envelope``
    and the guided failure projection in ``sessions/guided_audit.py``), so its
    keys are read directly. Failed calls may have consumed provider tokens
    before their response was lost, so missing usage remains unknown too.
    """
    if envelope["_kind"] != "llm_call_audit":
        raise AuditIntegrityError("Token usage is derived only from an llm_call_audit envelope")
    call = envelope["call"]
    prompt_tokens = call["prompt_tokens"]
    completion_tokens = call["completion_tokens"]
    return TokenUsageEntry(
        model=call["model_requested"] if call["model_returned"] is None else call["model_returned"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_prompt_tokens=call["cached_prompt_tokens"],
        reasoning_tokens=call["reasoning_tokens"],
        recorded_at=datetime.fromisoformat(call["finished_at"]),
        call_id=call["call_id"],
    )


def llm_call_usage_entries(tool_calls: Sequence[Mapping[str, Any]]) -> tuple[TokenUsageEntry, ...]:
    """Every chargeable entry among a cohort's ``tool_calls`` envelopes.

    Assistant rows carry OpenAI tool-call request dicts, which have no
    ``_kind`` key; only ``llm_call_audit`` envelopes are provider calls.
    """
    entries: list[TokenUsageEntry] = []
    for envelope in tool_calls:
        if "_kind" in envelope and envelope["_kind"] == "llm_call_audit":
            entry = token_usage_entry_from_llm_call_envelope(envelope)
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


def record_token_usage_on_connection(
    connection: Connection,
    *,
    session_id: str,
    source: TokenUsageSource,
    run_id: str | None,
    entries: Sequence[TokenUsageEntry],
    recorded_at: datetime,
) -> tuple[str, ...]:
    """Append ledger rows for ``entries`` on the caller's connection; return the new entry ids.

    ``connection`` must be the transaction that wrote the evidence the entries
    were derived from, so the evidence and its accounting commit together.

    For ``source='run'`` entries are the run's complete LLM call list. Stable
    call IDs identify replayed entries; changing prior evidence is corruption.
    """
    if source not in _TOKEN_USAGE_SOURCES:
        raise ValueError(f"token usage source {source!r} is not one of composer, run, auto_title")
    if (source == "run") != (run_id is not None):
        raise ValueError("run_id is required for, and only for, source='run'")
    if type(recorded_at) is not datetime or recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be an aware datetime")
    if any(type(entry) is not TokenUsageEntry for entry in entries):
        raise TypeError("entries must contain exact TokenUsageEntry instances")
    owner = connection.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id).with_for_update()).one_or_none()
    if owner is None:
        raise AuditIntegrityError("Token usage names a session that does not exist")
    existing_by_id = {}
    if run_id is not None:
        existing = connection.execute(
            select(token_usage_ledger_table).where(token_usage_ledger_table.c.run_id == run_id, token_usage_ledger_table.c.source == "run")
        ).all()
        existing_by_id = {row.entry_id: row for row in existing}
        if len(existing_by_id) > len(entries):
            raise AuditIntegrityError("Run token usage ledger holds more rows than the run has LLM calls")
        if any(entry.call_id is None or entry.recorded_at is None for entry in entries):
            raise ValueError("Run token usage requires each call_id and recorded_at")
        if len({entry.call_id for entry in entries}) != len(entries):
            raise AuditIntegrityError("Run token usage repeats a call_id")
    planned_ids = tuple(
        _usage_entry_id(session_id=session_id, source=source, run_id=run_id, call_id=entry.call_id)
        if entry.call_id is not None
        else str(uuid.uuid4())
        for entry in entries
    )
    if set(existing_by_id) - set(planned_ids):
        raise AuditIntegrityError("Run token usage omits previously recorded calls")
    entry_ids: list[str] = []
    for entry, entry_id in zip(entries, planned_ids, strict=True):
        if entry.call_id is not None:
            attempt = connection.execute(
                select(quota_provider_attempts_table.c.attempt_id).where(quota_provider_attempts_table.c.attempt_id == entry.call_id)
            ).one_or_none()
            if attempt is not None:
                settle_provider_attempt_on_connection(connection, session_id=session_id, attempt_id=entry.call_id, entry=entry)
                continue
        charged_at = (entry.recorded_at if entry.recorded_at is not None else recorded_at).astimezone(UTC)
        if entry.call_id is not None and run_id is None:
            previous_row = connection.execute(
                select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == entry_id)
            ).one_or_none()
            if previous_row is not None:
                existing_by_id[entry_id] = previous_row
        if entry_id in existing_by_id:
            previous = existing_by_id[entry_id]
            previous_time = previous.recorded_at
            if previous_time.tzinfo is None:
                previous_time = previous_time.replace(tzinfo=UTC)
            if (
                previous.identity_id != owner.user_id
                or previous.session_id != session_id
                or previous.model != entry.model
                or previous.prompt_tokens != entry.prompt_tokens
                or previous.completion_tokens != entry.completion_tokens
                or previous.cached_prompt_tokens != entry.cached_prompt_tokens
                or previous.reasoning_tokens != entry.reasoning_tokens
                or previous_time != charged_at
            ):
                raise AuditIntegrityError("Run token usage contradicts previously recorded call evidence")
            continue
        connection.execute(
            insert(token_usage_ledger_table).values(
                entry_id=entry_id,
                identity_id=owner.user_id,
                source=source,
                session_id=session_id,
                run_id=run_id,
                model=entry.model,
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                cached_prompt_tokens=entry.cached_prompt_tokens,
                reasoning_tokens=entry.reasoning_tokens,
                recorded_at=charged_at,
            )
        )
        entry_ids.append(entry_id)
    return tuple(entry_ids)


@final
class RepositoryQuotaAuthority:
    """Token-bound quota reads and ledger writes inside the caller's fenced transaction."""

    @staticmethod
    def record_token_usage(
        connection_token: str,
        *,
        session_id: str,
        source: TokenUsageSource,
        run_id: str | None,
        entries: Sequence[TokenUsageEntry],
        recorded_at: datetime,
    ) -> tuple[str, ...]:
        return record_token_usage_on_connection(
            _resolve_mutation_connection(connection_token),
            session_id=session_id,
            source=source,
            run_id=run_id,
            entries=entries,
            recorded_at=recorded_at,
        )

    @staticmethod
    def daily_token_total(
        connection_token: str,
        *,
        identity_id: str,
        day_start_utc: datetime,
        live_operation_context: SessionOperationContext | None = None,
    ) -> int | None:
        """``prompt + completion`` over ``[day_start_utc, day_start_utc + 1 day)``; ``None`` when any row is unknown."""
        if utc_day_start(day_start_utc) != day_start_utc or day_start_utc.utcoffset() != timedelta(0):
            raise ValueError("day_start_utc must be a UTC midnight")
        return _daily_token_total_on_connection(
            _resolve_mutation_connection(connection_token),
            identity_id=identity_id,
            day_start_utc=day_start_utc,
            live_operation_context=live_operation_context,
        )

    @staticmethod
    def container_daily_token_total(
        connection_token: str,
        *,
        day_start_utc: datetime,
        live_operation_context: SessionOperationContext | None = None,
    ) -> int | None:
        """Sum known token usage for every identity in the container's UTC day."""
        if utc_day_start(day_start_utc) != day_start_utc or day_start_utc.utcoffset() != timedelta(0):
            raise ValueError("day_start_utc must be a UTC midnight")
        return _daily_token_total_on_connection(
            _resolve_mutation_connection(connection_token),
            identity_id=None,
            day_start_utc=day_start_utc,
            live_operation_context=live_operation_context,
        )

    @staticmethod
    def active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies:
        """Lock and return the identity's unrevoked policy and the unrevoked container ceiling."""
        return _active_policy_rows(_resolve_mutation_connection(connection_token), identity_id=identity_id, for_update=True)

    @staticmethod
    def admit_storage_bytes(
        connection_token: str,
        *,
        session_id: str,
        additional_bytes: int,
        operation: StorageAdmissionOperation,
        record: Callable[[QuotaExceeded], None],
    ) -> StorageAdmission:
        return admit_storage_bytes_on_connection(
            _resolve_mutation_connection(connection_token),
            session_id=session_id,
            additional_bytes=additional_bytes,
            operation=operation,
            record=record,
        )


StorageAdmissionOperation = Literal["blob_create", "inline_custody", "run_output_finalize", "blob_replacement", "session_fork"]
_STORAGE_ADMISSION_OPERATIONS = frozenset({"blob_create", "inline_custody", "run_output_finalize", "blob_replacement", "session_fork"})


@final
@dataclass(frozen=True, slots=True)
class StorageAdmission:
    identity_id: str
    additional_bytes: int
    usage: int | None
    cap: int | None
    ceiling: int | None


def refuse_unrecorded_quota_exceeded(outcome: QuotaExceeded) -> None:
    """A quota refusal without a Landscape recorder fails closed."""
    raise AuditIntegrityError(f"quota_exceeded refusal for identity {outcome.identity_id} has no auth audit writer wired")


def _active_policy_rows(connection: Connection, *, identity_id: str, for_update: bool) -> ActiveQuotaPolicies:
    policy = quota_policies_table.c
    identity_query = select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes).where(
        policy.identity_id == identity_id, policy.revoked_at.is_(None)
    )
    container_query = select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes).where(
        policy.identity_id.is_(None), policy.revoked_at.is_(None)
    )
    if for_update:
        identity_query = identity_query.with_for_update()
        container_query = container_query.with_for_update()
    identity = connection.execute(identity_query).one_or_none()
    container = connection.execute(container_query).one_or_none()
    return ActiveQuotaPolicies(
        identity=None
        if identity is None
        else QuotaPolicyRow(policy_id=identity.policy_id, tokens_per_day=identity.tokens_per_day, storage_bytes=identity.storage_bytes),
        container=None
        if container is None
        else QuotaPolicyRow(policy_id=container.policy_id, tokens_per_day=container.tokens_per_day, storage_bytes=container.storage_bytes),
    )


def admit_storage_bytes_on_connection(
    connection: Connection,
    *,
    session_id: str,
    additional_bytes: int,
    operation: StorageAdmissionOperation,
    record: Callable[[QuotaExceeded], None],
) -> StorageAdmission:
    """Admit net growth against live identity and container storage, without a policy lock."""
    if operation not in _STORAGE_ADMISSION_OPERATIONS:
        raise ValueError(f"Invalid storage admission operation {operation!r}")
    if type(additional_bytes) is not int:
        raise TypeError("additional_bytes must be an exact int")
    if not callable(record):
        raise TypeError("record must be callable")
    try:
        owner = connection.execute(
            select(sessions_table.c.user_id, sessions_table.c.auth_provider_type).where(sessions_table.c.id == session_id)
        ).one_or_none()
        if owner is None:
            raise AuditIntegrityError("Storage admission names a session that does not exist")
        if additional_bytes <= 0:
            return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=None, cap=None, ceiling=None)
        policies = _active_policy_rows(connection, identity_id=owner.user_id, for_update=False)
        if policies.identity is None:
            # An explicitly revoked identity allowance suspends byte growth.
            # A never-issued identity can still use a container-only regime.
            previously_issued = connection.execute(
                select(quota_policies_table.c.policy_id)
                .where(quota_policies_table.c.identity_id == owner.user_id, quota_policies_table.c.revoked_at.is_not(None))
                .limit(1)
            ).first()
            if previously_issued is not None:
                raise StorageAccountingUnavailableError(session_id)
        if policies.identity is None and policies.container is None:
            return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=None, cap=None, ceiling=None)
        usage = connection.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0))
            .select_from(blobs_table.join(sessions_table, sessions_table.c.id == blobs_table.c.session_id))
            .where(sessions_table.c.user_id == owner.user_id)
        ).scalar_one()
    except SQLAlchemyError as exc:
        raise StorageAccountingUnavailableError(session_id) from exc
    if type(usage) is not int:
        raise AuditIntegrityError(f"Tier 1: identity storage total must be an exact integer, got {type(usage).__name__}")
    cap = policies.identity.storage_bytes if policies.identity is not None else None
    ceiling = policies.container.storage_bytes if policies.container is not None else None
    limit = min(bound for bound in (cap, ceiling) if bound is not None)
    if usage + additional_bytes > limit:
        record(
            QuotaExceeded(
                identity_id=owner.user_id,
                provider=owner.auth_provider_type,
                operation=operation,
                dimension="storage",
                cap=cap,
                ceiling=ceiling,
                usage=usage,
                identity_policy_id=policies.identity.policy_id if policies.identity is not None else None,
                container_policy_id=policies.container.policy_id if policies.container is not None else None,
            )
        )
        raise IdentityStorageQuotaExceededError(
            session_id,
            identity_id=owner.user_id,
            cap=cap,
            ceiling=ceiling,
            usage=usage,
            additional_bytes=additional_bytes,
        )
    return StorageAdmission(identity_id=owner.user_id, additional_bytes=additional_bytes, usage=usage, cap=cap, ceiling=ceiling)
