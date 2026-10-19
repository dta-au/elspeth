"""A real LLM result failure retains its cause before the literal-True fork."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.core.config import GateSettings
from elspeth.core.landscape.schema import calls_table, node_states_table
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.transforms.llm.transform import LLMTransform
from tests.fixtures.base_classes import as_sink, as_source, as_transform, inject_write_failure
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink


@pytest.mark.parametrize("accepted", [False, True], ids=["rejected", "accepted"])
@pytest.mark.parametrize("separate_review", [False, True], ids=["single-check", "ordered-review"])
@pytest.mark.parametrize("input_admission", [False, True], ids=["result-admission", "input-admission"])
def test_generated_result_admission_precedes_fork_and_keeps_audited_cause(
    tmp_path: Path, payload_store, accepted: bool, separate_review: bool, input_admission: bool
) -> None:
    input_path = tmp_path / "input.csv"
    original = "unadmitted original input" if input_admission and not accepted else "generic original input"
    input_path.write_text(f"text\n{original}\n")
    source = CSVSource({"path": str(input_path), "on_validation_failure": "discard", "schema": {"mode": "fixed", "fields": ["text: str"]}})
    options = {
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": "test-key",
            "prompt_template": "Assess {{ row.text }}",
            "required_input_fields": ["text"],
            "schema": {"mode": "observed"},
            "output_fields": [{"suffix": "answer", "type": "string"}],
            "result_checks": [{"name": "answer_allowed", "expression": "row['answer'] == 'accepted'"}],
    }
    if separate_review:
        options.pop("output_fields")
        options.update(
            query_input_mode="ordered",
            pool_size=1,
            queries=[
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
            result_checks=[{"name": "answer_allowed", "expression": "row['review_admit']"}],
        )
    if input_admission:
        options["input_checks"] = [{"name": "source_allowed", "expression": "row['text'] == 'generic original input'"}]
    transform = LLMTransform(options)
    transform.on_error = "failed_rows"
    target = tmp_path / "failed_rows.csv"
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
    summary, detail = CollectSink("summary"), CollectSink("detail")
    fork = GateSettings(name="publish", input="checked", condition="True", routes={"true": "fork", "false": "summary"}, fork_to=["summary", "detail"])
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        gates=[fork],
        sinks={"summary": as_sink(summary), "detail": as_sink(detail), "failed_rows": as_sink(retention)},
    )
    raw = json.dumps({"answer": "accepted" if accepted else "generated-private-rejection"})
    body = {
        "id": "completion-admission",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "openai/gpt-4.1-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": raw}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    review_body = {**body, "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"admit": accepted})}, "finish_reason": "stop"}]}
    db = make_landscape_db()
    try:
        graph = build_production_graph(config)
        llm_node_id = graph.get_transform_id_map()[0]
        with respx.mock(assert_all_mocked=True) as router:
            responses = [httpx.Response(200, json=body), httpx.Response(200, json=body)]
            if separate_review:
                responses.append(httpx.Response(200, json=review_body))
            request = router.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=responses)
            result = Orchestrator(db).run(config, graph=graph, payload_store=payload_store)
            row_called = not input_admission or accepted
            assert request.call_count == (1 + (2 if separate_review else 1) if row_called else 1)
            requests = [json.loads(call.request.content) for call in request.calls]
            assert "pre-flight smoke test" in json.dumps(requests[0])
            assert "generic original input" not in json.dumps(requests[0])
            if not row_called:
                assert len(requests) == 1  # Operation preflight is separate; no row writer or reviewer request.
            elif separate_review:
                candidate = "accepted" if accepted else "generated-private-rejection"
                assert f"Review {candidate} against generic original input" in json.dumps(requests[2])
                assert "Draft generic original input" in json.dumps(requests[1])
            else:
                assert "Assess generic original input" in json.dumps(requests[1])
        with db.engine.connect() as connection:
            calls = connection.execute(
                select(calls_table.c.call_type, calls_table.c.response_ref).join(node_states_table).where(
                    node_states_table.c.run_id == result.run_id, node_states_table.c.node_id == llm_node_id
                )
            ).all()
        if row_called:
            assert len(calls) == (4 if separate_review else 2) and {call.call_type for call in calls} == {"http", "llm"}
        else:
            assert calls == []
        assert all(call.response_ref for call in calls)
        if accepted:
            assert result.status is RunStatus.COMPLETED
            assert len(summary.results) == len(detail.results) == 1
            assert summary.results[0]["answer"] == detail.results[0]["answer"] == "accepted"
            assert not target.exists()
        else:
            assert result.status is RunStatus.FAILED  # Its only source row failed; the retained cause is still published.
            assert result.rows_failed == result.rows_routed_failure == 1
            assert summary.results == detail.results == []
            with target.open(newline="") as stream:
                records = list(csv.DictReader(stream))
            assert len(records) == 1
            record = records[0]
            assert record["run_id"] == result.run_id and record["path"] == "on_error_routed"
            assert json.loads(record["row_json"]) == {"text": original}
            check, category = ("source_allowed", "input_check_false") if input_admission else ("answer_allowed", "result_check_false")
            assert check in record["error_reason"] and category in record["error_reason"]
            assert "generated-private" not in record["error_reason"]
    finally:
        db.close()


@pytest.mark.parametrize("permission_stages", [0, 1, 2], ids=["default-refusal", "reader-refusal", "explicit-unavailable-input"])
def test_blank_llm_input_requires_each_reader_permission_and_retains_refusals(
    tmp_path: Path, payload_store, permission_stages: int
) -> None:
    input_path = tmp_path / "input.csv"
    input_path.write_text('text\n"   "\n')
    source = CSVSource({"path": str(input_path), "on_validation_failure": "discard", "schema": {"mode": "fixed", "fields": ["text: str"]}})
    transform = LLMTransform(
        {
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": "test-key",
            "prompt_template": "Assess {{ row.text }}",
            "required_input_fields": ["text"],
            "allow_blank_input_fields": ["text"] if permission_stages >= 1 else [],
            "schema": {"mode": "observed"},
        }
    )
    transform.on_error = "failed_rows"
    reader = LLMTransform(
        {
            "provider": "openrouter",
            "model": "openai/gpt-4.1-mini",
            "api_key": "test-key",
            "prompt_template": "Review {{ row.text }} against {{ row.llm_response }}",
            "required_input_fields": ["text", "llm_response"],
            "allow_blank_input_fields": ["text"] if permission_stages == 2 else [],
            "response_field": "review_result",
            "schema": {"mode": "observed"},
        }
    )
    reader.on_error = "failed_rows"
    target = tmp_path / "failed_rows.csv"
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
    ordinary = CollectSink("default")
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform), as_transform(reader)],
        sinks={"default": as_sink(ordinary), "failed_rows": as_sink(retention)},
    )
    response = {
        "id": "blank-input-preflight",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "openai/gpt-4.1-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    db = make_landscape_db()
    try:
        with respx.mock(assert_all_mocked=True) as router:
            request = router.post("https://openrouter.ai/api/v1/chat/completions").respond(200, json=response)
            result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
            assert request.call_count == 2 + permission_stages
            requests = [json.loads(call.request.content) for call in request.calls]
            assert all("pre-flight smoke test" in json.dumps(item) for item in requests[:2])
            if permission_stages >= 1:
                assert requests[2]["messages"][-1]["content"] == "Assess    "
            if permission_stages == 2:
                assert requests[3]["messages"][-1]["content"] == "Review     against ok"
        if permission_stages == 2:
            assert result.status is RunStatus.COMPLETED
            assert len(ordinary.results) == 1 and ordinary.results[0]["text"] == "   "
            assert ordinary.results[0]["llm_response"] == ordinary.results[0]["review_result"] == "ok"
            assert not target.exists()
        else:
            assert result.status is RunStatus.FAILED and result.rows_failed == 1
            assert ordinary.results == []
            with target.open(newline="") as stream:
                records = list(csv.DictReader(stream))
            assert len(records) == 1
            retained = json.loads(records[0]["row_json"])
            assert retained["text"] == "   "
            if permission_stages == 0:
                assert retained == {"text": "   "}
            else:
                assert retained["llm_response"] == "ok"
            assert records[0]["run_id"] == result.run_id and records[0]["row_id"]
            assert "required_input_field_blank" in records[0]["error_reason"]
            assert records[0]["path"] == "on_error_routed"
    finally:
        db.close()
