"""Production-boundary lifecycle evidence for the reviewed local plugin matrix.

Every parametrized node names one first-party subject and the SQLite WAL
single-leader profile.  Provider-backed subjects are deliberately absent from
this local pass set: their variants belong to the protected ``live_provider``
lanes and remain unknown until those lanes execute.
"""

from __future__ import annotations

import json
import multiprocessing
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, func, select

from elspeth.contracts import RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig, RuntimeRateLimitConfig
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    runs_table,
    scheduler_events_table,
    sink_effects_table,
    token_outcomes_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.rate_limit.registry import RateLimitRegistry
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
)
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.plugins.infrastructure.discovery import discover_all_plugins
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle, instantiate_plugins_from_config
from elspeth.plugins.sinks.database_sink import database_effect_ledger_table
from tests.fixtures.pdf_documents import minimal_pdf

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_MATRIX = REPOSITORY_ROOT / "tests/golden/state_engine/plugin_lifecycle_matrix.json"
SQLITE_SINGLE_LEADER = "sqlite-wal-single-process-leader"


@dataclass(frozen=True, slots=True)
class LocalLifecycleCase:
    plugin_key: str
    profile_case: str = SQLITE_SINGLE_LEADER


def _reviewed_local_subjects() -> tuple[str, ...]:
    document = json.loads(PLUGIN_MATRIX.read_text(encoding="utf-8"))
    return tuple(entry["plugin_key"] for entry in document["plugins"] if entry["external_observation_required"] is False)


LOCAL_LIFECYCLE_CASES = tuple(LocalLifecycleCase(plugin_key) for plugin_key in _reviewed_local_subjects())
RESUMABLE_LOCAL_LIFECYCLE_CASES = tuple(case for case in LOCAL_LIFECYCLE_CASES if case.plugin_key != "source:null")


def _plugin_types() -> dict[str, type[BaseSource] | type[BaseTransform] | type[BaseSink]]:
    discovered = discover_all_plugins()
    result: dict[str, type[BaseSource] | type[BaseTransform] | type[BaseSink]] = {}
    for collection, kind in (("sources", "source"), ("transforms", "transform"), ("sinks", "sink")):
        for discovered_type in discovered[collection]:
            plugin_type = cast(type[BaseSource] | type[BaseTransform] | type[BaseSink], discovered_type)
            result[f"{kind}:{plugin_type.name}"] = plugin_type
    return result


def _declared_options(plugin_type: type[BaseSource] | type[BaseTransform] | type[BaseSink]) -> dict[str, Any]:
    example = plugin_type.example_use
    assert type(example) is str and example.strip()
    document = yaml.safe_load(example)

    def walk(value: object) -> list[Mapping[str, Any]]:
        matches: list[Mapping[str, Any]] = []
        if isinstance(value, Mapping):
            if value.get("plugin") == plugin_type.name:
                matches.append(cast("Mapping[str, Any]", value))
            for child in value.values():
                matches.extend(walk(child))
        elif isinstance(value, list):
            for child in value:
                matches.extend(walk(child))
        return matches

    (node,) = walk(document)
    raw = node.get("options", {})
    assert isinstance(raw, Mapping)
    return dict(raw)


def _rows_for(plugin_key: str) -> list[dict[str, object]]:
    rows: dict[str, list[dict[str, object]]] = {
        "transform:batch_classifier_metrics": [
            {"actual_label": "pass", "predicted_label": "pass"},
            {"actual_label": "fail", "predicted_label": "pass"},
        ],
        "transform:batch_data_quality_report": [{"source": "a", "score_text": "0.9", "label": "pass"}],
        "transform:batch_distribution_profile": [
            {"latency_ms": 10.0, "variant": "control"},
            {"latency_ms": 12.0, "variant": "control"},
        ],
        "transform:batch_drift_compare": [
            {"cohort": "baseline", "score": 0.2},
            {"cohort": "candidate", "score": 0.3},
        ],
        "transform:batch_effect_size": [
            {"prompt_variant": "control", "score": 0.2},
            {"prompt_variant": "candidate", "score": 0.4},
        ],
        "transform:batch_experiment_compare": [
            {"prompt_variant": "control", "score": 0.2},
            {"prompt_variant": "candidate", "score": 0.4},
        ],
        "transform:batch_outlier_annotator": [{"latency_ms": 10.0}, {"latency_ms": 11.0}],
        "transform:batch_paired_preference": [
            {"case_id": "one", "variant": "control", "preference_score": 0.2},
            {"case_id": "one", "variant": "candidate", "preference_score": 0.4},
        ],
        "transform:batch_replicate": [{"copies": 2, "value": "kept"}],
        "transform:batch_stats": [{"amount": 2.0, "category": "a"}, {"amount": 4.0, "category": "a"}],
        "transform:batch_threshold_summary": [{"quality_score": 0.9}, {"quality_score": 0.6}],
        "transform:batch_top_k": [{"predicted_label": "pass", "model": "m"}],
        "transform:blob_csv_expand": [{"blob_ref": "filled-by-harness"}],
        "transform:blob_fetch": [{"document_url": "filled-by-harness"}],
        "transform:field_mapper": [{"id": 1, "applicant": "Ada", "notes": "ok"}],
        "transform:json_explode": [{"items": ["a", "b"]}],
        "transform:keyword_filter": [{"notes": "public material"}],
        "transform:line_explode": [{"content": "alpha\nbeta"}],
        "transform:passthrough": [{"value": "kept"}],
        "transform:pdf_rasterize": [{"blob_ref": "filled-by-harness"}],
        "transform:report_assemble": [{"line": "alpha"}, {"line": "beta"}],
        "transform:truncate": [{"notes": "brief"}],
        "transform:type_coerce": [{"price": "2.5", "quantity": "3", "in_stock": "true"}],
        "transform:reference_join": [{"product": "hats"}],
        "transform:value_transform": [{"price": 2.0, "quantity": 3}],
        "transform:web_scrape": [{"page_url": "filled-by-harness"}],
        "sink:document": [{"announcement_text": "hello"}],
        "sink:text": [{"line_text": "hello"}],
    }
    return rows.get(plugin_key, [{"id": 1, "value": "matrix"}])


def _serve_directory(directory: str, port: int, ready: multiprocessing.synchronize.Event) -> None:
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

    handler = lambda *args, **kwargs: QuietHandler(*args, directory=directory, **kwargs)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    ready.set()
    server.serve_forever()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _materialize_pipeline(
    case: LocalLifecycleCase,
    tmp_path: Path,
) -> tuple[str, FilesystemPayloadStore, multiprocessing.Process | None]:
    kind, plugin_name = case.plugin_key.split(":", maxsplit=1)
    plugin_type = _plugin_types()[case.plugin_key]
    options = _declared_options(plugin_type)
    store = FilesystemPayloadStore(tmp_path / "payloads")
    rows = _rows_for(case.plugin_key)
    server: multiprocessing.Process | None = None

    if case.plugin_key == "source:csv":
        source_path = tmp_path / "input.csv"
        source_path.write_text("id,value\n1,matrix\n", encoding="utf-8")
        options["path"] = str(source_path)
    elif case.plugin_key == "source:json":
        source_path = tmp_path / "input.jsonl"
        source_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
        options["path"] = str(source_path)
    elif case.plugin_key == "source:text":
        source_path = tmp_path / "input.txt"
        source_path.write_text("https://example.invalid/reference\n", encoding="utf-8")
        options["path"] = str(source_path)
    elif case.plugin_key == "source:blob_rows":
        payload = b"matrix-image-placeholder"
        payload_ref = store.store(payload)
        options["blobs"] = [
            {
                "blob_id": "11111111-1111-1111-1111-111111111111",
                "payload_ref": payload_ref,
                "filename": "page-1.png",
                "mime_type": "image/png",
                "size_bytes": len(payload),
            }
        ]

    if kind in {"transform", "sink"}:
        if case.plugin_key == "transform:blob_csv_expand":
            rows[0]["blob_ref"] = store.store(b"ignored\n1,hello\n")
        if case.plugin_key == "transform:pdf_rasterize":
            rows[0]["blob_ref"] = store.store(minimal_pdf(1))
        if case.plugin_key in {"transform:blob_fetch", "transform:web_scrape"}:
            served = tmp_path / "served"
            served.mkdir(exist_ok=True)
            (served / "document.csv").write_text("id,value\n1,matrix\n", encoding="utf-8")
            port = _unused_port()
            ready = multiprocessing.get_context("spawn").Event()
            server = cast(
                multiprocessing.Process,
                multiprocessing.get_context("spawn").Process(
                    target=_serve_directory,
                    args=(str(served), port, ready),
                ),
            )
            server.start()
            assert ready.wait(10), "real-process HTTP fixture did not start"
            url = f"http://127.0.0.1:{port}/document.csv"
            url_field = "document_url" if case.plugin_key == "transform:blob_fetch" else "page_url"
            rows[0][url_field] = url
            http = cast("dict[str, object]", options["http"])
            http["allowed_hosts"] = ["127.0.0.1/32"]

        input_path = tmp_path / "rows.jsonl"
        input_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        source = {
            "input": {
                "plugin": "json",
                "on_success": "subject_input" if kind == "transform" else "subject_sink",
                "options": {
                    "path": str(input_path),
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }
        }
    else:
        source = {"subject_source": {"plugin": plugin_name, "on_success": "output", "options": options}}

    pipeline: dict[str, object] = {"sources": source}
    if kind == "transform":
        if issubclass(plugin_type, BaseTransform) and plugin_type.is_batch_aware:
            aggregation = yaml.safe_load(cast(str, plugin_type.example_use))["aggregations"][0]
            aggregation["input"] = "subject_input"
            aggregation["on_success"] = "output"
            aggregation["trigger"] = {"count": len(rows)}
            aggregation["options"] = options
            pipeline["aggregations"] = [aggregation]
        else:
            pipeline["transforms"] = [
                {
                    "name": "subject",
                    "plugin": plugin_name,
                    "input": "subject_input",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": options,
                }
            ]
    if kind == "sink":
        if case.plugin_key in {"sink:csv", "sink:json", "sink:document", "sink:text"}:
            suffix = {"sink:csv": "csv", "sink:json": "jsonl", "sink:document": "txt", "sink:text": "txt"}[case.plugin_key]
            options["path"] = str(tmp_path / f"subject-output.{suffix}")
            options["collision_policy"] = "auto_increment"
        elif case.plugin_key == "sink:database":
            database_url = f"sqlite:///{tmp_path / 'subject-sink.db'}"
            options["url"] = database_url
            options["schema"] = {"mode": "fixed", "fields": ["id: int", "value: str"]}
            target_metadata = MetaData()
            Table(
                cast(str, options["table"]),
                target_metadata,
                Column("id", Integer, nullable=False),
                Column("value", Text, nullable=False),
            )
            database_effect_ledger_table(target_metadata, "_elspeth_sink_effects")
            target_engine = create_engine(database_url)
            target_metadata.create_all(target_engine)
            target_engine.dispose()
        pipeline["sinks"] = {
            "subject_sink": {
                "plugin": plugin_name,
                "on_write_failure": "cleanup_tripwire",
                "options": options,
            },
            "cleanup_tripwire": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / "cleanup-tripwire.jsonl"),
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
            },
        }
    else:
        pipeline["sinks"] = {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / "output.jsonl"),
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
            }
        }
    pipeline["landscape"] = {"url": f"sqlite:///{tmp_path / 'audit.db'}"}
    pipeline["payload_store"] = {"backend": "filesystem", "base_path": str(store.base_path)}
    pipeline["checkpoint"] = {"enabled": True, "frequency": "every_row"}
    return yaml.safe_dump(pipeline, sort_keys=False), store, server


def _subject_instance(case: LocalLifecycleCase, bundle: PluginBundle) -> BaseSource | BaseTransform | BaseSink:
    kind, _name = case.plugin_key.split(":", maxsplit=1)
    if kind == "source":
        return cast(BaseSource, next(iter(bundle.sources.values())))
    if kind == "sink":
        return cast(BaseSink, next(iter(bundle.sinks.values())))
    if bundle.transforms:
        return cast(BaseTransform, bundle.transforms[0].plugin)
    return cast(BaseTransform, next(iter(bundle.aggregations.values()))[0])


def _unbound_method(plugin_type: type[Any], method_name: str) -> Any:
    for owner in plugin_type.__mro__:
        if method_name in owner.__dict__:
            return owner.__dict__[method_name]
    raise AssertionError(f"{plugin_type.__qualname__} has no {method_name} method")


def _build_runtime(
    case: LocalLifecycleCase,
    tmp_path: Path,
) -> tuple[
    Any,
    PluginBundle,
    ExecutionGraph,
    Any,
    FilesystemPayloadStore,
    multiprocessing.Process | None,
]:
    settings_text, store, server = _materialize_pipeline(case, tmp_path)
    settings = load_settings_from_yaml_string(settings_text)
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=bundle.sink_effect_modes,
    )
    return settings, bundle, graph, config, store, server


def _stop_server(server: multiprocessing.Process | None) -> None:
    if server is None:
        return
    server.terminate()
    server.join(10)
    assert not server.is_alive()


def test_local_lifecycle_cases_cover_the_exact_reviewed_subject_set() -> None:
    assert tuple(case.plugin_key for case in LOCAL_LIFECYCLE_CASES) == _reviewed_local_subjects()
    assert len(LOCAL_LIFECYCLE_CASES) == 36
    assert {case.profile_case for case in LOCAL_LIFECYCLE_CASES} == {SQLITE_SINGLE_LEADER}


@pytest.mark.parametrize("case", LOCAL_LIFECYCLE_CASES, ids=lambda case: f"{case.profile_case}::{case.plugin_key}")
def test_local_first_party_plugin_crosses_the_production_lifecycle(
    case: LocalLifecycleCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    subject = _subject_instance(case, bundle)
    subject_type = _plugin_types()[case.plugin_key]
    assert type(subject) is subject_type

    teardown: list[str] = []
    original_complete = _unbound_method(subject_type, "on_complete")
    original_close = _unbound_method(subject_type, "close")

    def observed_complete(instance: Any, ctx: Any) -> None:
        if instance is subject:
            teardown.append("complete")
        original_complete(instance, ctx)

    def observed_close(instance: Any) -> None:
        if instance is subject:
            teardown.append("close")
        original_close(instance)

    monkeypatch.setattr(subject_type, "on_complete", observed_complete)
    monkeypatch.setattr(subject_type, "close", observed_close)

    db = LandscapeDB.from_url(settings.landscape.url)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        result = Orchestrator(db, rate_limit_registry=rate_limits).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=store,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
        assert result.status in {RunStatus.COMPLETED, RunStatus.EMPTY}
        assert subject._on_start_called is True
        assert subject._on_complete_called is True
        assert teardown == ["complete", "close"]

        with db.engine.connect() as connection:
            node_state_count = connection.scalar(
                select(func.count()).select_from(node_states_table).where(node_states_table.c.run_id == result.run_id)
            )
            scheduler_event_count = connection.scalar(
                select(func.count()).select_from(scheduler_events_table).where(scheduler_events_table.c.run_id == result.run_id)
            )
            outcome_count = connection.scalar(
                select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)
            )
            effects = tuple(
                connection.execute(select(sink_effects_table.c.state).where(sink_effects_table.c.run_id == result.run_id)).scalars()
            )
            artifact_count = connection.scalar(
                select(func.count()).select_from(artifacts_table).where(artifacts_table.c.run_id == result.run_id)
            )

        if result.status is RunStatus.EMPTY:
            assert case.plugin_key == "source:null"
            assert (node_state_count, outcome_count, effects, artifact_count) == (0, 0, (), 0)
        else:
            assert cast(int, node_state_count) > 0
            assert cast(int, scheduler_event_count) > 0
            assert cast(int, outcome_count) > 0
            assert effects and set(effects) == {"finalized"}
            assert cast(int, artifact_count) > 0
    finally:
        rate_limits.close()
        db.close()
        _stop_server(server)


@pytest.mark.parametrize("case", LOCAL_LIFECYCLE_CASES, ids=lambda case: f"{case.profile_case}::partial-start::{case.plugin_key}")
def test_partial_start_failure_never_cleans_the_unstarted_subject(
    case: LocalLifecycleCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    subject = _subject_instance(case, bundle)
    subject_type = _plugin_types()[case.plugin_key]
    close_calls: list[object] = []
    original_close = _unbound_method(subject_type, "close")

    def fail_start(instance: Any, _ctx: Any) -> None:
        if instance is subject:
            raise RuntimeError(f"injected partial-start failure for {case.plugin_key}")
        raise AssertionError("subject class unexpectedly produced a second pipeline instance")

    def observed_close(instance: Any) -> None:
        if instance is subject:
            close_calls.append(instance)
        original_close(instance)

    monkeypatch.setattr(subject_type, "on_start", fail_start)
    monkeypatch.setattr(subject_type, "close", observed_close)
    db = LandscapeDB.from_url(settings.landscape.url)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        with pytest.raises(RuntimeError, match="injected partial-start failure"):
            Orchestrator(db, rate_limit_registry=rate_limits).run(
                config,
                graph=graph,
                settings=settings,
                payload_store=store,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        assert subject._on_start_called is False
        assert subject._on_complete_called is False
        assert close_calls == []
        with db.engine.connect() as connection:
            assert connection.scalar(select(runs_table.c.status)) == RunStatus.FAILED.value
    finally:
        rate_limits.close()
        db.close()
        _stop_server(server)


@pytest.mark.parametrize("case", LOCAL_LIFECYCLE_CASES, ids=lambda case: f"{case.profile_case}::partial-cleanup::{case.plugin_key}")
def test_later_start_failure_cleans_the_already_started_subject_in_order(
    case: LocalLifecycleCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    subject = _subject_instance(case, bundle)
    if case.plugin_key.startswith("sink:"):
        later = bundle.sinks["cleanup_tripwire"]
    else:
        later = bundle.sinks["output"]
    subject_type = type(subject)
    later_type = type(later)
    teardown: list[tuple[str, object]] = []
    original_subject_complete = _unbound_method(subject_type, "on_complete")
    original_subject_close = _unbound_method(subject_type, "close")
    original_later_start = _unbound_method(later_type, "on_start")
    original_later_close = _unbound_method(later_type, "close")

    def observed_complete(instance: Any, ctx: Any) -> None:
        if instance is subject:
            teardown.append(("complete", instance))
        original_subject_complete(instance, ctx)

    def observed_subject_close(instance: Any) -> None:
        if instance is subject:
            teardown.append(("close", instance))
        original_subject_close(instance)

    def fail_later_start(instance: Any, ctx: Any) -> None:
        if instance is later:
            raise RuntimeError(f"injected later startup failure after {case.plugin_key}")
        original_later_start(instance, ctx)

    def observed_later_close(instance: Any) -> None:
        if instance is subject:
            teardown.append(("close", instance))
        elif instance is later:
            teardown.append(("later-close", instance))
        original_later_close(instance)

    monkeypatch.setattr(subject_type, "on_complete", observed_complete)
    monkeypatch.setattr(subject_type, "close", observed_subject_close)
    monkeypatch.setattr(later_type, "on_start", fail_later_start)
    monkeypatch.setattr(later_type, "close", observed_later_close)
    db = LandscapeDB.from_url(settings.landscape.url)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        with pytest.raises(RuntimeError, match="injected later startup failure"):
            Orchestrator(db, rate_limit_registry=rate_limits).run(
                config,
                graph=graph,
                settings=settings,
                payload_store=store,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        assert subject._on_start_called is True
        assert subject._on_complete_called is True
        assert later._on_start_called is False
        assert later._on_complete_called is False
        assert teardown == [("complete", subject), ("close", subject)]
    finally:
        rate_limits.close()
        db.close()
        _stop_server(server)


@pytest.mark.parametrize("case", LOCAL_LIFECYCLE_CASES, ids=lambda case: f"{case.profile_case}::exceptional::{case.plugin_key}")
def test_exceptional_operation_still_completes_and_closes_the_started_subject(
    case: LocalLifecycleCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    subject = _subject_instance(case, bundle)
    subject_type = _plugin_types()[case.plugin_key]
    teardown: list[str] = []
    original_complete = _unbound_method(subject_type, "on_complete")
    original_close = _unbound_method(subject_type, "close")

    def fail_operation(_instance: Any, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"injected operation failure for {case.plugin_key}")

    def observed_complete(instance: Any, ctx: Any) -> None:
        if instance is subject:
            teardown.append("complete")
        original_complete(instance, ctx)

    def observed_close(instance: Any) -> None:
        if instance is subject:
            teardown.append("close")
        original_close(instance)

    kind, _name = case.plugin_key.split(":", maxsplit=1)
    operation = {"source": "load", "transform": "process", "sink": "commit_effect"}[kind]
    monkeypatch.setattr(subject_type, operation, fail_operation)
    monkeypatch.setattr(subject_type, "on_complete", observed_complete)
    monkeypatch.setattr(subject_type, "close", observed_close)
    db = LandscapeDB.from_url(settings.landscape.url)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        try:
            result = Orchestrator(db, rate_limit_registry=rate_limits).run(
                config,
                graph=graph,
                settings=settings,
                payload_store=store,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        except RuntimeError as exc:
            assert "injected operation failure" in str(exc)
        else:
            assert result.status in {RunStatus.FAILED, RunStatus.COMPLETED_WITH_FAILURES}
        assert subject._on_start_called is True
        assert subject._on_complete_called is True
        assert teardown == ["complete", "close"]
    finally:
        rate_limits.close()
        db.close()
        _stop_server(server)


def test_null_source_has_no_runtime_item_or_effect_to_resume(tmp_path: Path) -> None:
    case = LocalLifecycleCase("source:null")
    settings, _bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    db = LandscapeDB.from_url(settings.landscape.url)
    checkpoints = CheckpointManager(db)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        result = Orchestrator(
            db,
            checkpoint_manager=checkpoints,
            checkpoint_config=RuntimeCheckpointConfig.from_settings(settings.checkpoint),
            rate_limit_registry=rate_limits,
        ).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=store,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
        assert result.status is RunStatus.EMPTY
        with db.engine.connect() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(sink_effects_table).where(sink_effects_table.c.run_id == result.run_id))
                == 0
            )
        assert checkpoints.get_latest_checkpoint(result.run_id) is None
    finally:
        rate_limits.close()
        db.close()
        _stop_server(server)


@pytest.mark.parametrize(
    "case",
    RESUMABLE_LOCAL_LIFECYCLE_CASES,
    ids=lambda case: f"{case.profile_case}::resume::{case.plugin_key}",
)
def test_public_resume_reconciles_the_subject_pipeline_after_a_finalized_effect_response_loss(
    case: LocalLifecycleCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _bundle, graph, config, store, server = _build_runtime(case, tmp_path)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    database_url = settings.landscape.url
    faulted = False
    original_fault = SinkEffectCoordinator._fault

    def lose_first_finalized_response(
        instance: SinkEffectCoordinator,
        seam: SinkEffectExecutionSeam,
    ) -> None:
        nonlocal faulted
        if seam is SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE and not faulted:
            faulted = True
            raise SinkEffectInjectedFault(seam)
        original_fault(instance, seam)

    monkeypatch.setattr(SinkEffectCoordinator, "_fault", lose_first_finalized_response)
    db = LandscapeDB.from_url(database_url)
    checkpoints = CheckpointManager(db)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        with pytest.raises(SinkEffectInjectedFault) as captured:
            Orchestrator(
                db,
                checkpoint_manager=checkpoints,
                checkpoint_config=checkpoint_config,
                rate_limit_registry=rate_limits,
            ).run(
                config,
                graph=graph,
                settings=settings,
                payload_store=store,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        assert captured.value.seam is SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE
        with db.engine.connect() as connection:
            run_id = cast(str, connection.scalar(select(runs_table.c.run_id)))
            assert tuple(connection.execute(select(sink_effects_table.c.state).where(sink_effects_table.c.run_id == run_id)).scalars()) == (
                "finalized",
            )
        assert checkpoints.get_latest_checkpoint(run_id) is not None
    finally:
        rate_limits.close()
        db.close()

    _stop_server(server)
    resumed_settings, resumed_bundle, resumed_graph, resumed_config, resumed_store, resumed_server = _build_runtime(case, tmp_path)
    reopened = LandscapeDB.from_url(database_url)
    reopened_checkpoints = CheckpointManager(reopened)
    resumed_rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(resumed_settings.rate_limit))
    resumed_subject = _subject_instance(case, resumed_bundle)
    try:
        recovery = RecoveryManager(reopened, reopened_checkpoints)
        check = recovery.can_resume(run_id, resumed_graph)
        assert check.can_resume, check.reason
        resume_point = recovery.get_resume_point(run_id, resumed_graph)
        assert resume_point is not None
        result = Orchestrator(
            reopened,
            checkpoint_manager=reopened_checkpoints,
            checkpoint_config=checkpoint_config,
            rate_limit_registry=resumed_rate_limits,
        ).resume(
            resume_point,
            resumed_config,
            resumed_graph,
            settings=resumed_settings,
            payload_store=resumed_store,
        )
        assert result.status is RunStatus.COMPLETED
        if case.plugin_key.startswith("source:"):
            assert resumed_subject._on_start_called is False
            assert resumed_subject._on_complete_called is False
        else:
            assert resumed_subject._on_start_called is True
            assert resumed_subject._on_complete_called is True
        with reopened.engine.connect() as connection:
            assert tuple(connection.execute(select(sink_effects_table.c.state).where(sink_effects_table.c.run_id == run_id)).scalars()) == (
                "finalized",
            )
            assert connection.scalar(select(func.count()).select_from(artifacts_table).where(artifacts_table.c.run_id == run_id)) == 1
        assert reopened_checkpoints.get_latest_checkpoint(run_id) is None
    finally:
        resumed_rate_limits.close()
        reopened.close()
        _stop_server(resumed_server)
