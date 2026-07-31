"""Deterministic contracts for bounded sink-effect lease waiting."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

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
from elspeth.core.landscape.execution import sink_effect_lifecycle
from elspeth.core.landscape.execution.sink_effect_lifecycle import SinkEffectLifecycle
from elspeth.core.landscape.schema import sink_effects_table
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
    SinkEffectLeaseHeld,
)
from tests.fixtures.sink_effects import DuplicateObservableSink, DuplicateObservableTarget
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members
from tests.unit.engine.test_sink_effect_executor import _execution_request


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


def test_coordination_latch_is_rechecked_immediately_before_payload_and_adapter_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each new irreversible cohort needs a fresh proof, not one admission check."""
    from tests.fixtures.landscape import make_factory, make_landscape_db

    db = make_landscape_db()
    factory = make_factory(db)
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _execution_request(run_id, sink_id, members)
    target = DuplicateObservableTarget()
    sink = DuplicateObservableSink(target)
    guarded = False
    observed: list[str] = []

    def latch() -> None:
        nonlocal guarded
        guarded = True

    def consume_guard(name: str) -> None:
        nonlocal guarded
        assert guarded, f"{name} started without a fresh coordination-latch proof"
        guarded = False
        observed.append(name)

    store = factory.payload_store
    assert store is not None
    original_store = store.store
    original_inspect = sink.inspect_effect
    original_prepare = sink.prepare_effect
    original_commit = sink.commit_effect

    def guarded_store(content: bytes) -> str:
        consume_guard("payload_store.store")
        return original_store(content)

    def guarded_inspect(request, ctx):  # type: ignore[no-untyped-def]
        consume_guard("inspect_effect")
        return original_inspect(request, ctx)

    def guarded_prepare(request, ctx):  # type: ignore[no-untyped-def]
        consume_guard("prepare_effect")
        return original_prepare(request, ctx)

    def guarded_commit(plan, ctx):  # type: ignore[no-untyped-def]
        consume_guard("commit_effect")
        return original_commit(plan, ctx)

    monkeypatch.setattr(store, "store", guarded_store)
    monkeypatch.setattr(sink, "inspect_effect", guarded_inspect)
    monkeypatch.setattr(sink, "prepare_effect", guarded_prepare)
    monkeypatch.setattr(sink, "commit_effect", guarded_commit)
    try:
        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="guarded-worker",
            check_coordination_latch=latch,
        ).execute(request, sink)

        assert result.effect.state is SinkEffectState.FINALIZED
        assert observed == [
            "payload_store.store",
            "inspect_effect",
            "prepare_effect",
            "commit_effect",
        ]
        assert target.publication_count == 1
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
