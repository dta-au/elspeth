# tests/integration/pipeline/test_output_declaration_batch_seams.py
"""ADR-050 at the batch seams: a batch transform's declared concrete type is checked against its values.

The per-row seam (``TransformExecutor``) already checks every declared,
concrete-typed field a transform produces. A batch-aware transform's output
is emitted at an aggregation flush or a collector flush instead, so the same
check (``engine/executors/declared_output_types``) runs in the shared batch
postflight (``batch_contract_validation.validate_success_outputs``), and the
batch transforms stamp their created fields through the one declaration
authority (``BaseTransform._batch_output_contract`` or the passthrough
plugins' ``_apply_declared_output_field_contracts`` call).

Every case is a real ``elspeth run --execute`` (in-process CLI) whose batch
plugin emits the WRONG type for a field it declares ``int``: a sentinel
string. A plugin whose declaration is correct never violates it, so the fault
is injected into the plugin's computation. The passthrough case is
``batch_replicate``'s ``copy_index`` (``int``); the reductive case is
``batch_stats``'s ``count`` (``int`` in its shipped ``created_output_fields()``
table). In every case
the violation fails the WHOLE batch — every buffered row follows the
aggregation's ``on_error``, or the collector group records a
``collector_contract_violation`` verdict — with a reason naming the field,
both type names, ``authorship: computed`` and ``declared_by: plugin`` (each
case's type is fixed by the plugin's ``created_output_fields()``: the RC-2
cell of the 2026-09-25 ruling), never the value; the sentinel is absent from
every Landscape table.

Before this change a batch transform's emitted contract carried no
declaration and the flush postflight typed nothing, so the sentinel reached
the sink under a clean COMPLETED (the mutation check in the lane evidence
reverts the postflight call and shows exactly that).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_replicate import BatchReplicate
from elspeth.plugins.transforms.batch_stats import BatchStats
from tests.integration.pipeline.test_output_declaration_routing import (
    _audit_cells_containing,
    _json_sink,
    _json_source,
    _outcomes,
    _read_jsonl,
    _run,
    _transform_error_reasons,
    _write_jsonl,
)

SENTINEL = "SENTINEL_ADR050_BATCH_VALUE"

_REAL_AGGREGATE_GROUP = BatchStats._aggregate_group
_REAL_REPLICATE_PROCESS = BatchReplicate.process


def _fault_batch_stats_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_stats computes ``count`` (declared ``int`` by its own ``created_output_fields()``) as the sentinel string."""

    def aggregate_group(self: BatchStats, grouped_rows: Any, group_value: Any) -> Any:
        result, error = _REAL_AGGREGATE_GROUP(self, grouped_rows, group_value)
        if error is None:
            result["count"] = SENTINEL
        return result, error

    monkeypatch.setattr(BatchStats, "_aggregate_group", aggregate_group)


def _fault_batch_replicate_copy_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_replicate writes ``copy_index`` (declared ``int`` by construction) as the sentinel string."""

    def process(self: BatchReplicate, rows: list[PipelineRow], ctx: Any) -> TransformResult:
        result = _REAL_REPLICATE_PROCESS(self, rows, ctx)
        if result.status != "success" or result.rows is None:
            return result
        faulted = [PipelineRow({**row.to_dict(), "copy_index": SENTINEL}, row.contract) for row in result.rows]
        return TransformResult.success_multi(faulted, success_reason=result.success_reason)

    monkeypatch.setattr(BatchReplicate, "process", process)


def _settings_file(tmp_path: Path, body: dict[str, Any]) -> Path:
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl")},
        "concurrency": {"max_workers": 1},
        **body,
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _assert_value_free_computed_reason(reason: dict[str, Any], *, field: str) -> None:
    assert reason["reason"] == "contract_violation"
    assert (reason["field"], reason["expected"], reason["actual"], reason["authorship"], reason["declared_by"]) == (
        field,
        "int",
        "str",
        "computed",
        "plugin",
    )
    assert "declared int by the transform itself" in reason["error"]
    assert SENTINEL not in json.dumps(reason)


def _terminal_outcomes(tmp_path: Path) -> dict[tuple[str, str], int]:
    """Completed token outcomes only (a buffered token's open outcome row has no outcome yet)."""
    return {(outcome, path): n for (outcome, path), n in _outcomes(tmp_path).items() if outcome is not None}


def _query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return list(con.execute(sql))
    finally:
        con.close()


_ROWS = [{"id": 1, "amount": 2}, {"id": 2, "amount": 3}, {"id": 3, "amount": 5}, {"id": 4, "amount": 7}]


def _stats_aggregation() -> dict[str, Any]:
    return {
        "aggregations": [
            {
                "name": "stats",
                "plugin": "batch_stats",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": 2},
                "output_mode": "transform",
                "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
            }
        ]
    }


def test_an_aggregation_computing_the_wrong_type_fails_the_batch_value_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reductive aggregation: every buffered row follows on_error; authorship=computed, never the value."""
    _fault_batch_stats_count(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", _ROWS)
    result = _run(_settings_file(tmp_path, _stats_aggregation()))

    assert result.exit_code == 2, result.output  # every row failed: both batches were routed
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert len(_read_jsonl(tmp_path / "q.jsonl")) == len(_ROWS)
    assert _terminal_outcomes(tmp_path) == {("failure", "on_error_routed"): len(_ROWS)}
    reasons = _transform_error_reasons(tmp_path)
    assert len(reasons) == len(_ROWS)  # one row per buffered token of each failed batch
    for reason in reasons:
        _assert_value_free_computed_reason(reason, field="count")
    assert _audit_cells_containing(tmp_path, SENTINEL) == []


def test_the_same_aggregation_with_a_correct_value_completes(tmp_path: Path) -> None:
    """Control: the shipped concrete declarations alone route nothing — only a value that breaks one does."""
    _write_jsonl(tmp_path / "in.jsonl", _ROWS)
    result = _run(_settings_file(tmp_path, _stats_aggregation()))

    assert result.exit_code == 0, result.output
    assert [row["count"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [2, 2]
    assert _read_jsonl(tmp_path / "q.jsonl") == []
    assert _transform_error_reasons(tmp_path) == []


def test_a_collector_computing_the_wrong_type_fails_the_group_value_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collector: the group's recorded verdict is a contract violation naming the field, never the value."""
    _fault_batch_stats_count(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "items": [3, 1, 2]}])
    settings = _settings_file(
        tmp_path,
        {
            "transforms": [
                {
                    "name": "explode",
                    "plugin": "json_explode",
                    "input": "rows",
                    "on_success": "pages",
                    "on_error": "quarantine",
                    "options": {"array_field": "items", "output_field": "item", "schema": {"mode": "observed"}},
                }
            ],
            "collectors": [
                {
                    "name": "stitch",
                    "plugin": "batch_stats",
                    "input": "pages",
                    "on_success": "out",
                    "options": {"value_field": "item", "schema": {"mode": "observed"}},
                }
            ],
            "scopes": [{"name": "document_pages", "opener": "explode", "closer": "stitch", "policy": "require_all"}],
        },
    )
    result = _run(settings)

    assert result.exit_code != 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    [(collector_node_id, failure_reason)] = _query(tmp_path, "select collector_node_id, failure_reason from collector_group_failures")
    assert "stitch" in collector_node_id
    assert failure_reason == "collector_contract_violation"
    contexts = [
        error["context"]
        for (text,) in _query(tmp_path, "select error_json from node_states where error_json is not null")
        if (error := json.loads(text)).get("context", {}).get("exception_type") == "DeclaredOutputTypeViolation"
    ]
    assert [(c["field"], c["expected_type"], c["actual_type"], c["authorship"], c["declared_by"]) for c in contexts] == [
        ("count", "int", "str", "computed", "plugin")
    ]
    assert _audit_cells_containing(tmp_path, SENTINEL) == []


def test_a_passthrough_batch_computing_the_wrong_type_fails_the_batch_value_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough-shaped batch: batch_replicate's copy_index is declared int; a str fails the whole batch."""
    _fault_batch_replicate_copy_index(monkeypatch)
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "copies": 2}, {"id": 2, "copies": 1}])
    settings = _settings_file(
        tmp_path,
        {
            "aggregations": [
                {
                    "name": "replicate",
                    "plugin": "batch_replicate",
                    "input": "rows",
                    "on_success": "out",
                    "on_error": "quarantine",
                    "trigger": {"count": 2},
                    "output_mode": "transform",
                    "options": {"schema": {"mode": "observed"}, "copies_field": "copies", "include_copy_index": True},
                }
            ]
        },
    )
    result = _run(settings)

    assert result.exit_code == 2, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert len(_read_jsonl(tmp_path / "q.jsonl")) == 2
    assert _terminal_outcomes(tmp_path) == {("failure", "on_error_routed"): 2}
    reasons = _transform_error_reasons(tmp_path)
    assert len(reasons) == 2
    for reason in reasons:
        _assert_value_free_computed_reason(reason, field="copy_index")
    assert _audit_cells_containing(tmp_path, SENTINEL) == []
