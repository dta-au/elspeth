# tests/integration/pipeline/test_landscape_bind_budget.py
"""No Landscape statement on the run path binds a parameter per row.

A statement whose bind count grows with a collection (an ``IN`` list over the
tokens of one sink write, the members of an aggregation batch, the children a
flush or an expansion emits) fails once the collection passes the database's
bind ceiling (SQLite 32,766, PostgreSQL 65,535). The run then aborts with exit
4 and every buffered token has no terminal outcome; ``batch_stats`` has no row
cap, so a count trigger or an end-of-source flush reaches that size on ordinary
data.

These runs lower SQLite's variable ceiling to 999 on every connection
(``lowered_sqlite_variable_limit``) and push 1,200 rows through each shape, so
SQLite itself refuses any statement that binds one parameter per row. Every
case is a real ``elspeth run --execute`` (the in-process CLI) with the built-in
plugins. ``tests/testcontainer/core/test_landscape_bind_budget_postgres.py``
runs the same cases on PostgreSQL, whose ceiling cannot be lowered, and
asserts instead on the most parameters any one statement bound during the run.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import operations_table, runs_table, token_outcomes_table, tokens_table
from tests.fixtures.landscape import lowered_sqlite_variable_limit
from tests.integration.pipeline.test_output_declaration_routing import _json_sink, _json_source, _read_jsonl, _write_jsonl

_VARIABLE_LIMIT = 999
_ROWS = 1200


@dataclass(frozen=True, slots=True)
class BindBudgetCase:
    """One run shape: its nodes, its input, and the recorded result it must reach."""

    aggregation: tuple[str, str, dict[str, Any]] | None
    explode: bool
    scored: bool
    outcomes: dict[tuple[str, str], int]
    status: str
    exit_code: int
    out_rows: int
    failed_rows: int


_STATS = {"value_field": "score", "compute_mean": True}
_RANK = {"value_field": "score"}


def bind_budget_cases(rows: int) -> dict[str, BindBudgetCase]:
    return {
        "plain_sink_write": BindBudgetCase(None, False, True, {("success", "default_flow"): rows}, "completed", 0, rows, 0),
        "batch_stats_transform_result": BindBudgetCase(
            ("batch_stats", "transform", _STATS),
            False,
            True,
            {("transient", "batch_consumed"): rows, ("success", "default_flow"): 1},
            "completed",
            0,
            1,
            0,
        ),
        "batch_stats_transform_failure_verdict": BindBudgetCase(
            ("batch_stats", "transform", _STATS), False, False, {("failure", "on_error_routed"): rows}, "failed", 2, 0, rows
        ),
        "batch_rank_passthrough_result": BindBudgetCase(
            ("batch_rank", "passthrough", _RANK), False, True, {("success", "default_flow"): rows}, "completed", 0, rows, 0
        ),
        "batch_rank_transform_children": BindBudgetCase(
            ("batch_rank", "transform", _RANK),
            False,
            True,
            {("transient", "batch_consumed"): rows, ("success", "default_flow"): rows},
            "completed",
            0,
            rows,
            0,
        ),
        "json_explode_children": BindBudgetCase(
            None, True, True, {("transient", "expand_parent"): 1, ("success", "default_flow"): rows}, "completed", 0, rows, 0
        ),
    }


def run_bind_budget_case(tmp_path: Path, case: BindBudgetCase, *, rows: int, landscape_url: str) -> None:
    """Run ``case`` over ``rows`` rows and assert the recorded result: every token terminal, no error text."""
    if case.explode:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 0, "items": list(range(rows))}])
    else:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": index, "score": float(index % 97) if case.scored else None} for index in range(rows)])
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl", on_success="rows" if case.aggregation or case.explode else "out")},
        "concurrency": {"max_workers": 1},
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl")},
        "landscape": {"backend": "postgresql" if landscape_url.startswith("postgresql") else "sqlite", "url": landscape_url},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    if case.aggregation is not None:
        plugin, output_mode, options = case.aggregation
        settings["aggregations"] = [
            {
                "name": "agg",
                "plugin": plugin,
                "input": "rows",
                "on_success": "out",
                "on_error": "failed",
                "trigger": {"count": rows},
                "output_mode": output_mode,
                "options": {"schema": {"mode": "observed"}, **options},
            }
        ]
        settings["sinks"]["failed"] = _json_sink(tmp_path / "failed.jsonl")
    if case.explode:
        settings["transforms"] = [
            {
                "name": "explode",
                "plugin": "json_explode",
                "input": "rows",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, "array_field": "items"},
            }
        ]
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))

    result = CliRunner().invoke(app, ["run", "-s", str(settings_path), "--execute"])

    assert result.exit_code == case.exit_code, result.output
    engine = create_engine(landscape_url)
    try:
        with engine.connect() as conn:
            run_id, status = conn.execute(select(runs_table.c.run_id, runs_table.c.status).order_by(runs_table.c.started_at.desc())).first()
            assert status == case.status
            terminal_count = (
                select(func.count())
                .select_from(token_outcomes_table)
                .where(token_outcomes_table.c.token_id == tokens_table.c.token_id, token_outcomes_table.c.completed == 1)
                .scalar_subquery()
            )
            not_single_terminal = conn.execute(
                select(func.count()).select_from(tokens_table).where(tokens_table.c.run_id == run_id, terminal_count != 1)
            ).scalar_one()
            assert not_single_terminal == 0
            outcomes = {
                (outcome, path): count
                for outcome, path, count in conn.execute(
                    select(token_outcomes_table.c.outcome, token_outcomes_table.c.path, func.count())
                    .where(token_outcomes_table.c.run_id == run_id, token_outcomes_table.c.completed == 1)
                    .group_by(token_outcomes_table.c.outcome, token_outcomes_table.c.path)
                )
            }
            assert outcomes == case.outcomes
            failed_operations = conn.execute(
                select(func.count())
                .select_from(operations_table)
                .where(operations_table.c.run_id == run_id, operations_table.c.error_message.is_not(None))
            ).scalar_one()
            assert failed_operations == 0
    finally:
        engine.dispose()
    assert len(_read_jsonl(tmp_path / "out.jsonl")) == case.out_rows
    assert len(_read_jsonl(tmp_path / "failed.jsonl")) == case.failed_rows


@pytest.fixture
def lowered_variable_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with lowered_sqlite_variable_limit(monkeypatch, _VARIABLE_LIMIT):
        yield


def test_lowered_ceiling_refuses_a_statement_that_binds_one_parameter_per_row(lowered_variable_limit: None) -> None:
    """Control: under the fixture a Landscape connection refuses a 1,200-bind statement."""
    db = LandscapeDB.in_memory()
    try:
        with db.connection() as conn, pytest.raises(OperationalError, match="too many SQL variables"):
            conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.token_id.in_([f"t{index}" for index in range(_ROWS)])))
    finally:
        db.close()


@pytest.mark.parametrize("case", sorted(bind_budget_cases(_ROWS)))
def test_run_path_statements_do_not_bind_per_row(tmp_path: Path, lowered_variable_limit: None, case: str) -> None:
    run_bind_budget_case(tmp_path, bind_budget_cases(_ROWS)[case], rows=_ROWS, landscape_url=f"sqlite:///{tmp_path / 'audit.db'}")
