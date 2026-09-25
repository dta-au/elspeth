"""A lost render worker is never a row's fault at a real consumer (RULINGS RC-9).

``SandboxedTemplate`` raises ``TemplateWorkerLostError`` (a retryable
``PluginRetryableError``, not a ``TemplateError``) when the worker rendering a
row is ended by a signal the row did not cause. The property that matters is
end to end: the row is retried on a new worker and never reaches ``on_error``
while retries remain. It holds only if every consumer that turns a
``TemplateError`` into a routed row error lets the lost worker through, and the
engine retries it. The exception class alone pins neither half: a consumer that
also caught ``PluginRetryableError`` would route the row after one attempt with
every class-level test still green (review-S3b-capacity-r2 F5, mutant N17).

The loss is real: a spawned render worker is SIGKILLed while it holds the
row's request. The first worker a test starts dies that way; every later one is
the production worker (``templates._template_worker``), so the retry renders on
a newly started worker exactly as it does in a run.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import RunStatus
from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.token_usage import TokenUsage
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, nodes_table, token_outcomes_table, transform_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure import templates
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.infrastructure.templates import TemplateWorkerLostError
from elspeth.plugins.transforms.azure.ai_search import AzureAISearchTransform
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context, make_token_info

# The worker a test starts reads this marker path from its environment (a
# spawned child inherits the parent's). An absent marker means "no worker has
# been lost yet": the worker creates it and dies holding the request.
_LOSS_MARKER_ENV = "ELSPETH_TEST_TEMPLATE_WORKER_LOSS_MARKER"
_LOST_TEXT = f"Template worker was stopped by signal {int(signal.SIGKILL)}"


def _worker_lost_on_the_first_request(connection: Any) -> None:
    """Spawn target (runs in the child): the first worker dies mid-request, later ones are real."""
    marker = Path(os.environ[_LOSS_MARKER_ENV])
    if marker.exists():
        templates._template_worker(connection)
        return
    marker.touch()
    connection.send(("ready", ""))
    connection.recv()
    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(60)


def _worker_lost_on_every_request(connection: Any) -> None:
    """Spawn target (runs in the child): every worker dies mid-request."""
    connection.send(("ready", ""))
    connection.recv()
    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(60)


@pytest.fixture(autouse=True)
def _set_fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-fingerprint-key-for-worker-loss")


@pytest.fixture
def first_worker_lost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """No warm worker; the next worker started dies holding the row's request."""
    marker = tmp_path / "worker-lost"
    monkeypatch.setenv(_LOSS_MARKER_ENV, str(marker))
    templates._stop_template_workers()
    monkeypatch.setattr(templates, "_template_worker", _worker_lost_on_the_first_request)
    try:
        yield marker
    finally:
        templates._stop_template_workers()


@pytest.fixture
def every_worker_lost(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    templates._stop_template_workers()
    monkeypatch.setattr(templates, "_template_worker", _worker_lost_on_every_request)
    try:
        yield
    finally:
        templates._stop_template_workers()


# --- each consumer that routes a TemplateError lets a lost worker through ----


def _llm_ctx() -> PluginContext:
    return make_context(
        state_id="state-123",
        run_id="run-123",
        token=TokenInfo(row_id="row-1", token_id="token-1", row_data=make_pipeline_row({})),
    )


def _llm_transform(**overrides: Any) -> tuple[LLMTransform, Mock]:
    config: dict[str, Any] = {
        "provider": "azure",
        "deployment_name": "gpt-4o",
        "endpoint": "https://test.openai.azure.com",
        "api_key": "test-key",
        "prompt_template": "Classify: {{ row.text }}",
        "schema": {"mode": "observed"},
        "required_input_fields": ["text"],
    }
    config.update(overrides)
    transform = LLMTransform(config)
    provider = Mock(spec=LLMProvider)
    provider.execute_query.return_value = LLMQueryResult(
        content="result", usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP
    )
    transform._provider = provider
    return transform, provider


@contextmanager
def _llm_single_query() -> Iterator[tuple[Mock, Callable[[], Any]]]:
    transform, provider = _llm_transform()
    yield provider.execute_query, lambda: transform._process_row(make_pipeline_row({"text": "hello"}), _llm_ctx())


@contextmanager
def _llm_multi_query() -> Iterator[tuple[Mock, Callable[[], Any]]]:
    # pool_size 1: the sequential per-query render (llm/transform.py's query loop).
    transform, provider = _llm_transform(
        prompt_template="Process this: {{ row.text_content }}",
        queries={"quality": {"input_fields": {"text_content": "text"}}},
    )
    yield provider.execute_query, lambda: transform._process_row(make_pipeline_row({"text": "hello"}), _llm_ctx())


@contextmanager
def _rag_query_template() -> Iterator[tuple[Mock, Callable[[], Any]]]:
    transform = AzureAISearchTransform(
        {
            "output_prefix": "policy",
            "query_field": "question",
            "query_template": "Policy question: {{ query }}",
            "endpoint": "https://test.search.windows.net",
            "index": "test-index",
            "api_key": "test-key",
            "schema_config": {"mode": "observed"},
        }
    )
    # on_start builds the provider and makes no network call (readiness is runtime_preflight's).
    transform.on_start(make_context(run_id="run-1", node_id="rag-retrieval", landscape=Mock()))
    row = PipelineRow({"question": "refunds?"}, SchemaContract(mode="OBSERVED", fields=()))
    ctx = make_context(run_id="run-1", state_id="state-1", token=make_token_info(token_id="token-1"))
    chunks = [RetrievalChunk(content="Section 1", score=0.9, source_id="doc1", metadata={})]
    with patch.object(transform._searcher, "search", return_value=chunks) as search:
        yield search, lambda: transform.process(row, ctx)


_CONSUMERS = {
    "llm_single_query": _llm_single_query,
    "llm_multi_query": _llm_multi_query,
    "rag_query_template": _rag_query_template,
}


@pytest.mark.usefixtures("first_worker_lost")
@pytest.mark.parametrize("consumer", sorted(_CONSUMERS))
def test_a_lost_worker_leaves_the_consumer_as_the_retryable_error_then_the_retry_renders(consumer: str) -> None:
    """Not a routed ``template_rendering_failed`` result: the error leaves the
    plugin for the engine's retry, and the provider is never called with the
    lost attempt. The next attempt on the same plugin renders on a new worker."""
    with _CONSUMERS[consumer]() as (downstream, process):
        with pytest.raises(TemplateWorkerLostError) as caught:
            process()
        assert str(caught.value) == _LOST_TEXT
        assert caught.value.retryable is True
        downstream.assert_not_called()

        result = process()
        assert result.status == "success"
        downstream.assert_called_once()


# --- a run retries the row on a new worker; only spent retries route it ------


def _rag_run(tmp_path: Path, *, max_attempts: int) -> tuple[LandscapeDB, Any, Path, Path]:
    """One row through a real ``rag_retrieval`` node (Chroma, ephemeral) under ``elspeth run``'s engine."""
    chromadb = pytest.importorskip("chromadb")
    collection = f"worker-loss-{tmp_path.name}"
    chromadb.Client().get_or_create_collection(name=collection, metadata={"hnsw:space": "cosine"}).add(
        documents=["Refunds are processed within 30 days of purchase."], ids=["policy-1"]
    )
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps({"id": 1, "q": "refund policy"}) + "\n")
    output_path = tmp_path / "output.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    settings = load_settings_from_yaml_string(
        f"""
sources:
  primary:
    plugin: json
    on_success: source_out
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: fixed
        fields: ["id: int", "q: str"]
transforms:
  - name: rag_0
    plugin: rag_retrieval
    input: source_out
    on_success: output
    on_error: quarantine
    options:
      query_field: q
      query_template: "Policy: {{{{ row.q }}}}"
      output_prefix: kb
      provider: chroma
      provider_config:
        collection: {collection}
        mode: ephemeral
        distance_function: cosine
      top_k: 1
      min_score: 0.0
      on_no_results: continue
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
  quarantine:
    plugin: json
    on_write_failure: discard
    options:
      path: {quarantine_path}
      format: jsonl
      schema:
        mode: observed
retry:
  max_attempts: {max_attempts}
  initial_delay_seconds: 0.01
  max_delay_seconds: 0.1
"""
    )
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
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    return db, result, output_path, quarantine_path


def _rag_attempts(db: LandscapeDB, run_id: str) -> list[tuple[int, str, dict[str, Any] | None]]:
    with db.connection() as conn:
        node_id = conn.execute(
            select(nodes_table.c.node_id).where(nodes_table.c.run_id == run_id).where(nodes_table.c.plugin_name == "rag_retrieval")
        ).scalar_one()
        states = conn.execute(
            select(node_states_table.c.attempt, node_states_table.c.status, node_states_table.c.error_json)
            .where(node_states_table.c.run_id == run_id)
            .where(node_states_table.c.node_id == node_id)
            .order_by(node_states_table.c.attempt)
        ).all()
    return [(int(s.attempt), str(s.status), None if s.error_json is None else json.loads(s.error_json)) for s in states]


def _outcomes(db: LandscapeDB, run_id: str) -> list[tuple[str, str | None]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(token_outcomes_table.c.outcome, token_outcomes_table.c.sink_name).where(token_outcomes_table.c.run_id == run_id)
        ).all()
    return [(str(r.outcome), r.sink_name) for r in rows]


def _transform_errors(db: LandscapeDB, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(transform_errors_table.c.destination, transform_errors_table.c.error_details_json).where(
                transform_errors_table.c.run_id == run_id
            )
        ).all()
    return [(str(r.destination), json.loads(r.error_details_json)) for r in rows]


_LOST_ATTEMPT = {"exception": _LOST_TEXT, "type": "TemplateWorkerLostError"}


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("first_worker_lost")
def test_a_run_retries_a_row_whose_render_worker_was_lost_and_never_routes_it(tmp_path: Path) -> None:
    db, result, output_path, quarantine_path = _rag_run(tmp_path, max_attempts=3)

    assert result.status == RunStatus.COMPLETED
    assert (result.rows_processed, result.rows_succeeded, result.rows_quarantined) == (1, 1, 0)
    assert _rag_attempts(db, result.run_id) == [(0, "failed", _LOST_ATTEMPT), (1, "completed", None)]
    assert _outcomes(db, result.run_id) == [("success", "output")]
    assert _transform_errors(db, result.run_id) == []
    assert [json.loads(line)["id"] for line in output_path.read_text().splitlines()] == [1]
    assert not quarantine_path.exists() or quarantine_path.read_text() == ""


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("every_worker_lost")
def test_a_row_goes_to_on_error_only_when_its_retries_are_spent(tmp_path: Path) -> None:
    db, result, _output_path, quarantine_path = _rag_run(tmp_path, max_attempts=2)

    assert _rag_attempts(db, result.run_id) == [(0, "failed", _LOST_ATTEMPT), (1, "failed", _LOST_ATTEMPT)]
    assert _outcomes(db, result.run_id) == [("failure", "quarantine")]
    assert _transform_errors(db, result.run_id) == [("quarantine", {"reason": "retry_exhausted", "error": _LOST_TEXT, "attempts": 2})]
    assert [json.loads(line)["id"] for line in quarantine_path.read_text().splitlines()] == [1]
