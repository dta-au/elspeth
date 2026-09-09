# tests/unit/cli/test_abandon_command.py
"""``elspeth abandon``: the operator verb for a leaderless RUNNING run (elspeth-5dd23f4df9).

When a multi-worker leader dies mid-run the run stays RUNNING with an expired
seat, ``elspeth resume`` refuses on the incomplete source lifecycle (correctly:
resume replays only persisted rows), and the followers' finished work is
stranded. ``abandon`` takes the dead seat and finalizes INTERRUPTED under it,
which is the fenced arm ADR-038's abandonment sweep runs on. The CLI's other
recovery advice (follower seat-dead exit, resume refusal) must point at the
verb that can actually succeed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.cli import _emit_leaderless_run_guidance, _emit_not_resumable_event, app
from elspeth.contracts import NodeType, RunStatus
from elspeth.contracts.checkpoint import CheckpointDraft, ResumeCheck
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import IncompleteSourceResumeError
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import RunSourceLifecycleState, runs_table, token_outcomes_table
from tests.fixtures.landscape import expire_leader_seat, leader_coordination_token, register_test_node, register_test_worker

runner = CliRunner()

RUN_ID = "run-leaderless-cli"
LEADER_WORKER_ID = f"worker:{RUN_ID}:dead-leader"
FOLLOWER_WORKER_ID = f"worker:{RUN_ID}:follower"
_TOPOLOGY_HASH = "a" * 64


def _write_settings(tmp_path: Path, db_path: Path) -> Path:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        f"""
sources:
  primary:
    plugin: csv
    on_success: default
    options:
      path: input.csv
      on_validation_failure: discard
      schema: {{mode: observed}}
sinks:
  default:
    plugin: json
    on_write_failure: discard
    options:
      path: output.json
      schema: {{mode: observed}}
landscape:
  url: "sqlite:///{db_path}"
"""
    )
    return settings_file


def _seed_leaderless_run(
    db: LandscapeDB,
    *,
    lifecycle_state: RunSourceLifecycleState = RunSourceLifecycleState.LOADING,
    expire_seat: bool = True,
    token_count: int = 3,
) -> list[str]:
    factory = RecorderFactory(db)
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=RUN_ID,
        leader_worker_id=LEADER_WORKER_ID,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    source_node_id = register_test_node(factory.data_flow, RUN_ID, "source-node", node_type=NodeType.SOURCE, plugin_name="source")
    factory.run_lifecycle.record_run_source(
        source_node_id=source_node_id,
        source_name="primary",
        plugin_name="source",
        config_hash="c" * 64,
        lifecycle_state=lifecycle_state,
        coordination_token=leader_coordination_token(factory, RUN_ID),
    )
    CheckpointManager(db).create_checkpoint(
        draft=CheckpointDraft(run_id=RUN_ID, sequence_number=0, upstream_topology_hash=_TOPOLOGY_HASH),
        coordination_token=leader_coordination_token(factory, RUN_ID),
    )
    token_ids: list[str] = []
    for index in range(token_count):
        _row, token = factory.data_flow.create_row_with_token(
            source_node_id,
            index,
            {"value": index},
            source_row_index=index,
            ingest_sequence=index,
            coordination_token=leader_coordination_token(factory, RUN_ID),
        )
        token_ids.append(token.token_id)
    register_test_worker(db, run_id=RUN_ID, worker_id=FOLLOWER_WORKER_ID)
    if expire_seat:
        expire_leader_seat(db, RUN_ID)
    return token_ids


@pytest.fixture
def leaderless(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    db_path = tmp_path / "audit.db"
    with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
        token_ids = _seed_leaderless_run(db)
    return _write_settings(tmp_path, db_path), db_path, token_ids


def _run_status(db_path: Path) -> str:
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db, db.connection() as conn:
        return str(conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == RUN_ID)).scalar_one())


def _abandoned_count(db_path: Path) -> int:
    with LandscapeDB.from_url(f"sqlite:///{db_path}", create_tables=False) as db, db.connection() as conn:
        return len(
            conn.execute(
                select(token_outcomes_table.c.token_id)
                .where(token_outcomes_table.c.run_id == RUN_ID)
                .where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
            ).fetchall()
        )


class TestAbandonDryRun:
    def test_reports_the_leaderless_state_and_mutates_nothing(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, db_path, token_ids = leaderless

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file)])

        assert result.exit_code == 0, result.output
        assert "running" in result.output
        assert LEADER_WORKER_ID in result.output
        assert "primary=loading" in result.output
        assert "cannot be resumed" in result.output
        assert f"Undecided tokens: {len(token_ids)}" in result.output
        assert "--execute" in result.output
        assert _run_status(db_path) == "running"
        assert _abandoned_count(db_path) == 0

    def test_json_preflight_event(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, db_path, token_ids = leaderless

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["event"] == "abandon_preflight"
        assert payload["run_id"] == RUN_ID
        assert payload["run_status"] == "running"
        assert payload["seat_live"] is False
        assert payload["leader_worker_id"] == LEADER_WORKER_ID
        assert payload["source_lifecycle"] == {"primary": "loading"}
        assert payload["resumable"] is False
        assert payload["undecided_tokens"] == len(token_ids)
        assert payload["admissible"] is True
        assert _run_status(db_path) == "running"

    def test_explicit_database_overrides_settings_url(self, leaderless: tuple[Path, Path, list[str]], tmp_path: Path) -> None:
        _settings_file, db_path, _token_ids = leaderless
        # Settings that point at a database which does not exist: --database must win.
        wrong_dir = tmp_path / "wrong"
        wrong_dir.mkdir()
        wrong_settings = _write_settings(wrong_dir, wrong_dir / "missing.db")

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(wrong_settings), "--database", str(db_path)])

        assert result.exit_code == 0, result.output
        assert "primary=loading" in result.output


class TestAbandonExecute:
    def test_finalizes_interrupted_and_abandons_undecided_tokens(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, db_path, token_ids = leaderless

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute"])

        assert result.exit_code == 0, result.output
        assert "interrupted" in result.output
        assert f"Tokens abandoned: {len(token_ids)}" in result.output
        assert _run_status(db_path) == "interrupted"
        assert _abandoned_count(db_path) == len(token_ids)

    def test_json_abandoned_event(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, db_path, token_ids = leaderless

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["event"] == "abandoned"
        assert payload["run_id"] == RUN_ID
        assert payload["run_status"] == "interrupted"
        assert payload["abandoned_tokens"] == len(token_ids)
        assert payload["leader_epoch"] == 2
        assert _run_status(db_path) == "interrupted"

    def test_second_execute_is_refused_as_terminal(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, db_path, _token_ids = leaderless
        first = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute"])
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute"])

        assert second.exit_code == 1, second.output
        assert "interrupted" in second.output
        assert _run_status(db_path) == "interrupted"


class TestAbandonRefusals:
    def test_live_seat_is_refused_without_mutation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "audit.db"
        with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
            _seed_leaderless_run(db, expire_seat=False)
        settings_file = _write_settings(tmp_path, db_path)

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--execute"])

        assert result.exit_code == 1, result.output
        assert LEADER_WORKER_ID in result.output
        assert "elspeth join" in result.output
        assert _run_status(db_path) == "running"
        assert _abandoned_count(db_path) == 0

    def test_live_seat_json_refusal(self, tmp_path: Path) -> None:
        db_path = tmp_path / "audit.db"
        with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
            _seed_leaderless_run(db, expire_seat=False)
        settings_file = _write_settings(tmp_path, db_path)

        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(settings_file), "--format", "json"])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["event"] == "abandon_refused"
        assert payload["run_id"] == RUN_ID
        assert LEADER_WORKER_ID in payload["reason"]

    def test_missing_run_is_refused(self, leaderless: tuple[Path, Path, list[str]]) -> None:
        settings_file, _db_path, _token_ids = leaderless

        result = runner.invoke(app, ["abandon", "run-nope", "--settings", str(settings_file)])

        assert result.exit_code == 1, result.output
        assert "not found" in result.output

    def test_missing_settings_is_an_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["abandon", RUN_ID, "--settings", str(tmp_path / "absent.yaml")])

        assert result.exit_code == 1, result.output
        assert "Settings file not found" in result.output


class TestLeaderlessRunGuidance:
    """The follower's seat-dead exit must direct the operator at a verb that can succeed."""

    def test_loading_source_directs_to_abandon(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db)

        _emit_leaderless_run_guidance(db, RUN_ID)

        out = capsys.readouterr().out
        assert "cannot be resumed" in out
        assert "primary=loading" in out
        assert f"elspeth abandon {RUN_ID} --execute" in out
        assert "elspeth resume" not in out

    def test_exhausted_source_directs_to_resume(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db, lifecycle_state=RunSourceLifecycleState.EXHAUSTED)

        _emit_leaderless_run_guidance(db, RUN_ID)

        out = capsys.readouterr().out
        assert f"elspeth resume {RUN_ID} --execute" in out
        assert "elspeth abandon" not in out

    def test_guidance_never_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        db.close()

        _emit_leaderless_run_guidance(db, RUN_ID)

        out = capsys.readouterr().out
        assert "Resumability check failed" in out
        assert f"elspeth resume {RUN_ID}" in out


class TestResumeRefusalAbandonHint:
    """A resume refused on source lifecycle names ``abandon`` only when the run is leaderless."""

    def _error(self) -> IncompleteSourceResumeError:
        return IncompleteSourceResumeError(RUN_ID, {"primary": "loading"})

    def test_leaderless_running_run_gets_the_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db)

        _emit_not_resumable_event(self._error(), "console", db=db)

        err = capsys.readouterr().err
        assert "Cannot resume run" in err
        assert f"elspeth abandon {RUN_ID} --execute" in err

    def test_leaderless_running_run_json_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db)

        _emit_not_resumable_event(self._error(), "json", db=db)

        payload = json.loads(capsys.readouterr().err.strip())
        assert payload["event"] == "not_resumable"
        assert payload["hint"] == f"elspeth abandon {RUN_ID} --execute"

    def test_interrupted_run_gets_no_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A leader that shut down gracefully already finalized: abandon would refuse."""
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db)
        with db.engine.begin() as conn:
            conn.execute(
                update(runs_table)
                .where(runs_table.c.run_id == RUN_ID)
                .values(status=RunStatus.INTERRUPTED.value, completed_at=datetime.now(UTC))
            )

        _emit_not_resumable_event(self._error(), "console", db=db)

        assert "elspeth abandon" not in capsys.readouterr().err

    def test_live_leader_gets_no_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        _seed_leaderless_run(db, expire_seat=False)

        _emit_not_resumable_event(self._error(), "console", db=db)

        assert "elspeth abandon" not in capsys.readouterr().err

    def test_without_a_database_handle_no_hint_is_attempted(self, capsys: pytest.CaptureFixture[str]) -> None:
        _emit_not_resumable_event(self._error(), "console")

        err = capsys.readouterr().err
        assert "Cannot resume run" in err
        assert "elspeth abandon" not in err

    def test_hint_lookup_failure_is_reported_not_raised(self, capsys: pytest.CaptureFixture[str]) -> None:
        db = LandscapeDB.in_memory()
        db.close()

        _emit_not_resumable_event(self._error(), "console", db=db)

        err = capsys.readouterr().err
        assert "Cannot resume run" in err
        assert "Leaderless-run check failed" in err
        assert f"probe with: elspeth abandon {RUN_ID}" in err


class TestResumePreflightAbandonHint:
    """The ``can_resume`` pre-flight is the refusal an operator actually sees
    for a leaderless run (measured live on examples/multi_worker): the source
    gate fires there, before ``--execute``. It must carry the abandon hint."""

    def test_preflight_refusal_names_abandon_for_a_leaderless_run(
        self, leaderless: tuple[Path, Path, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings_file, _db_path, _token_ids = leaderless
        from elspeth.core.checkpoint import RecoveryManager

        # The fixture's checkpoint carries a synthetic topology hash, so the
        # topology check (which precedes the source gate inside can_resume)
        # would refuse first; pin the pre-flight verdict to the source-gate
        # refusal the live run produced.
        monkeypatch.setattr(
            RecoveryManager,
            "can_resume",
            lambda self, run_id, graph: ResumeCheck(can_resume=False, reason="source lifecycle is incomplete (primary=loading)"),
        )

        result = runner.invoke(app, ["resume", RUN_ID, "--settings", str(settings_file)])

        assert result.exit_code == 1, result.output
        assert f"Cannot resume run {RUN_ID}: source lifecycle is incomplete" in result.output
        assert f"The run has no live leader. Finalize it with: elspeth abandon {RUN_ID} --execute" in result.output

    def test_preflight_refusal_on_a_live_led_run_names_no_abandon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "audit.db"
        with LandscapeDB.from_url(f"sqlite:///{db_path}") as db:
            _seed_leaderless_run(db, expire_seat=False)
        settings_file = _write_settings(tmp_path, db_path)
        from elspeth.core.checkpoint import RecoveryManager

        monkeypatch.setattr(
            RecoveryManager,
            "can_resume",
            lambda self, run_id, graph: ResumeCheck(can_resume=False, reason="Run is in progress under live leader"),
        )

        result = runner.invoke(app, ["resume", RUN_ID, "--settings", str(settings_file)])

        assert result.exit_code == 1, result.output
        assert "elspeth abandon" not in result.output
