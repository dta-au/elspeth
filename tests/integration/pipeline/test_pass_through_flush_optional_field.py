# tests/integration/pipeline/test_pass_through_flush_optional_field.py
"""An optional field some rows of a batch lack is not a pass-through drop.

An observed source records every field ``required: false`` and gives every
row the same contract, whether or not the row carries the field. The
aggregation flush's pass-through cross-check used to take each buffered
token's input fields from that contract alone, so a ``batch_replicate`` batch
mixing rows with and without its optional ``copies_field`` counted the field
as an input of the row that lacked it. The replica of that row (correctly)
carried no such field, and the run aborted on a Tier-1
``PassThroughContractViolation`` (exit 4) for a field the plugin never
received. The flush site now derives input fields with
``derive_effective_input_fields``, as the single-token path does: the fields
the contract declares AND the payload carries (elspeth-5887fb7928 S4).

Every case is a real ``elspeth run --execute`` (in-process CLI). The control
cases prove the Tier-1 check still fires in the same mixed-batch shape when
the plugin drops a field every buffered row carried.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_replicate import BatchReplicate
from tests.integration.pipeline.test_output_declaration_routing import (
    _json_sink,
    _json_source,
    _read_jsonl,
    _run,
    _transform_error_reasons,
    _write_jsonl,
)

_REAL_REPLICATE_PROCESS = BatchReplicate.process

_WITH_FIRST = [{"id": 1, "n": 2}, {"id": 2}, {"id": 3, "n": 1}, {"id": 4}]
_WITHOUT_FIRST = [{"id": 1}, {"id": 2, "n": 3}, {"id": 3}, {"id": 4, "n": 1}]


def _settings_file(tmp_path: Path, *, source_schema: dict[str, Any] | None = None) -> Path:
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl", schema=source_schema)},
        "concurrency": {"max_workers": 1},
        "aggregations": [
            {
                "name": "replicate",
                "plugin": "batch_replicate",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": 2},
                "output_mode": "transform",
                "options": {"schema": {"mode": "observed"}, "copies_field": "n", "default_copies": 1},
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


def _tokens_without_exactly_one_terminal(tmp_path: Path) -> int:
    (count,) = _query(
        tmp_path,
        "select count(*) from tokens t where (select count(*) from token_outcomes o where o.token_id = t.token_id and o.completed = 1) != 1",
    )[0]
    return int(count)


def _pass_through_violation_contexts(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        error["context"]
        for (text,) in _query(tmp_path, "select error_json from node_states where error_json is not null")
        if (error := json.loads(text)).get("context", {}).get("exception_type") == "PassThroughContractViolation"
    ]


@pytest.mark.parametrize("rows", [_WITH_FIRST, _WITHOUT_FIRST], ids=["field-present-first", "field-absent-first"])
def test_a_mixed_batch_replicates_every_row_with_default_copies_where_absent(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    _write_jsonl(tmp_path / "in.jsonl", rows)
    result = _run(_settings_file(tmp_path))

    assert result.exit_code == 0, result.output
    expected = [{**row, "copy_index": copy_index} for row in rows for copy_index in range(row["n"] if "n" in row else 1)]
    assert _read_jsonl(tmp_path / "out.jsonl") == expected
    assert _read_jsonl(tmp_path / "q.jsonl") == []
    assert _transform_error_reasons(tmp_path) == []
    assert _pass_through_violation_contexts(tmp_path) == []
    assert _tokens_without_exactly_one_terminal(tmp_path) == 0
    assert dict(_query(tmp_path, "select outcome, count(*) from token_outcomes where completed = 1 group by outcome")) == {
        "success": len(expected),
        "transient": len(rows),  # every buffered source token is consumed in its batch
    }


def _drop_id_from_every_replica(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_replicate loses ``id`` from each replica's payload — a field every buffered row carried."""

    def process(self: BatchReplicate, rows: list[PipelineRow], ctx: Any) -> TransformResult:
        result = _REAL_REPLICATE_PROCESS(self, rows, ctx)
        if result.status != "success" or result.rows is None:
            return result
        dropped = [PipelineRow({k: v for k, v in row.to_dict().items() if k != "id"}, row.contract) for row in result.rows]
        return TransformResult.success_multi(dropped, success_reason=result.success_reason)

    monkeypatch.setattr(BatchReplicate, "process", process)


@pytest.mark.parametrize(
    "source_schema",
    [None, {"mode": "flexible", "fields": ["id: int"]}],
    ids=["observed-id", "declared-required-id"],
)
def test_dropping_a_carried_field_in_the_same_mixed_batch_still_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_schema: dict[str, Any] | None
) -> None:
    """Control: the Tier-1 check still fires when the plugin drops a field every buffered row carried."""
    _drop_id_from_every_replica(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", _WITH_FIRST)
    settings = _settings_file(tmp_path, source_schema=source_schema)
    runner = CliRunner()
    validated = runner.invoke(app, ["validate", "-s", str(settings)])
    assert validated.exit_code == 0, validated.output

    result = runner.invoke(app, ["run", "-s", str(settings), "--execute"])

    assert result.exit_code == 4, result.output
    assert "PassThroughContractViolation" in result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    contexts = _pass_through_violation_contexts(tmp_path)
    assert contexts, "the violation is recorded on the buffered tokens' node states"
    assert {tuple(context["divergence_set"]) for context in contexts} == {("id",)}
