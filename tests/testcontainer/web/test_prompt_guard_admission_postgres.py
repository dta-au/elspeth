"""PromptShield seam 7 on PostgreSQL: the fenced guard decision commits before model admission.

The three SQLite admission-ordering cases are re-exported here against a
PostgreSQL ``authority``. ``_checks`` reads through a fresh pooled
``engine.connect()``, so under READ COMMITTED it observes only committed rows.
The two physical-gateway cases add the SDK seam the re-exports cannot see: an
actual PostgreSQL failure of the guard-audit INSERT, and cancellation while the
guard is still inspecting. Both must leave ``litellm.acompletion`` unentered.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import InternalError, OperationalError, SQLAlchemyError
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.fenced_session import seed_token_policies
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.composer.test_prompt_guard_admission import _checks, _GuardClient
from tests.unit.web.composer.test_prompt_guard_admission import (
    test_actual_audit_sql_failure_rolls_back_and_never_admits_model as test_actual_audit_sql_failure_rolls_back_and_never_admits_model,
)
from tests.unit.web.composer.test_prompt_guard_admission import (
    test_allow_commits_sanitized_check_under_exact_family_before_return as test_allow_commits_sanitized_check_under_exact_family_before_return,
)
from tests.unit.web.composer.test_prompt_guard_admission import (
    test_cancellation_joins_checker_and_commits_cancelled_audit_before_return as test_cancellation_joins_checker_and_commits_cancelled_audit_before_return,
)

from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.models import quota_provider_attempts_table

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_audit_sql_failure_rolls_back_and_never_admits_model",
    "test_allow_commits_sanitized_check_under_exact_family_before_return",
    "test_cancellation_joins_checker_and_commits_cancelled_audit_before_return",
]

_GUARD_AUDIT_FAULT = "INJECTED-GUARD-AUDIT-SQL-FAULT"


@pytest.fixture
def authority(operation_store):
    """The unit ``authority`` 4-tuple, bound to the PostgreSQL session store."""
    engine, repository, _operations, service, session_id = operation_store
    assert engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    context = repository.acquire(
        session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    yield engine, service, owner, coordinator
    repository.release(context)


def _forbid_sdk(monkeypatch) -> list[dict[str, object]]:
    entered: list[dict[str, object]] = []

    async def completion(**kwargs):
        entered.append(kwargs)
        raise AssertionError("litellm.acompletion was entered without a committed guard admission")

    monkeypatch.setattr(litellm, "acompletion", completion)
    return entered


def _quota_attempts(engine):
    with engine.connect() as connection:
        return connection.execute(select(quota_provider_attempts_table)).all()


def _gateway_call(custody, client):
    return provider_gateway._litellm_acompletion(
        provider_custody=custody,
        prompt_guard_mount=PromptGuardMount(client),
        prompt_guard_role="composer",
        prompt_guard_mode="required",
        prompt_guard_user_prompt="Compare the two neutral columns.",
        model="test/model",
        messages=[{"role": "user", "content": "Compare the two neutral columns."}],
    )


@pytest.mark.asyncio
async def test_postgres_guard_audit_insert_failure_never_enters_sdk_and_stays_a_database_failure(authority, monkeypatch):
    engine, _service, owner, coordinator = authority
    # A real PostgreSQL statement failure on exactly the guard-audit INSERT;
    # every other chat_messages write is unaffected.
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE FUNCTION refuse_prompt_guard_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.tool_calls::text LIKE '%prompt_guard_audit%' THEN
                        RAISE EXCEPTION '{_GUARD_AUDIT_FAULT}';
                    END IF;
                    RETURN NEW;
                END
                $$
                """
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER refuse_prompt_guard_audit BEFORE INSERT ON chat_messages "
                "FOR EACH ROW EXECUTE FUNCTION refuse_prompt_guard_audit()"
            )
        )
    sdk_calls = _forbid_sdk(monkeypatch)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    client = _GuardClient(PromptGuardDecision("allow", "clean"))

    with pytest.raises(BaseException) as caught:
        await _gateway_call(custody, client)

    # The guard allowed, but the decision never committed, so nothing was admitted.
    assert len(client.requests) == 1
    assert sdk_calls == []
    assert _checks(engine) == []
    assert _quota_attempts(engine) == []
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()

    # Classification: the original PostgreSQL failure propagates as itself. It is
    # never relabelled as a guard refusal and never projects to a 422 settlement.
    failure = caught.value
    assert type(failure) is InternalError
    assert _GUARD_AUDIT_FAULT in str(failure.orig)
    assert not isinstance(failure, (PromptGuardRefused, OperationalError))
    projected = project_composer_operation_error(failure, request_id="pg-guard-audit")
    assert isinstance(failure, SQLAlchemyError)
    assert projected.http_status == 500
    assert projected.error_type == "operation_failed"
    assert _GUARD_AUDIT_FAULT not in projected.model_dump_json()


@pytest.mark.asyncio
async def test_postgres_cancel_during_inspection_joins_checker_and_commits_cancelled_audit_before_sdk(authority, monkeypatch):
    engine, service, owner, coordinator = authority
    sdk_calls = _forbid_sdk(monkeypatch)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    order: list[str] = []
    entered = asyncio.Event()
    original_insert = service._insert_chat_message

    def observed_insert(*args, **kwargs):
        result = original_insert(*args, **kwargs)
        order.append("audit_insert")
        return result

    monkeypatch.setattr(service, "_insert_chat_message", observed_insert)

    class WaitingClient(_GuardClient):
        async def inspect(self, request):
            self.requests.append(request)
            order.append("inspect_entered")
            entered.set()
            try:
                await asyncio.Future()
            finally:
                order.append("inspect_joined")

    task = asyncio.create_task(_gateway_call(custody, WaitingClient(PromptGuardDecision("allow", "clean"))))
    async with asyncio.timeout(5):
        await entered.wait()
    # Cancelled before any guard decision exists: nothing is committed yet.
    assert _checks(engine) == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Ordering: the checker is joined, then the cancelled decision is written,
    # and it is already committed (visible on an independent pooled PostgreSQL
    # connection) when the caller observes the cancellation. The SDK never runs.
    assert order == ["inspect_entered", "inspect_joined", "audit_insert"]
    checks = _checks(engine)
    assert len(checks) == 1
    assert (checks[0]["disposition"], checks[0]["reason"]) == ("cancelled", "cancelled")
    assert checks[0]["operation_id"] == custody.context.fence.operation_id
    assert sdk_calls == []
    assert _quota_attempts(engine) == []
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
