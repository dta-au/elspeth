"""The V4 TITLE custody binds a committed admission before cancellation escapes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from threading import Event
from typing import TYPE_CHECKING

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkIssuer
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_title_provider as title_source
from elspeth.web.sessions.continuation_title_provider import read_v4_title_admission_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_title_provider import title_case

operation_store = operations.operation_store

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Connection


async def _prepared_custody(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer, work, prepared, _guard = await title_case(store, tmp_path, monkeypatch)
    custody = ProviderInvocationOwner(service=store[3], required_work=work).mint(ProviderInvocationFamily.TITLE)
    custody.bind_continuation_title(prepared)
    issuer = TitleNoSdkIssuer(prepared)
    custody.bind_title_no_sdk_issuer(issuer)
    return store, running, prepared, custody, issuer


@pytest.mark.asyncio
async def test_title_cancellation_during_committing_admission_keeps_original_pending_custody(operation_store, tmp_path, monkeypatch):
    store, running, prepared, custody, issuer = await _prepared_custody(operation_store, tmp_path, monkeypatch)
    entered, release = Event(), Event()
    original = title_source.admit_v4_title_provider_on_connection

    def pause_after_admission(*args, **kwargs):
        admitted = original(*args, **kwargs)
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("TITLE admission test barrier did not release")
        return admitted

    monkeypatch.setattr(title_source, "admit_v4_title_provider_on_connection", pause_after_admission)
    task = asyncio.create_task(custody.admit(model=prepared.request.kwargs["model"]))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
    finally:
        release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        admitted = custody.continuation_title_v4_admission
        assert admitted is not None and custody.attempt is admitted.attempt
        assert issuer._admission is admitted
        with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
            recovered = read_v4_title_admission_on_connection(conn, prepared=prepared)
        assert recovered == admitted
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_rolled_back_admission_never_binds_physical_custody(operation_store, tmp_path, monkeypatch):
    store, running, prepared, custody, issuer = await _prepared_custody(operation_store, tmp_path, monkeypatch)

    def reject_before_admission(*_args, **_kwargs):
        raise AuditIntegrityError("injected TITLE admission rollback")

    monkeypatch.setattr(title_source, "admit_v4_title_provider_on_connection", reject_before_admission)
    try:
        with pytest.raises(AuditIntegrityError, match="injected TITLE admission rollback"):
            await custody.admit(model=prepared.request.kwargs["model"])
        assert custody.attempt is None and custody.continuation_title_v4_admission is None
        assert issuer._admission is None
        with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
            assert read_v4_title_admission_on_connection(conn, prepared=prepared) is None
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_post_commit_worker_error_cold_recovers_original_admission(operation_store, tmp_path, monkeypatch):
    store, running, prepared, custody, issuer = await _prepared_custody(operation_store, tmp_path, monkeypatch)
    original_begin = SessionServiceImpl._session_process_locked_begin
    injected = False

    @contextmanager
    def fail_once_after_commit(self: SessionServiceImpl, session_id: str) -> Iterator[Connection]:
        nonlocal injected
        with original_begin(self, session_id) as conn:
            yield conn
        if not injected:
            injected = True
            raise AuditIntegrityError("injected TITLE post-commit handoff error")

    monkeypatch.setattr(SessionServiceImpl, "_session_process_locked_begin", fail_once_after_commit)
    try:
        with pytest.raises(AuditIntegrityError, match="injected TITLE post-commit handoff error"):
            await custody.admit(model=prepared.request.kwargs["model"])
        assert injected
        admitted = custody.continuation_title_v4_admission
        assert admitted is not None and custody.attempt is admitted.attempt
        assert issuer._admission is admitted
        with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
            assert read_v4_title_admission_on_connection(conn, prepared=prepared) == admitted
    finally:
        store[1].release(running.session_operation_context)
