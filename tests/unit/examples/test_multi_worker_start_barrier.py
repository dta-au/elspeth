"""Socket-free checks for the example start barrier."""

import fcntl
import json
import sqlite3
from pathlib import Path

import pytest
from examples.multi_worker.start_barrier import BarrierFailure, StartBarrier, _ExamplePlugins, _live_members, validate_worker_count

from elspeth.plugins.infrastructure.manager import PluginManager


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def manifest(directory: Path, *pids: int) -> None:
    (directory / "workers.json").write_text(json.dumps(list(pids)))


def test_all_arrivals_release_with_active_leases(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    calls = 0

    def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert not (tmp_path / "released.json").exists()
            (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
        clock.sleep(seconds)

    barrier = StartBarrier(tmp_path, 2, clock.monotonic, sleep, lambda pid: True, lambda run_id: {"l": "leader", "f": "follower"})
    barrier.arrive("r", "l", "leader", 10)
    assert (tmp_path / "released.json").exists()
    before = clock.now
    barrier.arrive("r", "l", "leader", 10)
    assert clock.now == before


def test_membership_alone_does_not_count_as_arrival(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    barrier = StartBarrier(
        tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader", "f": "follower"}, budget_seconds=1
    )
    with pytest.raises(BarrierFailure, match="start barrier: worker missing after 1s"):
        barrier.arrive("r", "l", "leader", 10)
    assert not (tmp_path / "released.json").exists()


def test_deadline_during_membership_check_cannot_release(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))

    def members(run_id: str) -> dict[str, str]:
        clock.now = 1.01
        return {"l": "leader", "f": "follower"}

    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, members, budget_seconds=1)
    with pytest.raises(BarrierFailure, match=r"start barrier: .*deadline"):
        barrier.arrive("r", "l", "leader", 10)
    assert not (tmp_path / "released.json").exists()


def test_deadline_during_release_staging_cannot_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
    original_write_text = Path.write_text

    def slow_release_write(path: Path, data: str) -> int:
        count = original_write_text(path, data)
        if path.name.startswith(".released.json."):
            clock.now = 1.01
        return count

    monkeypatch.setattr(Path, "write_text", slow_release_write)
    barrier = StartBarrier(
        tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader", "f": "follower"}, budget_seconds=1
    )
    with pytest.raises(BarrierFailure, match="start barrier: deadline reached after 1s"):
        barrier.arrive("r", "l", "leader", 10)
    assert not (tmp_path / "released.json").exists()


def test_held_decision_lock_fails_at_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
    original_flock = fcntl.flock

    def held_lock(fd: int, operation: int) -> None:
        assert operation & fcntl.LOCK_NB
        original_flock(fd, operation)

    with (tmp_path / "decision.lock").open("a+") as held:
        original_flock(held.fileno(), fcntl.LOCK_EX)
        monkeypatch.setattr(fcntl, "flock", held_lock)
        barrier = StartBarrier(
            tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader", "f": "follower"}, budget_seconds=1
        )
        with pytest.raises(BarrierFailure, match="start barrier: decision lock unavailable at deadline"):
            barrier.arrive("r", "l", "leader", 10)
    assert clock.now == 1
    assert not (tmp_path / "released.json").exists()


def test_lease_lost_while_waiting_for_lock_cannot_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
    original_flock = fcntl.flock
    attempts = 0

    def contended_lock(fd: int, operation: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BlockingIOError
        original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", contended_lock)
    checks = 0

    def members(run_id: str) -> dict[str, str]:
        nonlocal checks
        checks += 1
        return {"l": "leader", "f": "follower"} if checks == 1 else {"l": "leader"}

    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, members, budget_seconds=1)
    with pytest.raises(BarrierFailure, match="start barrier: active membership or live lease missing"):
        barrier.arrive("r", "l", "leader", 10)
    assert checks >= 2
    assert not (tmp_path / "released.json").exists()


def test_missing_worker_fails_at_budget(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader"}, budget_seconds=2)
    with pytest.raises(BarrierFailure, match="start barrier: worker missing after 2s"):
        barrier.arrive("r", "l", "leader", 10)
    assert clock.now == 2
    assert not (tmp_path / "released.json").exists()


def test_wait_stops_before_item_lease_expires(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader"})
    with pytest.raises(BarrierFailure, match="start barrier: item lease safety deadline reached"):
        barrier.arrive("r", "l", "leader", 10, lease_remaining_seconds=15)
    assert clock.now == 5
    assert not (tmp_path / "released.json").exists()


def test_exited_worker_fails_before_budget(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: pid != 20, lambda run_id: {"l": "leader"})
    with pytest.raises(BarrierFailure, match="start barrier: worker process 20 exited before arrival"):
        barrier.arrive("r", "l", "leader", 10)
    assert clock.now == 0


def test_exited_arrival_cannot_release(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
    barrier = StartBarrier(
        tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: pid != 20, lambda run_id: {"l": "leader", "f": "follower"}
    )
    with pytest.raises(BarrierFailure, match="start barrier: worker process 20 exited before release"):
        barrier.arrive("r", "l", "leader", 10)
    assert not (tmp_path / "released.json").exists()


def test_arrival_without_live_lease_cannot_release(tmp_path: Path) -> None:
    clock = Clock()
    manifest(tmp_path, 10, 20)
    (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
    barrier = StartBarrier(tmp_path, 2, clock.monotonic, clock.sleep, lambda pid: True, lambda run_id: {"l": "leader"}, budget_seconds=1)
    with pytest.raises(BarrierFailure, match="start barrier: active membership or live lease missing after 1s"):
        barrier.arrive("r", "l", "leader", 10)
    assert not (tmp_path / "released.json").exists()


def test_impossible_worker_count_is_refused() -> None:
    with pytest.raises(BarrierFailure, match="start barrier: 21 workers cannot share 20 first-fan-out rows"):
        validate_worker_count(20, 20)
    assert validate_worker_count(20, 19) == 20


def test_membership_query_requires_exact_run_active_worker_and_live_lease(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """CREATE TABLE run_workers (worker_id TEXT, run_id TEXT, role TEXT, status TEXT, heartbeat_expires_at TEXT);
            CREATE TABLE token_work_items (run_id TEXT, lease_owner TEXT, status TEXT, lease_expires_at TEXT);
            INSERT INTO run_workers VALUES ('leader','r','leader','active',datetime('now','+5 minutes'));
            INSERT INTO run_workers VALUES ('follower','r','follower','active',datetime('now','+5 minutes'));
            INSERT INTO run_workers VALUES ('departed','r','follower','departed',datetime('now','+5 minutes'));
            INSERT INTO token_work_items VALUES ('r','leader','leased',datetime('now','+5 minutes'));
            INSERT INTO token_work_items VALUES ('other','follower','leased',datetime('now','+5 minutes'));
            INSERT INTO token_work_items VALUES ('r','departed','leased',datetime('now','+5 minutes'));
            """
        )
    assert _live_members(db, "r") == {"leader": "leader"}


def test_example_plugin_registers_and_constructs_without_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_DEMO_BARRIER_DIR", raising=False)
    monkeypatch.delenv("ELSPETH_DEMO_BARRIER_WORKERS", raising=False)
    monkeypatch.delenv("ELSPETH_DEMO_BARRIER_DB", raising=False)
    manager = PluginManager()
    manager.register(_ExamplePlugins())
    transform = manager.create_transform("demonstration_start_barrier", {"schema": {"mode": "observed"}})
    assert transform.name == "demonstration_start_barrier"
