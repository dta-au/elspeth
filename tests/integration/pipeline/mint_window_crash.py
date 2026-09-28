"""Shared scenarios: a process death inside a mint window is resumed, not refused.

Fork, expand and collect commit their product tokens (and the producer's own
outcome) BEFORE the producing work completes; that completion, or the barrier
release, emits the products' work items in one transaction. A process that
dies between the two commits leaves products with no outcome and no item of
their own while their producer is still open work (LEASED, or BLOCKED at a
barrier). Re-driving the producer reconciles the committed products
(``_reconcile_*_replay``) and emits their items, so the run resumes to the
image a clean run leaves. Resume's coverage check counts such a product as
covered by its open producer; it refused them before (review QR r1, F1).

Every scenario is a REAL ``elspeth run --execute`` in a spawned child whose
process dies (``os._exit``, no ceremony) right after the named
``RowTokenRepository`` mint verb commits, then a real ``elspeth resume
--execute``. The dead run's leader seat and item leases are written into the
database's past through the ADR-047 fixtures instead of sleeping them out; no
token, outcome or work item is written by hand. Every mint runs after the
source is EXHAUSTED (an end-of-source aggregation feeds it): a death while the
source is still loading is refused by the source-lifecycle belt instead.

Module-level names only: the process-death child is spawned and re-imports
this module.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.data_flow.tokens import RowTokenRepository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    runs_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import expire_leader_seat, expire_lease

_CSV = "id,category,amount\n1,a,10\n2,a,20\n3,b,5\n4,b,7\n"
_OPEN = (TokenWorkStatus.READY.value, TokenWorkStatus.LEASED.value, TokenWorkStatus.BLOCKED.value, TokenWorkStatus.PENDING_SINK.value)


def _aggregation(on_success: str) -> dict[str, Any]:
    """End-of-source batch_stats per category: the downstream mint runs after the source is EXHAUSTED."""
    return {
        "name": "totals",
        "plugin": "batch_stats",
        "input": "batch_in",
        "on_success": on_success,
        "on_error": "discard",
        "trigger": {"count": 100},
        "output_mode": "transform",
        "options": {"schema": {"mode": "observed"}, "value_field": "amount", "group_by": "category"},
    }


def _fork_pipeline() -> dict[str, Any]:
    return {
        "aggregations": [_aggregation("agg_out")],
        "gates": [
            {
                "name": "fork_gate",
                "input": "agg_out",
                "condition": "True",
                "routes": {"true": "fork", "false": "out"},
                "fork_to": ["path_a", "path_b"],
            },
            {"name": "route_output", "input": "merge_results", "condition": "True", "routes": {"true": "out", "false": "out"}},
        ],
        "coalesce": [{"name": "merge_results", "branches": ["path_a", "path_b"], "policy": "require_all", "merge": "nested"}],
    }


def _scope_pipeline() -> dict[str, Any]:
    observed = {"mode": "observed"}
    return {
        "concurrency": {"max_workers": 1},
        "aggregations": [_aggregation("agg_out")],
        "transforms": [
            {
                "name": "make_pages",
                "plugin": "value_transform",
                "input": "agg_out",
                "on_success": "with_pages",
                "on_error": "discard",
                "options": {
                    "schema": observed,
                    "operations": [{"target": "pages", "expression": "[{'reading': row['sum']}, {'reading': row['count']}]"}],
                },
            },
            {
                "name": "explode_pages",
                "plugin": "json_explode",
                "input": "with_pages",
                "on_success": "page_in",
                "on_error": "discard",
                "options": {"array_field": "pages", "output_field": "page", "schema": observed},
            },
            {
                "name": "read_value",
                "plugin": "value_transform",
                "input": "page_in",
                "on_success": "pages",
                "on_error": "discard",
                "options": {"schema": observed, "operations": [{"target": "reading", "expression": "row['page']['reading'] + 0"}]},
            },
        ],
        "collectors": [
            {
                "name": "page_stitcher",
                "plugin": "batch_stats",
                "input": "pages",
                "on_success": "out",
                "options": {"value_field": "reading", "schema": observed},
            }
        ],
        "scopes": [{"name": "pages_scope", "opener": "explode_pages", "closer": "page_stitcher", "policy": "require_all"}],
    }


_FORK_ROWS = (
    {
        "path_a": {"batch_size": 2, "category": "a", "count": 2, "mean": 15, "sum": 30},
        "path_b": {"batch_size": 2, "category": "a", "count": 2, "mean": 15, "sum": 30},
    },
    {
        "path_a": {"batch_size": 2, "category": "b", "count": 2, "mean": 6, "sum": 12},
        "path_b": {"batch_size": 2, "category": "b", "count": 2, "mean": 6, "sum": 12},
    },
)
_SCOPE_ROWS = ({"batch_size": 2, "count": 2, "mean": 16, "sum": 32}, {"batch_size": 2, "count": 2, "mean": 7, "sum": 14})


@dataclass(frozen=True, slots=True)
class MintWindow:
    """One mint verb's window: which call dies, the crash image it must leave, the clean run's image."""

    verb: str
    pipeline: str
    call: int
    products: int
    producer_status: str
    clean_tokens: int
    clean_rows: tuple[dict[str, Any], ...]


# The call index aims each death at the named plugin's mint: the end-of-source
# aggregation flush is itself an expand (calls 1-2, one per category), so the
# json_explode expand is call 3. assert_crash_image checks the aim.
WINDOWS: dict[str, MintWindow] = {
    "fork": MintWindow("fork_token", "fork", 1, 2, TokenWorkStatus.LEASED.value, 12, _FORK_ROWS),
    "expand": MintWindow("expand_token", "scope", 3, 2, TokenWorkStatus.LEASED.value, 12, _SCOPE_ROWS),
    "collect": MintWindow("collect_tokens", "scope", 1, 1, TokenWorkStatus.BLOCKED.value, 12, _SCOPE_ROWS),
}


def write_settings(tmp_path: Path, pipeline: str, db_url: str) -> Path:
    (tmp_path / "in.csv").write_text(_CSV, encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    config: dict[str, Any] = {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "batch_in",
                "options": {
                    "path": str(tmp_path / "in.csv"),
                    "schema": {"mode": "fixed", "fields": ["id: int", "category: str", "amount: int"]},
                    "on_validation_failure": "discard",
                },
            }
        },
        **(_fork_pipeline() if pipeline == "fork" else _scope_pipeline()),
        "sinks": {
            "out": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / "out" / "out.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
        "landscape": {"url": db_url, "backend": "postgresql" if db_url.startswith("postgresql") else "sqlite"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config), encoding="utf-8")
    return settings


_MINT_VERBS: dict[str, Any] = {
    "fork_token": RowTokenRepository.fork_token,
    "expand_token": RowTokenRepository.expand_token,
    "collect_tokens": RowTokenRepository.collect_tokens,
}


def _die_after_mint_child(settings: str, verb: str, call: int) -> None:
    """Spawned child: a real run whose process dies (no ceremony) right after the ``call``-th ``verb`` commits."""
    original = _MINT_VERBS[verb]
    calls = [0]

    def mint_then_die(self: RowTokenRepository, *args: Any, **kwargs: Any) -> Any:
        committed = original(self, *args, **kwargs)
        calls[0] += 1
        if calls[0] == call:
            os._exit(137)
        return committed

    pytest.MonkeyPatch().setattr(RowTokenRepository, verb, mint_then_die)
    app(["run", "-s", settings, "--execute"], standalone_mode=False)
    os._exit(0)


@dataclass(frozen=True, slots=True)
class CrashImage:
    run_id: str
    products: frozenset[str]


def _undecided_uncovered(conn: Any, run_id: str) -> frozenset[str]:
    decided = (
        select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.run_id == run_id).where(token_outcomes_table.c.completed == 1)
    )
    owned = select(token_work_items_table.c.token_id).where(token_work_items_table.c.run_id == run_id)
    return frozenset(
        str(token_id)
        for token_id in conn.execute(
            select(tokens_table.c.token_id)
            .where(tokens_table.c.run_id == run_id)
            .where(tokens_table.c.token_id.not_in(decided))
            .where(tokens_table.c.token_id.not_in(owned))
        ).scalars()
    )


def assert_crash_image(db: LandscapeDB, window: MintWindow) -> CrashImage:
    """The death left the window's image: products with no outcome and no item, each under an open producer."""
    with db.connection() as conn:
        run_id = str(conn.execute(select(runs_table.c.run_id)).scalar_one())
        products = _undecided_uncovered(conn, run_id)
        assert len(products) == window.products, f"{window.verb}: expected {window.products} orphaned products, got {sorted(products)}"
        for product in products:
            parents = (
                conn.execute(
                    select(token_parents_table.c.parent_token_id)
                    .where(token_parents_table.c.run_id == run_id)
                    .where(token_parents_table.c.token_id == product)
                )
                .scalars()
                .all()
            )
            statuses = set(
                conn.execute(
                    select(token_work_items_table.c.status)
                    .where(token_work_items_table.c.run_id == run_id)
                    .where(token_work_items_table.c.token_id.in_(parents))
                ).scalars()
            )
            assert window.producer_status in statuses, f"{window.verb}: product {product} parents {parents} hold {statuses}"
    return CrashImage(run_id, products)


def _lapse_dead_run(db: LandscapeDB, run_id: str) -> None:
    """Write the dead leader's seat and every item lease it held into the database's past (ADR-047)."""
    expire_leader_seat(db, run_id)
    with db.connection() as conn:
        leased = [
            str(item)
            for item in conn.execute(
                select(token_work_items_table.c.work_item_id)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == TokenWorkStatus.LEASED.value)
            ).scalars()
        ]
    for work_item_id in leased:
        expire_lease(db.engine, work_item_id)


def _rows(path: Path) -> list[dict[str, Any]]:
    return sorted((json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), key=json.dumps)


def scenario_mint_window_death_resumes_to_the_clean_image(tmp_path: Path, *, window_name: str, db_url: str) -> None:
    window = WINDOWS[window_name]
    settings = write_settings(tmp_path, window.pipeline, db_url)
    child = multiprocessing.get_context("spawn").Process(target=_die_after_mint_child, args=(str(settings), window.verb, window.call))
    child.start()
    child.join(timeout=300)
    assert child.exitcode == 137, f"the run did not die after {window.verb} #{window.call}: exit {child.exitcode}"

    db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        image = assert_crash_image(db, window)
        _lapse_dead_run(db, image.run_id)

        result = CliRunner().invoke(app, ["resume", image.run_id, "-s", str(settings), "--execute"])
        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output, result.output
        with db.connection() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == image.run_id)).scalar_one()
            tokens = set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == image.run_id)).scalars())
            completed = [
                str(token_id)
                for token_id in conn.execute(
                    select(token_outcomes_table.c.token_id)
                    .where(token_outcomes_table.c.run_id == image.run_id)
                    .where(token_outcomes_table.c.completed == 1)
                ).scalars()
            ]
            open_items = conn.execute(
                select(token_work_items_table.c.work_item_id)
                .where(token_work_items_table.c.run_id == image.run_id)
                .where(token_work_items_table.c.status.in_(_OPEN))
            ).all()
        assert status == RunStatus.COMPLETED.value
        # Every token reaches exactly one recorded terminal outcome and no work is left open.
        assert sorted(completed) == sorted(tokens)
        assert open_items == []
        # The committed products were reconciled, not re-minted: each finished,
        # and the run holds exactly the tokens a clean run of these settings mints.
        assert image.products <= tokens
        assert len(tokens) == window.clean_tokens
        # One publication per result row, the rows a clean run writes.
        assert _rows(tmp_path / "out" / "out.jsonl") == sorted(window.clean_rows, key=json.dumps)
    finally:
        db.close()
