"""F7 controls for an already-started asynchronous tool stage."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_success
from elspeth.web.composer.tool_batch import _await_started_tool_stage


@pytest.mark.asyncio
async def test_started_tool_stage_closes_cancellation_once_and_rethrows_the_original() -> None:
    recorder = BufferingRecorder()
    audit = begin_dispatch("cancelled-tool", "get_pipeline_state", {}, version_before=3, actor="test")
    cause = ValueError("stage origin")
    cancellation = asyncio.CancelledError("coordinator_cancelled")
    stages: list[str] = []

    async def cancelled_stage() -> None:
        stages.append("cancelled")
        raise cancellation from cause

    with pytest.raises(asyncio.CancelledError) as caught:
        await _await_started_tool_stage(cancelled_stage(), audit=audit, recorder=recorder)

    assert stages == ["cancelled"]
    assert caught.value is cancellation
    assert caught.value.__cause__ is cause
    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.tool_call_id == audit.tool_call_id
    assert invocation.status is ComposerToolStatus.CANCELLED
    assert invocation.error_class == "CancelledError"
    assert invocation.started_at == audit.started_at
    assert invocation.finished_at >= invocation.started_at
    assert invocation.version_before == 3 and invocation.version_after is None
    assert invocation.result_canonical is None

    # A completed stage leaves success closure with its caller. It must neither
    # acquire a cancellation record nor cause a duplicate terminal invocation.
    success_audit = begin_dispatch("completed-tool", "get_pipeline_state", {}, version_before=3, actor="test")
    value = object()

    async def completed_stage() -> object:
        stages.append("completed")
        return value

    assert await _await_started_tool_stage(completed_stage(), audit=success_audit, recorder=recorder) is value
    assert stages == ["cancelled", "completed"]
    assert recorder.invocations == (invocation,)
    recorder.record(finish_success(success_audit, result_payload={"success": True}, version_after=3))
    assert [(item.tool_call_id, item.status) for item in recorder.invocations] == [
        ("cancelled-tool", ComposerToolStatus.CANCELLED),
        ("completed-tool", ComposerToolStatus.SUCCESS),
    ]
