"""Resume's coverage check refuses a run whose tokens it cannot account for.

Resume re-drives only durable scheduler work and never re-derives a row, so a
resumable run must account for every token: a completed outcome, or a READY /
LEASED / BLOCKED / PENDING_SINK work item. These are CORRUPTION-DETECTOR tests:
the base state is a REAL crashed run (a crash injected at the first sink
reservation, so all four tokens — three valid rows and the quarantined one —
park PENDING_SINK), and each test then corrupts the store directly into a state
the fenced ingest cannot produce — a token with no work item, an ABANDONED
token, a decided-and-ABANDONED token. The real-crash resume
tests (no corruption) live elsewhere and never share this module.

The coverage refusal must be recorded value-free in Landscape (one
``resume_refused`` coordination event: cause, count, the sorted head of the
token ids), must re-drive nothing, and must leave the run
resumable-but-refusing (lane ruling M2): a second resume refuses again.
ABANDONED tokens are refused by the status derive's ADR-038 belt before any
re-drive; the coverage check leaves them to it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import delete, insert, select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    run_coordination_events_table,
    runs_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam, SinkEffectInjectedFault
from elspeth.engine.orchestrator import resume as resume_module
from elspeth.engine.orchestrator.resume import ResumeCoordinator

_ROWS = ('{"id": 1, "line": "a"}', '{"id": 2, "line": "b"}', '{"id": "x", "line": "bad"}', '{"id": 3, "line": "c"}')


def _settings(tmp_path: Path, db_url: str) -> Path:
    data = tmp_path / "d.jsonl"
    data.write_text("\n".join(_ROWS) + "\n", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    config: dict[str, Any] = {
        "sources": {
            "src": {
                "plugin": "json",
                "on_success": "out",
                "options": {
                    "path": str(data),
                    "format": "jsonl",
                    "schema": {"mode": "fixed", "fields": ["id: int", "line: str"]},
                    "on_validation_failure": "bad",
                },
            }
        },
        "sinks": {
            name: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / name / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
            for name in ("out", "bad")
        },
        "landscape": {"url": db_url, "backend": "postgresql" if db_url.startswith("postgresql") else "sqlite"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config), encoding="utf-8")
    return settings


def _crashed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_url: str) -> tuple[Path, LandscapeDB, str]:
    """A real ``elspeth run`` that crashes at the first sink reservation: every token parks PENDING_SINK.

    The crash is injected at the coordinator's ``BEFORE_RESERVATION`` seam (the
    in-repo ``SinkEffectExecutionSeam`` fault point), so no effect is reserved
    and resume does not wait out an effect lease.
    """
    settings = _settings(tmp_path, db_url)

    def fail_at_reservation(self: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
        if seam is SinkEffectExecutionSeam.BEFORE_RESERVATION:
            raise SinkEffectInjectedFault(seam)

    with monkeypatch.context() as patched:
        patched.setattr(SinkEffectCoordinator, "_fault", fail_at_reservation)
        result = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    assert result.exit_code != 0, result.output
    assert "injected sink-effect fault at before_reservation" in result.output
    db = LandscapeDB.from_url(db_url, create_tables=False)
    with db.connection() as conn:
        run_id = str(conn.execute(select(runs_table.c.run_id)).scalar_one())
        statuses = conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id)).scalars().all()
        token_count = len(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).all())
    # The crash image resume is built for: four tokens (three valid, one
    # quarantined), each covered by its own PENDING_SINK handoff.
    assert token_count == 4
    assert sorted(statuses) == [TokenWorkStatus.PENDING_SINK.value] * 4
    return settings, db, run_id


def _resume(settings: Path, run_id: str) -> Any:
    return CliRunner().invoke(app, ["resume", run_id, "-s", str(settings), "--execute"])


def _refusal_events(db: LandscapeDB, run_id: str) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(run_coordination_events_table.c.context_json)
            .where(run_coordination_events_table.c.run_id == run_id)
            .where(run_coordination_events_table.c.event_type == "resume_refused")
            .order_by(run_coordination_events_table.c.seq)
        ).all()
    return [json.loads(row.context_json) for row in rows]


def _outcome_rows(db: LandscapeDB, run_id: str) -> list[tuple[str, str | None, str, int]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(
                token_outcomes_table.c.token_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
                token_outcomes_table.c.completed,
            ).where(token_outcomes_table.c.run_id == run_id)
        ).all()
    return sorted(
        ((str(row.token_id), row.outcome, str(row.path), int(row.completed)) for row in rows),
        key=lambda row: (row[0], row[2]),
    )


def _run_status(db: LandscapeDB, run_id: str) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one())


def _first_pending_token(db: LandscapeDB, run_id: str) -> str:
    """The first (by id) undecided token: covered only by its PENDING_SINK handoff."""
    with db.connection() as conn:
        return str(
            conn.execute(
                select(token_work_items_table.c.token_id)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == TokenWorkStatus.PENDING_SINK.value)
                .order_by(token_work_items_table.c.token_id)
            )
            .scalars()
            .first()
        )


def _insert_outcome(db: LandscapeDB, run_id: str, token_id: str, *, outcome_id: str, decided: bool) -> None:
    values: dict[str, Any] = {
        "outcome_id": outcome_id,
        "run_id": run_id,
        "token_id": token_id,
        "recorded_at": datetime.now(UTC),
        "context_json": "{}",
    }
    if decided:
        values |= {"outcome": TerminalOutcome.SUCCESS.value, "path": TerminalPath.DEFAULT_FLOW.value, "completed": 1, "sink_name": "out"}
    else:
        values |= {"outcome": None, "path": TerminalPath.ABANDONED.value, "completed": 0}
    with db.engine.begin() as conn:
        conn.execute(insert(token_outcomes_table).values(**values))


_CAUSE = "uncovered_undecided_tokens"


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'audit.db'}"


def scenario_token_without_work_item_or_outcome_is_refused_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str
) -> None:
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        victim = _first_pending_token(db, run_id)
        with db.engine.begin() as conn:
            conn.execute(delete(token_work_items_table).where(token_work_items_table.c.token_id == victim))
        outcomes_before = _outcome_rows(db, run_id)

        first = _resume(settings, run_id)
        # The CLI renders the Tier-1 refusal as FATAL, exit 4.
        assert first.exit_code == 4, first.output
        assert f"AuditIntegrityError: Resume of run {run_id!r} refused: 1 token(s)" in first.output
        assert victim in first.output
        assert _refusal_events(db, run_id) == [{"cause": _CAUSE, "first_token_ids": [victim], "token_count": 1}]
        # Nothing was re-driven or published, and the run stays FAILED.
        assert _outcome_rows(db, run_id) == outcomes_before
        assert not (tmp_path / "out" / "out.jsonl").exists()
        assert _run_status(db, run_id) == RunStatus.FAILED.value

        # Resumable-but-refusing (lane ruling M2): the same refusal, recorded again.
        second = _resume(settings, run_id)
        assert second.exit_code == 4, second.output
        assert [event["cause"] for event in _refusal_events(db, run_id)] == [_CAUSE, _CAUSE]
        assert _outcome_rows(db, run_id) == outcomes_before
    finally:
        db.close()


def scenario_run_with_no_work_left_is_refused_not_finalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str) -> None:
    """Every handoff lost: resume takes its no-work branch, which must refuse rather than finalize."""
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        with db.connection() as conn:
            undecided = sorted(
                conn.execute(select(token_work_items_table.c.token_id).where(token_work_items_table.c.run_id == run_id)).scalars().all()
            )
        with db.engine.begin() as conn:
            conn.execute(delete(token_work_items_table).where(token_work_items_table.c.run_id == run_id))

        result = _resume(settings, run_id)
        assert result.exit_code == 4, result.output
        assert _refusal_events(db, run_id) == [{"cause": _CAUSE, "first_token_ids": undecided, "token_count": 4}]
        assert _outcome_rows(db, run_id) == []
        assert _run_status(db, run_id) == RunStatus.FAILED.value
    finally:
        db.close()


def scenario_abandoned_token_is_refused_before_any_redrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str, decided: bool
) -> None:
    """ADR-038: an ABANDONED token (or one both decided and ABANDONED) is never re-driven.

    The status derive's ABANDONED belt refuses it on the resume path before
    any re-drive, so the coverage check leaves it to that belt and records
    nothing of its own.
    """
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        victim = _first_pending_token(db, run_id)
        _insert_outcome(db, run_id, victim, outcome_id="out-abandoned", decided=False)
        if decided:
            _insert_outcome(db, run_id, victim, outcome_id="out-decided", decided=True)
        outcomes_before = _outcome_rows(db, run_id)

        result = _resume(settings, run_id)
        assert result.exit_code == 4, result.output
        assert f"read an ABANDONED record for token {victim!r}" in result.output
        assert _refusal_events(db, run_id) == []
        assert _outcome_rows(db, run_id) == outcomes_before
        assert not (tmp_path / "out" / "out.jsonl").exists()
    finally:
        db.close()


def scenario_coverage_is_evaluated_under_the_won_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str) -> None:
    """The check reads the store AFTER the seat CAS: a token uncovered after the win is refused."""
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        victim = _first_pending_token(db, run_id)
        real_acquire = ResumeCoordinator._acquire_resume_leadership

        def acquire_then_lose_the_item(self: ResumeCoordinator, snapshot: Any) -> Any:
            token = real_acquire(self, snapshot)
            with db.engine.begin() as conn:
                conn.execute(delete(token_work_items_table).where(token_work_items_table.c.token_id == victim))
            return token

        monkeypatch.setattr(ResumeCoordinator, "_acquire_resume_leadership", acquire_then_lose_the_item)
        result = _resume(settings, run_id)
        assert result.exit_code == 4, result.output
        assert _refusal_events(db, run_id) == [{"cause": _CAUSE, "first_token_ids": [victim], "token_count": 1}]
    finally:
        db.close()


def scenario_uncorrupted_crash_resumes_to_every_token_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str) -> None:
    """Negative control: the same real crash, uncorrupted, resumes cleanly with no refusal recorded."""
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        result = _resume(settings, run_id)
        # Exit 1 is COMPLETED_WITH_FAILURES (the quarantined row), not an error.
        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert _refusal_events(db, run_id) == []
        assert _run_status(db, run_id) == RunStatus.COMPLETED_WITH_FAILURES.value
        outcomes = _outcome_rows(db, run_id)
        assert len(outcomes) == 4
        assert all(completed == 1 for _token, _outcome, _path, completed in outcomes)
        assert sorted(path for _token, _outcome, path, _completed in outcomes) == sorted(
            [TerminalPath.DEFAULT_FLOW.value] * 3 + [TerminalPath.QUARANTINED_AT_SOURCE.value]
        )
    finally:
        db.close()


def scenario_success_stamp_refuses_an_undecided_token_past_the_coverage_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str
) -> None:
    """QR-4 is its own backstop: with the coverage check bypassed, the success stamp still refuses.

    The same real crash, one token's handoff lost, and ``refuse_unaccounted_resume``
    replaced by a no-op (a hole in the coverage check, or a future sink-bound
    path that skips the fenced handoff). The other three tokens re-drive and
    publish, quiescence holds, and the completion statement's undecided-token
    arm must still refuse to stamp the run successful over the outcomeless one.
    """
    settings, db, run_id = _crashed_run(tmp_path, monkeypatch, db_url)
    try:
        victim = _first_pending_token(db, run_id)
        with db.engine.begin() as conn:
            conn.execute(delete(token_work_items_table).where(token_work_items_table.c.token_id == victim))
        monkeypatch.setattr(resume_module, "refuse_unaccounted_resume", lambda factory, coordination_token: None)

        result = _resume(settings, run_id)
        assert result.exit_code != 0, result.output
        assert "1 token(s) have no completed terminal outcome" in result.output
        assert victim in result.output
        assert _run_status(db, run_id) == RunStatus.FAILED.value
        assert victim not in {token for token, _outcome, _path, completed in _outcome_rows(db, run_id) if completed == 1}
    finally:
        db.close()


# SQLite runs of the scenarios; tests/testcontainer/core/test_resume_coverage_refusal_postgres.py
# runs the same scenarios against PostgreSQL.


def test_token_without_work_item_or_outcome_is_refused_and_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_token_without_work_item_or_outcome_is_refused_and_recorded(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path))


def test_coverage_is_evaluated_under_the_won_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_coverage_is_evaluated_under_the_won_seat(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path))


def test_uncorrupted_crash_resumes_to_every_token_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_uncorrupted_crash_resumes_to_every_token_terminal(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path))


@pytest.mark.parametrize("decided", [False, True], ids=["abandoned", "decided-and-abandoned"])
def test_abandoned_token_is_refused_before_any_redrive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decided: bool) -> None:
    scenario_abandoned_token_is_refused_before_any_redrive(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path), decided=decided)


def test_run_with_no_work_left_is_refused_not_finalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_run_with_no_work_left_is_refused_not_finalized(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path))


def test_success_stamp_refuses_an_undecided_token_past_the_coverage_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario_success_stamp_refuses_an_undecided_token_past_the_coverage_check(tmp_path, monkeypatch, db_url=_sqlite_url(tmp_path))
