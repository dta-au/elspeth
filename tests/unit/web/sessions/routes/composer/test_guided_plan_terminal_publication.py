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

    async def blocking_sink(_event: ComposerProgressEvent) -> None:
        started.set()
        await release.wait()

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
    with pytest.raises(asyncio.CancelledError):
        await task
