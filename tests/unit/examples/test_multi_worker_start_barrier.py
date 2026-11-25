"""Socket-free checks for the example start barrier."""

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
            (tmp_path / "20.json").write_text(json.dumps({"pid": 20, "run_id": "r", "worker_id": "f", "role": "follower"}))
        clock.sleep(seconds)

    barrier = StartBarrier(tmp_path, 2, clock.monotonic, sleep, lambda pid: True, lambda run_id: {"l": "leader", "f": "follower"})
    barrier.arrive("r", "l", "leader", 10)
    assert (tmp_path / "released.json").exists()
    before = clock.now
    barrier.arrive("r", "l", "leader", 10)
    assert clock.now == before


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


def test_example_plugin_registers_and_constructs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_DEMO_BARRIER_DIR", str(tmp_path))
    monkeypatch.setenv("ELSPETH_DEMO_BARRIER_WORKERS", "2")
    monkeypatch.setenv("ELSPETH_DEMO_BARRIER_DB", str(tmp_path / "audit.db"))
    manager = PluginManager()
    manager.register(_ExamplePlugins())
    transform = manager.create_transform("demonstration_start_barrier", {"schema": {"mode": "observed"}})
    assert transform.name == "demonstration_start_barrier"
