"""Complete-source annotation rejects individual cases before row model calls."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from elspeth.contracts import Determinism, RunStatus, SourceRow
from elspeth.contracts.contexts import SourceContext
from elspeth.contracts.types import AggregationName
from elspeth.core.config import AggregationSettings, GateSettings, SourceSettings, TriggerConfig
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.schema import calls_table, node_states_table, rows_table
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.plugins.transforms.batch_duplicate_annotator import BatchDuplicateAnnotator
from elspeth.plugins.transforms.llm.transform import LLMTransform
from tests.fixtures.base_classes import as_sink, as_source, as_transform, inject_write_failure
from tests.fixtures.factories import wire_transforms
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink


def test_complete_source_admission_retains_each_rejected_identity_before_writer_and_fork(tmp_path: Path, payload_store) -> None:
    originals = [
        {"case_id": "same", "first": "alpha", "second": "beta", "text": "reject-one"},
        {"case_id": "same", "first": "safeA", "second": "safeB", "text": "accept"},
        {"case_id": "same", "first": "gamma", "second": "alpha", "text": "reject-two"},
        {"case_id": "same", "first": "beta", "second": "delta", "text": "reject-three"},
        {"case_id": "same", "first": 3, "second": "unique", "text": "reject-four"},
    ]
    input_path = tmp_path / "cases.jsonl"
    input_path.write_text("\n".join(json.dumps(row) for row in originals))
    summary, detail = CollectSink("summary"), CollectSink("detail")
    target = tmp_path / "failed_rows.csv"

    class ExhaustionObservedSource(JSONSource):
        determinism = Determinism.IO_READ

        def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
            yield from super().load(ctx)
            # Runs at the real source exhaustion boundary, before its EOF flush.
            assert summary.results == detail.results == []
            assert not target.exists()

    source = ExhaustionObservedSource(
        {
            "path": str(input_path),
            "on_validation_failure": "discard",
            "schema": {"mode": "fixed", "fields": ["case_id: str", "first: any", "second: str", "text: str"]},
        }
    )
    source.on_success = "batch_in"
    annotator = BatchDuplicateAnnotator(
        {"schema": {"mode": "observed"}, "rules": [{"name": "repeated", "cells": [{"field": "first"}, {"field": "second"}]}]}
    )
    annotator.on_success = "annotated"
    annotator.on_error = "discard"
    writer = LLMTransform(
        {
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": "test-key",
            "prompt_template": "Draft {{ row.text }}",
            "required_input_fields": ["text"],
            "schema": {"mode": "observed"},
            "input_checks": [{"name": "complete_source_admitted", "expression": "row['duplicates_valid']"}],
            "query_input_mode": "ordered",
            "pool_size": 1,
            "queries": [
                {
                    "name": "draft",
                    "structured_output_prefix": "",
                    "input_fields": {"text": "text"},
                    "template": "Draft {{ row.text }}",
                    "response_format": "structured",
                    "output_fields": [{"suffix": "answer", "type": "string"}],
                },
                {
                    "name": "review",
                    "input_fields": {"answer": "answer", "text": "text"},
                    "template": "Review {{ row.answer }} against {{ row.text }}",
                    "response_format": "structured",
                    "output_fields": [{"suffix": "admit", "type": "boolean"}],
                },
            ],
            "result_checks": [{"name": "review_admitted", "expression": "row['review_admit']"}],
        }
    )
    writer.on_error = "failed_rows"
    retention = inject_write_failure(
        CSVSink(
            {
                "path": str(target),
                "schema": {"mode": "observed"},
                "require_all_rows": True,
                "include_audit_context": True,
                "include_error_reason": True,
            }
        ),
        "discard",
    )
    sinks = {"summary": as_sink(summary), "detail": as_sink(detail), "failed_rows": as_sink(retention)}
    fork = GateSettings(name="publish", input="checked", condition="True", routes={"true": "fork", "false": "summary"}, fork_to=["summary", "detail"])
    settings = AggregationSettings(
        name="compare", plugin=annotator.name, input="batch_in", on_success="annotated", on_error="discard", trigger=TriggerConfig(), output_mode="passthrough"
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="batch_in", options={})},
        transforms=wire_transforms([as_transform(writer)], source_connection="annotated", final_sink="checked", names=["writer"]),
        aggregations={"compare": (as_transform(annotator), settings)},
        sinks=sinks,
        gates=[fork],
    )
    aggregation_id = graph.get_aggregation_id_map()[AggregationName("compare")]
    annotator.node_id = aggregation_id
    config = PipelineConfig(
        sources={"primary": as_source(source)}, transforms=[as_transform(writer), as_transform(annotator)], sinks=sinks, gates=[fork], aggregation_settings={aggregation_id: settings}
    )
    body = {
        "id": "completion-admission", "object": "chat.completion", "created": 1700000000, "model": "openai/gpt-4.1-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"answer": "accepted"})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    review = {**body, "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"admit": True})}, "finish_reason": "stop"}]}
    db = make_landscape_db()
    try:
        with respx.mock(assert_all_mocked=True) as router:
            request = router.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=[httpx.Response(200, json=value) for value in (body, body, review)])
            result = Orchestrator(db).run(config, graph=graph, payload_store=payload_store)
            assert request.call_count == 3  # Startup preflight, admitted writer, admitted reviewer.
            requests = [json.loads(call.request.content) for call in request.calls]
            assert "pre-flight smoke test" in json.dumps(requests[0])
            assert "Draft accept" in json.dumps(requests[1])
            assert "Review accepted against accept" in json.dumps(requests[2])
            assert "reject-" not in json.dumps(requests)
        assert result.status is RunStatus.COMPLETED_WITH_FAILURES
        assert result.rows_failed == result.rows_routed_failure == 4
        assert len(summary.results) == len(detail.results) == 1
        assert summary.results[0]["text"] == detail.results[0]["text"] == "accept"
        assert summary.results[0]["answer"] == detail.results[0]["answer"] == "accepted"
        assert summary.results[0]["duplicates_valid"] is True
        assert summary.results[0]["duplicates_causes"] == "[]"
        with target.open(newline="") as stream:
            retained = list(csv.DictReader(stream))
        assert len(retained) == 4
        assert len({record["row_id"] for record in retained}) == len({record["token_id"] for record in retained}) == 4
        expected = {
            "reject-one": {("first", "duplicate"), ("second", "duplicate")},
            "reject-two": {("second", "duplicate")},
            "reject-three": {("first", "duplicate")},
            "reject-four": {("first", "invalid_string")},
        }
        for record in retained:
            row = json.loads(record["row_json"])
            original = next(item for item in originals if item["text"] == row["text"])
            assert {key: row[key] for key in original} == original
            assert row["duplicates_valid"] is False
            causes = json.loads(row["duplicates_causes"])
            assert {(cause["field"], cause["category"]) for cause in causes} == expected[row["text"]]
            assert all(cause["rule"] == "repeated" for cause in causes)
            assert record["run_id"] == result.run_id and record["path"] == "on_error_routed"
            assert "complete_source_admitted" in record["error_reason"] and "input_check_false" in record["error_reason"]
        with db.engine.connect() as connection:
            identities = connection.execute(select(rows_table.c.row_id, rows_table.c.source_row_index).where(rows_table.c.run_id == result.run_id)).all()
            calls = connection.execute(select(calls_table.c.call_type).join(node_states_table).where(node_states_table.c.run_id == result.run_id)).all()
        by_id = dict(identities)
        assert {by_id[record["row_id"]] for record in retained} == {0, 2, 3, 4}
        assert sorted(call.call_type for call in calls) == ["http", "http", "llm", "llm"]
    finally:
        db.close()
