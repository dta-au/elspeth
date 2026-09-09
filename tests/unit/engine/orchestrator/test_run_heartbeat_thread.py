"""Unit tests for RunHeartbeatThread (ADR-030 §A.3, slice 4).

All beats are driven synchronously via ``_step_beat()`` on the test thread
(injected ``wait_fn=lambda _: False`` so no real wall-clock sleeps occur).
The repository is a stub that records calls and returns configurable snapshots.

Tests cover:

1. **Beats both rows in one txn** — ``worker_heartbeat`` called on every tick
   with the correct ``worker_id``, ``now``, and ``window_seconds`` (the
   underlying repo verb handles the single-transaction guarantee; this test
   verifies the thread calls the verb correctly).
2. **BUSY tolerated** — a write-lock-contention ``OperationalError`` from
   ``worker_heartbeat`` (SQLITE_BUSY / SQLITE_LOCKED, read off the DBAPI
   cause) is NOT fatal, does NOT set the latch, and does NOT terminate the
   thread; ``check_and_raise()`` must not raise after a busy tick. Any OTHER
   ``OperationalError`` is an audit-store fault carrying no liveness evidence
   and latches for the drain boundary.
3. **heartbeat_degraded fires past threshold** — after ``k`` consecutive busy
   failures ``record_heartbeat_degraded`` is called exactly once with the
   correct ``failures`` count; does NOT re-fire on the (k+1)-th miss (the
   count continues to grow but the event fires each time).
4. **Fatal latch set when seat taken; surfaced at boundary** —
   ``worker_heartbeat`` returning ``worker_active=True`` but
   ``leader_worker_id != our_id`` sets ``_coordination_lost_event`` and
   ``check_and_raise()`` raises ``RunWorkerEvictedError``.
5. **Fatal latch set on membership loss** — ``worker_heartbeat``
   returning ``WorkerMembershipLost`` sets the latch.
6. **Clean start/join lifecycle** — start() + step_beat() (healthy) +
   stop() completes without leaking threads; the thread is a daemon so it
   does not prevent process exit, but stop() must join it within the test.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.coordination import (
    DEFAULT_RUN_HEARTBEAT_SECONDS,
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    CoordinationSnapshot,
    WorkerMembershipLost,
    WorkerMembershipToken,
)
from elspeth.contracts.errors import RunWorkerEvictedError
from elspeth.core.landscape.lease_deadlines import LeaseDeadlineExpiredError
from elspeth.engine.orchestrator.heartbeat import RunHeartbeatThread

# ---------------------------------------------------------------------------
# Stub repo — avoids MagicMock() to keep the unspecced-mock baseline clean
# ---------------------------------------------------------------------------


class _StubRepo:
    """Minimal stub of RunCoordinationRepository for heartbeat unit tests.

    Configurable via ``snapshot`` (the next heartbeat outcome to return),
    ``side_effect`` (raise this exception from worker_heartbeat instead),
    ``side_effects`` (per-call sequence that overrides both), and
    ``degraded_exception`` (raise this from record_heartbeat_degraded).
    Records calls via lists for assertion.
    """

    def __init__(self) -> None:
        self.snapshot: CoordinationSnapshot | WorkerMembershipLost | None = None
        self.side_effect: Exception | None = None
        self.side_effects: list[CoordinationSnapshot | WorkerMembershipLost | Exception] = []
        self.degraded_exception: Exception | None = None

        self.worker_heartbeat_calls: list[dict[str, Any]] = []
        self.record_heartbeat_degraded_calls: list[dict[str, Any]] = []

    def worker_heartbeat(
        self, *, member_token: WorkerMembershipToken, window_seconds: float
    ) -> CoordinationSnapshot | WorkerMembershipLost:
        self.worker_heartbeat_calls.append({"worker_id": member_token.worker_id, "window_seconds": window_seconds})
        if self.side_effects:
            result = self.side_effects.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if self.side_effect is not None:
            raise self.side_effect
        if self.snapshot is None:
            raise AssertionError("_StubRepo.snapshot must be set before calling worker_heartbeat")
        return self.snapshot

    def record_heartbeat_degraded(self, *, member_token: WorkerMembershipToken, failures: int, now: datetime) -> None:
        self.record_heartbeat_degraded_calls.append({"member_token": member_token, "failures": failures, "now": now})
        if self.degraded_exception is not None:
            raise self.degraded_exception


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_RUN_ID = "run-heartbeat-test"
_WORKER_ID = f"worker:{_RUN_ID}:abc123"
# The heartbeat is a MEMBER write (ADR-030 D4): the thread carries a
# WorkerMembershipToken — a leader's is derived from its coordination token.
_TOKEN = WorkerMembershipToken(run_id=_RUN_ID, worker_id=_WORKER_ID)

# Healthy snapshot: our worker is active, our worker is the leader.
_HEALTHY_SNAPSHOT = CoordinationSnapshot(
    leader_worker_id=_WORKER_ID,
    leader_epoch=1,
    seat_live=True,
    worker_active=True,
)

# Refused heartbeat: our registry row left 'active', with no seat observation.
_EVICTED_OUTCOME = WorkerMembershipLost(member_token=_TOKEN)

# Deposed snapshot: another worker took the seat (our row still active).
_DEPOSED_SNAPSHOT = CoordinationSnapshot(
    leader_worker_id="worker:other-run:usurper",
    leader_epoch=2,
    seat_live=True,
    worker_active=True,
)


def _busy_error(statement: str = "UPDATE run_workers SET heartbeat_expires_at=?") -> OperationalError:
    """A realistic SQLITE_BUSY: ``begin_write``'s busy_timeout poll surfaces the DBAPI error.

    ``heartbeat._is_lock_contention`` reads the message off ``exc.orig``, not
    off ``str(exc)`` (whose rendering appends the statement text), so a
    contention fixture MUST carry a DBAPI exception. An ``OperationalError``
    without one is deliberately not contention.
    """
    return OperationalError(statement, None, sqlite3.OperationalError("database is locked"))


def test_snapshot_rejects_inactive_membership() -> None:
    """Inactive membership must use the refusal outcome, never a seat snapshot."""
    with pytest.raises(ValueError, match="WorkerMembershipLost"):
        CoordinationSnapshot(
            leader_worker_id=_WORKER_ID,
            leader_epoch=1,
            seat_live=True,
            worker_active=False,
        )


def _make_thread(
    repo: Any,
    *,
    degraded_threshold: int = 3,
    now_fn: Callable[[], datetime] | None = None,
) -> RunHeartbeatThread:
    """Build a thread with a no-sleep wait_fn for deterministic stepping."""
    if now_fn is None:
        now_fn = lambda: datetime.now(UTC)  # noqa: E731
    return RunHeartbeatThread(
        repo,
        member_token=_TOKEN,
        heartbeat_seconds=DEFAULT_RUN_HEARTBEAT_SECONDS,
        window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        now_fn=now_fn,
        wait_fn=lambda _: False,  # never blocks; stop_event NOT waited
        degraded_threshold=degraded_threshold,
    )


# ---------------------------------------------------------------------------
# 1. Beats both rows in one transaction (via worker_heartbeat call args)
# ---------------------------------------------------------------------------


class TestBeatsCorrectly:
    def test_calls_worker_heartbeat_with_correct_args(self) -> None:
        """worker_heartbeat is called with the thread's worker_id and window — never a caller clock.

        The beat's deadline is the Landscape database clock plus the window
        (ADR-047); the thread's ``now_fn`` survives only for its wait loop
        and the forensic degraded record.
        """
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        fixed_now = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)
        thread = _make_thread(repo, now_fn=lambda: fixed_now)

        thread._step_beat()

        assert len(repo.worker_heartbeat_calls) == 1
        call = repo.worker_heartbeat_calls[0]
        assert call == {"worker_id": _WORKER_ID, "window_seconds": DEFAULT_RUN_LIVENESS_WINDOW_SECONDS}

    def test_healthy_beat_does_not_set_latch(self) -> None:
        """A healthy beat leaves check_and_raise() quiet."""
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        thread = _make_thread(repo)
        thread._step_beat()

        assert not thread._coordination_lost_event.is_set()
        thread.check_and_raise()  # must not raise

    def test_multiple_healthy_beats_accumulate_without_error(self) -> None:
        """Multiple sequential healthy beats do not trip the latch."""
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        thread = _make_thread(repo)
        for _ in range(5):
            thread._step_beat()

        assert not thread._coordination_lost_event.is_set()
        assert len(repo.worker_heartbeat_calls) == 5

    def test_beat_count_equals_window_seconds_times_1000_divided_by_heartbeat(self) -> None:
        """Confirm the module constant relationship: window >= 4*(beat+busy)."""
        busy_timeout_seconds = 5.0  # from _SQLITE_PRAGMA_INVARIANTS_FILE
        assert 4 * (DEFAULT_RUN_HEARTBEAT_SECONDS + busy_timeout_seconds) <= DEFAULT_RUN_LIVENESS_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# 2. BUSY tolerated (OperationalError = liveness-unknown, not eviction)
# ---------------------------------------------------------------------------


class TestFatalIntegrityLatch:
    """Tier-1 integrity failures fail closed at the drain boundary (elspeth-d0ce4e12af).

    The heartbeat's swallow-and-continue doctrine is scoped to DB CONTENTION
    (liveness-unknown). A Tier-1 error from worker_heartbeat — e.g.
    AuditIntegrityError when the worker's registry row has vanished — is
    corruption, not contention: the thread latches it (never raising on its
    own stack) and check_and_raise() surfaces it at the next drain boundary.
    """

    def test_audit_integrity_error_latches_fatal_and_raises_at_boundary(self) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        repo = _StubRepo()
        repo.side_effect = AuditIntegrityError("worker_heartbeat for unregistered worker_id")

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()  # must not raise on the beat thread's stack

        # Not the busy path: no degraded counting for corruption.
        assert thread._consecutive_busy == 0
        # Not the eviction latch: the follower finalize-departure read-only
        # view (coordination_lost) must be unaffected by the fatal latch.
        assert thread.coordination_lost is False

        with pytest.raises(AuditIntegrityError, match="unregistered worker_id"):
            thread.check_and_raise()

    def test_fatal_latch_persists_across_subsequent_beats(self) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        repo = _StubRepo()
        repo.side_effects = [
            AuditIntegrityError("registry row vanished"),
            _HEALTHY_SNAPSHOT,
        ]

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()  # fatal
        thread._step_beat()  # healthy — must NOT clear the fatal latch

        with pytest.raises(AuditIntegrityError, match="registry row vanished"):
            thread.check_and_raise()

    @pytest.mark.parametrize("first_tier1", [True, False])
    @pytest.mark.parametrize("first_degraded", [True, False])
    @pytest.mark.parametrize("second_degraded", [True, False])
    def test_first_fatal_identity_survives_later_failure(
        self, first_tier1: bool, first_degraded: bool, second_degraded: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        first = AuditIntegrityError("first failure") if first_tier1 else RuntimeError("first failure")
        later = RuntimeError("later failure") if first_tier1 else AuditIntegrityError("later failure")
        repo = _StubRepo()
        if first_degraded:
            repo.side_effect = _busy_error()
            repo.degraded_exception = first
        else:
            repo.side_effect = first
        thread = _make_thread(repo, degraded_threshold=1)
        thread._step_beat()

        if second_degraded:
            repo.side_effect = _busy_error()
            repo.degraded_exception = later
        else:
            repo.side_effect = later
        thread._step_beat()

        with pytest.raises(type(first)) as raised:
            thread.check_and_raise()
        assert raised.value is first
        assert any(record.exc_info is not None and record.exc_info[1] is later for record in caplog.records)

    def test_unexpected_exception_is_latched_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Programming errors reach the drain without killing the beat thread."""
        import logging

        repo = _StubRepo()
        repo.side_effect = RuntimeError("repository contract regression")

        thread = _make_thread(repo, degraded_threshold=100)
        with caplog.at_level(logging.DEBUG, logger="elspeth.engine.orchestrator.heartbeat"):
            thread._step_beat()

        assert not thread._coordination_lost_event.is_set()
        with pytest.raises(RuntimeError, match="repository contract regression") as raised:
            thread.check_and_raise()
        assert raised.value is repo.side_effect
        assert thread._consecutive_busy == 0
        unexpected = [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]
        assert unexpected, "unexpected exceptions must be logged at WARNING+ with traceback"


class TestBusyTolerated:
    def test_deadline_guard_rejection_degrades_then_recovers_on_next_tick(self) -> None:
        repo = _StubRepo()
        repo.side_effects = [LeaseDeadlineExpiredError("heartbeat reserve exhausted"), _HEALTHY_SNAPSHOT]
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()

        assert thread._consecutive_busy == 1
        assert len(repo.record_heartbeat_degraded_calls) == 1
        assert not thread._coordination_lost_event.is_set()
        assert not thread._fatal_event.is_set()
        thread.check_and_raise()

        thread._step_beat()

        assert len(repo.worker_heartbeat_calls) == 2
        assert thread._consecutive_busy == 0
        assert not thread._coordination_lost_event.is_set()
        assert not thread._fatal_event.is_set()
        thread.check_and_raise()

    def test_unrelated_timeout_still_fails_closed(self) -> None:
        failure = TimeoutError("unexpected owned operation timeout")
        repo = _StubRepo()
        repo.side_effect = failure
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()

        assert thread._consecutive_busy == 0
        assert repo.record_heartbeat_degraded_calls == []
        with pytest.raises(TimeoutError) as raised:
            thread.check_and_raise()
        assert raised.value is failure

    def test_operational_error_does_not_set_latch(self) -> None:
        """SQLITE_BUSY does NOT set the coordination-lost latch."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()

        assert not thread._coordination_lost_event.is_set()
        thread.check_and_raise()  # must not raise

    @pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
    def test_postgresql_lock_timeout_records_degradation(self, driver: str) -> None:
        postgres = pytest.importorskip(driver)
        failure = OperationalError(
            "SELECT run_coordination FOR UPDATE",
            None,
            postgres.errors.LockNotAvailable(
                'canceling statement due to lock timeout\nCONTEXT:  while locking tuple (0,1) in relation "run_coordination"'
            ),
        )
        repo = _StubRepo()
        repo.side_effect = failure
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()

        assert thread._consecutive_busy == 1
        assert len(repo.record_heartbeat_degraded_calls) == 1
        assert not thread.coordination_lost
        thread.check_and_raise()

    @pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
    @pytest.mark.parametrize("reason", ["statement timeout", "user request"])
    def test_postgresql_non_lock_cancellation_remains_fatal(self, driver: str, reason: str) -> None:
        postgres = pytest.importorskip(driver)
        failure = OperationalError(
            "SELECT run_coordination FOR UPDATE", None, postgres.errors.QueryCanceled(f"canceling statement due to {reason}")
        )
        repo = _StubRepo()
        repo.side_effect = failure
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()

        assert thread._consecutive_busy == 0
        assert repo.record_heartbeat_degraded_calls == []
        with pytest.raises(OperationalError) as raised:
            thread.check_and_raise()
        assert raised.value is failure

    @pytest.mark.parametrize(
        "driver_message",
        ["unable to open database file", "disk I/O error", "attempt to write a readonly database"],
    )
    def test_non_contention_operational_error_latches_fatal(self, driver_message: str, caplog: pytest.LogCaptureFixture) -> None:
        """Only write-lock contention is liveness-unknown; other DB faults fail closed.

        SQLITE_BUSY says nothing about this worker, which is what licenses the
        swallow-and-continue arm. An audit store that cannot be opened, read or
        written is a different fact: counting it as a busy tick would let the
        run keep traversing while its beat never lands, so it is latched for
        ``check_and_raise`` and the busy counter never advances.
        """
        repo = _StubRepo()
        failure = OperationalError("UPDATE run_workers", None, sqlite3.OperationalError(driver_message))
        repo.side_effect = failure

        thread = _make_thread(repo, degraded_threshold=1)
        thread._step_beat()

        assert thread._consecutive_busy == 0
        assert len(repo.record_heartbeat_degraded_calls) == 0
        assert not thread.coordination_lost
        with pytest.raises(OperationalError) as raised:
            thread.check_and_raise()
        assert raised.value is failure
        assert any("non-contention operational failure" in record.getMessage() and record.exc_info for record in caplog.records)

    def test_operational_error_without_a_dbapi_cause_is_not_contention(self) -> None:
        """No DBAPI exception means no evidence of contention: fail closed.

        ``str(exc)`` on a SQLAlchemy error appends ``[SQL: <statement>]``, so
        reading contention off the rendered string would let a statement that
        merely mentions a lock pass as SQLITE_BUSY. The predicate reads
        ``exc.orig``; with nothing there the unknown case must latch, not
        continue.
        """
        repo = _StubRepo()
        failure = OperationalError("database is locked", None, None)
        repo.side_effect = failure

        thread = _make_thread(repo, degraded_threshold=1)
        thread._step_beat()

        assert thread._consecutive_busy == 0
        with pytest.raises(OperationalError) as raised:
            thread.check_and_raise()
        assert raised.value is failure

    def test_any_exception_does_not_set_latch(self) -> None:
        """Any DB error is treated as liveness-unknown, never eviction."""
        repo = _StubRepo()
        repo.side_effect = RuntimeError("unexpected DB error")

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()

        assert not thread._coordination_lost_event.is_set()

    def test_thread_continues_after_busy_failure(self) -> None:
        """After a busy tick the thread can recover with a healthy beat."""
        repo = _StubRepo()
        repo.side_effects = [
            _busy_error(),
            _HEALTHY_SNAPSHOT,
        ]

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()  # busy
        thread._step_beat()  # healthy

        assert not thread._coordination_lost_event.is_set()
        assert thread._consecutive_busy == 0  # reset after healthy beat

    def test_busy_counter_resets_on_success(self) -> None:
        """_consecutive_busy resets to 0 after a successful beat."""
        repo = _StubRepo()
        repo.side_effects = [
            _busy_error(),
            _busy_error(),
            _HEALTHY_SNAPSHOT,
        ]

        thread = _make_thread(repo, degraded_threshold=100)
        thread._step_beat()
        assert thread._consecutive_busy == 1
        thread._step_beat()
        assert thread._consecutive_busy == 2
        thread._step_beat()
        assert thread._consecutive_busy == 0


# ---------------------------------------------------------------------------
# 3. heartbeat_degraded fires past threshold
# ---------------------------------------------------------------------------


class TestHeartbeatDegraded:
    def test_late_registered_database_integrity_failure_is_not_diagnostic_loss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from elspeth.contracts import tier_registry

        repo = _StubRepo()
        repo.side_effect = _busy_error()
        thread = _make_thread(repo, degraded_threshold=1)
        with monkeypatch.context() as isolated:
            isolated.setattr(tier_registry, "_REGISTRY", list(tier_registry._REGISTRY))
            isolated.setattr(tier_registry, "_REASONS", dict(tier_registry._REASONS))
            isolated.setattr(tier_registry, "_FROZEN", False)

            @tier_registry.tier_1_error(reason="test late database integrity registration", caller_module=__name__)
            class DatabaseIntegrityFailure(SQLAlchemyError):
                pass

            failure = DatabaseIntegrityFailure("corrupt diagnostic contract")
            repo.degraded_exception = failure
            thread._step_beat()

            with pytest.raises(DatabaseIntegrityFailure) as raised:
                thread.check_and_raise()
            assert raised.value is failure

    @pytest.mark.parametrize("failure", [RuntimeError("broken writer"), pytest.param(None, id="tier1")])
    def test_degraded_failure_is_latched_at_drain_boundary(self, failure: Exception | None) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        error = AuditIntegrityError("broken ledger") if failure is None else failure
        repo = _StubRepo()
        repo.side_effect = _busy_error()
        repo.degraded_exception = error
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()
        with pytest.raises(type(error)) as raised:
            thread.check_and_raise()
        assert raised.value is error
        assert not thread.coordination_lost

    def test_degraded_fires_at_threshold(self) -> None:
        """record_heartbeat_degraded is called when busy_count reaches k=3."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()

        thread = _make_thread(repo, degraded_threshold=3)

        thread._step_beat()  # busy=1 — not yet
        assert len(repo.record_heartbeat_degraded_calls) == 0

        thread._step_beat()  # busy=2 — not yet
        assert len(repo.record_heartbeat_degraded_calls) == 0

        thread._step_beat()  # busy=3 — fires NOW
        assert len(repo.record_heartbeat_degraded_calls) == 1
        call_kwargs = repo.record_heartbeat_degraded_calls[0]
        assert call_kwargs["member_token"] is _TOKEN
        assert call_kwargs["failures"] == 3
        assert isinstance(call_kwargs["now"], datetime)

    def test_degraded_fires_again_on_subsequent_busy_beats(self) -> None:
        """Each beat past threshold keeps firing (failures grows monotonically)."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()

        thread = _make_thread(repo, degraded_threshold=2)
        thread._step_beat()  # busy=1
        thread._step_beat()  # busy=2 — first fire
        thread._step_beat()  # busy=3 — fires again with failures=3

        assert len(repo.record_heartbeat_degraded_calls) == 2
        last_call = repo.record_heartbeat_degraded_calls[-1]
        assert last_call["failures"] == 3

    def test_degraded_not_fired_below_threshold(self) -> None:
        """record_heartbeat_degraded is NOT called below the threshold."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()

        thread = _make_thread(repo, degraded_threshold=5)
        for _ in range(4):
            thread._step_beat()

        assert len(repo.record_heartbeat_degraded_calls) == 0

    def test_degraded_event_error_does_not_propagate(self) -> None:
        """record_heartbeat_degraded raising does NOT crash the thread."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()
        repo.degraded_exception = RuntimeError("degraded write failed")

        thread = _make_thread(repo, degraded_threshold=1)
        thread._step_beat()  # fires, degraded raises — must NOT propagate

        assert not thread._coordination_lost_event.is_set()

    def test_degraded_db_failure_is_latched_as_a_broken_repository_contract(self, caplog: pytest.LogCaptureFixture) -> None:
        """``record_heartbeat_degraded`` never raises a DB error by contract.

        Its repository implementation catches every ``SQLAlchemyError`` and
        reports the declared ``LOST_TO_DB_FAULT`` result, so a DB error
        arriving here is a breach of an owned contract, not audit-store
        unavailability. It is latched for the drain boundary rather than
        absorbed — the thread itself still does not raise.
        """
        repo = _StubRepo()
        repo.side_effect = _busy_error()
        failure = OperationalError("INSERT INTO run_coordination_events", None, sqlite3.OperationalError("disk I/O error"))
        repo.degraded_exception = failure
        thread = _make_thread(repo, degraded_threshold=1)

        thread._step_beat()

        assert not thread.coordination_lost
        with pytest.raises(OperationalError) as raised:
            thread.check_and_raise()
        assert raised.value is failure
        assert any(record.getMessage() == "run_heartbeat: degraded event invariant failed" and record.exc_info for record in caplog.records)

    def test_degraded_correct_worker_id_and_run_id(self) -> None:
        """Degraded event carries the thread's own worker_id and run_id."""
        repo = _StubRepo()
        repo.side_effect = _busy_error()

        thread = _make_thread(repo, degraded_threshold=1)
        thread._step_beat()

        call_kwargs = repo.record_heartbeat_degraded_calls[0]
        assert call_kwargs["member_token"] is _TOKEN


# ---------------------------------------------------------------------------
# 4. Fatal latch: seat taken by foreign leader
# ---------------------------------------------------------------------------


class TestFatalLatchForeignLeader:
    def test_deposed_sets_latch(self) -> None:
        """Snapshot with foreign leader_worker_id latches coordination_lost."""
        repo = _StubRepo()
        repo.snapshot = _DEPOSED_SNAPSHOT

        thread = _make_thread(repo)
        thread._step_beat()

        assert thread._coordination_lost_event.is_set()

    def test_check_and_raise_raises_after_deposition(self) -> None:
        """check_and_raise() raises RunWorkerEvictedError when deposed."""
        repo = _StubRepo()
        repo.snapshot = _DEPOSED_SNAPSHOT

        thread = _make_thread(repo)
        thread._step_beat()

        with pytest.raises(RunWorkerEvictedError) as exc_info:
            thread.check_and_raise()

        assert exc_info.value.worker_id == _WORKER_ID
        assert exc_info.value.run_id == _RUN_ID

    def test_healthy_then_deposed_sets_latch(self) -> None:
        """A previously healthy thread latches when it later observes deposition."""
        repo = _StubRepo()
        repo.side_effects = [_HEALTHY_SNAPSHOT, _DEPOSED_SNAPSHOT]

        thread = _make_thread(repo)
        thread._step_beat()
        assert not thread._coordination_lost_event.is_set()

        thread._step_beat()
        assert thread._coordination_lost_event.is_set()

    def test_none_leader_does_not_set_latch(self) -> None:
        """A vacant seat (leader_worker_id=None) is not treated as foreign-leader."""
        vacant_snapshot = CoordinationSnapshot(
            leader_worker_id=None,
            leader_epoch=0,
            seat_live=False,
            worker_active=True,
        )
        repo = _StubRepo()
        repo.snapshot = vacant_snapshot

        thread = _make_thread(repo)
        thread._step_beat()

        # A vacant seat does NOT trigger the deposition latch — only a FOREIGN
        # non-None leader does. This is important for N=1: between release_seat
        # and the final join, the seat may be vacant.
        assert not thread._coordination_lost_event.is_set()


# ---------------------------------------------------------------------------
# 5. Fatal latch: membership lost (evicted or departed)
# ---------------------------------------------------------------------------


class TestFatalLatchEvicted:
    def test_worker_inactive_sets_latch(self) -> None:
        """WorkerMembershipLost latches coordination_lost without snapshot fields."""
        repo = _StubRepo()
        repo.snapshot = _EVICTED_OUTCOME

        thread = _make_thread(repo)
        thread._step_beat()

        assert thread._coordination_lost_event.is_set()

    def test_check_and_raise_raises_after_eviction(self) -> None:
        """check_and_raise() raises RunWorkerEvictedError when evicted."""
        repo = _StubRepo()
        repo.snapshot = _EVICTED_OUTCOME

        thread = _make_thread(repo)
        thread._step_beat()

        with pytest.raises(RunWorkerEvictedError):
            thread.check_and_raise()

    def test_check_and_raise_quiet_before_eviction(self) -> None:
        """check_and_raise() is a no-op before the latch is set."""
        thread = _make_thread(_StubRepo())  # no beats driven
        thread.check_and_raise()  # must not raise


# ---------------------------------------------------------------------------
# 6. Clean start/join lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_background_logger_failure_is_delivered_to_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.engine.orchestrator import heartbeat

        failure = RuntimeError("logging backend failed")

        def fail_log(*args: object, **kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(heartbeat.logger, "debug", fail_log)
        repo = _StubRepo()
        repo.side_effect = _busy_error()
        thread = RunHeartbeatThread(repo, member_token=_TOKEN)
        thread.start()
        thread.stop()

        assert not thread._thread.is_alive()
        with pytest.raises(RuntimeError) as raised:
            thread.raise_fatal_failure()
        assert raised.value is failure

    @pytest.mark.parametrize("degraded", [False, True])
    def test_final_beat_fatal_failure_remains_available_after_join(self, degraded: bool) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        failure = AuditIntegrityError("final beat corruption")
        repo = _StubRepo()
        repo.side_effect = _busy_error() if degraded else failure
        repo.degraded_exception = failure if degraded else None
        thread = RunHeartbeatThread(repo, member_token=_TOKEN, degraded_threshold=1)
        thread.start()
        thread.stop()

        assert not thread._thread.is_alive()
        with pytest.raises(AuditIntegrityError) as raised:
            thread.raise_fatal_failure()
        assert raised.value is failure

    def test_final_membership_departure_is_not_a_fatal_thread_failure(self) -> None:
        repo = _StubRepo()
        repo.snapshot = _EVICTED_OUTCOME
        thread = RunHeartbeatThread(repo, member_token=_TOKEN)
        thread.start()
        thread.stop()

        assert thread.coordination_lost
        thread.raise_fatal_failure()

    def test_start_and_stop_no_leak(self) -> None:
        """start() + stop() without any beats completes without thread leak."""
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        # Use a real wait_fn that blocks until stop_event is set — this is
        # the production shape; stop() signals the event, the thread exits.
        thread_obj = RunHeartbeatThread(
            repo,
            member_token=_TOKEN,
            wait_fn=None,  # default: stop_event.wait
        )
        thread_obj.start()
        thread_obj.stop()

        assert not thread_obj._thread.is_alive()

    def test_stop_is_idempotent(self) -> None:
        """stop() called twice does not deadlock or raise."""
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        thread_obj = RunHeartbeatThread(
            repo,
            member_token=_TOKEN,
            wait_fn=None,
        )
        thread_obj.start()
        thread_obj.stop()
        thread_obj.stop()  # second call must be a no-op

        assert not thread_obj._thread.is_alive()

    def test_stop_can_skip_final_beat_for_known_terminal_exit(self) -> None:
        """A caller that observed terminal state can stop without re-beating a departed row."""
        repo = _StubRepo()
        repo.snapshot = _EVICTED_OUTCOME
        thread_obj = RunHeartbeatThread(
            repo,
            member_token=_TOKEN,
            wait_fn=None,
        )
        thread_obj.start()

        try:
            thread_obj.stop(final_beat=False)
        finally:
            if thread_obj._thread.is_alive():
                thread_obj.stop()

        assert repo.worker_heartbeat_calls == []
        assert not thread_obj.coordination_lost

    def test_thread_is_daemon(self) -> None:
        """The heartbeat thread is a daemon so it does not block process exit."""
        thread_obj = RunHeartbeatThread(
            _StubRepo(),
            member_token=_TOKEN,
        )
        assert thread_obj._thread.daemon is True

    def test_exception_during_run_does_not_prevent_stop(self) -> None:
        """An exception path in core.py (exception raised before release_seat)
        still stops the thread via the finally safety-net stop()."""
        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        thread_obj = RunHeartbeatThread(
            repo,
            member_token=_TOKEN,
            wait_fn=None,
        )
        thread_obj.start()
        # Simulate the finally safety-net: stop() called unconditionally.
        thread_obj.stop()

        assert not thread_obj._thread.is_alive()

    def test_step_beat_on_background_thread_concurrent_check(self) -> None:
        """check_and_raise() is safe to call from the drain thread while the
        beat thread runs.  The latch is a threading.Event (atomic set/is_set)."""
        results: list[bool] = []

        repo = _StubRepo()
        repo.snapshot = _HEALTHY_SNAPSHOT

        thread_obj = RunHeartbeatThread(
            repo,
            member_token=_TOKEN,
            wait_fn=None,
        )
        thread_obj.start()

        def drain_check() -> None:
            for _ in range(20):
                try:
                    thread_obj.check_and_raise()
                    results.append(True)
                except RunWorkerEvictedError:
                    results.append(False)

        drain = threading.Thread(target=drain_check)
        drain.start()
        drain.join(timeout=5)
        thread_obj.stop()

        assert drain.is_alive() is False, "drain check hung"
        # All checks should be True (healthy thread, no eviction)
        assert all(results), f"unexpected eviction in concurrent check: {results}"


# ---------------------------------------------------------------------------
# 7. Follower role: deposed-latch is role-gated (ADR-030 §B, slice 5)
# ---------------------------------------------------------------------------


class TestFollowerHeartbeatRoleGating:
    """A follower seeing a foreign leader_worker_id must NOT latch.

    Design §B.2: trigger evaluation is leader-only.  A follower's
    leader_worker_id is always a different process's worker_id — this is
    the NORMAL, HEALTHY case.  The deposed-latch must only fire for leaders.
    """

    def test_follower_foreign_leader_does_not_latch(self) -> None:
        """worker_role='follower' + foreign leader_worker_id → no latch."""
        follower_worker_id = f"worker:{_RUN_ID}:follower-abc"
        follower_token = WorkerMembershipToken(run_id=_RUN_ID, worker_id=follower_worker_id)
        repo = _StubRepo()
        # Snapshot: our row is active (worker_active=True), but leader is a
        # DIFFERENT process — normal for a follower.
        repo.snapshot = CoordinationSnapshot(
            leader_worker_id="worker:some-run:the-leader",
            leader_epoch=1,
            seat_live=True,
            worker_active=True,
            worker_role="follower",  # this worker is a follower
        )

        thread = RunHeartbeatThread(
            repo,
            member_token=follower_token,
            heartbeat_seconds=DEFAULT_RUN_HEARTBEAT_SECONDS,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            wait_fn=lambda _: False,
        )
        thread._step_beat()

        # MUST NOT latch — a follower seeing a live foreign leader is healthy.
        assert not thread._coordination_lost_event.is_set()
        thread.check_and_raise()  # must not raise

    def test_follower_eviction_does_latch(self) -> None:
        """Membership loss latches a follower without reading any role field."""
        follower_worker_id = f"worker:{_RUN_ID}:follower-xyz"
        follower_token = WorkerMembershipToken(run_id=_RUN_ID, worker_id=follower_worker_id)
        repo = _StubRepo()
        repo.snapshot = WorkerMembershipLost(member_token=follower_token)

        thread = RunHeartbeatThread(
            repo,
            member_token=follower_token,
            heartbeat_seconds=DEFAULT_RUN_HEARTBEAT_SECONDS,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            wait_fn=lambda _: False,
        )
        thread._step_beat()

        # MUST latch — eviction applies to followers too.
        assert thread._coordination_lost_event.is_set()
        with pytest.raises(RunWorkerEvictedError):
            thread.check_and_raise()

    def test_leader_foreign_leader_still_latches(self) -> None:
        """worker_role='leader' (default) + foreign leader_worker_id → latch set."""
        # This is the pre-existing deposed-leader case; must still work.
        repo = _StubRepo()
        repo.snapshot = _DEPOSED_SNAPSHOT  # worker_role defaults to "leader"

        thread = _make_thread(repo)
        thread._step_beat()

        assert thread._coordination_lost_event.is_set()


# ---------------------------------------------------------------------------
# Constant import guard
# ---------------------------------------------------------------------------


def test_default_heartbeat_constant_imported() -> None:
    """DEFAULT_RUN_HEARTBEAT_SECONDS is importable from contracts.coordination."""
    from elspeth.contracts.coordination import DEFAULT_RUN_HEARTBEAT_SECONDS as C

    assert C == 15.0


def test_heartbeat_and_window_constants_satisfy_sizing_rule() -> None:
    """window >= 4 * (beat + busy_timeout) by the §A.3 sizing rule."""
    busy_timeout_seconds = 5.0
    assert 4 * (DEFAULT_RUN_HEARTBEAT_SECONDS + busy_timeout_seconds) <= DEFAULT_RUN_LIVENESS_WINDOW_SECONDS


def test_stop_timeout_reports_live_thread_and_skips_later_final_beat() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockedRepo(_StubRepo):
        def worker_heartbeat(
            self, *, member_token: WorkerMembershipToken, window_seconds: float
        ) -> CoordinationSnapshot | WorkerMembershipLost:
            entered.set()
            assert release.wait(5), "test failed to release blocked heartbeat"
            return super().worker_heartbeat(member_token=member_token, window_seconds=window_seconds)

    repo = BlockedRepo()
    repo.snapshot = _HEALTHY_SNAPSHOT
    thread = RunHeartbeatThread(repo, member_token=_TOKEN, heartbeat_seconds=0.001, stop_timeout_seconds=0.05)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match=r"heartbeat.*did not stop"):
            thread.stop()
    finally:
        release.set()
        thread._thread.join(2)
    thread.stop()
    assert len(repo.worker_heartbeat_calls) == 1


def test_stop_before_start_is_safe() -> None:
    thread = _make_thread(_StubRepo())
    thread.stop()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_stop_timeout_requires_finite_positive_budget(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RunHeartbeatThread(_StubRepo(), member_token=_TOKEN, stop_timeout_seconds=timeout)
