"""A collector group's FAILED verdict on PostgreSQL (elspeth-5887fb7928 CODEX-R2).

The SQLite suite (``tests/integration/pipeline/test_collector_failure_verdict.py``)
proves the verdict is one transaction and final on resume. This is the
PostgreSQL twin: ``complete_collector_failure`` locks the member tokens and
every state ``FOR UPDATE`` (a no-op on SQLite's serialised writer) and the
restore reads the verdict back through the read model. Run with
``pytest -m testcontainer -n 0``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts
from tests.integration.pipeline.test_collector_failure_verdict import (
    _COLLECTOR_PIPELINE,
    _DOCS,
    _collector_hold_statuses,
    _Crash,
    _flip_first_call,
    _inject,
)

from elspeth.contracts.enums import NodeStateStatus, RunStatus
from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


def test_resume_completes_a_recorded_collector_verdict_on_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash after the verdict, then again before the resumed release: the third run completes it once, without the plugin."""
    with postgres_test_target(driver="psycopg") as control_url, postgres_test_target(driver="psycopg") as crashed_url:
        control_db = LandscapeDB.from_url(control_url)
        db = LandscapeDB.from_url(crashed_url)
        assert db.engine.dialect.name == "postgresql"
        try:
            control_env = build_pipeline(tmp_path / "control", _COLLECTOR_PIPELINE, _DOCS, db=control_db)
            _flip_first_call(monkeypatch, "returned_error")
            assert run_pipeline(control_env).status is RunStatus.FAILED

            env = build_pipeline(tmp_path / "crashed", _COLLECTOR_PIPELINE, _DOCS, db=db)
            calls = _flip_first_call(monkeypatch, "returned_error")
            with monkeypatch.context() as crash_patch:
                fired = _inject(crash_patch, "after_verdict")
                with pytest.raises(_Crash):
                    run_pipeline(env)
            assert fired == ["after_verdict"]
            assert _collector_hold_statuses(env) == [NodeStateStatus.FAILED.value] * 3
            with monkeypatch.context() as crash_patch:
                fired = _inject(crash_patch, "before_release")
                with pytest.raises(_Crash):
                    resume_pipeline(env)
            assert fired == ["before_release"]

            resumed = resume_pipeline(env)

            assert len(calls) == 1
            assert resumed.status is RunStatus.FAILED
            assert terminal_counts(db) == terminal_counts(control_db)
        finally:
            db.close()
            control_db.close()
