"""Local operator diagnostics retain the real BUSY producer's worker roster."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import Result
from sqlalchemy import Engine, event, select, update
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.checkpoint import ResumeCheck, ResumePoint
from elspeth.contracts.coordination import RegisteredWorker
from elspeth.contracts.errors import WriteLockHeldError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import run_coordination_table, run_workers_table, runs_table
from tests.integration.cli.test_cli import _make_jsonl_settings, _make_minimal_settings
from tests.unit.cli.test_abandon_command import RUN_ID, _abandoned_count, _seed_leaderless_run
from tests.unit.cli.test_abandon_command import _write_settings as _abandon_settings
from tests.unit.cli.test_export_resume_command import _make_landscape_db_with_run
from tests.unit.cli.test_export_resume_command import _write_settings as _export_settings

runner = CliRunner()
ROSTERS = (
    (
        RegisteredWorker(worker_id="worker-a", role="leader", status="departed", hostname="host-a", pid=4242),
        RegisteredWorker(worker_id="worker-b", role="follower", status="evicted", hostname="host-b", pid=4242),
    ),
    (
        RegisteredWorker(worker_id="worker-c", role="leader", status="active", hostname="host-c", pid=8181),
        RegisteredWorker(worker_id="worker-d", role="follower", status="departed", hostname=None, pid=None),
    ),
)


def _assert_json_roster(result: Result, run_id: str, workers: tuple[RegisteredWorker, ...]) -> None:
    assert result.exit_code == 1, result.output
    assert result.stderr, repr(result.exception)
    assert result.stderr.lstrip().startswith("{"), result.stderr
    # Parse all stderr as one document: duplicate events and traceback noise fail.
    payload = json.loads(result.stderr)
    assert payload["event"] == "write_lock_held"
    assert payload["run_id"] == run_id
    assert payload["message"]
    assert payload["lock_owner_identified"] is False
    assert payload["registered_workers"] == [
        {"worker_id": w.worker_id, "role": w.role, "status": w.status, "hostname": w.hostname, "pid": w.pid} for w in workers
    ], "registered worker roster differs from seeded values"
    guidance = payload["guidance"].lower()
    assert "stale" in guidance
    assert "reused" in guidance
    assert "host" in guidance and "container" in guidance and "namespace" in guidance
    assert "may not be listed" in guidance
    assert "Traceback" not in result.output
    assert "SIGKILL" not in result.output
    assert "kill -9" not in result.output
    assert "BEGIN IMMEDIATE" not in result.output


def _assert_console_roster(result: Result, workers: tuple[RegisteredWorker, ...]) -> None:
    assert result.exit_code == 1, result.output
    assert result.stderr.count("Registered worker candidates (not confirmed lock holders)") == 1
    for worker in workers:
        assert repr(worker.worker_id) in result.stderr
        assert repr(worker.role) in result.stderr
        assert repr(worker.status) in result.stderr
        assert repr(worker.hostname) in result.stderr
        assert str(worker.pid) in result.stderr
    assert "stale" in result.stderr and "reused" in result.stderr
    assert "namespace" in result.stderr and "may not be listed" in result.stderr
    assert "SIGKILL" not in result.output and "kill -9" not in result.output
    assert "Traceback" not in result.output and "BEGIN IMMEDIATE" not in result.output


@pytest.mark.parametrize("workers", ROSTERS)
@pytest.mark.parametrize("command", ["run", "resume"])
@pytest.mark.parametrize("output_format", ["json", "console"])
def test_execution_boundary_delivers_exact_roster(
    tmp_path: Path, command: str, workers: tuple[RegisteredWorker, ...], output_format: str
) -> None:
    """Run is an injected taxonomy check; fresh run does not use takeover CAS."""
    error = WriteLockHeldError(run_id="run-injected", workers=workers)
    error.__cause__ = RuntimeError("private-cause-marker")
    if command == "run":
        settings = _make_minimal_settings(tmp_path)
        with patch("elspeth.cli._execute_pipeline_with_instances", autospec=True, side_effect=error):
            result = runner.invoke(app, ["run", "-s", str(settings), "--execute", "--format", output_format])
    else:
        settings = _make_jsonl_settings(tmp_path)
        with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}"):
            pass
        point = Mock(spec=ResumePoint, sequence_number=0, barrier_scalars=None)
        with (
            patch("elspeth.core.checkpoint.RecoveryManager.can_resume", autospec=True, return_value=ResumeCheck(can_resume=True)),
            patch("elspeth.core.checkpoint.RecoveryManager.get_resume_point", autospec=True, return_value=point),
            patch("elspeth.core.checkpoint.RecoveryManager.get_unprocessed_rows", autospec=True, return_value=[]),
            patch("elspeth.core.checkpoint.RecoveryManager.count_blocked_barrier_items", autospec=True, return_value=0),
            patch("elspeth.cli._execute_resume_with_instances", autospec=True, side_effect=error),
        ):
            result = runner.invoke(app, ["resume", "run-injected", "-s", str(settings), "--execute", "--format", output_format])
    if output_format == "json":
        _assert_json_roster(result, "run-injected", workers)
    else:
        _assert_console_roster(result, workers)
    assert "private-cause-marker" not in result.output


@pytest.mark.parametrize("workers", [*ROSTERS, ()])
def test_renderer_json_preserves_null_and_empty_rosters(capsys: pytest.CaptureFixture[str], workers: tuple[RegisteredWorker, ...]) -> None:
    from elspeth.cli import _emit_write_lock_held

    _emit_write_lock_held(WriteLockHeldError(run_id="render-run", workers=workers), "json")
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["registered_workers"] == [
        {"worker_id": w.worker_id, "role": w.role, "status": w.status, "hostname": w.hostname, "pid": w.pid} for w in workers
    ]
    assert payload["lock_owner_identified"] is False
    if not workers:
        assert "empty or unreadable" in payload["guidance"]


def test_console_escapes_registered_values(capsys: pytest.CaptureFixture[str]) -> None:
    from elspeth.cli import _emit_write_lock_held

    worker = RegisteredWorker(worker_id="worker\nforged", role="leader", status="evicted", hostname="host\x1b[31m\nforged", pid=4242)
    _emit_write_lock_held(WriteLockHeldError(run_id="render-run", workers=(worker,)), "console")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert repr(worker.worker_id) in captured.err
    assert repr(worker.hostname) in captured.err
    assert "\x1b" not in captured.err
    assert "\nforged" not in captured.err
    assert "not confirmed lock holders" in captured.err
    assert "evicted" in captured.err


def _invoke_real_busy_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, workers: tuple[RegisteredWorker, ...], output_format: str = "json"
) -> Result:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "audit.db"
    run_id = RUN_ID if command == "abandon" else "run-export"
    if command == "abandon":
        with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
            _seed_leaderless_run(db)
        settings = _abandon_settings(tmp_path, db_path)
    else:
        _make_landscape_db_with_run(db_path)
        settings = _export_settings(tmp_path, db_path)
    payload_path = tmp_path / ".elspeth" / "payloads"
    payload_path.mkdir(parents=True, mode=0o700)

    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db, db.engine.begin() as conn:
        if command == "abandon":
            # Update seeded rows, keeping their registry/seat identity intact.
            ids = conn.execute(select(run_workers_table.c.worker_id).order_by(run_workers_table.c.registered_at)).scalars().all()
            for worker_id, worker in zip(ids, workers, strict=True):
                conn.execute(
                    update(run_workers_table)
                    .where(run_workers_table.c.worker_id == worker_id)
                    .values(
                        role=worker.role,
                        status=worker.status,
                        hostname=worker.hostname,
                        pid=worker.pid,
                        evicted_at=datetime.now(UTC) if worker.status == "evicted" else None,
                    )
                )
            workers = tuple(
                RegisteredWorker(worker_id=i, role=w.role, status=w.status, hostname=w.hostname, pid=w.pid)
                for i, w in zip(ids, workers, strict=True)
            )
        else:
            for worker in workers:
                conn.execute(
                    run_workers_table.insert().values(
                        run_id=run_id,
                        worker_id=worker.worker_id,
                        role=worker.role,
                        status=worker.status,
                        hostname=worker.hostname,
                        pid=worker.pid,
                        registered_at=datetime.now(UTC),
                        heartbeat_expires_at=datetime.now(UTC),
                        evicted_at=datetime.now(UTC) if worker.status == "evicted" else None,
                    )
                )
        before_run = conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).mappings().one()
        before_seat = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings().all()
        before_workers = conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == run_id)).mappings().all()

    def short_timeout(connection: sqlite3.Connection, _record: object, _proxy: object) -> None:
        connection.execute("PRAGMA busy_timeout=50")

    holder = sqlite3.connect(db_path)
    original = (
        RunCoordinationRepository.acquire_run_leadership if command == "abandon" else RunCoordinationRepository.acquire_export_leadership
    )
    method_name = "acquire_run_leadership" if command == "abandon" else "acquire_export_leadership"
    started = False

    def acquire_under_real_lock(*args, **kwargs):
        nonlocal started
        # Let CLI startup verify its ordinary PRAGMA contract first. Start the
        # competing transaction immediately before the actual acquisition.
        holder.execute("BEGIN IMMEDIATE")
        event.listen(Engine, "checkout", short_timeout)
        started = True
        return original(*args, **kwargs)

    try:
        with patch.object(RunCoordinationRepository, method_name, autospec=True, side_effect=acquire_under_real_lock) as acquire:
            result = runner.invoke(app, [command, run_id, "-s", str(settings), "--execute", "--format", output_format])
            acquire.assert_called_once()
    finally:
        holder.rollback()
        holder.close()
        if started:
            event.remove(Engine, "checkout", short_timeout)
    if output_format == "json":
        _assert_json_roster(result, run_id, workers)
    else:
        _assert_console_roster(result, workers)
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db, db.connection() as conn:
        assert conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).mappings().one() == before_run
        assert conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings().all() == before_seat
        assert conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == run_id)).mappings().all() == before_workers
    if command == "abandon":
        assert _abandoned_count(db_path) == 0
    return result


@pytest.mark.parametrize("command", ["abandon", "export-resume"])
@pytest.mark.parametrize("workers", ROSTERS)
@pytest.mark.parametrize("output_format", ["json", "console"])
def test_real_sqlite_busy_reaches_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, workers: tuple[RegisteredWorker, ...], output_format: str
) -> None:
    _invoke_real_busy_command(tmp_path, monkeypatch, command, workers, output_format)


@pytest.mark.parametrize("command", ["abandon", "export-resume"])
@pytest.mark.parametrize("replacement", [(), ROSTERS[1]], ids=["dropped-roster", "constant-same-size-roster"])
def test_real_producer_roster_mutation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, replacement: tuple[RegisteredWorker, ...]
) -> None:
    """Mutate only the forensic read; actual SQLite contention and acquisition remain."""
    with patch.object(RunCoordinationRepository, "_read_registered_workers", autospec=True, return_value=replacement) as read:
        with pytest.raises(AssertionError, match="registered worker roster differs from seeded values"):
            _invoke_real_busy_command(tmp_path, monkeypatch, command, ROSTERS[0])
        read.assert_called_once()
