# tests/integration/pipeline/test_passthrough_flush_shape.py
"""A passthrough flush that is not one row per buffered row fails every token before the Tier-1 abort.

``output_mode: passthrough`` continues each buffered token with its own output
row, so build admits only a batch plugin declaring
``flush_emits_one_row_per_buffered_row`` (lane-owner decision 2026-09-27,
option A). A plugin that declares it and then returns another shape is a plugin
bug: the run still aborts (exit 4), but the flush cross-check first records
every buffered token FAILURE / UNROUTED with the value-free
``BatchPassthroughShapeError`` evidence. Before, the executor's plain
``OrchestrationInvariantError`` fired after the cross-check had passed the
shape through, and every token of the batch was left without an outcome.

Every case is a real ``elspeth validate`` + ``elspeth run --execute`` (the
in-process CLI) with the plugins resolved through a test plugin manager.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import Determinism, PipelineRow
from elspeth.contracts.contexts import TransformContext
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, scoped_plugin_manager
from elspeth.plugins.infrastructure.results import TransformResult
from tests.fixtures.passthrough_batch_plugins import PASSTHROUGH_IDENTITY_BATCH, PassthroughIdentityBatch
from tests.integration.pipeline.test_output_declaration_routing import _json_sink, _json_source, _read_jsonl, _write_jsonl

_SENTINEL = "PASSTHROUGH_SHAPE_SENTINEL_VALUE"
_ROWS = [{"id": 1, "note": _SENTINEL}, {"id": 2, "note": _SENTINEL}, {"id": 3, "note": _SENTINEL}]


def _identity_rows(plugin: PassthroughIdentityBatch, rows: PipelineRow | list[PipelineRow], ctx: TransformContext) -> list[PipelineRow]:
    result = PassthroughIdentityBatch.process(plugin, rows, ctx)
    assert result.rows is not None
    return list(result.rows)


class _DropsTheLastRow(PassthroughIdentityBatch):
    name = "test_passthrough_drops_last_row"
    determinism = Determinism.DETERMINISTIC

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        return TransformResult.success_multi(_identity_rows(self, row, ctx)[:-1], success_reason={"action": "passthrough"})


class _DuplicatesTheLastRow(PassthroughIdentityBatch):
    """The OVERCOUNT side of the shape rule (P5 review r1 F2): one row more than it buffered."""

    name = "test_passthrough_duplicates_last_row"
    determinism = Determinism.DETERMINISTIC

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        rows = _identity_rows(self, row, ctx)
        return TransformResult.success_multi([*rows, rows[-1]], success_reason={"action": "passthrough"})


class _ReturnsOneRow(PassthroughIdentityBatch):
    name = "test_passthrough_single_row"
    determinism = Determinism.DETERMINISTIC

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        return TransformResult.success(_identity_rows(self, row, ctx)[0], success_reason={"action": "passthrough"})


class _QuarantinesAnInput(PassthroughIdentityBatch):
    name = "test_passthrough_quarantines"
    determinism = Determinism.DETERMINISTIC

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        return TransformResult.success_multi(
            _identity_rows(self, row, ctx),
            success_reason={"action": "passthrough", "metadata": {"quarantined_indices": [0]}},
        )


@pytest.fixture(autouse=True)
def _shape_plugins() -> Iterator[None]:
    """Resolve plugin names against the built-ins plus the identity plugin and the four that break it."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    manager.register(
        create_dynamic_hookimpl(
            [PassthroughIdentityBatch, _DropsTheLastRow, _DuplicatesTheLastRow, _ReturnsOneRow, _QuarantinesAnInput],
            "elspeth_get_transforms",
        )
    )
    with scoped_plugin_manager(manager):
        yield


def _settings_file(tmp_path: Path, plugin: str) -> Path:
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl")},
        "concurrency": {"max_workers": 1},
        "aggregations": [
            {
                "name": "agg",
                "plugin": plugin,
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": len(_ROWS)},
                "output_mode": "passthrough",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return list(con.execute(sql))
    finally:
        con.close()


def _completed_outcomes(tmp_path: Path) -> dict[str, int]:
    rows = _query(tmp_path, "select outcome || '/' || path, count(*) from token_outcomes where completed = 1 group by 1")
    return {str(key): int(count) for key, count in rows}


def _tokens_without_exactly_one_terminal(tmp_path: Path) -> int:
    (count,) = _query(
        tmp_path,
        "select count(*) from tokens t where (select count(*) from token_outcomes o where o.token_id = t.token_id and o.completed = 1) != 1",
    )[0]
    return int(count)


def _run(tmp_path: Path, plugin: str) -> Any:
    _write_jsonl(tmp_path / "in.jsonl", _ROWS)
    settings = _settings_file(tmp_path, plugin)
    runner = CliRunner()
    validated = runner.invoke(app, ["validate", "-s", str(settings)])
    assert validated.exit_code == 0, validated.output
    return runner.invoke(app, ["run", "-s", str(settings), "--execute"])


def test_a_plugin_that_honours_the_declaration_completes_every_row(tmp_path: Path) -> None:
    """Control: the identity plugin returns one row per buffered row; every token continues with its own row."""
    result = _run(tmp_path, PASSTHROUGH_IDENTITY_BATCH)

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == _ROWS
    assert _tokens_without_exactly_one_terminal(tmp_path) == 0
    assert _completed_outcomes(tmp_path) == {"success/default_flow": len(_ROWS)}


@pytest.mark.parametrize(
    ("plugin", "failure_kind", "emitted_row_count", "message"),
    [
        ("test_passthrough_drops_last_row", "row_count_mismatch", 2, "returned 2 rows but received 3 input rows"),
        ("test_passthrough_duplicates_last_row", "row_count_mismatch", 4, "returned 4 rows but received 3 input rows"),
        ("test_passthrough_single_row", "single_row_result", 1, "requires multi-row result"),
        ("test_passthrough_quarantines", "quarantined_indices_declared", 3, "cannot declare quarantined_indices"),
    ],
)
def test_a_plugin_that_breaks_the_declaration_fails_every_token_before_the_abort(
    tmp_path: Path, plugin: str, failure_kind: str, emitted_row_count: int, message: str
) -> None:
    result = _run(tmp_path, plugin)

    assert result.exit_code == 4, result.output
    assert "BatchPassthroughShapeError" in result.output
    assert message in result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert _tokens_without_exactly_one_terminal(tmp_path) == 0
    assert _completed_outcomes(tmp_path) == {"failure/unrouted": len(_ROWS)}
    contexts = [json.loads(text) for (text,) in _query(tmp_path, "select context_json from token_outcomes where completed = 1")]
    for context in contexts:
        assert context["exception_type"] == "BatchPassthroughShapeError"
        assert (context["failure_kind"], context["plugin"]) == (failure_kind, plugin)
        assert (context["buffered_token_count"], context["emitted_row_count"]) == (len(_ROWS), emitted_row_count)
    assert all(_SENTINEL not in str(cell) for row in _query(tmp_path, "select * from token_outcomes") for cell in row)
