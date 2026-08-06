"""Real-engine integration proof for the source-native single-prompt LLM."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import patch

import httpx
import pytest
import respx
from litellm.types.utils import ModelResponse, Usage
from sqlalchemy import func, select

from elspeth.contracts import CallStatus, CallType, Determinism, PipelineRow, ResumePoint, RunStatus, SourceProtocol
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import GracefulShutdownError, IncompleteSourceResumeError
from elspeth.contracts.sink_effects import SinkEffectState
from elspeth.contracts.types import AggregationName
from elspeth.core.canonical import stable_hash
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import AggregationSettings, CheckpointSettings, SourceSettings, TriggerConfig
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import RowDataState
from elspeth.core.landscape.schema import node_states_table, operations_table, rows_table, tokens_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl, discover_all_plugins
from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.llm import LLMSource
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform

ProviderName = Literal["azure", "bedrock", "openrouter", "gateway"]

_GATEWAY_CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"


def _provider_config(provider: ProviderName) -> dict[str, Any]:
    common: dict[str, Any] = {
        "prompt_template": "Summarise the audit topic.",
        "response_field": "answer",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    if provider == "azure":
        return {
            **common,
            "provider": "azure",
            "deployment_name": "gpt-4o-mini",
            "endpoint": "https://example.openai.azure.com",
            "api_key": "test-api-key",
        }
    if provider == "bedrock":
        return {
            **common,
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku",
        }
    if provider == "openrouter":
        return {
            **common,
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "test-api-key",
        }
    return {
        **common,
        "provider": "gateway",
        "model": "summariser",
        "endpoint": "https://gateway.example/v1",
        "api_key": "test-api-key",
    }


def _isolated_llm_manager() -> PluginManager:
    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([LLMSource], "elspeth_get_source"))
    return manager


@pytest.fixture
def isolated_manager() -> Iterator[PluginManager]:
    shared = get_shared_plugin_manager()
    shared_before = {
        "sources": tuple(shared.get_sources()),
        "transforms": tuple(shared.get_transforms()),
        "sinks": tuple(shared.get_sinks()),
    }
    discovered_before = {kind: tuple(classes) for kind, classes in discover_all_plugins().items()}
    assert LLMSource in shared_before["sources"]
    assert LLMSource in discovered_before["sources"]

    manager = _isolated_llm_manager()
    assert manager is not shared
    assert manager.get_source_by_name("llm") is LLMSource
    assert manager.get_sources() == [LLMSource]
    assert manager.get_transforms() == []
    assert manager.get_sinks() == []
    yield manager

    shared_after = get_shared_plugin_manager()
    assert shared_after is shared
    assert tuple(shared_after.get_sources()) == shared_before["sources"]
    assert tuple(shared_after.get_transforms()) == shared_before["transforms"]
    assert tuple(shared_after.get_sinks()) == shared_before["sinks"]
    assert {kind: tuple(classes) for kind, classes in discover_all_plugins().items()} == discovered_before


def _build_pipeline(
    *,
    manager: PluginManager,
    provider: ProviderName,
    tmp_path: Path,
) -> tuple[LandscapeDB, FilesystemPayloadStore, LLMSource, Path, PipelineConfig, ExecutionGraph]:
    config = _provider_config(provider)
    source_cls = manager.get_source_by_name("llm")
    source = cast(LLMSource, source_cls(config))
    source.on_success = "output"
    output_path = tmp_path / "output.jsonl"
    sink = JSONSink(
        {
            "path": str(output_path),
            "format": "jsonl",
            "mode": "write",
            "collision_policy": "fail_if_exists",
            "schema": {"mode": "observed"},
        }
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": cast(SourceProtocol, source)},
        source_settings_map={
            "primary": SourceSettings(
                plugin="llm",
                on_success="output",
                options=config,
            )
        },
        transforms=[],
        sinks={"output": as_sink(sink)},
        aggregations={},
        gates=[],
    )
    pipeline = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"output": as_sink(sink)},
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    return db, payload_store, source, output_path, pipeline, graph


def _assert_persisted_row_and_artifact(
    *,
    factory: RecorderFactory,
    payload_store: FilesystemPayloadStore,
    output_path: Path,
    run_id: str,
    expected_row: dict[str, Any],
    expected_token_count: int = 1,
) -> tuple[str, str, str, str]:
    """Prove the exact source row crosses payload, token, effect, and artifact boundaries."""
    rows = factory.query.get_rows(run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.row_index == 0
    assert row.source_row_index == 0
    assert row.ingest_sequence == 0
    assert row.source_data_ref is not None
    payload_bytes = payload_store.retrieve(row.source_data_ref)
    assert sha256(payload_bytes).hexdigest() == row.source_data_ref
    assert row.source_data_hash == stable_hash(expected_row)
    assert row.source_data_ref == row.source_data_hash
    assert json.loads(payload_bytes) == expected_row
    row_data = factory.query.get_row_data(row.row_id)
    assert row_data.state is RowDataState.AVAILABLE
    assert row_data.data is not None
    assert dict(row_data.data) == expected_row

    tokens = factory.query.get_tokens(row.row_id)
    assert len(tokens) == expected_token_count

    output_bytes = output_path.read_bytes()
    assert [json.loads(line) for line in output_bytes.splitlines()] == [expected_row]
    artifacts = factory.execution.get_artifacts(run_id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.path_or_uri == output_path.as_uri()
    assert artifact.content_hash == sha256(output_bytes).hexdigest()
    assert artifact.size_bytes == len(output_bytes)
    assert artifact.publication_performed is True

    effects = factory.execution.sink_effects.get_effects_for_run(run_id)
    assert len(effects) == 1
    effect = effects[0]
    assert effect.state is SinkEffectState.FINALIZED
    assert effect.artifact_id == artifact.artifact_id
    assert effect.result_descriptor_hash is not None
    assert effect.publication_performed is True
    assert artifact.sink_effect_id == effect.effect_id
    assert artifact.idempotency_key == effect.artifact_idempotency_key
    members = factory.execution.sink_effects.get_members(effect.effect_id)
    assert len(members) == 1
    member = members[0]
    assert member.ordinal == 0
    assert member.row_id == row.row_id
    assert member.token_id in {token.token_id for token in tokens}
    assert member.payload_hash == stable_hash(expected_row)
    assert member.prepared_disposition == "accepted"
    assert factory.query.get_effect_artifact_members(run_id) == {artifact.artifact_id: (member.token_id,)}
    return row.row_id, member.token_id, effect.effect_id, artifact.artifact_id


def _assert_only_source_operation_exists(db: LandscapeDB) -> None:
    with db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(rows_table)) == 0
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 0
        assert conn.scalar(select(func.count()).select_from(node_states_table)) == 0
        operations = conn.execute(select(operations_table.c.operation_type, operations_table.c.status)).all()
        assert operations == [("source_load", "open")]


def _azure_response() -> SimpleNamespace:
    raw = {
        "model": "served-azure-model",
        "choices": [{"finish_reason": "stop", "message": {"content": "A careful answer"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    return SimpleNamespace(
        model="served-azure-model",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="A careful answer"))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        model_dump=lambda: raw,
    )


def _bedrock_response() -> ModelResponse:
    return ModelResponse(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "A careful answer"},
                "finish_reason": "stop",
            }
        ],
        model="bedrock/served-model",
        usage=Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    )


def _http_success_body(provider: ProviderName) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "summariser" if provider == "gateway" else "openai/gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "A careful answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    return body


@contextmanager
def _provider_seam(
    provider: ProviderName,
    *,
    db: LandscapeDB,
    fail: bool,
) -> Iterator[None]:
    def boundary_check() -> None:
        _assert_only_source_operation_exists(db)

    if provider == "azure":

        def create(**_kwargs: Any) -> Any:
            boundary_check()
            if fail:
                raise RuntimeError("invalid provider request")
            return _azure_response()

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=lambda: None,
        )
        with patch("openai.AzureOpenAI", return_value=client):
            yield
        return

    if provider == "bedrock":

        def completion(**_kwargs: Any) -> Any:
            boundary_check()
            if fail:
                raise RuntimeError("invalid provider request")
            return _bedrock_response()

        with patch("litellm.completion", side_effect=completion):
            yield
        return

    endpoint = "https://gateway.example/v1/chat/completions" if provider == "gateway" else "https://openrouter.ai/api/v1/chat/completions"

    def respond(_request: httpx.Request) -> httpx.Response:
        boundary_check()
        headers = {"content-type": "application/json"}
        if provider == "gateway":
            headers[_GATEWAY_CONTRACT_HEADER] = "1"
        if fail:
            if provider == "gateway":
                body = {
                    "error": {
                        "message": "sanitized",
                        "type": "gateway_error",
                        "code": "invalid_request",
                        "retryable": False,
                        "request_id": "request-1",
                    }
                }
            else:
                body = {"error": {"message": "invalid provider request"}}
            return httpx.Response(400, json=body, headers=headers)
        return httpx.Response(200, json=_http_success_body(provider), headers=headers)

    with respx.mock(assert_all_called=True) as router:
        router.post(endpoint).mock(side_effect=respond)
        yield


def _single_run_id(factory: RecorderFactory) -> str:
    runs = factory.run_lifecycle.list_runs()
    assert len(runs) == 1
    return runs[0].run_id


class _FailOnceEOFPassthrough(BaseTransform):
    """Crash the first EOF aggregation flush, then pass its only row."""

    name = "fail_once_llm_eof"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema
    is_batch_aware = True

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.on_success = "output"
        self.on_error = "discard"
        self._fail_next_batch = True
        self.batch_calls = 0

    def process(self, rows: list[PipelineRow], ctx: Any) -> TransformResult:  # type: ignore[override]
        del ctx
        self.batch_calls += 1
        if self._fail_next_batch:
            self._fail_next_batch = False
            raise RuntimeError("injected LLM EOF flush crash")
        assert len(rows) == 1
        return TransformResult.success(rows[0], success_reason={"action": "test"})


def _build_eof_pipeline(
    *,
    manager: PluginManager,
    tmp_path: Path,
) -> tuple[
    LandscapeDB,
    FilesystemPayloadStore,
    LLMSource,
    Path,
    _FailOnceEOFPassthrough,
    PipelineConfig,
    ExecutionGraph,
]:
    source_config = _provider_config("openrouter")
    source_cls = manager.get_source_by_name("llm")
    source = cast(LLMSource, source_cls(source_config))
    source.on_success = "batch_in"
    output_path = tmp_path / "output.jsonl"
    sink = JSONSink(
        {
            "path": str(output_path),
            "format": "jsonl",
            "mode": "write",
            "collision_policy": "fail_if_exists",
            "schema": {"mode": "observed"},
        }
    )
    transform = _FailOnceEOFPassthrough()
    settings = AggregationSettings(
        name="eof_llm",
        plugin=transform.name,
        input="batch_in",
        on_success="output",
        on_error="discard",
        trigger=TriggerConfig(count=100, timeout_seconds=3600),
        output_mode="transform",
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": cast(SourceProtocol, source)},
        source_settings_map={
            "primary": SourceSettings(plugin="llm", on_success="batch_in", options=source_config),
        },
        transforms=[],
        sinks={"output": as_sink(sink)},
        aggregations={"eof_llm": (as_transform(transform), settings)},
        gates=[],
    )
    aggregation_id = graph.get_aggregation_id_map()[AggregationName("eof_llm")]
    transform.node_id = aggregation_id
    pipeline = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={"output": as_sink(sink)},
        aggregation_settings={aggregation_id: settings},
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    return db, payload_store, source, output_path, transform, pipeline, graph


@pytest.mark.parametrize("provider", ["azure", "bedrock", "openrouter", "gateway"])
def test_provider_success_rows_share_real_source_load_operation(
    provider: ProviderName,
    isolated_manager: PluginManager,
    tmp_path: Path,
) -> None:
    db, payload_store, _source, output_path, pipeline, graph = _build_pipeline(
        manager=isolated_manager,
        provider=provider,
        tmp_path=tmp_path,
    )

    with _provider_seam(provider, db=db, fail=False):
        result = Orchestrator(db).run(pipeline, graph=graph, payload_store=payload_store)

    assert result.status is RunStatus.COMPLETED
    assert result.rows_processed == 1
    expected_row = {
        "answer": "A careful answer",
        "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        "answer_model": "summariser"
        if provider == "gateway"
        else ("bedrock/served-model" if provider == "bedrock" else ("served-azure-model" if provider == "azure" else "openai/gpt-4o-mini")),
    }

    factory = RecorderFactory(db, payload_store=payload_store)
    operations = factory.execution.get_operations_for_run(result.run_id)
    source_operations = [operation for operation in operations if operation.operation_type == "source_load"]
    assert len(source_operations) == 1
    operation = source_operations[0]
    assert operation.status == "completed"
    calls = factory.execution.get_operation_calls(operation.operation_id)
    expected_types = [CallType.LLM] if provider in {"azure", "bedrock"} else [CallType.HTTP, CallType.LLM]
    assert [call.call_type for call in calls] == expected_types
    assert all(call.status is CallStatus.SUCCESS for call in calls)
    assert all(call.operation_id == operation.operation_id and call.state_id is None for call in calls)

    _assert_persisted_row_and_artifact(
        factory=factory,
        payload_store=payload_store,
        output_path=output_path,
        run_id=result.run_id,
        expected_row=expected_row,
    )


@pytest.mark.parametrize("provider", ["azure", "bedrock", "openrouter", "gateway"])
def test_provider_failure_rows_are_operation_parented_errors_without_rows_or_tokens(
    provider: ProviderName,
    isolated_manager: PluginManager,
    tmp_path: Path,
) -> None:
    db, payload_store, _source, output_path, pipeline, graph = _build_pipeline(
        manager=isolated_manager,
        provider=provider,
        tmp_path=tmp_path,
    )

    with (
        _provider_seam(provider, db=db, fail=True),
        pytest.raises(
            LLMClientError,
            match=r"LLM provider request failed|Bedrock LLM request failed|Gateway LLM request failed|HTTP 400",
        ),
    ):
        Orchestrator(db).run(pipeline, graph=graph, payload_store=payload_store)

    factory = RecorderFactory(db, payload_store=payload_store)
    run_id = _single_run_id(factory)
    operations = factory.execution.get_operations_for_run(run_id)
    source_operations = [operation for operation in operations if operation.operation_type == "source_load"]
    assert len(source_operations) == 1
    operation = source_operations[0]
    assert operation.status == "failed"
    calls = factory.execution.get_operation_calls(operation.operation_id)
    expected_types = [CallType.LLM] if provider in {"azure", "bedrock"} else [CallType.HTTP, CallType.LLM]
    assert [call.call_type for call in calls] == expected_types
    assert all(call.status is CallStatus.ERROR for call in calls)
    assert all(call.operation_id == operation.operation_id and call.state_id is None for call in calls)
    assert factory.query.get_rows(run_id) == []
    assert factory.execution.get_artifacts(run_id) == []
    assert factory.execution.sink_effects.get_effects_for_run(run_id) == ()
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("provider_usage", "expected_usage"),
    [
        ({"prompt_tokens": 7, "completion_tokens": 3}, {"prompt_tokens": 7, "completion_tokens": 3}),
        ({"prompt_tokens": 7}, {"prompt_tokens": 7}),
        (None, {}),
        (
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 99},
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 99},
        ),
    ],
    ids=["known", "partial", "unknown", "inconsistent-total"],
)
def test_usage_shapes_survive_real_pipeline_without_fabrication(
    provider_usage: dict[str, int] | None,
    expected_usage: dict[str, int],
    isolated_manager: PluginManager,
    tmp_path: Path,
) -> None:
    db, payload_store, _source, output_path, pipeline, graph = _build_pipeline(
        manager=isolated_manager,
        provider="openrouter",
        tmp_path=tmp_path,
    )
    body = _http_success_body("openrouter")
    if provider_usage is None:
        del body["usage"]
    else:
        body["usage"] = provider_usage

    def respond(_request: httpx.Request) -> httpx.Response:
        _assert_only_source_operation_exists(db)
        return httpx.Response(200, json=body, headers={"content-type": "application/json"})

    with respx.mock(assert_all_called=True) as router:
        router.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=respond)
        result = Orchestrator(db).run(pipeline, graph=graph, payload_store=payload_store)

    assert result.status is RunStatus.COMPLETED
    expected_row = {
        "answer": "A careful answer",
        "answer_usage": expected_usage,
        "answer_model": "openai/gpt-4o-mini",
    }
    _assert_persisted_row_and_artifact(
        factory=RecorderFactory(db, payload_store=payload_store),
        payload_store=payload_store,
        output_path=output_path,
        run_id=result.run_id,
        expected_row=expected_row,
    )


def test_exhausted_llm_source_is_not_reopened_when_eof_work_resumes(
    isolated_manager: PluginManager,
    tmp_path: Path,
) -> None:
    db, payload_store, source, output_path, transform, pipeline, graph = _build_eof_pipeline(
        manager=isolated_manager,
        tmp_path=tmp_path,
    )
    checkpoint_manager = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    orchestrator = Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=checkpoint_config,
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        _assert_only_source_operation_exists(db)
        return httpx.Response(
            200,
            json=_http_success_body("openrouter"),
            headers={"content-type": "application/json"},
        )

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=respond)
        with pytest.raises(RuntimeError, match="injected LLM EOF flush crash"):
            orchestrator.run(pipeline, graph=graph, payload_store=payload_store)

        factory = RecorderFactory(db, payload_store=payload_store)
        run_id = _single_run_id(factory)
        lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records(run_id)
        source_lifecycle = next(record for record in lifecycle.values() if record.source_name == "primary")
        assert source_lifecycle.lifecycle_state == "exhausted"
        assert source._load_started is True
        assert route.call_count == 1
        assert not output_path.exists()
        failed_run = factory.run_lifecycle.get_run(run_id)
        assert failed_run is not None
        assert failed_run.status is RunStatus.FAILED
        operations_before_resume = factory.execution.get_operations_for_run(run_id)
        assert len(operations_before_resume) == 1
        source_operation = operations_before_resume[0]
        assert source_operation.operation_type == "source_load"
        assert source_operation.status == "failed"
        calls_before_resume = factory.execution.get_operation_calls(source_operation.operation_id)
        assert [call.call_type for call in calls_before_resume] == [CallType.HTTP, CallType.LLM]
        assert all(call.status is CallStatus.SUCCESS for call in calls_before_resume)
        rows_before_resume = factory.query.get_rows(run_id)
        assert len(rows_before_resume) == 1
        assert len(factory.query.get_tokens(rows_before_resume[0].row_id)) == 1
        assert factory.execution.get_artifacts(run_id) == []
        assert factory.execution.sink_effects.get_effects_for_run(run_id) == ()
        assert checkpoint_manager.get_latest_checkpoint(run_id) is not None

        recovery = RecoveryManager(db, checkpoint_manager)
        resume_point = recovery.get_resume_point(run_id, graph)
        assert resume_point is not None
        resumed = orchestrator.resume(
            resume_point=resume_point,
            config=pipeline,
            graph=graph,
            payload_store=payload_store,
        )

    assert resumed.status is RunStatus.COMPLETED
    assert route.call_count == 1
    assert transform.batch_calls == 2
    post_factory = RecorderFactory(db, payload_store=payload_store)
    completed_run = post_factory.run_lifecycle.get_run(run_id)
    assert completed_run is not None
    assert completed_run.status is RunStatus.COMPLETED
    post_lifecycle = post_factory.run_lifecycle.get_run_source_lifecycle_records(run_id)
    post_source_lifecycle = next(record for record in post_lifecycle.values() if record.source_name == "primary")
    assert post_source_lifecycle.lifecycle_state == "exhausted"
    operations_after_resume = post_factory.execution.get_operations_for_run(run_id)
    source_operations_after_resume = [operation for operation in operations_after_resume if operation.operation_type == "source_load"]
    assert source_operations_after_resume == operations_before_resume
    assert post_factory.execution.get_operation_calls(source_operation.operation_id) == calls_before_resume
    assert checkpoint_manager.get_latest_checkpoint(run_id) is None
    _assert_persisted_row_and_artifact(
        factory=post_factory,
        payload_store=payload_store,
        output_path=output_path,
        run_id=run_id,
        expected_row={
            "answer": "A careful answer",
            "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            "answer_model": "openai/gpt-4o-mini",
        },
        expected_token_count=2,
    )


def test_interrupted_llm_source_refuses_resume_before_exhaustion(
    isolated_manager: PluginManager,
    tmp_path: Path,
) -> None:
    db, payload_store, source, output_path, pipeline, graph = _build_pipeline(
        manager=isolated_manager,
        provider="openrouter",
        tmp_path=tmp_path,
    )
    checkpoint_manager = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    orchestrator = Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=checkpoint_config,
    )
    shutdown_event = threading.Event()

    def respond(_request: httpx.Request) -> httpx.Response:
        _assert_only_source_operation_exists(db)
        shutdown_event.set()
        return httpx.Response(
            200,
            json=_http_success_body("openrouter"),
            headers={"content-type": "application/json"},
        )

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=respond)
        with pytest.raises(GracefulShutdownError):
            orchestrator.run(
                pipeline,
                graph=graph,
                payload_store=payload_store,
                shutdown_event=shutdown_event,
            )

        factory = RecorderFactory(db, payload_store=payload_store)
        run_id = _single_run_id(factory)
        lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records(run_id)
        source_lifecycle = next(record for record in lifecycle.values() if record.source_name == "primary")
        assert source_lifecycle.lifecycle_state == "interrupted"
        assert source._load_started is True
        assert route.call_count == 1
        interrupted_run = factory.run_lifecycle.get_run(run_id)
        assert interrupted_run is not None
        assert interrupted_run.status is RunStatus.INTERRUPTED
        operations_before_resume = factory.execution.get_operations_for_run(run_id)
        source_operations_before_resume = [operation for operation in operations_before_resume if operation.operation_type == "source_load"]
        assert len(source_operations_before_resume) == 1
        source_operation = source_operations_before_resume[0]
        assert source_operation.operation_type == "source_load"
        assert source_operation.status == "completed"
        calls_before_resume = factory.execution.get_operation_calls(source_operation.operation_id)
        assert [call.call_type for call in calls_before_resume] == [CallType.HTTP, CallType.LLM]
        assert all(call.status is CallStatus.SUCCESS for call in calls_before_resume)
        persisted_ids_before_resume = _assert_persisted_row_and_artifact(
            factory=factory,
            payload_store=payload_store,
            output_path=output_path,
            run_id=run_id,
            expected_row={
                "answer": "A careful answer",
                "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                "answer_model": "openai/gpt-4o-mini",
            },
        )
        checkpoint_before_resume = checkpoint_manager.get_latest_checkpoint(run_id)
        assert checkpoint_before_resume is not None
        rows_before_resume = factory.query.get_rows(run_id)
        tokens_before_resume = factory.query.get_tokens(rows_before_resume[0].row_id)
        artifacts_before_resume = factory.execution.get_artifacts(run_id)
        effects_before_resume = factory.execution.sink_effects.get_effects_for_run(run_id)
        members_before_resume = factory.execution.sink_effects.get_members_for_run(run_id)
        output_before_resume = output_path.read_bytes()

        # elspeth-1f5b83cd28: the advisory gate refuses the interrupted
        # source, so get_resume_point returns None. Hand-build the resume
        # point — the enforcing guard must refuse on its own authority.
        recovery = RecoveryManager(db, checkpoint_manager)
        check = recovery.can_resume(run_id, graph)
        assert not check.can_resume
        assert check.reason is not None
        assert "primary=interrupted" in check.reason
        resume_point = ResumePoint(
            checkpoint=checkpoint_before_resume,
            sequence_number=checkpoint_before_resume.sequence_number,
        )
        with pytest.raises(IncompleteSourceResumeError, match=r"primary.*interrupted"):
            orchestrator.resume(
                resume_point=resume_point,
                config=pipeline,
                graph=graph,
                payload_store=payload_store,
            )

    assert route.call_count == 1
    assert output_path.read_bytes() == output_before_resume
    post_factory = RecorderFactory(db, payload_store=payload_store)
    assert post_factory.run_lifecycle.get_run(run_id) == interrupted_run
    post_lifecycle = post_factory.run_lifecycle.get_run_source_lifecycle_records(run_id)
    post_source_lifecycle = next(record for record in post_lifecycle.values() if record.source_name == "primary")
    assert post_source_lifecycle.lifecycle_state == "interrupted"
    assert post_factory.execution.get_operations_for_run(run_id) == operations_before_resume
    assert post_factory.execution.get_operation_calls(source_operation.operation_id) == calls_before_resume
    assert post_factory.query.get_rows(run_id) == rows_before_resume
    assert post_factory.query.get_tokens(rows_before_resume[0].row_id) == tokens_before_resume
    assert post_factory.execution.get_artifacts(run_id) == artifacts_before_resume
    assert post_factory.execution.sink_effects.get_effects_for_run(run_id) == effects_before_resume
    assert post_factory.execution.sink_effects.get_members_for_run(run_id) == members_before_resume
    assert checkpoint_manager.get_latest_checkpoint(run_id) == checkpoint_before_resume
    assert (
        _assert_persisted_row_and_artifact(
            factory=post_factory,
            payload_store=payload_store,
            output_path=output_path,
            run_id=run_id,
            expected_row={
                "answer": "A careful answer",
                "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                "answer_model": "openai/gpt-4o-mini",
            },
        )
        == persisted_ids_before_resume
    )
