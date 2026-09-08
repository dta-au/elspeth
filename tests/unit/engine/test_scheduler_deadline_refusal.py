"""Lease completion refusal remains operational through the scheduler drain."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from elspeth.contracts import RowResult
from elspeth.contracts.scheduler import GroupLossSpec, TokenWorkStatus
from elspeth.core.landscape.lease_deadlines import LeaseDeadlineExpiredError
from elspeth.engine.work_items import WorkItem
from tests.unit.engine.test_scheduler_drain_characterization import (
    LEADER_OWNER,
    _build,
    _ctx,
    _dropped_result,
    _durable_claim_image,
    _enqueue_ready,
    _row_status,
)


@pytest.mark.parametrize("boundary", ["during_plugin", "after_plugin", "after_plugin_failure"])
def test_deadline_refusal_abandons_without_retry_or_failure_disposition(boundary: str) -> None:
    processor, spy, setup, clock = _build(lease_owner=LEADER_OWNER, heartbeat_seconds=60)
    try:
        work_item_id, token = _enqueue_ready(setup, spy, clock, sequence=0)
        refusal = LeaseDeadlineExpiredError("item lease has insufficient completion reserve")
        claim_images: list[object] = []

        def process(**kwargs: object) -> tuple[RowResult, list[WorkItem]]:
            processor._pending_group_losses.append(
                GroupLossSpec(
                    closer_name="merge",
                    group_id="fg-merge",
                    member_key="left",
                    token_id=token.token_id,
                    reason="staged before completion refusal",
                )
            )
            claim_images.append(_durable_claim_image(setup, work_item_id))
            clock.advance(61)
            if boundary == "during_plugin":
                processor._heartbeat_active_claim()
            if boundary == "after_plugin_failure":
                raise ValueError("plugin failure before heartbeat refusal")
            return _dropped_result(token), []

        spy.calls.clear()
        with (
            patch.object(processor, "_process_single_token", new=process),
            patch.object(setup.factory.scheduler, "heartbeat_lease", side_effect=refusal),
            pytest.raises(LeaseDeadlineExpiredError) as caught,
        ):
            processor._drain_scheduler_claims(ctx=_ctx(setup), pending_items={}, recover_pending_sinks=False)
        assert caught.value is refusal
        assert len(spy.calls_for("heartbeat_lease")) == 1
        assert processor._pending_group_losses == []
        assert len(claim_images) == 1
        assert _durable_claim_image(setup, work_item_id) == claim_images[0]
        assert _row_status(setup, work_item_id) == (TokenWorkStatus.LEASED.value, LEADER_OWNER)
        for verb in ("mark_failed", "mark_terminal", "mark_pending_sink", "mark_blocked"):
            assert spy.calls_for(verb) == []
        # Active bookkeeping is cleared even though the claim remains durable.
        clock.advance(61)
        processor._heartbeat_active_claim()
        assert len(spy.calls_for("heartbeat_lease")) == 1
    finally:
        setup.db.close()


def test_plugin_timeout_keeps_ordinary_failure_disposition() -> None:
    processor, spy, setup, clock = _build(lease_owner=LEADER_OWNER, heartbeat_seconds=60)
    try:
        work_item_id, _ = _enqueue_ready(setup, spy, clock, sequence=0)
        plugin_timeout = TimeoutError("plugin request timed out")
        spy.calls.clear()
        with (
            patch.object(processor, "_process_single_token", side_effect=plugin_timeout),
            pytest.raises(TimeoutError) as caught,
        ):
            processor._drain_scheduler_claims(ctx=_ctx(setup), pending_items={}, recover_pending_sinks=False)
        assert caught.value is plugin_timeout
        assert len(spy.calls_for("mark_failed")) == 1
        assert _row_status(setup, work_item_id)[0] == TokenWorkStatus.FAILED.value
    finally:
        setup.db.close()
