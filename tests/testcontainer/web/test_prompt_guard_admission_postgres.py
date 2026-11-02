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
import json
from uuid import uuid4

import httpx
import litellm
import pytest
from pydantic import SecretStr
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
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.prompt_guard_config import PromptGuardSettings
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


def _gateway_call(custody, client, *, messages=None):
    return provider_gateway._litellm_acompletion(
        provider_custody=custody,
        prompt_guard_mount=PromptGuardMount(client),
        prompt_guard_role="composer",
        prompt_guard_mode="required",
        prompt_guard_user_prompt="Compare the two neutral columns.",
        model="test/model",
        messages=messages if messages is not None else [{"role": "user", "content": "Compare the two neutral columns."}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("document_characters", [10_000, 10_001])
async def test_postgres_exact_guard_document_limit_keeps_whole_original_before_sdk(authority, monkeypatch, document_characters):
    engine, _service, owner, coordinator = authority
    # The untrusted assistant message is a single JSON shadow document. Derive
    # the payload size from its complete wrapper, so these are exact adapter
    # boundaries rather than approximate lengths of its inner scalar.
    empty_shadow = json.dumps({"role": "assistant", "content": ""}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    original = "x" * (document_characters - len(empty_shadow))
    message = {"role": "assistant", "content": original}
    exact_document = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(exact_document) == document_characters
    sent_to_shield: list[dict[str, object]] = []
    entered_sdk: list[dict[str, object]] = []

    def shield(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["userPrompt"] == "Compare the two neutral columns."
        assert payload["documents"] == [exact_document]
        sent_to_shield.append(payload)
        return httpx.Response(
            200,
            json={"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": False}]},
        )

    settings = PromptGuardSettings(endpoint="https://exact-guard.example.test", subscription_key=SecretStr("offline-exact-guard"))
    client = AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield))

    async def completion(**kwargs):
        checks = _checks(engine)  # A new PostgreSQL connection sees the committed guard audit.
        assert len(checks) == 1 and (checks[0]["disposition"], checks[0]["reason"]) == ("allow", "clean")
        assert kwargs["messages"] == [message]
        entered_sdk.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    if document_characters == 10_000:
        await _gateway_call(custody, client, messages=[message])
        assert len(sent_to_shield) == len(entered_sdk) == 1
        assert custody.attempt is not None
    else:
        with pytest.raises(PromptGuardRefused) as refused:
            await _gateway_call(custody, client, messages=[message])
        assert refused.value.http_status == 422
        assert sent_to_shield == entered_sdk == []
        assert _quota_attempts(engine) == []
        assert custody.attempt is None and custody.call is None
    checks = _checks(engine)
    assert len(checks) == 1
    assert checks[0]["operation_id"] == custody.context.fence.operation_id
    assert (checks[0]["disposition"], checks[0]["reason"]) == (
        ("allow", "clean") if document_characters == 10_000 else ("unavailable", "unsupported_input")
    )
    coordinator.assert_completed()


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
