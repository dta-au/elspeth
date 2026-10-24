"""Actual guard/custody/audit/ledger/title writer; only external transports controlled."""

import asyncio
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.prompt_guard import build_prompt_guard_mount, prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.auto_title_outcome import AutoTitleRaised, AutoTitleReturned, observe_auto_title_session
from elspeth.web.sessions.models import sessions_table
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_token_usage_adapters import _ledger

operation_store = operations.operation_store
USER = "Label this customer document workflow"


def title_case(store, tmp_path, monkeypatch, *, grant_role=True):
    engine, repository, _authority, service, sid = store
    with engine.begin() as conn:
        if grant_role:
            grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.TITLE)
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    arguments = {
        "service": service,
        "session_id": sid,
        "user_message": USER,
        "model": "test/model",
        "temperature": None,
        "seed": None,
        "session_operation_context": context,
        "prompt_guard_mount": build_prompt_guard_mount(settings.composer_prompt_guard),
        "prompt_guard_mode": "required",
        "provider_custody": custody,
    }
    audits = []
    original_builder = _auto_title.build_llm_call_record

    def record_original(*args, **kwargs):
        result = original_builder(*args, **kwargs)
        audits.append(result)
        return result

    monkeypatch.setattr(_auto_title, "build_llm_call_record", record_original)
    writes = []
    original_write = service.update_session_title

    async def observe_original_write(*args, **kwargs):
        result = await original_write(*args, **kwargs)
        writes.append(result)
        return result

    monkeypatch.setattr(service, "update_session_title", observe_original_write)
    return arguments, coordinator, requests, audits, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("updated", "empty_response", "rejected", "timeout", "malformed", "legacy_none"))
async def test_title_observation_retains_actual_sdk_audit_attempt_and_current_writer_once(operation_store, tmp_path, monkeypatch, case):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)
    custody = args["provider_custody"]
    physical = []
    response = ModelResponse(
        model="test/model",
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None if case == "empty_response" else "one" if case == "rejected" else "Customer Document Workflow",
                }
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )
    timeout = TimeoutError("actual title transport timeout")

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], args["session_operation_context"], requests, role="composer", user_prompt=USER)
        assert custody.attempt is not None and kwargs["timeout"] == 5.0
        assert kwargs["num_retries"] == 0 and kwargs["max_retries"] == 0
        physical.append(custody.attempt)
        if case == "timeout":
            raise timeout
        return object() if case == "malformed" else response

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with prompt_guard_user_scope(USER):
            if case == "legacy_none":
                assert await _auto_title.maybe_auto_title_session(**args) is None
                result = None
            else:
                result = await observe_auto_title_session(**args)
                assert type(result) is AutoTitleReturned
        assert len(physical) == len(audits) == len(_ledger(operation_store[0])) == 1
        assert audits[0].call_id == physical[0].attempt_id and audits[0].started_at == physical[0].started_at
        assert custody.attempt is None and custody.call is None
        if result is not None:
            observed = result.outcome
            assert observed.audit_call is audits[0] and observed.attempt is physical[0]
            assert observed.disposition == ("provider_failure" if case in ("timeout", "malformed") else case)
            if case == "timeout":
                assert observed.handled_error is timeout and observed.response is None
                assert observed.audit_call.status is ComposerLLMCallStatus.TIMEOUT
            elif case == "malformed":
                assert type(observed.handled_error) is _auto_title._MalformedAutoTitleResponseError
                assert observed.response is not None and observed.admitted is None
            else:
                assert observed.response is response and observed.admitted is not None
        if case in ("updated", "legacy_none"):
            assert len(writes) == 1 and writes[0].title == "Customer Document Workflow"
            if result is not None:
                assert result.outcome.title_record is writes[0]
                assert result.outcome.candidate == writes[0].title
        else:
            assert not writes
            assert result.outcome.title_record is None
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_empty_title_request_is_explicitly_undispatched_without_guard_quota_or_title_write(operation_store, tmp_path, monkeypatch):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)

    async def forbidden(**kwargs):
        raise AssertionError("Empty original title request must not dispatch")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    try:
        result = await observe_auto_title_session(**{**args, "user_message": "  "})
        assert type(result) is AutoTitleReturned and result.outcome.disposition == "empty_request"
        assert result.outcome.audit_call is None and result.outcome.attempt is None
        assert not audits and not writes and not requests and not _ledger(operation_store[0])
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_actual_title_role_refusal_returns_original_undispatched_disposition(operation_store, tmp_path, monkeypatch):
    args, coordinator, _requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch, grant_role=False)

    async def forbidden(**kwargs):
        raise AssertionError("Actual title workload role refusal must precede SDK entry")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_auto_title_session(**args)
        assert type(result) is AutoTitleReturned and result.outcome.disposition == "admission_refused"
        assert isinstance(result.outcome.handled_error, ChargeableAdmissionRefused)
        assert result.outcome.handled_error.decision.refusal_reason.value == "user_role_required"
        assert result.outcome.audit_call is None and result.outcome.attempt is None
        assert not audits and not writes and not _ledger(operation_store[0])
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_actual_title_cancellation_retains_original_exception_and_already_settled_physical_audit(
    operation_store, tmp_path, monkeypatch
):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)
    entered = asyncio.Event()
    observed_cancellations = []
    physical = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], args["session_operation_context"], requests, role="composer", user_prompt=USER)
        physical.append(args["provider_custody"].attempt)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            observed_cancellations.append(error)
            raise

    monkeypatch.setattr(litellm, "acompletion", completion)

    async def actual_observe():
        with prompt_guard_user_scope(USER):
            return await observe_auto_title_session(**args)

    task = asyncio.create_task(actual_observe())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("actual title caller cancellation")
        result = await task
        assert type(result) is AutoTitleRaised and result.error is observed_cancellations[0]
        assert result.outcome.audit_call is audits[0] and result.outcome.attempt is physical[0]
        assert result.outcome.audit_call.status is ComposerLLMCallStatus.CANCELLED
        assert len(_ledger(operation_store[0])) == 1 and not writes
        assert args["provider_custody"].attempt is None
        coordinator.assert_completed()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_title_post_write_failure_is_original_raised_outcome_not_a_fabricated_success_receipt(operation_store, tmp_path, monkeypatch):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)
    original = AuditIntegrityError("actual title local projection failure after SQL commit")
    actual_write = args["service"].update_session_title

    async def fail_after_actual_write(*values, **kwargs):
        await actual_write(*values, **kwargs)
        raise original

    monkeypatch.setattr(args["service"], "update_session_title", fail_after_actual_write)

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], args["session_operation_context"], requests, role="composer", user_prompt=USER)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "Customer Document Workflow"}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_auto_title_session(**args)
        assert type(result) is AutoTitleRaised and result.error is original
        assert result.outcome.audit_call is audits[0] and len(writes) == 1
        assert result.outcome.title_record is None and result.outcome.disposition == "incomplete"
        with operation_store[0].connect() as conn:
            assert (
                conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(operation_store[4]))).scalar_one()
                == writes[0].title
            )
        assert len(_ledger(operation_store[0])) == 1
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_foreign_title_custody_retains_original_guard_failure_without_snapshot_attribute_access(
    operation_store, tmp_path, monkeypatch
):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)
    failures = []
    actual_required = _auto_title._maybe_auto_title_session_required

    async def observe_required(*values, **kwargs):
        try:
            return await actual_required(*values, **kwargs)
        except BaseException as error:
            failures.append(error)
            raise

    monkeypatch.setattr(_auto_title, "_maybe_auto_title_session_required", observe_required)

    async def forbidden(**kwargs):
        raise AssertionError("Foreign title custody must not dispatch")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_auto_title_session(**{**args, "provider_custody": object()})
        assert type(result) is AutoTitleRaised and result.error is failures[0]
        assert type(result.error) is AuditIntegrityError
        assert result.outcome.audit_call is None and result.outcome.attempt is None
        assert not audits and not writes and not requests and not _ledger(operation_store[0])
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])


@pytest.mark.asyncio
async def test_title_original_audit_construction_failure_retains_actual_unfinished_attempt_without_synthetic_audit(
    operation_store, tmp_path, monkeypatch
):
    args, coordinator, requests, audits, writes = title_case(operation_store, tmp_path, monkeypatch)
    physical = []
    original = AuditIntegrityError("original title audit construction failure")

    def fail_original_builder(*values, **kwargs):
        raise original

    monkeypatch.setattr(_auto_title, "build_llm_call_record", fail_original_builder)

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], args["session_operation_context"], requests, role="composer", user_prompt=USER)
        physical.append(args["provider_custody"].attempt)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "Customer Document Workflow"}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_auto_title_session(**args)
        assert type(result) is AutoTitleRaised and isinstance(result.error, BaseExceptionGroup)
        assert result.error.exceptions[0] is original
        assert result.outcome.audit_call is None and result.outcome.attempt is physical[0]
        assert args["provider_custody"].attempt is physical[0] and physical[0] is not None
        assert not audits and not writes and not _ledger(operation_store[0])
        coordinator.assert_completed()
    finally:
        operation_store[1].release(args["session_operation_context"])
