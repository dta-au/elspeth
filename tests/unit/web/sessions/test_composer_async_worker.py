"""Production-path detached work and exact progress custody controls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from uuid import uuid4

import pytest

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_operations import ComposerOperationError, composer_operation_admission_request_hash
from elspeth.web.sessions.schemas import ComposerOperationSnapshot, SendMessageRequest
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.continuation_app import ACTOR as CONTINUATION_ACTOR
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, settle, submit_and_settle
from tests.unit.web.sessions.test_freeform_route_custody import _AuditedComposer
from tests.unit.web.sessions.test_freeform_route_custody import _file_app as _fixture_file_app


class _BudgetedComposer(_AuditedComposer):
    async def compose(self, *args, budget_seconds: float | None = None, **kwargs):
        assert budget_seconds is not None and budget_seconds > 0
        return await super().compose(*args, **kwargs)


class _HeldComposer(_BudgetedComposer):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatches = 0

    async def compose(self, *args, **kwargs):
        self.dispatches += 1
        self.entered.set()
        await self.release.wait()
        return await super().compose(*args, **kwargs)


def _file_app(tmp_path):
    app, service, engine, _composer = _fixture_file_app(tmp_path)
    composer = _BudgetedComposer()
    app.state.composer_service = composer
    return app, service, engine, composer


@dataclass
class _SnapshotFactory:
    original: object

    def for_user_id(self, user_id):
        return self.original(UserIdentity(user_id=user_id, username=user_id))


@pytest.mark.asyncio
async def test_exact_progress_recompose_custody_and_finished_lease() -> None:
    registry = ComposerProgressRegistry()
    first = await registry.start_request("session", "alice")
    one = await registry.claim_request(
        session_id="session",
        user_id="alice",
        request_id="same-user",
        lease=first,
        operation_id="first",
        session_operation_id="sol-first",
        session_operation_epoch=1,
    )
    second = await registry.start_request("session", "alice")
    two = await registry.claim_request(
        session_id="session",
        user_id="alice",
        request_id="same-user",
        lease=second,
        operation_id="second",
        session_operation_id="sol-second",
        session_operation_epoch=2,
    )
    event = ComposerProgressEvent(phase="calling_model", headline="Safe model call")
    await two(event)
    await one(ComposerProgressEvent(phase="using_tools", headline="Stale tools"))
    assert (
        await registry.get_for_operation(
            session_id="session", user_id="alice", operation_id="first", session_operation_id="sol-first", session_operation_epoch=1
        )
        is None
    )
    snapshot = await registry.get_for_operation(
        session_id="session", user_id="alice", operation_id="second", session_operation_id="sol-second", session_operation_epoch=2
    )
    assert snapshot is not None and snapshot.headline == event.headline and snapshot.request_token == second.request_token
    await registry.finish_request(second)
    await two(ComposerProgressEvent(phase="using_tools", headline="After lease finish"))
    assert (await registry.get_latest("session")).headline == event.headline
    await registry.finish_request(first)


def _worker(app, authority) -> ComposerAsyncWorker:
    app.state.plugin_snapshot_factory = _SnapshotFactory(app.state.plugin_snapshot_factory)
    instance_draining = threading.Event()
    return ComposerAsyncWorker(
        app=app,
        authority=authority,
        concurrency=2,
        scan_interval_seconds=0.01,
        claim_lease_seconds=30,
        drain_seconds=0.05,
        owner_instance_id=app.state.session_service.session_operation_owner_instance_id,
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(instance_draining), instance_draining=instance_draining),
        instance_draining=instance_draining,
    )


async def _admit(app, service, authority, *, operation_id: str, deadline_seconds: float = 85.0):
    # ``deadline_seconds`` stays in the signature for the importers of this
    # helper. Continuation admission takes no operation deadline (segments own
    # their deadline), so it is not forwarded.
    del deadline_seconds
    session = await service.create_session("alice", "Owned job", app.state.settings.auth_provider)
    request = SendMessageRequest(operation_id=operation_id, content="Answer plainly", state_id=None)
    record, fresh = authority.admit(
        quota=app.state.rate_limiter.composer_admission,
        session_id=session.id,
        operation_id=operation_id,
        kind="compose_message",
        request_hash=composer_operation_admission_request_hash(session_id=session.id, kind="compose_message", request=request),
        actor_user_id="alice",
        request_id="accepted-request",
        base_state_id=None,
        request_json=request.model_dump_json(),
        max_nonterminal=64,
        auth_provider_type=app.state.settings.auth_provider,
    )
    assert fresh
    return record


class _HeldSDK(ScriptedSDK):
    """A scripted SDK whose first physical call parks until the test releases it."""

    def __init__(self, steps):
        super().__init__(steps)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().__call__(**kwargs)


def _continuation_message(operation_id: str) -> dict[str, object]:
    return {"content": "Answer plainly", "operation_id": operation_id, "state_id": None}


@pytest.mark.asyncio
async def test_worker_completes_admitted_job_with_one_provider_call(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = ScriptedSDK([("primary", ("Answer plainly.", None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        operation_id = str(uuid4())
        snapshot = await submit_and_settle(harness, _continuation_message(operation_id))
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        assert sdk.roles == ["primary", "end"]
        rows = await harness.app.state.session_service.get_messages(harness.session_id, limit=None)
        assert len([row for row in rows if row.role == "user"]) == 1
        worker = harness.app.state.composer_async_worker
        assert not worker._jobs
        authority = harness.app.state.composer_async_operation_authority
        saved = authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert saved is not None and saved.status == "completed"
        async with harness.client() as client:
            late = await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel")
        assert late.status_code == 200, late.text
        late_snapshot = ComposerOperationSnapshot.model_validate_json(late.content, strict=True)
        assert late_snapshot.status == "completed" and late_snapshot.cancel_requested is False
        after = authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert after is not None and after.status == "completed" and after.result_sha256 == saved.result_sha256
        assert after.cancel_requested_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ("stop", "shutdown"))
async def test_running_stop_and_shutdown_have_distinct_durable_causes(tmp_path, monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    sdk = _HeldSDK([("primary", ("Answer plainly.", None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        operation_id = str(uuid4())
        worker = harness.app.state.composer_async_worker
        authority = harness.app.state.composer_async_operation_authority
        async with harness.client() as client:
            accepted = await client.post(f"/api/sessions/{harness.session_id}/messages", json=_continuation_message(operation_id))
            assert accepted.status_code == 202, accepted.text
            await asyncio.wait_for(sdk.entered.wait(), timeout=30)
            if cause == "stop":
                stopped = await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel")
                assert stopped.status_code == 202, stopped.text
                sdk.release.set()
                snapshot = await settle(harness, operation_id=operation_id, client=client)
                assert snapshot.status == "failed"
            else:
                stop = asyncio.create_task(worker.stop())
                await asyncio.sleep(0)
                sdk.release.set()
                await asyncio.wait_for(stop, timeout=30)
        current = authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert current is not None
        assert (current.cancel_requested_at is not None) == (cause == "stop")
        if cause == "stop":
            assert current.status == "failed" and current.failure_code == "request_cancelled", current
            assert current.result_json is not None
            error = ComposerOperationError.model_validate_json(current.result_json, strict=True)
            assert error.http_status == 499
            assert error.body["detail"] == "The composer request was stopped."
        else:
            # Shutdown is not a Stop: the Continuation owner keeps its lease
            # and fence custody, and the lost-owner settlement records the
            # distinct worker_lost cause once the lease lapses (covered by
            # test_composer_continuation_handover.py::
            # test_settle_lost_segment_pairs_revocation_and_settles_never_started_parents).
            assert current.status == "running" and current.failure_code is None and current.result_json is None, current
        assert len([role for role in sdk.roles if role == "primary"]) == 1
        assert not worker._jobs


@pytest.mark.asyncio
async def test_cancel_unclaimed_job_never_dispatches_provider(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    # The lifespan worker scans once at start and then waits for a wake; a
    # direct authority admission sends none, so the job stays unclaimed.
    async with continuation_app(tmp_path, monkeypatch, composer_async_scan_interval_seconds=60) as harness:
        app = harness.app
        authority = app.state.composer_async_operation_authority
        operation_id = str(uuid4())
        request = SendMessageRequest(operation_id=operation_id, content="Answer plainly", state_id=None)
        record, fresh = authority.admit(
            quota=app.state.rate_limiter.composer_admission,
            session_id=harness.session_id,
            operation_id=operation_id,
            kind="compose_message",
            request_hash=composer_operation_admission_request_hash(session_id=harness.session_id, kind="compose_message", request=request),
            actor_user_id=CONTINUATION_ACTOR,
            request_id="accepted-request",
            base_state_id=None,
            request_json=request.model_dump_json(),
            max_nonterminal=64,
            auth_provider_type=app.state.settings.auth_provider,
        )
        assert fresh and record.status == "queued"
        marked = authority.request_cancel(session_id=harness.session_id, operation_id=operation_id)
        assert marked is not None and marked.cancel_requested_at is not None
        await app.state.composer_async_worker.run_until_idle()
        current = authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert current is not None and current.status == "failed" and current.failure_code == "request_cancelled"
        assert sdk.requests == []
