"""Unit tests for Orchestrator.join_run() admission (ADR-030 §B.1, slice 5).

Pins contract (b) from the §H test matrix:
  test_join_attaches_follower_to_running_run — happy path: worker_id returned,
  run_workers row role=follower/status=active, worker_register event in ledger.

Plus the four refusal arms and atomicity guarantee:

- terminal run (COMPLETED) → JoinRefusedError("terminal")
- config_hash mismatch → JoinRefusedError("does not match")
- vacant seat / expired leader → JoinRefusedError("no live leader")
- filesystem preflight permission failure → JoinRefusedError(run_id, path)
- atomicity: the admission is ONE BEGIN IMMEDIATE transaction — leader +
  follower registered OR nothing (no ghost follower row on refusal)

Seat liveness is judged against the Landscape database clock (ADR-047):
``join_run`` carries no clock, the live seat is minted from database time and
the expired-seat arms lapse it through the database. Real file-backed SQLite
for the permission-failure test (in-memory has no filesystem).  The filesystem
preflight test uses ``unittest.mock.patch`` on ``os.access`` — no real chmod,
no process re-exec.
"""

from __future__ import annotations

import os
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.coordination import mint_worker_id
from elspeth.contracts.errors import JoinRefusedError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.schema import (
    run_coordination_events_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
)
from elspeth.engine.orchestrator.core import Orchestrator
from tests.fixtures.landscape import expire_leader_seat, make_landscape_db, on_fresh_database_second

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUN_ID = "run-join-test-1"
NOW = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)  # forensic seed stamps only
WINDOW = 80.0

# Sentinel config_hash used for all tests that need a matching hash.
# Raw insert bypasses begin_run so the seat image is fully controlled.
_SENTINEL_HASH = "sentinel-config-hash-abc123"

# Patches for resolve_config+stable_hash so joiner_config_hash == _SENTINEL_HASH.
_PATCH_RESOLVE = patch("elspeth.engine.orchestrator.join_admission.resolve_config", return_value={})
_PATCH_HASH_SENTINEL = patch("elspeth.engine.orchestrator.join_admission.stable_hash", return_value=_SENTINEL_HASH)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _orchestrator(db: LandscapeDB) -> Orchestrator:
    return Orchestrator(db)


def _begin_run_with_leader(db: LandscapeDB, *, run_id: str = RUN_ID, config_hash: str = _SENTINEL_HASH) -> str:
    """Seed a RUNNING run via raw INSERT whose seat is live for WINDOW seconds of database time.

    Returns the minted leader worker_id.  Uses raw INSERT (not begin_run) so
    the seat image is fully controlled; the deadline is minted from the
    Landscape database clock because that is what admission compares against.
    """
    leader_id = mint_worker_id(run_id)
    with db.write_connection() as conn:
        live_until = read_landscape_transaction_time(conn) + timedelta(seconds=WINDOW)
        conn.execute(
            insert(runs_table).values(
                run_id=run_id,
                started_at=NOW,
                config_hash=config_hash,
                settings_json="{}",
                canonical_version="v1",
                status="running",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        conn.execute(
            insert(run_coordination_table).values(
                run_id=run_id,
                leader_worker_id=leader_id,
                leader_epoch=1,
                leader_heartbeat_expires_at=live_until,
                updated_at=NOW,
            )
        )
        conn.execute(
            insert(run_workers_table).values(
                worker_id=leader_id,
                run_id=run_id,
                role="leader",
                status="active",
                registered_at=NOW,
                heartbeat_expires_at=live_until,
            )
        )
    return leader_id


def _worker_rows(db: LandscapeDB, run_id: str = RUN_ID) -> list[dict[str, object]]:
    with db.engine.connect() as conn:
        rows = conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == run_id)).mappings().all()
    return [dict(r) for r in rows]


def _coord_events(db: LandscapeDB, run_id: str = RUN_ID) -> list[dict[str, object]]:
    with db.engine.connect() as conn:
        rows = (
            conn.execute(
                select(run_coordination_events_table)
                .where(run_coordination_events_table.c.run_id == run_id)
                .order_by(run_coordination_events_table.c.seq)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# §H test #2(b): happy path — test_join_attaches_follower_to_running_run
# ---------------------------------------------------------------------------


class TestJoinAdmissionHappyPath:
    """§H contract (b): join_run attaches a follower to a RUNNING run."""

    def test_join_attaches_follower_to_running_run(self) -> None:
        """Happy path: returns worker_id; follower row active; event in ledger."""
        db = make_landscape_db()
        _begin_run_with_leader(db)  # seeds run with _SENTINEL_HASH, seat live for WINDOW s of database time

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL:
            orch = _orchestrator(db)
            worker_id = orch.join_run(run_id=RUN_ID, settings=fake_settings)

        # worker_id has the expected shape
        assert worker_id.startswith(f"worker:{RUN_ID}:")
        assert len(worker_id.rsplit(":", 1)[1]) == 32  # uuid4().hex

        # run_workers row: role=follower, status=active
        workers = _worker_rows(db)
        follower_rows = [w for w in workers if w["worker_id"] == worker_id]
        assert len(follower_rows) == 1
        follower = follower_rows[0]
        assert follower["role"] == "follower"
        assert follower["status"] == "active"
        assert follower["run_id"] == RUN_ID

        # worker_register event in ledger (last event for this run)
        events = _coord_events(db)
        register_events = [e for e in events if e["event_type"] == "worker_register" and e["worker_id"] == worker_id]
        assert len(register_events) == 1
        assert register_events[0]["worker_id"] == worker_id

    def test_returned_worker_id_is_unique_per_call(self) -> None:
        """Single-use identity: two successive joins yield different worker_ids."""
        db = make_landscape_db()
        _begin_run_with_leader(db)

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL:
            orch = _orchestrator(db)
            wid1 = orch.join_run(run_id=RUN_ID, settings=fake_settings)
            wid2 = orch.join_run(run_id=RUN_ID, settings=fake_settings)

        assert wid1 != wid2


# ---------------------------------------------------------------------------
# Refusal arms
# ---------------------------------------------------------------------------


class TestJoinAdmissionRefusals:
    """Four refusal arms: terminal run, config_hash mismatch, dead/vacant seat,
    filesystem permission failure."""

    def test_terminal_run_refused(self) -> None:
        """A COMPLETED run cannot be joined."""
        db = make_landscape_db()
        # Seed a COMPLETED run via raw insert (bypassing begin_run's status constraint).
        with db.write_connection() as conn:
            conn.execute(
                insert(runs_table).values(
                    run_id=RUN_ID,
                    started_at=NOW,
                    config_hash=_SENTINEL_HASH,
                    settings_json="{}",
                    canonical_version="v1",
                    status="completed",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
            conn.execute(
                insert(run_coordination_table).values(
                    run_id=RUN_ID,
                    leader_worker_id=None,
                    leader_epoch=1,
                    leader_heartbeat_expires_at=None,
                    updated_at=NOW,
                )
            )

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL, pytest.raises(JoinRefusedError) as exc_info:
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        assert exc_info.value.run_id == RUN_ID
        assert "terminal" in str(exc_info.value).lower()

    def test_failed_run_refused_with_resume_hint(self) -> None:
        """A FAILED run tells the operator to use elspeth resume."""
        db = make_landscape_db()
        with db.write_connection() as conn:
            conn.execute(
                insert(runs_table).values(
                    run_id=RUN_ID,
                    started_at=NOW,
                    config_hash=_SENTINEL_HASH,
                    settings_json="{}",
                    canonical_version="v1",
                    status="failed",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
            conn.execute(
                insert(run_coordination_table).values(
                    run_id=RUN_ID,
                    leader_worker_id=None,
                    leader_epoch=1,
                    leader_heartbeat_expires_at=None,
                    updated_at=NOW,
                )
            )

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL, pytest.raises(JoinRefusedError) as exc_info:
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        # Should mention resume (not terminal)
        assert "resume" in str(exc_info.value).lower()

    def test_config_hash_mismatch_refused(self) -> None:
        """A joiner with a different settings hash is refused."""
        db = make_landscape_db()
        _begin_run_with_leader(db, config_hash="stored-hash-abc")

        fake_settings = types.SimpleNamespace()
        with (
            patch("elspeth.engine.orchestrator.join_admission.resolve_config", return_value={}),
            patch("elspeth.engine.orchestrator.join_admission.stable_hash", return_value="different-hash-xyz"),
            pytest.raises(JoinRefusedError) as exc_info,
        ):
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        err = exc_info.value
        assert err.run_id == RUN_ID
        assert "does not match" in str(err)

        # Zero mutation: no follower row was inserted
        workers = _worker_rows(db)
        assert all(w["role"] == "leader" for w in workers)

    def test_expired_seat_refused_with_resume_hint(self) -> None:
        """No live leader — expired seat — tells operator to use resume."""
        db = make_landscape_db()
        _begin_run_with_leader(db)
        expire_leader_seat(db, RUN_ID)  # the seat lapsed in the database clock's past

        fake_settings = types.SimpleNamespace()
        with (
            _PATCH_RESOLVE,
            _PATCH_HASH_SENTINEL,
            pytest.raises(JoinRefusedError) as exc_info,
        ):
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        err = exc_info.value
        assert err.run_id == RUN_ID
        assert "no live leader" in str(err).lower()
        assert "resume" in str(err).lower()

        # Zero mutation: no follower row
        workers = _worker_rows(db)
        assert all(w["role"] == "leader" for w in workers)

    def test_vacant_seat_refused(self) -> None:
        """A RUNNING run with a vacant seat (NULL leader_worker_id) is refused."""
        db = make_landscape_db()
        with db.write_connection() as conn:
            conn.execute(
                insert(runs_table).values(
                    run_id=RUN_ID,
                    started_at=NOW,
                    config_hash=_SENTINEL_HASH,
                    settings_json="{}",
                    canonical_version="v1",
                    status="running",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
            # Vacant seat: leader_worker_id=NULL
            conn.execute(
                insert(run_coordination_table).values(
                    run_id=RUN_ID,
                    leader_worker_id=None,
                    leader_epoch=1,
                    leader_heartbeat_expires_at=None,
                    updated_at=NOW,
                )
            )

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL, pytest.raises(JoinRefusedError) as exc_info:
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        assert "no live leader" in str(exc_info.value).lower()

    def test_preflight_db_file_permission_failure(self, tmp_path: Path) -> None:
        """Filesystem preflight: JoinRefusedError names the unwritable path."""
        db_file = tmp_path / "audit.db"
        db = LandscapeDB.from_url(f"sqlite:///{db_file}")

        # Patch os.access to simulate the DB file being unwritable.
        original_access = os.access

        def fake_access(path: object, mode: int) -> bool:
            if mode == os.W_OK and str(path) == str(db_file.resolve()):
                return False
            return original_access(path, mode)  # type: ignore[arg-type]

        fake_settings = types.SimpleNamespace()
        with patch("elspeth.engine.orchestrator.join_admission.os.access", side_effect=fake_access):
            orch = _orchestrator(db)
            with pytest.raises(JoinRefusedError) as exc_info:
                orch.join_run(run_id=RUN_ID, settings=fake_settings)

        err = exc_info.value
        assert err.run_id == RUN_ID
        # The error message must name the problematic path (operator-actionable).
        assert str(db_file.resolve()) in str(err)

    def test_preflight_wal_sidecar_permission_failure(self, tmp_path: Path) -> None:
        """Filesystem preflight: unwritable -wal sidecar is caught and named."""
        db_file = tmp_path / "audit.db"
        wal_file = tmp_path / "audit.db-wal"
        db = LandscapeDB.from_url(f"sqlite:///{db_file}")

        # Create the -wal sidecar so the preflight checks it.
        wal_file.touch()

        original_access = os.access

        def fake_access(path: object, mode: int) -> bool:
            if mode == os.W_OK and str(path) == str(wal_file.resolve()):
                return False
            return original_access(path, mode)  # type: ignore[arg-type]

        fake_settings = types.SimpleNamespace()
        with patch("elspeth.engine.orchestrator.join_admission.os.access", side_effect=fake_access):
            orch = _orchestrator(db)
            with pytest.raises(JoinRefusedError) as exc_info:
                orch.join_run(run_id=RUN_ID, settings=fake_settings)

        err = exc_info.value
        assert err.run_id == RUN_ID
        assert str(wal_file.resolve()) in str(err)


# ---------------------------------------------------------------------------
# Atomicity: one BEGIN IMMEDIATE — no ghost rows on refusal
# ---------------------------------------------------------------------------


class TestJoinAdmissionAtomicity:
    """Admission is atomic: a refused join leaves ZERO mutation in the DB."""

    def test_no_follower_row_on_config_hash_mismatch(self) -> None:
        """Config-hash refusal: the DB has only the leader row, no ghost follower."""
        db = make_landscape_db()
        _begin_run_with_leader(db, config_hash="canonical-hash")

        events_before = _coord_events(db)

        fake_settings = types.SimpleNamespace()
        with (
            patch("elspeth.engine.orchestrator.join_admission.resolve_config", return_value={}),
            patch("elspeth.engine.orchestrator.join_admission.stable_hash", return_value="wrong-hash"),
            pytest.raises(JoinRefusedError),
        ):
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        # run_workers: still only the leader row
        workers = _worker_rows(db)
        assert len(workers) == 1
        assert workers[0]["role"] == "leader"

        # coordination_events: no new rows (the transaction rolled back)
        assert _coord_events(db) == events_before

    def test_no_follower_row_on_expired_seat_refusal(self) -> None:
        """Dead-seat refusal: zero mutation — no ghost follower, no new event."""
        db = make_landscape_db()
        _begin_run_with_leader(db)
        expire_leader_seat(db, RUN_ID)

        events_before = _coord_events(db)

        fake_settings = types.SimpleNamespace()
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL, pytest.raises(JoinRefusedError):
            orch = _orchestrator(db)
            orch.join_run(run_id=RUN_ID, settings=fake_settings)

        workers = _worker_rows(db)
        assert len(workers) == 1
        assert workers[0]["role"] == "leader"
        assert _coord_events(db) == events_before


class TestSeatLivenessBoundary:
    """ADR-047: admission decides ``leader_heartbeat_expires_at >= database_now`` on the Landscape clock.

    The comparison moved from ``> now`` (process clock) to ``>= database_now``,
    which changes admission AT exact equality. Both sides of the boundary are
    pinned here: a seat whose deadline EQUALS database time is still live and
    admits the follower; one second past it is refused.
    """

    def test_seat_deadline_equal_to_database_time_admits_and_one_second_past_refuses(self) -> None:
        db = make_landscape_db()
        _begin_run_with_leader(db)
        fake_settings = types.SimpleNamespace()

        def stamp_seat_at_database_now_and_join(database_now: datetime) -> str:
            with db.write_connection() as conn:
                stamped = read_landscape_transaction_time(conn)
                assert stamped == database_now
                conn.execute(
                    update(run_coordination_table)
                    .where(run_coordination_table.c.run_id == RUN_ID)
                    .values(leader_heartbeat_expires_at=stamped)
                )
            with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL:
                return _orchestrator(db).join_run(run_id=RUN_ID, settings=fake_settings)

        # Equality arm, inside one whole SQLite database second.
        worker_id = on_fresh_database_second(db.engine, stamp_seat_at_database_now_and_join)
        assert worker_id.startswith(f"worker:{RUN_ID}:")
        assert [w["status"] for w in _worker_rows(db) if w["worker_id"] == worker_id] == ["active"]

        # Strictly-past arm: the seat lapsed one database second ago.
        expire_leader_seat(db, RUN_ID)
        with _PATCH_RESOLVE, _PATCH_HASH_SENTINEL, pytest.raises(JoinRefusedError) as exc_info:
            _orchestrator(db).join_run(run_id=RUN_ID, settings=fake_settings)
        assert "no live leader" in str(exc_info.value).lower()
        assert len([w for w in _worker_rows(db) if w["role"] == "follower"]) == 1, "the refused join registered no second follower"
