"""Real fenced title lifecycle and recording hooks for controlled SDK tests."""

from __future__ import annotations

import tempfile
from datetime import UTC
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.pool import NullPool

from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.prompt_guard import PromptGuardMode
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageEntry
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkTicket,
)
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.fenced_session import seed_token_policies


class RecordingTitleService(SessionServiceImpl):
    """Actual SQLite service; hooks inject before submission or after admission."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="elspeth-title-test-")
        engine = create_session_engine(f"sqlite:///{Path(self._directory.name) / 'sessions.db'}", poolclass=NullPool)
        initialize_session_schema(engine)
        with engine.begin() as connection:
            ensure_test_identity(connection, identity_id="title-user")
            grant_test_pipeline_user(connection, identity_id="title-user")
            seed_token_policies(connection, identity_id="title-user")
        authority = SQLiteLocalSessionOperationAuthority(engine)
        super().__init__(
            engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.auto_title"),
            session_operation_authority=authority,
            chargeable_admission_policy=ChargeableAdmissionPolicy(
                identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
            ),
        )
        session = authority.create_session_with_initial_fence(
            user_id="title-user", title="Untitled", auth_provider_type="local", owner_instance_id="title-test", lease_seconds=60
        )
        self.session_id = session.id
        self.updates: list[tuple[object, str]] = []
        self.attempts: list[ProviderAttempt] = []
        self.cancelled_attempts: list[str] = []
        self.last_context: SessionOperationContext | None = None

    @property
    def usage(self) -> list[tuple[str, None, tuple[TokenUsageEntry, ...]]]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(token_usage_ledger_table, quota_provider_attempts_table.c.attempt_id)
                .outerjoin(
                    quota_provider_attempts_table, quota_provider_attempts_table.c.ledger_entry_id == token_usage_ledger_table.c.entry_id
                )
                .order_by(token_usage_ledger_table.c.recorded_at)
            ).all()
        assert all(row.run_id is None for row in rows)
        assert all(row.attempt_id is not None for row in rows), "Title ledger rows must retain their actual provider attempt"
        return [
            (
                row.source,
                None,
                (
                    TokenUsageEntry(
                        model=row.model,
                        prompt_tokens=row.prompt_tokens,
                        completion_tokens=row.completion_tokens,
                        cached_prompt_tokens=row.cached_prompt_tokens,
                        reasoning_tokens=row.reasoning_tokens,
                        recorded_at=row.recorded_at.replace(tzinfo=UTC),
                        call_id=row.attempt_id,
                    ),
                ),
            )
            for row in rows
        ]

    async def before_admission(self) -> None:
        pass

    async def after_admission(self, attempt: ProviderAttempt) -> None:
        pass

    async def before_settlement(self) -> None:
        pass

    async def before_title_write(self, session_id: UUID, title: str, context: SessionOperationContext) -> None:
        pass

    async def before_undispatched_cancellation(self, attempt_id: str, context: SessionOperationContext) -> None:
        pass

    async def begin_provider_attempt(self, *, session_operation_context, source, run_id=None, required_work=None):
        try:
            await self.before_admission()
        except BaseException as failure:
            if required_work is not None:
                required_work.complete_without_submission(failure)
            raise
        attempt = await super().begin_provider_attempt(
            session_operation_context=session_operation_context, source=source, run_id=run_id, required_work=required_work
        )
        self.attempts.append(attempt)
        await self.after_admission(attempt)
        return attempt

    async def finish_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall, required_work: RequiredWorkTicket | None = None
    ) -> None:
        try:
            await self.before_settlement()
        except BaseException as failure:
            if required_work is not None:
                required_work.complete_without_submission(failure)
            raise
        await super().finish_provider_attempt(session_operation_context=session_operation_context, call=call, required_work=required_work)

    async def update_session_title(self, session_id, title, *, session_operation_context, required_work=None):
        try:
            await self.before_title_write(session_id, title, session_operation_context)
        except BaseException as failure:
            if required_work is not None:
                required_work.complete_without_submission(failure)
            raise
        result = await super().update_session_title(
            session_id, title, session_operation_context=session_operation_context, required_work=required_work
        )
        self.updates.append((session_id, title))
        return result

    async def cancel_undispatched_provider_attempt(self, *, session_operation_context, attempt_id, requested_model, required_work=None):
        try:
            await self.before_undispatched_cancellation(attempt_id, session_operation_context)
        except BaseException as failure:
            if required_work is not None:
                required_work.complete_without_submission(failure)
            raise
        await super().cancel_undispatched_provider_attempt(
            session_operation_context=session_operation_context,
            attempt_id=attempt_id,
            requested_model=requested_model,
            required_work=required_work,
        )
        self.cancelled_attempts.append(attempt_id)


async def call_auto_title(
    *,
    service: RecordingTitleService,
    user_message: str,
    model: str,
    temperature: float | None,
    seed: int | None,
    prompt_guard_mount: PromptGuardMount | None = None,
    prompt_guard_mode: PromptGuardMode = "debug-when-unconfigured",
    api_base: str | None = None,
    api_key: str | None = None,
) -> None:
    """Use actual COMPOSE/TITLE ownership; explicit debug is test configuration."""
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=service.session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="title-test",
        lease_seconds=60,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, lease.context, invocation_id=str(uuid4()))
    )
    lease.bind_required_work(coordinator)
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    service.last_context = lease.context
    body: BaseException | None = None
    try:
        await _auto_title.maybe_auto_title_session(
            service=service,
            session_id=service.session_id,
            user_message=user_message,
            model=model,
            temperature=temperature,
            seed=seed,
            session_operation_context=lease.context,
            prompt_guard_mount=prompt_guard_mount,
            prompt_guard_mode=prompt_guard_mode,
            api_base=api_base,
            api_key=api_key,
            provider_custody=owner.mint(ProviderInvocationFamily.TITLE),
        )
    except BaseException as failure:
        body = failure
        raise
    finally:
        try:
            coordinator.settlement_failure_receipts()
            await lease.close()
        except BaseException as failure:
            if body is not None and failure is not body:
                raise BaseExceptionGroup("Title test body and lease completion outcomes", [body, failure]) from None
            raise
