"""Execute the colour contract repair through real plugins and the orchestrator.

Only the outbound provider SDK is scripted. ``run_colour_pipeline`` also accepts
Composer-resolved YAML, so authoring regressions can reuse these runtime oracles.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import CallStatus, CallType
from elspeth.core.canonical import stable_hash
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, nodes_table, transform_errors_table, validation_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from tests.fixtures.landscape import make_factory

ColourScenario = Literal["happy", "missing_answer", "ragged_csv"]
COLOUR_ANSWERS = {
    "red": ("green", "#FF0000"),
    "blue": ("orange", "#0000FF"),
    "green": ("red", "#008000"),
    "yellow": ("purple", "#FFFF00"),
    "purple": ("yellow", "#800080"),
}


def colour_pipeline_yaml(tmp_path: Path, provider: Literal["azure", "bedrock"] = "azure") -> str:
    """The repaired pipeline: generated fields are required only downstream."""
    provider_binding = (
        """provider: azure
      deployment_name: gpt-4o
      endpoint: https://test.openai.azure.com
      api_key: test-key"""
        if provider == "azure"
        else """provider: bedrock
      model: bedrock/anthropic.claude-3-haiku-20240307-v1:0
      region_name: us-east-1
      max_capacity_retry_seconds: 30"""
    )
    return f"""
sources:
  colours:
    plugin: csv
    on_success: colour_rows
    options:
      path: {tmp_path / "colours.csv"}
      on_validation_failure: source_errors
      schema:
        mode: fixed
        fields: ['colour: str']
queues:
  colour_rows: {{}}
  answered_rows: {{}}
transforms:
  - name: ask_colours
    plugin: llm
    input: colour_rows
    on_success: answered_rows
    on_error: llm_errors
    options:
      {provider_binding}
      system_prompt: Give concise factual colour answers in the requested format.
      required_input_fields: [colour]
      schema:
        mode: flexible
        fields: ['colour: str']
      queries:
        good_colour_pair:
          input_fields: {{colour: colour}}
          template: 'What colour pairs well with {{{{ row.colour }}}}?'
          response_format: structured
          output_fields: [{{suffix: answer, type: string}}]
        approximate_hex:
          input_fields: {{colour: colour}}
          template: 'What is an approximate hex value for {{{{ row.colour }}}}?'
          response_format: structured
          output_fields: [{{suffix: answer, type: string}}]
  - name: select_answers
    plugin: field_mapper
    input: answered_rows
    on_success: output
    on_error: llm_errors
    options:
      select_only: true
      mapping:
        colour: colour
        good_colour_pair_answer: good_colour_pair_answer
        approximate_hex_answer: approximate_hex_answer
      schema:
        mode: flexible
        fields: ['colour: str', 'good_colour_pair_answer: str', 'approximate_hex_answer: str']
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: {tmp_path / "colours-output.csv"}
      schema:
        mode: fixed
        fields: ['colour: str', 'good_colour_pair_answer: str', 'approximate_hex_answer: str']
  source_errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "source-errors.jsonl"}
      format: jsonl
      schema: {{mode: observed}}
  llm_errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "llm-errors.jsonl"}
      format: jsonl
      schema: {{mode: observed}}
"""


@contextmanager
def scripted_colour_sdk(scenario: ColourScenario) -> Generator[list[tuple[str, str]], None, None]:
    """Return answers by prompt identity, allowing arbitrary query scheduling."""
    prompts = {}
    for colour, (pair, hex_code) in COLOUR_ANSWERS.items():
        prompts[f"What colour pairs well with {colour}?"] = (colour, "good_colour_pair", pair)
        prompts[f"What is an approximate hex value for {colour}?"] = (colour, "approximate_hex", hex_code)
    calls: list[tuple[str, str]] = []
    lock = Lock()

    def complete(**kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        user_prompts = [message["content"] for message in messages if message["role"] == "user"]
        assert len(user_prompts) == 1
        if user_prompts == ["This is a pre-flight smoke test. Please reply with ok."]:
            colour, query, content = "preflight", "preflight", "ok"
            assert "response_format" not in kwargs
        else:
            colour, query, answer = prompts[user_prompts[0]]
            response_format = kwargs["response_format"]
            assert response_format["type"] == "json_schema"
            schema = response_format["json_schema"]["schema"]
            assert schema["required"] == ["answer"]
            assert schema["properties"] == {"answer": {"type": "string"}}
            payload = {} if scenario == "missing_answer" and (colour, query) == ("green", "approximate_hex") else {"answer": answer}
            content = json.dumps(payload)
        with lock:
            calls.append((colour, query))
        raw_response = {"model": kwargs["model"], "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
            model=kwargs["model"],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model_dump=lambda *_args, **_kwargs: raw_response,
        )

    from openai import AzureOpenAI

    client = MagicMock(spec=AzureOpenAI)
    client.chat.completions.create.side_effect = complete
    with patch("openai.AzureOpenAI", return_value=client), patch("litellm.completion", side_effect=complete):
        yield calls


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def run_colour_pipeline(
    tmp_path: Path,
    yaml_text: str,
    scenario: ColourScenario,
    *,
    output_path: Path | None = None,
    source_error_path: Path | None = None,
    llm_error_path: Path | None = None,
) -> None:
    """Run supplied YAML and prove output, provider calls, and retained errors.

    The source must already exist; in Composer tests these are the materialized,
    approved bytes. Optional output paths support Composer's materialization.
    """
    output_path = output_path or tmp_path / "colours-output.csv"
    source_error_path = source_error_path or tmp_path / "source-errors.jsonl"
    llm_error_path = llm_error_path or tmp_path / "llm-errors.jsonl"
    settings = load_settings_from_yaml_string(yaml_text)
    source_error_sink = next(name for name, sink in settings.sinks.items() if Path(sink.options["path"]) == source_error_path)
    llm_error_sink = next(name for name, sink in settings.sinks.items() if Path(sink.options["path"]) == llm_error_path)
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    payload_store = FilesystemPayloadStore(tmp_path / "colour-payloads")
    db = LandscapeDB(f"sqlite:///{tmp_path / 'colour-audit.db'}")
    with scripted_colour_sdk(scenario) as calls:
        result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=payload_store)
    assert result.status.name == ("COMPLETED" if scenario == "happy" else "COMPLETED_WITH_FAILURES")
    expected_calls = Counter((colour, query) for colour in COLOUR_ANSWERS for query in ("good_colour_pair", "approximate_hex"))
    assert Counter(calls) == expected_calls + Counter({("preflight", "preflight"): 1})
    assert len([call for call in calls if call != ("preflight", "preflight")]) == 10

    with output_path.open(newline="") as output:
        reader = csv.DictReader(output)
        assert reader.fieldnames == ["colour", "good_colour_pair_answer", "approximate_hex_answer"]
        output_rows = list(reader)
    expected_colours = set(COLOUR_ANSWERS) - ({"green"} if scenario == "missing_answer" else set())
    assert len(output_rows) == len(expected_colours)
    assert {row["colour"] for row in output_rows} == expected_colours
    for row in output_rows:
        pair, hex_code = COLOUR_ANSWERS[row["colour"]]
        assert row == {"colour": row["colour"], "good_colour_pair_answer": pair, "approximate_hex_answer": hex_code}

    with db.connection() as conn:
        states = conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.run_id == result.run_id)).fetchall()
        transform_errors = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).fetchall()
        source_errors = conn.execute(select(validation_errors_table).where(validation_errors_table.c.run_id == result.run_id)).fetchall()
        llm_output_hashes = (
            conn.execute(
                select(node_states_table.c.output_hash)
                .join(
                    nodes_table,
                    (nodes_table.c.node_id == node_states_table.c.node_id) & (nodes_table.c.run_id == node_states_table.c.run_id),
                )
                .where(
                    node_states_table.c.run_id == result.run_id,
                    nodes_table.c.plugin_name == "llm",
                    node_states_table.c.output_hash.is_not(None),
                )
            )
            .scalars()
            .all()
        )
    llm_options = next(transform.options for transform in settings.transforms if transform.plugin == "llm")
    model = llm_options["deployment_name"] if llm_options["provider"] == "azure" else llm_options["model"]
    expected_internal_hashes = []
    for colour in expected_colours:
        enriched: dict[str, Any] = {"colour": colour}
        for query, answer in zip(("good_colour_pair", "approximate_hex"), COLOUR_ANSWERS[colour], strict=True):
            enriched[f"{query}_answer"] = answer
            enriched[f"{query}_llm_response"] = json.dumps({"answer": answer})
            enriched[f"{query}_llm_response_model"] = model
            enriched[f"{query}_llm_response_usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
        expected_internal_hashes.append(stable_hash(enriched))
    # The recorded intermediate rows retain operational fields before the
    # mapper projects the three-column business output checked above.
    assert Counter(llm_output_hashes) == Counter(expected_internal_hashes)
    factory = make_factory(db, payload_store=payload_store)
    audited_calls = [
        call for call in factory.query.get_calls_for_states([state.state_id for state in states]) if call.call_type == CallType.LLM
    ]
    assert len(audited_calls) == 10
    assert all(call.status == CallStatus.SUCCESS for call in audited_calls)
    assert all(call.request_ref is not None and call.response_ref is not None for call in audited_calls)

    retained_llm = _jsonl_rows(llm_error_path)
    retained_source = _jsonl_rows(source_error_path)
    if scenario == "missing_answer":
        assert retained_llm == [{"colour": "green"}]
        assert len(transform_errors) == 1
        error = transform_errors[0]
        assert error.destination == llm_error_sink
        assert json.loads(error.row_data_json) == {"colour": "green"}
        details = json.loads(error.error_details_json)
        assert details["reason"] == "missing_output_field"
        assert details["field"] == "answer"
        assert details["query_name"] == "approximate_hex"
    else:
        assert retained_llm == []
        assert transform_errors == []
    if scenario == "ragged_csv":
        assert len(retained_source) == 1
        assert retained_source[0]["__raw_line__"] == "invalid-colour,unexpected-cell"
        assert len(source_errors) == 1
        error = source_errors[0]
        assert error.destination == source_error_sink
        assert error.schema_mode == "parse"
        assert "expected 1 fields, got 2" in error.error
        assert json.loads(error.row_data_json) == retained_source[0]
    else:
        assert retained_source == []
        assert source_errors == []


@pytest.mark.parametrize("scenario", ["happy", "missing_answer", "ragged_csv"])
@pytest.mark.parametrize("provider", ["azure", "bedrock"])
def test_colour_multi_query_contract_repair(tmp_path: Path, scenario: ColourScenario, provider: Literal["azure", "bedrock"]) -> None:
    source_text = "colour\n" + "\n".join(COLOUR_ANSWERS) + "\n"
    if scenario == "ragged_csv":
        source_text += "invalid-colour,unexpected-cell\n"
    (tmp_path / "colours.csv").write_text(source_text)
    run_colour_pipeline(tmp_path, colour_pipeline_yaml(tmp_path, provider), scenario)
