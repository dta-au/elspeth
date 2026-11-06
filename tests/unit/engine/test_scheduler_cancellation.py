"""The scheduler stops admitting work at cooperative cancellation boundaries."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts.errors import RunMembershipLostError
from elspeth.contracts.scheduler import TokenWorkStatus
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.engine.test_collector_executor import TestRestore as _CollectorRestoreCases
from tests.unit.engine.test_collector_executor import _CollectorEnv
from tests.unit.engine.test_scheduler_drain_characterization import (
    LEADER_OWNER,
    _build,
    _ctx,
    _dropped_result,
    _enqueue_ready,
    _row_status,
)


@pytest.mark.parametrize("boundary", ["entry", "before-claim", "intake", "after-claim", "result"])
def test_cancellation_preserves_ready_work_at_each_admission_boundary(boundary: str) -> None:
    processor, scheduler, setup, clock = _build(lease_owner=LEADER_OWNER, bind_leader_token=True)
    first_id, first_token = _enqueue_ready(setup, scheduler, clock, sequence=0)
    second_id, _ = _enqueue_ready(setup, scheduler, clock, sequence=1)
    ctx = _ctx(setup)
    ctx.shutdown_event = event = threading.Event()
    processed: list[str] = []
    intake = processor._run_barrier_intake_pass
    claim_ready = scheduler.claim_ready

    def process(**kwargs: Any):
        processed.append(kwargs["token"].token_id)
        event.set()
        return _dropped_result(kwargs["token"]), []

    def cancelling_intake(context):
        result = intake(context)
        event.set()
        return result

    def cancelling_claim(**kwargs: Any):
        claimed = claim_ready(**kwargs)
        event.set()
        return claimed

    if boundary == "entry":
        event.set()
    scheduler.calls.clear()
    with (
        patch.object(processor, "_process_single_token", new=process),
        patch.object(processor, "_run_barrier_intake_pass", new=cancelling_intake if boundary == "intake" else intake),
        patch.object(scheduler, "claim_ready", new=cancelling_claim if boundary == "after-claim" else claim_ready),
    ):
        results = processor._drain_scheduler_claims(
            ctx=ctx,
            pending_items={},
            recover_pending_sinks=True,
            before_claim=event.set if boundary == "before-claim" else None,
        )

    # A claimed item whose operation never started stays undecided and
    # immediately resumable. An operation that already returned keeps its
    # accepted result. Neither path may claim the second READY item.
    admitted = boundary == "result"
    assert processed == ([first_token.token_id] if admitted else [])
    assert [result.token.token_id for result in results] == processed
    expected_status = TokenWorkStatus.TERMINAL.value if admitted else TokenWorkStatus.READY.value
    if boundary == "after-claim":
        expected_status = TokenWorkStatus.FAILED.value
    assert _row_status(setup, first_id)[0] == expected_status
    assert _row_status(setup, second_id) == (TokenWorkStatus.READY.value, None)
    assert scheduler.verbs().count("claim_ready") == (1 if boundary in {"result", "after-claim"} else 0)


def test_restored_collector_sweep_returns_accepted_group_and_preserves_next_group(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CollectorEnv(policy="require_all")
    fresh = env.fresh_executor()
    helpers = _CollectorRestoreCases()
    first_members, first_group = helpers._park_complete_group(env, fresh)
    second_members, second_group = helpers._park_complete_group(env, fresh)
    members = (*first_members, *second_members)
    items = [env.blocked_item_for(member, collector_name="stitch") for member in members]
    fresh.restore_from_journal(
        items=items,
        state_ids=env.state_ids_for(items),
        attempt_offsets={member.token_id: 0 for member in members},
        resume_checkpoint_id="ckpt-1",
    )
    original_process = fresh.transform.process
    env.ctx.shutdown_event = event = threading.Event()

    def cancel(rows, ctx):
        result = original_process(rows, ctx)
        event.set()
        return result

    monkeypatch.setattr(fresh.transform, "process", cancel)
    outcomes = fresh.flush_restored_complete_groups(ctx=env.ctx)
    assert [outcome.group_id for outcome in outcomes] == [first_group]
    assert fresh.transform.call_count == 1
    assert fresh.buffered_member_count() == len(second_members)
    assert env.open_hold_token_ids(node="stitch") == sorted(member.token_id for member in second_members)
    event.clear()
    outcomes = fresh.flush_restored_complete_groups(ctx=env.ctx)
    assert [outcome.group_id for outcome in outcomes] == [second_group]
    assert fresh.transform.call_count == 2
    assert fresh.buffered_member_count() == 0


def test_cancellation_does_not_override_membership_loss_while_preserving_a_new_claim() -> None:
    processor, scheduler, setup, clock = _build(lease_owner=LEADER_OWNER, bind_leader_token=True)
    work_id, _ = _enqueue_ready(setup, scheduler, clock, sequence=0)
    ctx = _ctx(setup)
    ctx.shutdown_event = event = threading.Event()
    original_claim = scheduler.claim_ready

    def depart_after_claim(**kwargs: Any):
        claim = original_claim(**kwargs)
        assert claim is not None
        setup.factory.run_coordination.release_seat(token=leader_coordination_token(setup.factory, setup.run_id))
        event.set()
        return claim

    with patch.object(scheduler, "claim_ready", new=depart_after_claim), pytest.raises(RunMembershipLostError):
        processor.drain_scheduled_work(ctx)
    # The real member fence refused the cancellation disposition, leaving the
    # abandoned lease for the successor. Cancellation never authorizes a write.
    assert _row_status(setup, work_id) == (TokenWorkStatus.LEASED.value, LEADER_OWNER)


def test_preclaimed_source_item_gets_fenced_undecided_disposition_on_shutdown() -> None:
    processor, scheduler, setup, clock = _build(lease_owner=LEADER_OWNER, bind_leader_token=True)
    work_id, token = _enqueue_ready(setup, scheduler, clock, sequence=0)
    ctx = _ctx(setup)
    claimed = scheduler.claim_ready(member_token=ctx.require_member_token(), lease_owner=LEADER_OWNER, lease_seconds=60)
    assert claimed is not None
    ctx.shutdown_event = threading.Event()
    ctx.shutdown_event.set()
    with patch.object(processor, "_process_single_token", autospec=True, return_value=(_dropped_result(token), [])) as process:
        results = processor._scheduler_drain.drain_claims(
            ctx=ctx, pending_items={}, preclaimed_items=[claimed], recover_pending_sinks=False
        )
    assert results == []
    process.assert_not_called()
    assert _row_status(setup, work_id) == (TokenWorkStatus.FAILED.value, None)


@pytest.mark.parametrize("policy", ["require_all", "best_effort"])
def test_shutdown_finishes_plugin_free_all_lost_verdict(policy: str) -> None:
    env = _CollectorEnv(policy=policy)
    members, group_id = env.seed_group(count=2)
    assert env.executor.notify_member_lost("stitch", group_id, members[0].token_id, "quarantined") is None
    env.ctx.shutdown_event = threading.Event()
    env.ctx.shutdown_event.set()
    outcome = env.executor.notify_member_lost("stitch", group_id, members[1].token_id, "quarantined")
    assert outcome is not None and not outcome.held
    assert env.transform.call_count == 0
    assert env.factory.run_status_projection.count_failed_collector_groups(env.run_id) == int(policy == "require_all")
    assert outcome.failure_reason == ("collector_missing_members" if policy == "require_all" else None)
    assert outcome.closed_without_plugin == (None if policy == "require_all" else "all_members_lost")


def test_live_collector_deferred_by_shutdown_closes_once_when_same_executor_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CollectorEnv(policy="require_all")
    members, _ = env.seed_group(count=2)
    assert env.executor.accept(members[0], "stitch").held
    env.ctx.shutdown_event = event = threading.Event()
    event.set()
    assert env.executor.accept(members[1], "stitch").held
    assert env.transform.call_count == 0
    assert env.open_hold_token_ids(node="stitch") == sorted(member.token_id for member in members)
    event.clear()
    original_close = env.executor._executor._close_group

    def cancel_inside_close(*args: Any, **kwargs: Any):
        event.set()
        return original_close(*args, **kwargs)

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(env.executor._executor, "_close_group", cancel_inside_close)
        assert env.executor.flush_restored_complete_groups() == ()
    assert env.transform.call_count == 0
    event.clear()
    assert len(env.executor.flush_restored_complete_groups()) == 1
    assert env.transform.call_count == 1
    assert env.executor.flush_restored_complete_groups() == ()
