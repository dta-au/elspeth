"""Terminal progress publication must never replace an established primary outcome.

``_publish_guided_full_terminal_preserving_primary`` runs only after a fence
failure or durable winner has decided the route's outcome. A secondary sink
fault is recorded, never promoted — except a Tier-1 integrity failure, which
is the mandatory error channel and escapes.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from structlog.testing import capture_logs

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.routes.composer.guided_plan import _publish_guided_full_terminal_preserving_primary
from elspeth.web.sessions.routes.guided_operations import _await_guided_terminal_write


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/guided/plan", "headers": [], "query_string": b""})


def _event() -> ComposerProgressEvent:
    return ComposerProgressEvent(phase="complete", headline="Guided planning finished.", evidence=())


@pytest.mark.asyncio
async def test_ordinary_sink_failure_is_recorded_and_the_primary_outcome_survives() -> None:
    async def failing_sink(_event: ComposerProgressEvent) -> None:
        raise RuntimeError("SENSITIVE_SINK_DETAIL")

    with capture_logs() as logs:
        await _publish_guided_full_terminal_preserving_primary(
            request=_request(),
            progress=failing_sink,
            primary_outcome="durable_complete",
            event=_event(),
        )

    secondary = [entry for entry in logs if entry["event"] == "guided.plan_failure_settlement_secondary_failure"]
    assert [(entry["site"], entry["primary_failure_code"], entry["secondary_exc_class"]) for entry in secondary] == [
        ("terminal_progress", "durable_complete", "RuntimeError")
    ]
    assert "SENSITIVE_SINK_DETAIL" not in repr(logs)


@pytest.mark.asyncio
async def test_integrity_failure_from_the_sink_escapes_instead_of_becoming_a_diagnostic() -> None:
    failure = AuditIntegrityError("progress registry integrity")

    async def corrupt_sink(_event: ComposerProgressEvent) -> None:
        raise failure

    with capture_logs() as logs, pytest.raises(AuditIntegrityError) as caught:
        await _publish_guided_full_terminal_preserving_primary(
            request=_request(),
            progress=corrupt_sink,
            primary_outcome="durable_complete",
            event=_event(),
        )

    assert caught.value is failure
    assert [entry for entry in logs if entry["event"] == "guided.plan_failure_settlement_secondary_failure"] == []


@pytest.mark.asyncio
async def test_sink_internal_cancellation_is_a_secondary_failure_but_injected_cancellation_unwinds() -> None:
    async def self_cancelling_sink(_event: ComposerProgressEvent) -> None:
        raise asyncio.CancelledError("sink cancelled itself")

    with capture_logs() as logs:
        await _publish_guided_full_terminal_preserving_primary(
            request=_request(),
            progress=self_cancelling_sink,
            primary_outcome="fence_lost",
            event=_event(),
        )
    secondary = [entry for entry in logs if entry["event"] == "guided.plan_failure_settlement_secondary_failure"]
    assert [entry["secondary_exc_class"] for entry in secondary] == ["CancelledError"]

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def blocking_sink(_event: ComposerProgressEvent) -> None:
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(
        _publish_guided_full_terminal_preserving_primary(
            request=_request(),
            progress=blocking_sink,
            primary_outcome="fence_lost",
            event=_event(),
        )
    )
    await started.wait()
    task.cancel()
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_terminal_write_child_integrity_failure_outweighs_caller_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    integrity_failure = AuditIntegrityError("terminal write integrity failed")

    async def failing_write() -> None:
        started.set()
        try:
            await release.wait()
            raise integrity_failure
        finally:
            finished.set()

    task = asyncio.create_task(_await_guided_terminal_write(failing_write(), propagate_caller_cancellation=True))
    await started.wait()
    task.cancel()
    try:
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(AuditIntegrityError) as caught:
        await asyncio.wait_for(task, timeout=5)
    assert caught.value is integrity_failure
    assert finished.is_set()


@pytest.mark.asyncio
async def test_injected_cancellation_outweighs_an_ordinary_terminal_progress_failure() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    sink_failure = RuntimeError("SENSITIVE_SINK_DETAIL")

    async def failing_sink(_event: ComposerProgressEvent) -> None:
        started.set()
        try:
            await release.wait()
            raise sink_failure
        finally:
            finished.set()

    task = asyncio.create_task(
        _publish_guided_full_terminal_preserving_primary(
            request=_request(),
            progress=failing_sink,
            primary_outcome="durable_complete",
            event=_event(),
        )
    )
    await started.wait()
    task.cancel()
    try:
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=5)
    assert caught.value.__cause__ is sink_failure
    assert finished.is_set()


@pytest.mark.asyncio
async def test_terminal_write_success_preserves_an_existing_ordinary_failure() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    primary_failure = RuntimeError("ordinary primary failure")

    async def terminal_write() -> None:
        started.set()
        await release.wait()
        finished.set()

    async def failure_settlement() -> None:
        try:
            raise primary_failure
        except RuntimeError:
            await _await_guided_terminal_write(terminal_write())
            raise

    task = asyncio.create_task(failure_settlement())
    await started.wait()
    task.cancel()
    try:
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(RuntimeError) as caught:
        await asyncio.wait_for(task, timeout=5)
    assert caught.value is primary_failure
    assert finished.is_set()
