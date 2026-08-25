# tests/integration/pipeline/test_llm_image_input_pipeline.py
"""E2E: rasterized-page shape through a stub provider; audit is bytes-free.

Task 6 (2026-08-25 llm-image-input plan). Rows carry ``page_blob_ref`` +
``page_mime_type`` — the ``pdf_rasterize`` output shape, authored directly
here (that branch is NOT a dependency) — through an ``llm`` transform
configured with ``image_inputs`` and a mocked Azure OpenAI SDK client acting
as the stub provider. Assembly follows the production path used by
tests/integration/pipeline/test_multi_source_isolation.py and
tests/integration/pipeline/test_field_resolution_union.py: YAML settings ->
instantiate_plugins_from_config -> ExecutionGraph -> PipelineConfig ->
Orchestrator.run(payload_store=...).

Assertions (spec, per task-6-brief.md Step 1):
  (a) output rows carry the response;
  (b) recorded call request ``messages`` contain the audit view, and —
      load-bearing — ``"base64" not in canonical_json(recorded_request)``;
  (c) a row whose blob ref is absent from the store produces an
      on_error-routed row with ``reason == "blob_not_found"`` while sibling
      rows succeed;
  (d) a text-only sibling pipeline's recorded request equals the pre-change
      dict shape ``[{"role": "user", "content": "<prompt>"}]`` exactly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.hashing import canonical_json
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, transform_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from tests.fixtures.landscape import make_factory
from tests.unit.contracts.test_chat_parts import PNG_BYTES

PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
_AZURE_MODEL = "gpt-4o"


@dataclass(frozen=True, slots=True)
class _RunFixture:
    db: LandscapeDB
    run_id: str
    payload_store: FilesystemPayloadStore


def _run_yaml_pipeline(tmp_path: Path, yaml_text: str, *, payload_store: FilesystemPayloadStore, db_name: str) -> _RunFixture:
    settings = load_settings_from_yaml_string(yaml_text)
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
    db = LandscapeDB(f"sqlite:///{tmp_path / db_name}")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=payload_store)
    return _RunFixture(db=db, run_id=result.run_id, payload_store=payload_store)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@contextmanager
def _stub_azure_provider(content: str) -> Generator[MagicMock, None, None]:
    """Stub provider: mocks the Azure OpenAI SDK client the real
    AzureLLMProvider constructs (``AzureLLMProvider._get_underlying_client``
    does ``from openai import AzureOpenAI`` at call time), mirroring
    tests/integration/plugins/llm/test_multi_query.py's mock. The transform,
    provider, and audit-recording code under test all run for real — only
    the outbound SDK boundary is stubbed.
    """

    def make_response(**_kwargs: Any) -> SimpleNamespace:
        raw_response = {"model": _AZURE_MODEL, "choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
            model=_AZURE_MODEL,
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model_dump=lambda *_a, **_k: raw_response,
        )

    client = MagicMock()
    client.chat.completions.create.side_effect = make_response
    with patch("openai.AzureOpenAI", return_value=client) as mock_cls:
        yield mock_cls


def _image_pipeline_yaml(*, input_path: Path, output_path: Path, quarantine_path: Path) -> str:
    return f"""
sources:
  rows:
    plugin: json
    on_success: inbound
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
queues:
  inbound: {{}}
transforms:
  - name: describe_page
    plugin: llm
    input: inbound
    on_success: output
    on_error: quarantine
    options:
      provider: azure
      deployment_name: {_AZURE_MODEL}
      endpoint: https://test.openai.azure.com
      api_key: test-key
      prompt_template: "Describe the attached page image."
      temperature: 0.0
      schema:
        mode: observed
      image_inputs:
        - field: page_blob_ref
          format_field: page_mime_type
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: {quarantine_path}
      format: jsonl
      schema:
        mode: observed
"""


def _text_only_pipeline_yaml(*, input_path: Path, output_path: Path) -> str:
    return f"""
sources:
  rows:
    plugin: json
    on_success: inbound
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
queues:
  inbound: {{}}
transforms:
  - name: greet
    plugin: llm
    input: inbound
    on_success: output
    on_error: discard
    options:
      provider: azure
      deployment_name: {_AZURE_MODEL}
      endpoint: https://test.openai.azure.com
      api_key: test-key
      prompt_template: "Say hello."
      temperature: 0.0
      schema:
        mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
"""


def _successful_llm_request_dicts(fixture: _RunFixture) -> list[dict[str, Any]]:
    """Retrieve every recorded SUCCESS LLM call's audit request as a dict.

    The request payload lives in the payload store, referenced by
    ``call.request_ref``, serialized as ``canonical_json`` bytes
    (core/landscape/execution/calls.py:_prepare_call_payloads).
    """
    with fixture.db.connection() as conn:
        state_rows = conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.run_id == fixture.run_id)).fetchall()
    state_ids = [row.state_id for row in state_rows]
    factory = make_factory(fixture.db, payload_store=fixture.payload_store)
    calls = factory.query.get_calls_for_states(state_ids)
    llm_success_calls = [c for c in calls if c.call_type == CallType.LLM and c.status == CallStatus.SUCCESS]
    requests: list[dict[str, Any]] = []
    for call in llm_success_calls:
        assert call.request_ref is not None, "SUCCESS LLM call must have a materialized request_ref"
        raw = fixture.payload_store.retrieve(call.request_ref)
        requests.append(json.loads(raw.decode("utf-8")))
    return requests


def _transform_error_reasons(fixture: _RunFixture) -> list[dict[str, Any]]:
    with fixture.db.connection() as conn:
        rows = conn.execute(
            select(transform_errors_table.c.error_details_json, transform_errors_table.c.destination).where(
                transform_errors_table.c.run_id == fixture.run_id
            )
        ).fetchall()
    return [{"reason": json.loads(row.error_details_json), "destination": row.destination} for row in rows]


def test_image_input_pipeline_e2e(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    payload_store.store(PNG_BYTES)

    missing_ref = "f" * 64
    rows = [
        {"id": 1, "page_blob_ref": PNG_SHA256, "page_mime_type": "image/png"},
        {"id": 2, "page_blob_ref": PNG_SHA256, "page_mime_type": "image/png"},
        {"id": 3, "page_blob_ref": missing_ref, "page_mime_type": "image/png"},
    ]
    input_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    response_content = "A scanned page showing a mountain landscape."

    with _stub_azure_provider(response_content):
        fixture = _run_yaml_pipeline(
            tmp_path,
            _image_pipeline_yaml(input_path=input_path, output_path=output_path, quarantine_path=quarantine_path),
            payload_store=payload_store,
            db_name="audit.db",
        )

    # (a) output rows carry the response; sibling rows succeed despite row 3's failure.
    output_rows = _read_jsonl(output_path)
    assert {row["id"] for row in output_rows} == {1, 2}
    for row in output_rows:
        assert row["llm_response"] == response_content
        assert row["page_blob_ref"] == PNG_SHA256

    # (c) the row with an unresolvable blob ref is on_error-routed with reason blob_not_found.
    quarantine_rows = _read_jsonl(quarantine_path)
    assert len(quarantine_rows) == 1
    assert quarantine_rows[0]["id"] == 3
    assert quarantine_rows[0]["page_blob_ref"] == missing_ref

    error_reasons = _transform_error_reasons(fixture)
    assert len(error_reasons) == 1
    assert error_reasons[0]["reason"]["reason"] == "blob_not_found"
    assert error_reasons[0]["reason"]["field"] == "page_blob_ref"
    assert error_reasons[0]["reason"]["blob_ref"] == missing_ref
    assert error_reasons[0]["destination"] == "quarantine"

    # (b) recorded call requests carry the bytes-free audit view — never base64.
    requests = _successful_llm_request_dicts(fixture)
    assert len(requests) == 2
    for request in requests:
        recorded_json = canonical_json(request)
        assert "base64" not in recorded_json
        messages = request["messages"]
        assert len(messages) == 1
        user_message = messages[0]
        assert user_message["role"] == "user"
        content = user_message["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        text_part, image_part = content
        assert text_part == {"type": "text", "text": "Describe the attached page image."}
        assert image_part == {
            "type": "image",
            "format": "png",
            "sha256": PNG_SHA256,
            "byte_count": len(PNG_BYTES),
            "blob_ref": PNG_SHA256,
        }


def test_text_only_sibling_pipeline_recorded_request_matches_pre_change_shape(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    input_path.write_text(json.dumps({"id": 1}) + "\n")

    response_content = "Hello!"

    with _stub_azure_provider(response_content):
        fixture = _run_yaml_pipeline(
            tmp_path,
            _text_only_pipeline_yaml(input_path=input_path, output_path=output_path),
            payload_store=payload_store,
            db_name="audit.db",
        )

    output_rows = _read_jsonl(output_path)
    assert len(output_rows) == 1
    assert output_rows[0]["llm_response"] == response_content

    requests = _successful_llm_request_dicts(fixture)
    assert len(requests) == 1
    # (d) pre-change dict shape, byte-identical: plain str content, no system message.
    assert requests[0]["messages"] == [{"role": "user", "content": "Say hello."}]
