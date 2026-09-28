"""A source-quarantined row survives a real crash: resume finishes it once, with its original error.

Before the fenced quarantine ingest, a quarantined row had no durable work
item: after a crash a fixed-schema resume died restoring it (exit 4, valid
tokens trapped), and an observed-schema resume re-validated the rejected row,
published it to the SUCCESS sink and stamped the run ``completed`` (exit 0)
with the quarantine token outcomeless. Every test here is a real run crashed
at a named window, then a real resume (see ``quarantine_resume_matrix``).
The PostgreSQL twin is
``tests/testcontainer/core/test_quarantine_resume_crash_matrix_postgres.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import runs_table, token_outcomes_table, token_work_items_table, tokens_table
from tests.integration.pipeline.quarantine_resume_matrix import (
    KINDS,
    scenario_crash_then_resume,
    scenario_mid_ingest_death_is_refused_then_abandoned,
    scenario_quarantine_storm_resumes_every_parked_handoff,
    write_settings,
)


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'audit.db'}"


@pytest.mark.parametrize("window_name", ["W1", "W2", "W3"])
@pytest.mark.parametrize("kind_name", sorted(KINDS))
def test_every_kind_resumes_to_one_quarantine_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind_name: str, window_name: str
) -> None:
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name=kind_name, window_name=window_name, process_death=False, db_url=_sqlite_url(tmp_path)
    )


@pytest.mark.parametrize("kind_name", sorted(KINDS))
def test_every_kind_resumes_after_process_death_past_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind_name: str) -> None:
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name=kind_name, window_name="W3", process_death=True, db_url=_sqlite_url(tmp_path)
    )


@pytest.mark.parametrize("kind_name", ["json_type", "json_drift"])
def test_finalized_effect_with_open_handoff_is_terminalized_without_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind_name: str
) -> None:
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name=kind_name, window_name="W4", process_death=False, db_url=_sqlite_url(tmp_path)
    )


def test_regression_3pt_fixed_schema_other_sink_fails_first_resumes_every_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane's 3pt shape: before the fix, resume exited 4 at recovery.py:699 and trapped the valid tokens."""
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name="json_parse", window_name="W1", process_death=False, db_url=_sqlite_url(tmp_path)
    )


@pytest.mark.parametrize("window_name", ["W2", "W3"])
def test_regression_json_drift_rejected_row_never_reaches_the_success_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window_name: str
) -> None:
    """Before the fix, resume re-validated the rejected row, published it to ``out`` as SUCCESS and exited 0."""
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name="json_drift", window_name=window_name, process_death=False, db_url=_sqlite_url(tmp_path)
    )


@pytest.mark.parametrize("process_death", [False, True], ids=["raise", "process_death"])
@pytest.mark.parametrize("kind_name", ["json_type", "json_drift"])
def test_mid_ingest_death_is_refused_then_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind_name: str, process_death: bool
) -> None:
    scenario_mid_ingest_death_is_refused_then_abandoned(
        tmp_path, monkeypatch, kind_name=kind_name, process_death=process_death, db_url=_sqlite_url(tmp_path)
    )


def test_quarantine_storm_resumes_every_parked_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_quarantine_storm_resumes_every_parked_handoff(tmp_path, monkeypatch, rows=200, db_url=_sqlite_url(tmp_path))


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_replayed_quarantined_row_takes_the_same_durable_handoff(tmp_path: Path, mode: str) -> None:
    """A quarantined row replayed from a completed run is parked and finished once, with the live error hash, publishing nothing."""
    db_url = _sqlite_url(tmp_path)
    settings = write_settings(tmp_path, KINDS["json_type"], db_url)
    runner = CliRunner()
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings), "--execute", "--format", "json"])
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    published = {path: path.read_bytes() for path in (tmp_path / "out" / "out.jsonl", tmp_path / "bad" / "bad.jsonl")}
    config = yaml.safe_load(settings.read_text(encoding="utf-8"))
    config["run_mode"] = mode
    config["replay_from"] = live_run_id
    config["concurrency"] = {"max_workers": 1}
    settings.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    replayed = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings), "--execute", "--format", "json"])
    replay_run_id = json.loads(replayed.output.strip().splitlines()[-1])["run_id"]
    assert replay_run_id != live_run_id
    assert {path: path.read_bytes() for path in published} == published, "a non-live run published"
    db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        by_run: dict[str, list[Any]] = {}
        with db.connection() as conn:
            for run_id in (live_run_id, replay_run_id):
                status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
                assert status == RunStatus.COMPLETED_WITH_FAILURES.value, (run_id, status)
                token_ids = set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).scalars())
                outcomes = conn.execute(
                    select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_id).where(token_outcomes_table.c.completed == 1)
                ).all()
                assert sorted(str(o.token_id) for o in outcomes) == sorted(token_ids)
                open_items = conn.execute(
                    select(token_work_items_table.c.work_item_id)
                    .where(token_work_items_table.c.run_id == run_id)
                    .where(token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value)
                ).all()
                assert open_items == []
                by_run[run_id] = [o for o in outcomes if o.path == TerminalPath.QUARANTINED_AT_SOURCE.value]
        (live_quarantine,) = by_run[live_run_id]
        (replay_quarantine,) = by_run[replay_run_id]
        assert (replay_quarantine.outcome, replay_quarantine.sink_name) == (TerminalOutcome.FAILURE.value, "bad")
        assert replay_quarantine.error_hash == live_quarantine.error_hash
    finally:
        db.close()
