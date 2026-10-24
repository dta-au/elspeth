"""Independent action headroom and immutable database-time observations."""

import asyncio
import time
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elspeth.web.composer.continuation_clock import (
    ContinuationActionClock,
    ContinuationTimingObservation,
    SegmentFit,
    admit_action,
    freeze_observation,
    size_segment,
)
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationSegmentPolicy


def test_long_action_gets_its_own_full_allowance() -> None:
    action = ContinuationActionPolicy(preparation=10, guard=30, quota=10, physical=400, retention=10)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    assert action.total == 460
    assert size_segment(action, policy) == 605
    assert admit_action(action, policy, hard_remaining=605, cooperative_remaining=180, fresh=True) is SegmentFit.ADMIT
    assert admit_action(action, policy, hard_remaining=460, cooperative_remaining=180, fresh=False) is SegmentFit.YIELD
    with pytest.raises(ValueError, match="fresh segment cannot fit"):
        admit_action(action, policy, hard_remaining=460, cooperative_remaining=180, fresh=True)


def test_yield_watermark_never_shortens_an_admitted_action() -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    assert admit_action(action, policy, hard_remaining=200, cooperative_remaining=0, fresh=False) is SegmentFit.YIELD
    assert admit_action(action, policy, hard_remaining=120, cooperative_remaining=40, fresh=False) is SegmentFit.YIELD
    assert admit_action(action, policy, hard_remaining=200, cooperative_remaining=40, fresh=False) is SegmentFit.ADMIT
    assert action.total == 7


def test_exact_action_and_settlement_headroom_is_enough_before_the_watermark() -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=1, quantum=180, reserve=25)

    assert admit_action(action, policy, hard_remaining=32, cooperative_remaining=10, fresh=True) is SegmentFit.ADMIT
    assert admit_action(action, policy, hard_remaining=31.99, cooperative_remaining=10, fresh=False) is SegmentFit.YIELD
    assert size_segment(action, policy) == 300


def test_frozen_observation_uses_db_timestamp_and_is_immutable() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    observation = freeze_observation(
        sequence=2,
        generation=3,
        now=now,
        deadline=now + timedelta(seconds=500),
        cooperative=now + timedelta(seconds=50),
        watermark=120,
    )

    assert observation.observed_at == now
    assert observation.hard_remaining_seconds == 500
    assert observation.cooperative_remaining_seconds == 50
    assert not observation.yield_pending
    with pytest.raises(FrozenInstanceError):
        observation.generation = 4


@pytest.mark.parametrize(
    "hard,cooperative",
    (
        (True, 10),
        (200, True),
        ("200", 10),
        (200, None),
        (float("nan"), 10),
        (200, float("inf")),
        pytest.param(10**1000, 10, id="overflowing-integer"),
    ),
)
def test_admission_refuses_non_numeric_boolean_and_nonfinite_observations(hard, cooperative) -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    with pytest.raises(ValueError, match="finite"):
        admit_action(action, policy, hard_remaining=hard, cooperative_remaining=cooperative, fresh=False)


@pytest.mark.parametrize("fresh", (0, 1, "fresh", None))
def test_admission_requires_actual_boolean_freshness(fresh) -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    with pytest.raises(ValueError, match="fresh"):
        admit_action(action, policy, hard_remaining=200, cooperative_remaining=40, fresh=fresh)


@pytest.mark.parametrize("hard,cooperative", ((100, 101), (0, 1), (-2, -1)))
def test_admission_refuses_cooperative_time_beyond_hard_remaining(hard, cooperative) -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    with pytest.raises(ValueError, match="ordered"):
        admit_action(action, policy, hard_remaining=hard, cooperative_remaining=cooperative, fresh=False)


def test_independently_valid_policy_values_must_fit_finite_combined_headroom() -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=1e308, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=1e308)

    with pytest.raises(ValueError, match="finite"):
        size_segment(action, policy)
    with pytest.raises(ValueError, match="finite"):
        admit_action(action, policy, hard_remaining=300, cooperative_remaining=180, fresh=False)


def test_clock_ports_require_the_actual_owned_policy_types() -> None:
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)

    with pytest.raises(TypeError, match="policy"):
        size_segment(object(), policy)
    with pytest.raises(TypeError, match="policy"):
        size_segment(action, object())
    with pytest.raises(TypeError, match="policy"):
        admit_action(object(), policy, hard_remaining=200, cooperative_remaining=40, fresh=False)


@pytest.mark.parametrize("sequence,generation", ((True, 1), (1, True), (1.5, 1), (1, 0), (None, 1), (1, "1")))
def test_observation_requires_positive_integer_owner_identities(sequence, generation) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="identit"):
        freeze_observation(
            sequence=sequence,
            generation=generation,
            now=now,
            deadline=now + timedelta(seconds=500),
            cooperative=now + timedelta(seconds=50),
            watermark=120,
        )


@pytest.mark.parametrize(
    "watermark", (0, -1, True, float("inf"), float("nan"), "120", None, pytest.param(10**1000, id="overflowing-integer"))
)
def test_observation_requires_a_positive_finite_watermark(watermark) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="watermark"):
        freeze_observation(
            sequence=1,
            generation=1,
            now=now,
            deadline=now + timedelta(seconds=500),
            cooperative=now + timedelta(seconds=50),
            watermark=watermark,
        )


@pytest.mark.parametrize("naive_position", ("now", "deadline", "cooperative"))
def test_observation_refuses_naive_database_times(naive_position) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    deadline, cooperative = now + timedelta(seconds=500), now + timedelta(seconds=50)
    values = {"now": now, "deadline": deadline, "cooperative": cooperative}
    values[naive_position] = values[naive_position].replace(tzinfo=None)

    with pytest.raises(ValueError, match="aware"):
        freeze_observation(sequence=1, generation=1, **values, watermark=120)


@pytest.mark.parametrize("cooperative_seconds", (500, 501))
def test_observation_refuses_a_cooperative_deadline_at_or_after_the_hard_deadline(cooperative_seconds) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="cooperative"):
        freeze_observation(
            sequence=1,
            generation=1,
            now=now,
            deadline=now + timedelta(seconds=500),
            cooperative=now + timedelta(seconds=cooperative_seconds),
            watermark=120,
        )


def test_observation_uses_elapsed_instants_across_a_repeated_local_hour() -> None:
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=0)
    cooperative = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    deadline = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)

    observation = freeze_observation(sequence=1, generation=1, now=now, deadline=deadline, cooperative=cooperative, watermark=120)

    assert observation.observed_at == now.astimezone(UTC)
    assert observation.hard_remaining_seconds == 3600
    assert observation.cooperative_remaining_seconds == 1800
    assert observation.yield_pending is False


def test_observation_counts_down_fixed_deadlines_without_refreshing_them() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    deadline, cooperative = now + timedelta(seconds=500), now + timedelta(seconds=50)
    first = freeze_observation(sequence=2, generation=3, now=now, deadline=deadline, cooperative=cooperative, watermark=120)
    later = freeze_observation(
        sequence=2, generation=3, now=now + timedelta(seconds=20), deadline=deadline, cooperative=cooperative, watermark=120
    )
    yielded = freeze_observation(
        sequence=2, generation=3, now=now + timedelta(seconds=51), deadline=deadline, cooperative=cooperative, watermark=120
    )
    expired = freeze_observation(
        sequence=2, generation=3, now=deadline + timedelta(seconds=1), deadline=deadline, cooperative=cooperative, watermark=120
    )

    assert (first.hard_remaining_seconds, first.cooperative_remaining_seconds) == (500, 50)
    assert (later.hard_remaining_seconds, later.cooperative_remaining_seconds) == (480, 30)
    assert (yielded.hard_remaining_seconds, yielded.cooperative_remaining_seconds, yielded.yield_pending) == (449, 0, True)
    assert (expired.hard_remaining_seconds, expired.cooperative_remaining_seconds, expired.yield_pending) == (0, 0, True)


@pytest.mark.parametrize(
    "hard,cooperative,watermark,yield_pending",
    (
        (float("inf"), 10, 5, False),
        (100, float("nan"), 5, False),
        (100, 10, True, False),
        (100, 101, 5, False),
        (0, 0, 5, False),
        (-1, 0, 5, True),
        (100, 10, 5, "pending"),
    ),
)
def test_direct_frozen_observations_cannot_claim_inconsistent_or_nonfinite_state(
    hard,
    cooperative,
    watermark,
    yield_pending,
) -> None:
    with pytest.raises(ValueError):
        ContinuationTimingObservation(
            sequence=1,
            generation=1,
            observed_at=datetime(2026, 10, 20, tzinfo=UTC),
            hard_remaining_seconds=hard,
            cooperative_remaining_seconds=cooperative,
            watermark_seconds=watermark,
            yield_pending=yield_pending,
        )


@pytest.mark.asyncio
async def test_stage_expiry_survives_a_joined_owner_that_retains_cancellation() -> None:
    policy = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.05, retention=1)
    normal = ContinuationActionClock(policy)
    normal.finish_preparation()
    expected = object()

    async def immediate():
        return expected

    assert await normal.run("physical", immediate) is expected

    release = asyncio.Event()
    observed = asyncio.Event()
    retained = []

    async def actual_child():
        await release.wait()
        return expected

    child = asyncio.create_task(actual_child())

    async def joined_owner():
        try:
            return await asyncio.shield(child)
        except asyncio.CancelledError as original:
            retained.append(original)
            observed.set()
            return await asyncio.shield(child)

    clock = ContinuationActionClock(policy)
    clock.finish_preparation()
    task = asyncio.create_task(clock.run("physical", joined_owner))
    try:
        await asyncio.wait_for(observed.wait(), timeout=2)
        assert not child.done() and not task.done()
        release.set()
        with pytest.raises(TimeoutError, match="actual owner was joined"):
            await asyncio.wait_for(task, timeout=2)
        assert child.done() and child.result() is expected
        assert len(retained) == 1 and isinstance(retained[0], asyncio.CancelledError)
    finally:
        release.set()
        await asyncio.gather(child, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stage_expiry_checks_actual_monotonic_time_before_timeout_callback() -> None:
    policy = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.02, retention=1)
    clock = ContinuationActionClock(policy)
    clock.finish_preparation()

    async def synchronous_phase():
        time.sleep(0.04)
        return "late actual result"

    with pytest.raises(TimeoutError, match="actual owner was joined"):
        await clock.run("physical", synchronous_phase)


@pytest.mark.asyncio
async def test_a_stage_cannot_replenish_the_same_action_clock() -> None:
    clock = ContinuationActionClock(ContinuationActionPolicy(preparation=1, guard=1, quota=1, physical=1, retention=1))
    clock.finish_preparation()

    async def done():
        return "complete"

    assert await clock.run("guard", done) == "complete"
    original = clock.deadline
    with pytest.raises(ValueError, match="repeated"):
        await clock.run("guard", done)
    assert clock.deadline == original


@pytest.mark.asyncio
async def test_physical_timeout_is_its_declared_bound_not_segment_remainder() -> None:
    clock = ContinuationActionClock(ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.01, retention=1))
    clock.finish_preparation()

    async def physical():
        await asyncio.sleep(0.05)
        return "too late"

    with pytest.raises(TimeoutError):
        await clock.run("physical", physical)
    with pytest.raises(ValueError, match="inapplicable"):
        await clock.run("quota", physical)
