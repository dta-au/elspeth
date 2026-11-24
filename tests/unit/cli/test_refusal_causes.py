"""Observed refusal decisions survive the actual CLI rendering boundary."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.cli import _emit_not_resumable_event, app
from elspeth.contracts import Checkpoint
from elspeth.contracts import errors as contract_errors
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.core.checkpoint.recovery import NonResumableRunError, RecoveryManager
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
from elspeth.core.landscape.schema import checkpoints_table, run_workers_table, runs_table
from elspeth.engine.orchestrator import Orchestrator
from tests.fixtures.landscape import insert_crashed_leader_seat
from tests.integration.cli.test_cli import _make_jsonl_settings
from tests.unit.cli.test_export_resume_command import _make_landscape_db_with_run, _write_settings


def test_join_not_ready_has_distinct_json_event(tmp_path: Path) -> None:
    assert "JoinNotReadyError" in vars(contract_errors)
    not_ready_error = vars(contract_errors)["JoinNotReadyError"]("run-join", "graph registration is pending")
    assert isinstance(not_ready_error, contract_errors.JoinRefusedError)
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    with patch.object(Orchestrator, "join_run", side_effect=not_ready_error):
        result = CliRunner().invoke(app, ["join", "run-join", "-s", str(settings), "--format", "json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == "join_not_ready"
    assert payload["run_id"] == "run-join"


@pytest.mark.parametrize("output_format", ["json", "console"])
@pytest.mark.parametrize("departure_failure", [None, "SQLAlchemyError: departure unavailable"])
def test_join_refusal_reports_departure_failure(tmp_path: Path, output_format: str, departure_failure: str | None) -> None:
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    refusal = contract_errors.JoinRefusedError("run-join", "graph registration incomplete")
    refusal.departure_failure = departure_failure
    with patch.object(Orchestrator, "join_run", side_effect=refusal):
        result = CliRunner().invoke(app, ["join", "run-join", "-s", str(settings), "--format", output_format])
    assert result.exit_code == 1, result.output
    if output_format == "json":
        payload = json.loads(result.stderr)
        assert payload["event"] == "join_refused"
        assert payload["reason"] == "graph registration incomplete"
        if departure_failure is None:
            assert "departure_failed" not in payload
            assert "departure_failure" not in payload
        else:
            assert payload["departure_failed"] is True
            assert payload["departure_failure"] == departure_failure
    else:
        assert "graph registration incomplete" in result.stderr
        if departure_failure is None:
            assert "seat could not be released" not in result.stderr
        else:
            assert "seat could not be released" in result.stderr
            assert "heartbeat expiry" in result.stderr
            assert departure_failure in result.stderr


def test_join_post_admission_error_has_join_label(tmp_path: Path) -> None:
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    with patch.object(Orchestrator, "join_run", side_effect=ValueError("post-check unavailable")):
        result = CliRunner().invoke(app, ["join", "run-join", "-s", str(settings)])
    assert result.exit_code == 1, result.output
    assert "Join failed: post-check unavailable" in result.stderr


def test_join_graph_build_failure_writes_no_follower_row(tmp_path: Path) -> None:
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    db_path = tmp_path / "landscape.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db:
        with db.engine.begin() as connection:
            run_id = connection.execute(select(runs_table.c.run_id)).scalar_one()
            connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="running", completed_at=None))
        RunCoordinationRepository(db.engine).acquire_run_leadership(
            run_id=run_id,
            worker_id=f"worker:{run_id}:leader",
            window_seconds=10**9,
        )
        with patch("elspeth.cli._build_resume_graph", side_effect=ValueError("graph unavailable")):
            result = CliRunner().invoke(app, ["join", run_id, "-s", str(settings), "--format", "json"])
        with db.engine.connect() as connection:
            followers = connection.execute(
                select(run_workers_table.c.worker_id).where(run_workers_table.c.run_id == run_id, run_workers_table.c.role == "follower")
            ).all()
    assert result.exit_code == 1, result.output
    assert "Error building execution graph: graph unavailable" in result.stderr
    assert followers == []


@pytest.mark.parametrize(
    ("failure", "event"),
    [
        (ValueError("PROBE-CONFIG-INVALID"), "error"),
        (contract_errors.AuditIntegrityError("PROBE-CONFIG-INVALID"), "fatal"),
    ],
)
def test_join_pipeline_config_error_keeps_structured_event_without_follower(tmp_path: Path, failure: Exception, event: str) -> None:
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    db_path = tmp_path / "landscape.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db:
        with db.engine.begin() as connection:
            run_id = connection.execute(select(runs_table.c.run_id)).scalar_one()
            connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="running", completed_at=None))
        RunCoordinationRepository(db.engine).acquire_run_leadership(
            run_id=run_id, worker_id=f"worker:{run_id}:leader", window_seconds=10**9
        )
        with patch("elspeth.engine.orchestrator.types.PipelineConfig", side_effect=failure):
            result = CliRunner().invoke(app, ["join", run_id, "-s", str(settings), "--format", "json"])
        with db.engine.connect() as connection:
            followers = connection.execute(
                select(run_workers_table.c.worker_id).where(run_workers_table.c.run_id == run_id, run_workers_table.c.role == "follower")
            ).all()
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == event
    assert payload["error_type"] == type(failure).__name__
    assert payload["error"] == "PROBE-CONFIG-INVALID"
    assert followers == []


@pytest.mark.parametrize("command", ["resume", "abandon"])
def test_cli_advisory_reports_old_version_as_not_resumable(tmp_path: Path, command: str) -> None:
    settings = _make_jsonl_settings(tmp_path)
    initial = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert initial.exit_code == 0, initial.output
    db_path = tmp_path / "landscape.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db, db.engine.begin() as connection:
        run_id = connection.execute(select(runs_table.c.run_id)).scalar_one()
        connection.execute(
            update(runs_table)
            .where(runs_table.c.run_id == run_id)
            .values(
                status="failed" if command == "resume" else "running",
                completed_at=None,
                canonical_version="sha256-rfc8785-previous",
            )
        )
    result = CliRunner().invoke(app, [command, run_id, "-s", str(settings), "--format", "json"])
    if command == "resume":
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stderr)
        assert payload["event"] == "not_resumable"
        assert payload["reason"] == "canonical_version_changed"
    else:
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["event"] == "abandon_preflight"
        assert payload["resumable"] is False
        assert "canonical version changed" in payload["resume_reason"]


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
def test_post_preflight_format_refusal_reaches_cli(
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
        original = RecoveryManager.count_active_scheduler_work

        def format_changes_after_preflight(self: RecoveryManager, observed_run_id: str) -> int:
            # The checkpoint changes after the CLI's advisory preflight read;
            # the resume's own checkpoint re-verification must refuse it.
            with db.engine.begin() as connection:
                connection.execute(
                    update(checkpoints_table).where(checkpoints_table.c.run_id == observed_run_id).values(format_version=format_version)
                )
            return original(self, observed_run_id)

        with patch.object(
            RecoveryManager, "count_active_scheduler_work", autospec=True, side_effect=format_changes_after_preflight
        ) as preflight_read:
            result = runner.invoke(app, ["resume", run_id, "-s", str(settings), "--execute", "--format", "json"])
        preflight_read.assert_called_once()

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["event"] == "not_resumable"
    assert payload["reason"] == expected
    assert payload["run_id"] == run_id
    assert "Traceback" not in result.output
