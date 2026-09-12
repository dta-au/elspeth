"""Observed refusal decisions survive the actual CLI rendering boundary."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.cli import _emit_not_resumable_event, app
from elspeth.contracts import Checkpoint
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.core.checkpoint.recovery import NonResumableRunError, RecoveryManager
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
from elspeth.core.landscape.schema import checkpoints_table, runs_table
from tests.fixtures.landscape import insert_crashed_leader_seat
from tests.integration.cli.test_cli import _make_jsonl_settings
from tests.unit.cli.test_export_resume_command import _make_landscape_db_with_run, _write_settings


@pytest.mark.parametrize("exists, expected", [(False, "run_not_found"), (True, "run_terminal")])
def test_resume_real_status_refusal_is_json(tmp_path: Path, exists: bool, expected: str) -> None:
    db_path = tmp_path / "landscape.db"
    _make_landscape_db_with_run(db_path)
    settings = _write_settings(tmp_path, db_path)
    run_id = "run-export" if exists else "absent-run"

    result = CliRunner().invoke(app, ["resume", run_id, "-s", str(settings), "--format", "json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == "not_resumable"
    assert payload["reason"] == expected
    assert payload["run_id"] == run_id


@pytest.mark.parametrize(
    "status, export_status, expected",
    [("running", "failed", "run_not_finalized"), ("completed", "completed", "export_already_completed")],
)
def test_export_actual_preflight_preserves_cause(tmp_path: Path, status: str, export_status: str, expected: str) -> None:
    db_path = tmp_path / "landscape.db"
    _make_landscape_db_with_run(db_path, run_status=status, export_status=export_status)
    settings = _write_settings(tmp_path, db_path)

    result = CliRunner().invoke(app, ["export-resume", "run-export", "-s", str(settings), "--format", "json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["cause"] == expected
    assert payload["event"] == "export_resume_refused"


@pytest.mark.parametrize("cause", [ResumeRefusalCause.LEADER_LIVE, ResumeRefusalCause.CHECKPOINT_NOT_LATEST])
def test_same_human_reason_keeps_distinct_observed_codes(capsys: pytest.CaptureFixture[str], cause: ResumeRefusalCause) -> None:
    error = NonResumableRunError("run-a", "Admission refused", cause=cause)
    _emit_not_resumable_event(error, "json")
    payload = json.loads(capsys.readouterr().err)
    assert payload["message"] == "Admission refused"
    assert payload["reason"] == cause.value


@pytest.mark.parametrize("expected", ["leader_live", "run_not_finalized"])
def test_export_transaction_refusal_reaches_cli_after_state_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: str) -> None:
    """The admission transaction selects the code; rendering never reclassifies it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".elspeth" / "payloads").mkdir(parents=True, mode=0o700)
    db_path = tmp_path / "landscape.db"
    _make_landscape_db_with_run(db_path)
    settings = _write_settings(tmp_path, db_path)
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db:
        with db.engine.begin() as connection:
            insert_crashed_leader_seat(connection, run_id="run-export")
        repo = RunCoordinationRepository(db.engine)
        incumbent = repo.acquire_export_leadership(run_id="run-export", worker_id="incumbent", window_seconds=80)
        original = RunCoordinationRepository.acquire_export_leadership

        def acquire(self, *, run_id, worker_id, window_seconds):
            if expected == "run_not_finalized":
                with db.engine.begin() as connection:
                    connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="running", completed_at=None))
            try:
                return original(self, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds)
            except NonResumableRunError:
                # Undo the refusal condition before the actual CLI handler runs.
                if expected == "leader_live":
                    repo.release_seat(token=incumbent)
                else:
                    with db.engine.begin() as connection:
                        connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="failed"))

                def unexpected_classification_read(*args, **kwargs):
                    pytest.fail("CLI re-read run state after the observed refusal")

                monkeypatch.setattr(RunLifecycleRepository, "get_run", unexpected_classification_read)
                raise

        with patch.object(RunCoordinationRepository, "acquire_export_leadership", autospec=True, side_effect=acquire) as acquisition:
            result = CliRunner().invoke(app, ["export-resume", "run-export", "-s", str(settings), "--execute", "--format", "json"])
        acquisition.assert_called_once()

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == "export_resume_refused"
    assert payload["cause"] == expected
    assert payload["preflight"]["run_status"] == "completed"
    assert "run_status" not in payload
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "format_version, expected",
    [(None, "checkpoint_format_missing"), (Checkpoint.CURRENT_FORMAT_VERSION + 1, "checkpoint_format_incompatible")],
)
def test_real_workset_format_refusal_reaches_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_version: int | None, expected: str
) -> None:
    settings = _make_jsonl_settings(tmp_path)
    runner = CliRunner()
    # Retain the real run's checkpoint so the fixture can exercise recovery.
    with patch("elspeth.engine.orchestrator.run_lifecycle.RunLifecycleCoordinator._delete_checkpoints_after_success", autospec=True):
        initial = runner.invoke(app, ["run", "-s", str(settings), "--execute", "--format", "json"])
    assert initial.exit_code == 0, initial.output
    db_path = tmp_path / "landscape.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db:
        with db.engine.begin() as connection:
            run_id = connection.execute(select(runs_table.c.run_id)).scalar_one()
            connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="failed"))
        original = RecoveryManager.get_unprocessed_rows

        def incompatible_workset(self: RecoveryManager, observed_run_id: str) -> list[str]:
            with db.engine.begin() as connection:
                connection.execute(
                    update(checkpoints_table).where(checkpoints_table.c.run_id == observed_run_id).values(format_version=format_version)
                )
            try:
                return original(self, observed_run_id)
            finally:
                # Rendering must preserve the refused observation after it changes.
                with db.engine.begin() as connection:
                    connection.execute(
                        update(checkpoints_table)
                        .where(checkpoints_table.c.run_id == observed_run_id)
                        .values(format_version=Checkpoint.CURRENT_FORMAT_VERSION)
                    )

        with patch.object(RecoveryManager, "get_unprocessed_rows", autospec=True, side_effect=incompatible_workset) as workset:
            result = runner.invoke(app, ["resume", run_id, "-s", str(settings), "--execute", "--format", "json"])
        assert workset.call_count == 1, (result.output, result.exception)
        workset.assert_called_once()

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == "not_resumable"
    assert payload["reason"] == expected
    assert payload["run_id"] == run_id
    assert "Traceback" not in result.output
