"""Deterministic contracts for bounded sink-effect lease waiting."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from elspeth.contracts.audit import SinkEffect
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectFinalizationResult,
    SinkEffectInspection,
    SinkEffectInspectionRequest,
    SinkEffectLease,
    SinkEffectPlan,
    SinkEffectReconcileResult,
    SinkEffectState,
)
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution import sink_effect_lifecycle
from elspeth.core.landscape.execution.sink_effect_lifecycle import SinkEffectLifecycle
from elspeth.core.landscape.schema import sink_effects_table
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
    SinkEffectLeaseHeld,
    SinkEffectPredecessorPending,
)
from tests.fixtures.sink_effects import DuplicateObservableSink, DuplicateObservableTarget
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members, _pipeline_request
from tests.unit.engine.test_sink_effect_executor import _CumulativeObservableSink, _CumulativeTarget, _execution_request


class _AdvanceSleep:
    def __init__(
        self,
        clock: MockClock,
        *,
        after_sleep: Callable[[], None] | None = None,
    ) -> None:
        self.clock = clock
        self.after_sleep = after_sleep
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        assert seconds > 0.0, "lease polling must never busy-spin"
        self.calls.append(seconds)
        self.clock.advance(seconds)
        if self.after_sleep is not None:
            self.after_sleep()


def _shutdown_error() -> GracefulShutdownError:
    return GracefulShutdownError(
        rows_processed=0,
        run_id="run-shutdown",
        rows_succeeded=0,
        rows_failed=0,
        rows_quarantined=0,
        rows_routed_success=0,
        rows_routed_failure=0,
        routed_destinations={},
    )


def _held_effect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lease_ttl: timedelta,
) -> tuple[
    object,
    object,
    object,
    DuplicateObservableSink,
    DuplicateObservableTarget,
    MockClock,
]:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _execution_request(run_id, sink_id, members)
    target = DuplicateObservableTarget()
    sink = DuplicateObservableSink(target)
    clock = MockClock(start=datetime.now(UTC).timestamp())
    monkeypatch.setattr(sink_effect_lifecycle, "now", clock.now_utc)

    def stop_before_publication(seam: SinkEffectExecutionSeam) -> None:
        if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
            raise SinkEffectInjectedFault(seam)

    with pytest.raises(SinkEffectInjectedFault):
        SinkEffectCoordinator(
            factory=factory,
            worker_id="lease-holder",
            lease_ttl=lease_ttl,
            fault_hook=stop_before_publication,
            clock=clock,
        ).execute(request, sink)
    effect = factory.execution.sink_effects.get_effects_for_run(run_id)[0]
    assert effect.state is SinkEffectState.IN_FLIGHT
    assert effect.lease_owner == "lease-holder"
    assert target.publication_count == 0
    return db, factory, request, sink, target, clock


def test_foreign_lease_expires_then_waiter_reclaims_and_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=2)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    sleep = _AdvanceSleep(clock)
    try:
        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="lease-waiter",
            lease_ttl=lease_ttl,
            clock=clock,
            sleep=sleep,
            poll_interval=0.5,
        ).execute_with_lease_wait(request, sink)

        assert result.effect.state is SinkEffectState.FINALIZED
        assert result.effect.effect_id == target.effect_id
        assert target.publication_count == 1
        assert sleep.calls
        assert all(0.0 < seconds <= 0.5 for seconds in sleep.calls)
        assert sum(sleep.calls) <= lease_ttl.total_seconds() + 0.5
        assert len(factory.execution.sink_effects.get_effects_for_run(result.effect.run_id)) == 1
    finally:
        db.close()


def test_waiter_reuses_peer_finalized_effect_without_publishing_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=5)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    peer_result: list[object] = []

    def finalize_from_peer() -> None:
        if peer_result:
            return
        peer_result.append(
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-holder",
                lease_ttl=lease_ttl,
                clock=clock,
            ).execute(request, sink)
        )

    sleep = _AdvanceSleep(clock, after_sleep=finalize_from_peer)
    try:
        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="lease-waiter",
            lease_ttl=lease_ttl,
            clock=clock,
            sleep=sleep,
            poll_interval=0.5,
        ).execute_with_lease_wait(request, sink)

        assert len(peer_result) == 1
        peer = peer_result[0]
        assert result.effect.effect_id == peer.effect.effect_id  # type: ignore[attr-defined]
        assert result.artifact.artifact_id == peer.artifact.artifact_id  # type: ignore[attr-defined]
        assert target.publication_count == 1
    finally:
        db.close()


def test_successor_waits_for_peer_to_finalize_predecessor_then_advances_stream_once() -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 2)
    predecessor_request = _execution_request(run_id, sink_id, members[:1])
    successor_request = _execution_request(run_id, sink_id, members[1:])
    target = _CumulativeTarget()
    sink = _CumulativeObservableSink(target)
    clock = MockClock(start=datetime.now(UTC).timestamp())

    predecessor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True)).new_effect
    successor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True)).new_effect
    assert predecessor is not None
    assert successor is not None
    assert successor.predecessor_effect_id == predecessor.effect_id

    peer_results: list[SinkEffectFinalizationResult] = []

    def finalize_predecessor_from_peer() -> None:
        if peer_results:
            return
        peer_results.append(
            SinkEffectCoordinator(
                factory=factory,
                worker_id="predecessor-peer",
                clock=clock,
            ).execute(predecessor_request, sink)
        )

    sleep = _AdvanceSleep(clock, after_sleep=finalize_predecessor_from_peer)
    try:
        with pytest.raises(SinkEffectPredecessorPending, match="waiting for predecessor"):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="immediate-successor",
                clock=clock,
            ).execute(successor_request, sink)
        assert target.published_rows == []

        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="successor-waiter",
            lease_ttl=timedelta(seconds=2),
            clock=clock,
            sleep=sleep,
            poll_interval=0.5,
        ).execute_with_lease_wait(successor_request, sink)

        assert len(peer_results) == 1
        assert result.effect.effect_id == successor.effect_id
        assert sleep.calls == [0.5]
        assert all(seconds > 0.0 for seconds in sleep.calls)
        assert sum(sleep.calls) <= 2.5
        assert sink.commit_calls == 2
        assert target.published_rows == [
            [{"ordinal": 0}],
            [{"ordinal": 0}, {"ordinal": 1}],
        ]
        effects = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert [(effect.stream_sequence, effect.state, effect.publication_performed) for effect in effects] == [
            (0, SinkEffectState.FINALIZED, True),
            (1, SinkEffectState.FINALIZED, True),
        ]
        artifacts = factory.execution.get_artifacts(run_id)
        assert len(artifacts) == 2
        assert {artifact.sink_effect_id for artifact in artifacts} == {predecessor.effect_id, successor.effect_id}
    finally:
        db.close()


def test_predecessor_then_foreign_lease_share_one_fixed_wait_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    lease_ttl = timedelta(seconds=2)
    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 2)
    predecessor_request = _execution_request(run_id, sink_id, members[:1])
    successor_request = _execution_request(run_id, sink_id, members[1:])
    target = _CumulativeTarget()
    sink = _CumulativeObservableSink(target)
    clock = MockClock(start=datetime.now(UTC).timestamp())
    monkeypatch.setattr(sink_effect_lifecycle, "now", clock.now_utc)

    predecessor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True)).new_effect
    successor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True)).new_effect
    assert predecessor is not None
    assert successor is not None

    peer_result: list[SinkEffectFinalizationResult] = []
    successor_claim: list[SinkEffectLease] = []

    def keep_successor_contended() -> None:
        if not peer_result:
            peer_result.append(
                SinkEffectCoordinator(
                    factory=factory,
                    worker_id="predecessor-peer",
                    lease_ttl=lease_ttl,
                    clock=clock,
                ).execute(predecessor_request, sink)
            )
            successor_claim.append(
                factory.execution.sink_effects.claim_preparation(
                    successor.effect_id,
                    owner="successor-holder",
                    ttl=lease_ttl,
                )
            )
            return
        claim = successor_claim[0]
        factory.execution.sink_effects.heartbeat_lease(
            successor.effect_id,
            owner=claim.owner,
            generation=claim.generation,
            ttl=lease_ttl,
        )

    sleep = _AdvanceSleep(clock, after_sleep=keep_successor_contended)
    try:
        with pytest.raises(SinkEffectLeaseHeld, match="preparation"):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="successor-waiter",
                lease_ttl=lease_ttl,
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
            ).execute_with_lease_wait(successor_request, sink)

        assert len(peer_result) == 1
        assert sleep.calls == [0.5] * 5
        assert sum(sleep.calls) == lease_ttl.total_seconds() + 0.5
        assert target.published_rows == [[{"ordinal": 0}]]
        current = factory.execution.sink_effects.get_effect(successor.effect_id)
        assert current is not None
        assert current.lease_owner == "successor-holder"
        assert current.state is SinkEffectState.RESERVED
    finally:
        db.close()


def test_shutdown_interrupts_predecessor_wait_before_peer_finalization() -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 2)
    successor_request = _execution_request(run_id, sink_id, members[1:])
    predecessor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True)).new_effect
    successor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True)).new_effect
    assert predecessor is not None
    assert successor is not None
    target = _CumulativeTarget()
    shutdown = threading.Event()
    clock = MockClock(start=datetime.now(UTC).timestamp())
    sleep = _AdvanceSleep(clock, after_sleep=shutdown.set)
    try:
        with pytest.raises(GracefulShutdownError):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="successor-waiter",
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
                shutdown_event=shutdown,
                make_shutdown_error=_shutdown_error,
            ).execute_with_lease_wait(successor_request, _CumulativeObservableSink(target))

        assert sleep.calls == [0.5]
        assert target.published_rows == []
        current_predecessor = factory.execution.sink_effects.get_effect(predecessor.effect_id)
        current_successor = factory.execution.sink_effects.get_effect(successor.effect_id)
        assert current_predecessor is not None
        assert current_successor is not None
        assert current_predecessor.state is SinkEffectState.RESERVED
        assert current_successor.state is SinkEffectState.RESERVED
    finally:
        db.close()


def test_coordination_latch_interrupts_predecessor_wait_before_peer_finalization() -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 2)
    successor_request = _execution_request(run_id, sink_id, members[1:])
    predecessor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True)).new_effect
    successor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True)).new_effect
    assert predecessor is not None
    assert successor is not None
    target = _CumulativeTarget()
    clock = MockClock(start=datetime.now(UTC).timestamp())
    sleep = _AdvanceSleep(clock)
    latch_calls = 0

    class _Deposed(RuntimeError):
        pass

    def latch() -> None:
        nonlocal latch_calls
        latch_calls += 1
        if latch_calls == 2:
            raise _Deposed("worker epoch was deposed")

    try:
        with pytest.raises(_Deposed, match="deposed"):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="successor-waiter",
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
                check_coordination_latch=latch,
            ).execute_with_lease_wait(successor_request, _CumulativeObservableSink(target))

        assert latch_calls == 2
        assert sleep.calls == [0.5]
        assert target.published_rows == []
        current_predecessor = factory.execution.sink_effects.get_effect(predecessor.effect_id)
        current_successor = factory.execution.sink_effects.get_effect(successor.effect_id)
        assert current_predecessor is not None
        assert current_successor is not None
        assert current_predecessor.state is SinkEffectState.RESERVED
        assert current_successor.state is SinkEffectState.RESERVED
    finally:
        db.close()


def test_successor_wait_traverses_multi_hop_predecessor_chain() -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 3)
    requests = tuple(_execution_request(run_id, sink_id, members[index : index + 1]) for index in range(3))
    effects = tuple(
        factory.execution.sink_effects.reserve(
            _pipeline_request(run_id, sink_id, members[index : index + 1], replacing_target=True)
        ).new_effect
        for index in range(3)
    )
    assert all(effect is not None for effect in effects)
    first, second, third = effects
    assert first is not None
    assert second is not None
    assert third is not None
    assert second.predecessor_effect_id == first.effect_id
    assert third.predecessor_effect_id == second.effect_id
    target = _CumulativeTarget()
    sink = _CumulativeObservableSink(target)
    clock = MockClock(start=datetime.now(UTC).timestamp())
    finalized_predecessors: list[SinkEffectFinalizationResult] = []

    def finalize_next_predecessor() -> None:
        index = len(finalized_predecessors)
        if index >= 2:
            raise AssertionError("successor wait polled after both predecessors finalized")
        finalized_predecessors.append(
            SinkEffectCoordinator(
                factory=factory,
                worker_id=f"predecessor-peer-{index}",
                clock=clock,
            ).execute(requests[index], sink)
        )

    sleep = _AdvanceSleep(clock, after_sleep=finalize_next_predecessor)
    try:
        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="successor-waiter",
            clock=clock,
            sleep=sleep,
            poll_interval=0.5,
        ).execute_with_lease_wait(requests[2], sink)

        assert result.effect.effect_id == third.effect_id
        assert sleep.calls == [0.5, 0.5]
        assert sink.commit_calls == 3
        assert target.published_rows == [
            [{"ordinal": 0}],
            [{"ordinal": 0}, {"ordinal": 1}],
            [{"ordinal": 0}, {"ordinal": 1}, {"ordinal": 2}],
        ]
    finally:
        db.close()


def test_predecessor_disappearing_during_wait_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 2)
    successor_request = _execution_request(run_id, sink_id, members[1:])
    predecessor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True)).new_effect
    successor = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True)).new_effect
    assert predecessor is not None
    assert successor is not None

    def refuse_sleep(_seconds: float) -> None:
        raise AssertionError("missing predecessor must not be polled")

    coordinator = SinkEffectCoordinator(
        factory=factory,
        worker_id="successor-waiter",
        sleep=refuse_sleep,
    )
    original_get_effect = coordinator._effects.get_effect
    predecessor_reads = 0

    def disappear_after_initial_read(effect_id: str) -> SinkEffect | None:
        nonlocal predecessor_reads
        if effect_id == predecessor.effect_id:
            predecessor_reads += 1
            if predecessor_reads > 1:
                return None
        return original_get_effect(effect_id)

    monkeypatch.setattr(coordinator._effects, "get_effect", disappear_after_initial_read)
    try:
        with pytest.raises(LandscapeRecordError, match="predecessor disappeared"):
            coordinator.execute_with_lease_wait(successor_request, _CumulativeObservableSink(_CumulativeTarget()))
    finally:
        db.close()


def test_live_lease_through_budget_reraises_original_exception_after_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=2)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    effect = factory.execution.sink_effects.get_effects_for_run(request.reservation.run_id)[0]

    def keep_peer_live() -> None:
        factory.execution.sink_effects.heartbeat_lease(
            effect.effect_id,
            owner="lease-holder",
            generation=effect.generation,
            ttl=lease_ttl,
        )

    sleep = _AdvanceSleep(clock, after_sleep=keep_peer_live)
    try:
        with pytest.raises(SinkEffectLeaseHeld, match="live lease") as captured:
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-waiter",
                lease_ttl=lease_ttl,
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
            ).execute_with_lease_wait(request, sink)

        assert type(captured.value) is SinkEffectLeaseHeld
        assert target.publication_count == 0
        assert sleep.calls
        assert all(0.0 < seconds <= 0.5 for seconds in sleep.calls)
        assert sum(sleep.calls) <= lease_ttl.total_seconds() + 0.5
        current = factory.execution.sink_effects.get_effect(effect.effect_id)
        assert current is not None
        assert (current.lease_owner, current.generation) == ("lease-holder", effect.generation)
    finally:
        db.close()


def test_shutdown_interrupts_wait_before_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=2)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    shutdown = threading.Event()
    effect = factory.execution.sink_effects.get_effects_for_run(request.reservation.run_id)[0]
    sleep = _AdvanceSleep(clock, after_sleep=shutdown.set)
    try:
        with pytest.raises(GracefulShutdownError):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-waiter",
                lease_ttl=lease_ttl,
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
                shutdown_event=shutdown,
                make_shutdown_error=_shutdown_error,
            ).execute_with_lease_wait(request, sink)

        current = factory.execution.sink_effects.get_effect(effect.effect_id)
        assert current is not None
        assert (current.lease_owner, current.generation) == ("lease-holder", effect.generation)
        assert target.publication_count == 0
    finally:
        db.close()


def test_coordination_latch_interrupts_wait_before_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=2)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    effect = factory.execution.sink_effects.get_effects_for_run(request.reservation.run_id)[0]
    latch_calls = 0

    class _Deposed(RuntimeError):
        pass

    def latch() -> None:
        nonlocal latch_calls
        latch_calls += 1
        if latch_calls == 2:
            raise _Deposed("worker epoch was deposed")

    sleep = _AdvanceSleep(clock)
    try:
        with pytest.raises(_Deposed, match="deposed"):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-waiter",
                lease_ttl=lease_ttl,
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
                check_coordination_latch=latch,
            ).execute_with_lease_wait(request, sink)

        current = factory.execution.sink_effects.get_effect(effect.effect_id)
        assert current is not None
        assert (current.lease_owner, current.generation) == ("lease-holder", effect.generation)
        assert target.publication_count == 0
    finally:
        db.close()


def test_corrupt_effect_state_during_authoritative_poll_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ttl = timedelta(seconds=2)
    db, factory, request, sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    effect = factory.execution.sink_effects.get_effects_for_run(request.reservation.run_id)[0]
    corrupted = False

    def corrupt_after_first_poll() -> None:
        nonlocal corrupted
        if corrupted:
            return
        corrupted = True
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                update(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id).values(lease_expires_at=None)
            )

    sleep = _AdvanceSleep(clock, after_sleep=corrupt_after_first_poll)
    try:
        with pytest.raises(ValueError, match="in-flight effect lifecycle fields are incomplete"):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-waiter",
                lease_ttl=lease_ttl,
                clock=clock,
                sleep=sleep,
                poll_interval=0.5,
            ).execute_with_lease_wait(request, sink)

        assert target.publication_count == 0
        assert sleep.calls == [0.5]
    finally:
        db.close()


def test_execution_heartbeat_prevents_takeover_during_blocked_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _execution_request(run_id, sink_id, members)
    target = DuplicateObservableTarget()
    commit_entered = threading.Event()
    heartbeat_observed = threading.Event()
    release_commit = threading.Event()
    holder_errors: list[BaseException] = []
    lease_ttl = timedelta(seconds=1)

    original_heartbeat = factory.execution.sink_effects.heartbeat_lease

    def observe_execution_heartbeat(
        effect_id: str,
        *,
        owner: str,
        generation: int,
        ttl: timedelta,
    ) -> SinkEffectLease:
        lease = original_heartbeat(
            effect_id,
            owner=owner,
            generation=generation,
            ttl=ttl,
        )
        if commit_entered.is_set() and owner == "lease-holder":
            heartbeat_observed.set()
        return lease

    monkeypatch.setattr(factory.execution.sink_effects, "heartbeat_lease", observe_execution_heartbeat)

    class _BlockingSink(DuplicateObservableSink):
        def commit_effect(self, plan, ctx):  # type: ignore[no-untyped-def]
            if threading.current_thread().name == "lease-holder":
                commit_entered.set()
                if not release_commit.wait(timeout=5):
                    raise AssertionError("timed out waiting to release blocked commit")
            return super().commit_effect(plan, ctx)

    sink = _BlockingSink(target)

    def run_holder() -> None:
        try:
            SinkEffectCoordinator(
                factory=factory,
                worker_id="lease-holder",
                lease_ttl=lease_ttl,
            ).execute(request, sink)
        except BaseException as exc:
            holder_errors.append(exc)

    holder = threading.Thread(target=run_holder, name="lease-holder")
    holder.start()
    try:
        assert commit_entered.wait(timeout=5), "holder never reached external commit"
        assert heartbeat_observed.wait(timeout=5), "holder did not heartbeat its execution lease"
        with pytest.raises(SinkEffectLeaseHeld, match="live lease"):
            SinkEffectCoordinator(
                factory=make_factory(db),
                worker_id="lease-waiter",
                lease_ttl=lease_ttl,
                poll_interval=0.05,
            ).execute_with_lease_wait(request, sink)
        assert target.publication_count == 0
    finally:
        release_commit.set()
        holder.join(timeout=5)
    try:
        assert not holder.is_alive()
        assert holder_errors == []
        assert target.publication_count == 1
        (effect,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert effect.state is SinkEffectState.FINALIZED
        assert effect.publication_performed is True
        assert effect.effect_id == target.effect_id
        (artifact,) = factory.execution.get_artifacts(run_id)
        assert artifact.artifact_id == effect.artifact_id
        assert artifact.sink_effect_id == effect.effect_id
    finally:
        db.close()


def test_concurrent_preparation_claim_loser_enters_shared_wait_and_reuses_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _execution_request(run_id, sink_id, members)
    target = DuplicateObservableTarget()
    inspection_entered = threading.Event()
    release_inspection = threading.Event()
    claim_barrier = threading.Barrier(2)

    class _BlockingInspectionSink(DuplicateObservableSink):
        def inspect_effect(
            self,
            request: SinkEffectInspectionRequest,
            ctx: RestrictedSinkEffectContext,
        ) -> SinkEffectInspection:
            inspection_entered.set()
            if not release_inspection.wait(timeout=5):
                raise AssertionError("timed out waiting to release inspection")
            return super().inspect_effect(request, ctx)

    sink = _BlockingInspectionSink(target)
    original_claim = SinkEffectLifecycle.claim_preparation

    def synchronized_claim(
        lifecycle: SinkEffectLifecycle,
        effect_id: str,
        *,
        owner: str,
        ttl: timedelta,
    ) -> SinkEffectLease:
        claim_barrier.wait(timeout=5)
        return original_claim(lifecycle, effect_id, owner=owner, ttl=ttl)

    monkeypatch.setattr(SinkEffectLifecycle, "claim_preparation", synchronized_claim)
    results: list[SinkEffectFinalizationResult] = []
    errors: list[BaseException] = []

    def execute(worker_id: str) -> None:
        try:
            results.append(
                SinkEffectCoordinator(
                    factory=make_factory(db),
                    worker_id=worker_id,
                    poll_interval=0.01,
                ).execute_with_lease_wait(request, sink)
            )
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=execute, args=("worker-a",)),
        threading.Thread(target=execute, args=("worker-b",)),
    ]
    for worker in workers:
        worker.start()
    try:
        assert inspection_entered.wait(timeout=5), "claim winner never reached inspection"
        release_inspection.set()
        for worker in workers:
            worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert {result.effect.effect_id for result in results} == {results[0].effect.effect_id}
        assert {result.artifact.artifact_id for result in results} == {results[0].artifact.artifact_id}
        assert target.publication_count == 1
        (effect,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert effect.state is SinkEffectState.FINALIZED
        assert effect.effect_id == results[0].effect.effect_id
    finally:
        release_inspection.set()
        for worker in workers:
            worker.join(timeout=5)
        db.close()


def test_concurrent_execution_lease_acquire_loser_enters_shared_wait_and_reuses_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _execution_request(run_id, sink_id, members)
    target = DuplicateObservableTarget()
    reconcile_entered = threading.Event()
    release_reconcile = threading.Event()
    acquire_barrier = threading.Barrier(2)

    class _BlockingReconcileSink(DuplicateObservableSink):
        def reconcile_effect(
            self,
            plan: SinkEffectPlan,
            ctx: RestrictedSinkEffectContext,
        ) -> SinkEffectReconcileResult:
            reconcile_entered.set()
            if not release_reconcile.wait(timeout=5):
                raise AssertionError("timed out waiting to release reconciliation")
            return super().reconcile_effect(plan, ctx)

    sink = _BlockingReconcileSink(target)
    setup = SinkEffectCoordinator(factory=factory, worker_id="setup-worker")
    setup._persist_pipeline_member_payloads(request.effect_input)
    reserved = factory.execution.sink_effects.reserve(request.reservation).new_effect
    assert reserved is not None
    setup._prepare(reserved, request, sink, setup._context(reserved))
    prepared = factory.execution.sink_effects.get_effect(reserved.effect_id)
    assert prepared is not None and prepared.state is SinkEffectState.PREPARED

    original_acquire = SinkEffectLifecycle.acquire_lease

    def synchronized_acquire(
        lifecycle: SinkEffectLifecycle,
        effect_id: str,
        *,
        owner: str,
        ttl: timedelta,
    ) -> SinkEffectLease:
        acquire_barrier.wait(timeout=5)
        return original_acquire(lifecycle, effect_id, owner=owner, ttl=ttl)

    monkeypatch.setattr(SinkEffectLifecycle, "acquire_lease", synchronized_acquire)
    results: list[SinkEffectFinalizationResult] = []
    errors: list[BaseException] = []

    def execute(worker_id: str) -> None:
        try:
            results.append(
                SinkEffectCoordinator(
                    factory=make_factory(db),
                    worker_id=worker_id,
                    poll_interval=0.01,
                ).execute_with_lease_wait(request, sink)
            )
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=execute, args=("worker-a",)),
        threading.Thread(target=execute, args=("worker-b",)),
    ]
    for worker in workers:
        worker.start()
    try:
        assert reconcile_entered.wait(timeout=5), "lease winner never reached reconciliation"
        release_reconcile.set()
        for worker in workers:
            worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert {result.effect.effect_id for result in results} == {results[0].effect.effect_id}
        assert {result.artifact.artifact_id for result in results} == {results[0].artifact.artifact_id}
        assert target.publication_count == 1
        (effect,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert effect.state is SinkEffectState.FINALIZED
        assert effect.effect_id == results[0].effect.effect_id
    finally:
        release_reconcile.set()
        for worker in workers:
            worker.join(timeout=5)
        db.close()


def test_concurrent_expired_takeover_loser_waits_for_single_reclaim_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two expired-lease waiters race; the loser adopts the reclaim winner."""
    from tests.fixtures.landscape import make_factory

    lease_ttl = timedelta(seconds=2)
    db, factory, request, _sink, target, clock = _held_effect(monkeypatch, lease_ttl=lease_ttl)
    clock.advance(lease_ttl.total_seconds() + 0.01)
    takeover_barrier = threading.Barrier(2)
    reconcile_entered = threading.Event()
    release_reconcile = threading.Event()

    class _BlockingReconcileSink(DuplicateObservableSink):
        def reconcile_effect(
            self,
            plan: SinkEffectPlan,
            ctx: RestrictedSinkEffectContext,
        ) -> SinkEffectReconcileResult:
            reconcile_entered.set()
            if not release_reconcile.wait(timeout=5):
                raise AssertionError("timed out waiting to release takeover reconciliation")
            return super().reconcile_effect(plan, ctx)

    sink = _BlockingReconcileSink(target)
    original_takeover = SinkEffectLifecycle.takeover_expired

    def synchronized_takeover(
        lifecycle: SinkEffectLifecycle,
        effect_id: str,
        *,
        owner: str,
        ttl: timedelta,
    ) -> SinkEffectLease:
        takeover_barrier.wait(timeout=5)
        return original_takeover(lifecycle, effect_id, owner=owner, ttl=ttl)

    monkeypatch.setattr(SinkEffectLifecycle, "takeover_expired", synchronized_takeover)
    results: list[SinkEffectFinalizationResult] = []
    errors: list[BaseException] = []

    def execute(worker_id: str) -> None:
        try:
            results.append(
                SinkEffectCoordinator(
                    factory=make_factory(db),
                    worker_id=worker_id,
                    lease_ttl=lease_ttl,
                    clock=clock,
                    poll_interval=0.01,
                ).execute_with_lease_wait(request, sink)
            )
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=execute, args=("takeover-a",)),
        threading.Thread(target=execute, args=("takeover-b",)),
    ]
    for worker in workers:
        worker.start()
    try:
        assert reconcile_entered.wait(timeout=5), "takeover winner never reached reconciliation"
        release_reconcile.set()
        for worker in workers:
            worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert {result.effect.effect_id for result in results} == {results[0].effect.effect_id}
        assert {result.artifact.artifact_id for result in results} == {results[0].artifact.artifact_id}
        assert target.publication_count == 1
        (effect,) = factory.execution.sink_effects.get_effects_for_run(request.reservation.run_id)
        assert effect.state is SinkEffectState.FINALIZED
        assert effect.publication_performed is True
        assert effect.effect_id == results[0].effect.effect_id
        (artifact,) = factory.execution.get_artifacts(request.reservation.run_id)
        assert artifact.artifact_id == effect.artifact_id
        assert artifact.sink_effect_id == effect.effect_id
    finally:
        release_reconcile.set()
        for worker in workers:
            worker.join(timeout=5)
        db.close()
