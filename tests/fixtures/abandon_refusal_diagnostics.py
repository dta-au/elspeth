"""Observed abandonment causes survive preflight races and CLI rendering."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import event, select, update
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import AbandonRefusedError
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_coordination_table, runs_table
from elspeth.engine.orchestrator.abandon import AbandonOutcome, _acquire_leaderless_run_seat, abandon_leaderless_run, inspect_leaderless_run
from tests.unit.cli.test_abandon_command import RUN_ID, _abandoned_count, _seed_leaderless_run, _write_settings


def _set_refusing_state(db: LandscapeDB, cause: ResumeRefusalCause) -> None:
    """Control the database race; production inspection decides the cause."""
    with db.engine.begin() as conn:
        now = read_landscape_transaction_time(conn)
        conn.execute(
            update(runs_table)
            .where(runs_table.c.run_id == RUN_ID)
            .values(
                status=RunStatus.INTERRUPTED.value if cause is ResumeRefusalCause.RUN_NOT_RUNNING else RunStatus.RUNNING.value,
                completed_at=now if cause is ResumeRefusalCause.RUN_NOT_RUNNING else None,
            )
        )
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_heartbeat_expires_at=now + timedelta(seconds=600 if cause is ResumeRefusalCause.LEADER_LIVE else -1))
        )


def assert_abandon_preflight_cause(tmp_path: Path, expected_cause: ResumeRefusalCause) -> None:
    import json

    db_path = tmp_path / "audit.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
        _seed_leaderless_run(db)
        if expected_cause is not ResumeRefusalCause.RUN_NOT_FOUND:
            _set_refusing_state(db, expected_cause)
    settings_file = _write_settings(tmp_path, db_path)
    run_id = "absent-run" if expected_cause is ResumeRefusalCause.RUN_NOT_FOUND else RUN_ID

    with patch("elspeth.engine.orchestrator.abandon.inspect_leaderless_run", wraps=inspect_leaderless_run) as inspect:
        result = CliRunner().invoke(app, ["abandon", run_id, "--settings", str(settings_file), "--execute", "--format", "json"])

    assert result.exit_code == 1, result.output
    inspect.assert_called_once()
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["event"] == "abandon_refused"
    assert payload["run_id"] == run_id
    assert payload["cause"] == expected_cause.value
    assert payload["reason"]
    assert _abandoned_count(db_path) == 0


def assert_abandon_execution_refusal_cause(tmp_path: Path, race_cause: ResumeRefusalCause) -> None:
    import json

    db_path = tmp_path / "audit.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
        _seed_leaderless_run(db)
    settings_file = _write_settings(tmp_path, db_path)
    observed: list[AbandonRefusedError] = []
    rendering_queries: list[str] = []

    def race_before_execution(db: LandscapeDB, run_id: str) -> AbandonOutcome:
        # CLI's first, actual preflight admitted the expired seat. Change the
        # state before the actual execution function performs its second check.
        _set_refusing_state(db, race_cause)
        try:
            return abandon_leaderless_run(db, run_id)
        except AbandonRefusedError as exc:
            observed.append(exc)
            other_cause = (
                ResumeRefusalCause.LEADER_LIVE if race_cause is ResumeRefusalCause.RUN_NOT_RUNNING else ResumeRefusalCause.RUN_NOT_RUNNING
            )
            _set_refusing_state(db, other_cause)
            # Observe SQL through rendering, first proving the listener sees
            # a known query before clearing that control observation.
            event.listen(
                db.engine,
                "before_cursor_execute",
                lambda conn, cursor, statement, parameters, context, many: rendering_queries.append(statement),
            )
            with db.connection() as conn:
                conn.exec_driver_sql("SELECT 1")
            assert "SELECT 1" in rendering_queries
            rendering_queries.clear()
            raise

    with (
        patch("elspeth.engine.orchestrator.abandon.inspect_leaderless_run", wraps=inspect_leaderless_run) as inspect,
        patch("elspeth.engine.orchestrator.abandon.abandon_leaderless_run", autospec=True, side_effect=race_before_execution) as abandon,
    ):
        result = CliRunner().invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute", "--format", "json"])

    assert result.exit_code == 1, result.output
    abandon.assert_called_once()
    assert inspect.call_count == 2
    assert len(observed) == 1
    assert observed[0].cause is race_cause
    assert result.output, repr(result.exception)
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["event"] == "abandon_refused"
    assert payload["run_id"] == RUN_ID
    assert payload["reason"] == observed[0].reason
    assert payload["cause"] == race_cause.value
    assert rendering_queries == []
    assert _abandoned_count(db_path) == 0


def assert_abandon_refusal_cause_reaches_cli() -> None:
    """No-argument guard obligation exercising actual producer and consumer."""
    for cause in (ResumeRefusalCause.RUN_NOT_RUNNING, ResumeRefusalCause.LEADER_LIVE):
        with TemporaryDirectory(prefix="elspeth-abandon-cause-") as directory:
            assert_abandon_execution_refusal_cause(Path(directory), cause)


def assert_abandon_cas_refusal_cause(tmp_path: Path) -> None:
    """Both preflights admit; the real takeover CAS refuses a revived seat."""
    import json

    db_path = tmp_path / "audit.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
        _seed_leaderless_run(db)
    settings_file = _write_settings(tmp_path, db_path)
    observed: list[NonResumableRunError] = []
    rendering_queries: list[str] = []

    def revive_before_cas(factory: RecorderFactory, *, run_id: str) -> CoordinationToken:
        db = factory._db
        _set_refusing_state(db, ResumeRefusalCause.LEADER_LIVE)
        with db.connection() as conn:
            run_before = conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).one()
            seat_before = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).one()
        try:
            return _acquire_leaderless_run_seat(factory, run_id=run_id)
        except NonResumableRunError as exc:
            observed.append(exc)
            with db.connection() as conn:
                assert conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).one() == run_before
                assert conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).one() == seat_before
            _set_refusing_state(db, ResumeRefusalCause.RUN_NOT_RUNNING)
            event.listen(
                db.engine,
                "before_cursor_execute",
                lambda conn, cursor, statement, parameters, context, many: rendering_queries.append(statement),
            )
            with db.connection() as conn:
                conn.exec_driver_sql("SELECT 1")
            assert "SELECT 1" in rendering_queries
            rendering_queries.clear()
            raise

    with (
        patch("elspeth.engine.orchestrator.abandon.inspect_leaderless_run", wraps=inspect_leaderless_run) as inspect,
        patch("elspeth.engine.orchestrator.abandon._acquire_leaderless_run_seat", autospec=True, side_effect=revive_before_cas) as acquire,
    ):
        result = CliRunner().invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute", "--format", "json"])

    assert result.exit_code == 1, result.output
    acquire.assert_called_once()
    assert inspect.call_count == 2
    assert len(observed) == 1
    assert observed[0].cause is ResumeRefusalCause.LEADER_LIVE
    assert result.output, repr(result.exception)
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["event"] == "abandon_refused"
    assert payload["run_id"] == RUN_ID
    assert payload["reason"] == observed[0].reason
    assert payload["cause"] == ResumeRefusalCause.LEADER_LIVE.value
    assert rendering_queries == []
    assert _abandoned_count(db_path) == 0


def assert_abandon_cas_cause_reaches_cli() -> None:
    """No-argument diagnostic obligation for the real CAS refusal."""
    with TemporaryDirectory(prefix="elspeth-abandon-cas-") as directory:
        assert_abandon_cas_refusal_cause(Path(directory))
