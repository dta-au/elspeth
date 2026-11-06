"""Real model yield retains immutable readback after process drain closes work."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.lifecycle import SessionOperationLeaseDisposition
from elspeth.web.sessions import continuation_driver
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.continuation_driver import ContinuationSegmentYielded
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.helpers.composer_operations import message_body
from tests.unit.web.sessions.test_continuation_completion_owners import _continuation_app, _LateToolSDK
from tests.unit.web.sessions.test_continuation_control_successor import _control


def _retains_original(root, original):
    pending, seen = [root], set()
    while pending:
        current = pending.pop()
        if current is original:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _safe_error(error):
    frames = []
    trace = error.__traceback__
    while trace is not None:
        frames.append((trace.tb_frame.f_code.co_name, trace.tb_lineno))
        trace = trace.tb_next
    return type(error).__name__, str(error).splitlines()[:1], frames


@asynccontextmanager
async def _readback_app(tmp_path, monkeypatch, *, expect_unknown):
    lifetime = _continuation_app(tmp_path, monkeypatch)
    value = await lifetime.__aenter__()
    try:
        yield value
    finally:
        # Keep body assertions outside this negative cleanup expectation: an
        # unrelated assertion must not be hidden by the expected refusal.
        if expect_unknown:
            with pytest.raises(AuditIntegrityError, match="Composer worker still owns unfinished shutdown work"):
                await lifetime.__aexit__(None, None, None)
        else:
            await lifetime.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("committed", "ambiguous", "reserve-expired", "tampered", "absent", "precommit-stop", "precommit-cas"))
async def test_actual_model_yield_readback_retains_original_lifecycle_bound_after_signal(tmp_path, monkeypatch, mode):
    app_ref = []
    sdk = _LateToolSDK(app_ref, [("primary", ("Continue at the same durable checkpoint.", [_control()]))])
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    original = RuntimeError("controlled ambiguous successor commit")
    finished = asyncio.Event()
    drivers, receipts, returned, errors, commits, readbacks, releases = [], [], [], [], [], [], []
    actual_yield = continuation_driver._SegmentDriver.yield_control

    async def observe_yield(driver, *args, **kwargs):
        drivers.append(driver)
        try:
            value = await actual_yield(driver, *args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise
        else:
            returned.append(value)
            return value
        finally:
            finished.set()

    monkeypatch.setattr(continuation_driver._SegmentDriver, "yield_control", observe_yield)
    async with _readback_app(tmp_path, monkeypatch, expect_unknown=mode in ("absent", "tampered")) as (app, token, session_id):
        app_ref.append(app)
        repository = app.state.session_service.session_operation_authority
        repository_type = type(repository)
        actual_commit = repository_type.commit_checkpoint_and_successor
        actual_readback = repository_type.read_yield_outcome
        actual_release = repository_type.release

        def observe_release(owner, context, **kwargs):
            releases.append(context)
            return actual_release(owner, context, **kwargs)

        def signal_after_commit(owner, running, **kwargs):
            commits.append(running)
            if mode == "precommit-stop":
                stopped = app.state.composer_async_operation_authority.request_cancel(
                    session_id=running.claim.session_id, operation_id=running.claim.operation_id
                )
                assert stopped is not None and stopped.cancel_requested_at is not None
                return actual_commit(owner, running, **kwargs)
            if mode == "precommit-cas":
                return actual_commit(owner, running, **{**kwargs, "expected_cursor": kwargs["expected_cursor"] - 1})
            if mode != "absent":
                receipts.append(actual_commit(owner, running, **kwargs))
            assert len(drivers) == 1 and drivers[0].running is running
            driver = drivers[0]
            if mode == "reserve-expired":
                clock = driver.sql_deadline
                action = clock.policy.actions.session_sql
                clock.begin_action(action, started_at=time.monotonic() - action.total - clock.policy.segment.reserve - 1)
                clock.begin_settlement()
                with pytest.raises(TimeoutError):
                    clock.remaining_statement(session_id=str(session_id))
                assert clock.remaining_statement(session_id=str(session_id), lifecycle=True) > 0
            # The production signal owner preserves its actual monotonic origin
            # and schedules begin_drain on the owning event loop.
            app.state.composer_drain_signal.request()
            if mode in ("ambiguous", "absent"):
                raise original
            return receipts[-1]

        def observe_readback(owner, running):
            value = actual_readback(owner, running)
            readbacks.append(value)
            if mode == "tampered" and value is not None:
                return replace(value, cursor=value.cursor + 1)
            return value

        monkeypatch.setattr(repository_type, "commit_checkpoint_and_successor", signal_after_commit)
        monkeypatch.setattr(repository_type, "read_yield_outcome", observe_readback)
        monkeypatch.setattr(repository_type, "release", observe_release)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=message_body("Continue explaining this pipeline.", operation_id=operation_id),
            )
            assert response.status_code == 202, response.text
            await asyncio.wait_for(finished.wait(), timeout=30)
        expected_readbacks = 0 if mode in ("precommit-stop", "precommit-cas") else 1
        assert len(commits) == 1 and len(readbacks) == expected_readbacks, [_safe_error(error) for error in errors]
        assert [request["role"] for request in sdk.requests] == ["primary"] and not sdk.steps
        with app.state.session_engine.connect() as conn:
            rows = tuple(conn.execute(select(segments).where(segments.c.operation_id == operation_id).order_by(segments.c.sequence)))
        driver = drivers[0]
        deadline = driver.worker.drain_deadline
        signal = app.state.composer_drain_signal
        if mode in ("absent", "tampered"):
            jobs = tuple(driver.worker._jobs.values())
            if jobs:
                _done, pending = await asyncio.wait(jobs, timeout=3)
                assert pending == set()
            lease = driver.lease
            assert lease.closed and lease.disposition is SessionOperationLeaseDisposition.UNKNOWN
            assert not lease.continuation_finished
            assert lease._close_task is not None and lease._close_task.done()
            assert lease._renewal_task is not None and lease._renewal_task.done()
            assert lease.required_work is not None and lease.required_work.all_completed
            key = (driver.running.claim.session_id, driver.running.claim.operation_id)
            assert driver.worker._continuation_adoptions[key].lease is lease
            assert releases == []
        if mode in ("precommit-stop", "precommit-cas"):
            assert deadline is None and signal.started_at is None and len(rows) == 1 and receipts == []
            if mode == "precommit-stop":
                assert errors == [] and len(returned) == 1
                assert returned[0].terminal.status == "failed" and returned[0].terminal.failure_code == "request_cancelled"
            else:
                assert returned == [] and len(errors) == 1 and isinstance(errors[0], ContinuationCASConflict)
        elif mode == "absent":
            assert deadline is not None and signal.started_at is not None
            assert deadline.expires_at == signal.started_at + driver.sql_deadline.drain_seconds
            assert len(rows) == 1 and receipts == [] and readbacks == [None] and returned == []
            assert len(errors) == 1 and _retains_original(errors[0], original)
        elif mode == "tampered":
            assert len(rows) == 2 and returned == [] and len(errors) == 1
            assert isinstance(errors[0], AuditIntegrityError)
        else:
            assert deadline is not None and signal.started_at is not None
            assert deadline.expires_at == signal.started_at + driver.sql_deadline.drain_seconds
            assert errors == [] and len(returned) == 1 and type(returned[0]) is ContinuationSegmentYielded
            assert returned[0].receipt == receipts[0] == readbacks[0]
            assert (rows[0].state, rows[1].state) == ("yielded", "queued")
            assert rows[1].sequence == rows[0].sequence + 1 and rows[1].generation == rows[0].generation + 1
            if mode == "ambiguous":
                assert any(_retains_original(failure, original) for failure in driver._consumed_native)
