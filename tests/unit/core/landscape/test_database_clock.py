"""Pins for the Landscape database clock (ADR-047, C6.0).

``read_landscape_transaction_time`` is the single read site of Landscape
database time. These pins hold its contract on SQLite (a real in-memory
Landscape), its PostgreSQL normalisation branch (a fake connection standing
in for a ``timestamptz`` under a non-UTC session time zone — the live twin
is tests/testcontainer/core/test_database_clock_postgres.py), its Tier-1
refusals, and the clock-authority gate's own classification of the helper:
an authority caller that binds the helper's value scans clean, and a process
clock hidden in the helper's body is classified as process time at that
caller's deadline sink.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text

from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import (
    DEFAULT_ITEM_STALL_BUDGET_SECONDS,
    DEFAULT_RUN_HEARTBEAT_SECONDS,
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    CoordinationToken,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB, begin_write
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository, verify_and_extend_leader_fence
from elspeth.core.landscape.schema import run_coordination_table, runs_table
from tests.fixtures.landscape import assert_deadline_within, landscape_database_now, make_landscape_db
from tests.helpers.run_coordination import register_run_leader
from tests.unit.core.landscape.test_database_clock_authority import _clock_returning_references, _scan_sources

_ROOT = Path(__file__).resolve().parents[4]
_HELPER_PATH = "src/elspeth/core/landscape/database_clock.py"
_CALLER_PATH = "src/elspeth/core/landscape/custody_probe.py"
_AUTHORITY_CALLER = """
from datetime import timedelta

from sqlalchemy import update

from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.schema import run_workers_table


def worker_heartbeat(conn, worker_id):
    database_now = read_landscape_transaction_time(conn)
    conn.execute(
        update(run_workers_table)
        .where(run_workers_table.c.worker_id == worker_id)
        .values(heartbeat_expires_at=database_now + timedelta(seconds=30))
    )
"""


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeConnection:
    """The two surfaces the helper touches: ``dialect.name`` and ``scalar``."""

    def __init__(self, dialect: str, value: object) -> None:
        self.dialect = _FakeDialect(dialect)
        self._value = value
        self.statements: list[object] = []

    def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self._value


class TestSqliteRoundTrip:
    def test_returns_aware_utc_within_the_database_second(self) -> None:
        db = make_landscape_db()
        try:
            with db.engine.begin() as conn:
                before = datetime.now(UTC).replace(microsecond=0)
                moment = read_landscape_transaction_time(conn)
                after = datetime.now(UTC)
                # The same instant the database reports, re-read directly.
                direct = conn.scalar(func.current_timestamp())
        finally:
            db.close()
        assert moment.tzinfo is UTC
        assert moment.microsecond == 0
        assert before - timedelta(seconds=1) <= moment <= after + timedelta(seconds=1)
        assert type(direct) is datetime and direct.tzinfo is None
        assert abs(moment - direct.replace(tzinfo=UTC)) <= timedelta(seconds=1)

    def test_reads_exactly_once(self) -> None:
        conn = _FakeConnection("sqlite", datetime.fromisoformat("2026-09-05T08:00:00"))
        assert read_landscape_transaction_time(conn) == datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)  # type: ignore[arg-type]
        assert len(conn.statements) == 1
        assert str(conn.statements[0]) == "CURRENT_TIMESTAMP"


class TestPostgresNormalisation:
    def test_non_utc_session_timestamptz_becomes_the_same_instant_in_utc(self) -> None:
        sydney = timezone(timedelta(hours=10))
        local = datetime(2026, 9, 5, 18, 0, 0, 123456, tzinfo=sydney)
        moment = read_landscape_transaction_time(_FakeConnection("postgresql", local))  # type: ignore[arg-type]
        assert moment.tzinfo is UTC
        assert moment == local
        assert moment == datetime(2026, 9, 5, 8, 0, 0, 123456, tzinfo=UTC)
        assert moment.utcoffset() == timedelta(0)

    def test_naive_postgres_result_is_landscape_corruption(self) -> None:
        with pytest.raises(AuditIntegrityError, match="expected an aware datetime"):
            read_landscape_transaction_time(_FakeConnection("postgresql", datetime.fromisoformat("2026-09-05T08:00:00")))  # type: ignore[arg-type]


class TestRefusals:
    def test_aware_sqlite_result_is_landscape_corruption(self) -> None:
        with pytest.raises(AuditIntegrityError, match="expected a naive UTC datetime"):
            read_landscape_transaction_time(_FakeConnection("sqlite", datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)))  # type: ignore[arg-type]

    @pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
    def test_non_datetime_result_is_landscape_corruption(self, dialect: str) -> None:
        with pytest.raises(AuditIntegrityError, match="Tier 1"):
            read_landscape_transaction_time(_FakeConnection(dialect, "2026-09-05 08:00:00"))  # type: ignore[arg-type]

    def test_unknown_dialect_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="'mssql'"):
            read_landscape_transaction_time(_FakeConnection("mssql", datetime.fromisoformat("2026-09-05T08:00:00")))  # type: ignore[arg-type]


class TestClockAuthorityGateClassification:
    """The gate's scanner is the positive control for the helper's provenance."""

    @staticmethod
    def _helper_source() -> str:
        return (_ROOT / _HELPER_PATH).read_text(encoding="utf-8")

    def test_helper_is_not_a_process_clock_and_an_authority_caller_scans_clean(self) -> None:
        sources = {_HELPER_PATH: self._helper_source(), _CALLER_PATH: _AUTHORITY_CALLER}
        process_callables, _database_callables, _unresolved = _clock_returning_references(sources)
        assert "elspeth.core.landscape.database_clock.read_landscape_transaction_time" not in process_callables
        assert _scan_sources(sources) == ()

    def test_process_clock_in_the_helper_body_is_classified_at_the_authority_sink(self) -> None:
        """The mutation the gate must catch: the helper minting process time."""
        mutant = self._helper_source().replace("return stamped.astimezone(UTC)", "return datetime.now(UTC)")
        assert mutant != self._helper_source()
        sources = {_HELPER_PATH: mutant, _CALLER_PATH: _AUTHORITY_CALLER}
        process_callables, _database_callables, _unresolved = _clock_returning_references(sources)
        assert "elspeth.core.landscape.database_clock.read_landscape_transaction_time" in process_callables
        kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == _CALLER_PATH}
        assert {"process-clock-authority", "missing-database-time"} <= kinds

    def test_helper_module_imports_nothing_from_the_web_tree(self) -> None:
        """Distinct authorities, same contract: the Landscape clock never imports the Sessions one."""
        tree = ast.parse(self._helper_source())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
        assert imported, "the helper must import its sqlalchemy and contracts dependencies explicitly"
        assert not {name for name in imported if name == "elspeth.web" or name.startswith("elspeth.web.")}


class TestFirstFenceDatabaseDeadline:
    """C6.1: the leader fence writes its deadline from the database clock, in SQL.

    The SQLite text artefact the ADR documents — ``datetime(CURRENT_TIMESTAMP,
    '+N seconds')`` is whole-second UTC with no fraction, so a fence-written
    expiry compares as expired up to one second early against a ``.ffffff``
    bound — is pinned here together with the floor that keeps it harmless:
    every production liveness window is at least ten seconds.
    """

    @staticmethod
    def _seated_run(db: LandscapeDB) -> tuple[str, CoordinationToken]:
        run_id = f"run-fence-clock-{uuid4().hex[:8]}"
        with db.engine.begin() as conn:
            conn.execute(
                insert(runs_table).values(
                    run_id=run_id,
                    started_at=datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC),
                    config_hash="cfg",
                    settings_json="{}",
                    canonical_version="v1",
                    status=RunStatus.RUNNING.value,
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
        token = register_run_leader(
            RunCoordinationRepository(db.engine),
            run_id=run_id,
            worker_id="worker-leader",
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        )
        return run_id, token

    def test_sqlite_fence_deadline_is_database_time_plus_window_as_whole_second_text(self) -> None:
        db = make_landscape_db()
        try:
            run_id, token = self._seated_run(db)
            with begin_write(db.engine) as conn:
                database_now = read_landscape_transaction_time(conn)
                verify_and_extend_leader_fence(conn, token=token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, verb="unit-test")
            with db.engine.connect() as conn:
                raw = conn.execute(
                    text("SELECT leader_heartbeat_expires_at FROM run_coordination WHERE run_id = :run_id"), {"run_id": run_id}
                ).scalar_one()
                stamped = conn.execute(
                    select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id)
                ).scalar_one()
        finally:
            db.close()
        assert isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw), raw
        assert_deadline_within(stamped, database_now + timedelta(seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS))

    def test_fence_text_artefact_is_under_one_second_against_a_fractional_bound(self) -> None:
        """A ``.ffffff`` bound for the same instant differs from the fence text by less than the window floor."""
        db = make_landscape_db()
        try:
            _run_id, token = self._seated_run(db)
            with begin_write(db.engine) as conn:
                verify_and_extend_leader_fence(conn, token=token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, verb="unit-test")
            bound = datetime.now(UTC) + timedelta(seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS)
            stamped = landscape_database_now(db.engine)  # a whole-second read of the same clock
        finally:
            db.close()
        assert stamped.microsecond == 0
        assert abs((bound - timedelta(seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS)) - stamped) < timedelta(seconds=1)

    def test_every_production_window_is_at_least_ten_seconds(self) -> None:
        assert DEFAULT_RUN_HEARTBEAT_SECONDS >= 10
        assert DEFAULT_RUN_LIVENESS_WINDOW_SECONDS >= 10
        assert DEFAULT_ITEM_STALL_BUDGET_SECONDS >= 10
