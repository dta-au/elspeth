"""Production-path execution harness for the maintained DAG scenario corpus."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from types import MappingProxyType
from typing import Any, cast

import yaml
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.config import ElspethSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB, LandscapeExporter, RecorderFactory
from elspeth.core.landscape.schema import node_states_table, token_work_items_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle, instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from tests.fixtures.dag_scenario_corpus.loader import resolve_fixture_path
from tests.fixtures.dag_scenario_corpus.schema import (
    AuditEvidence,
    AuditRecordCount,
    ConfigEvidence,
    GraphEvidence,
    GraphNodeType,
    GraphNodeTypeCount,
    HarnessCaseSpec,
    RecoveryEvidence,
    RuntimeEvidence,
    ScenarioRunEvidence,
    ScenarioSpec,
    SinkOutputProjection,
    StableNodeStateProjection,
    StableRouteProjection,
    StableRowProjection,
    StableRunProjection,
    StableSchedulerWorkProjection,
    StableTerminalDisposition,
    StableTokenProjection,
    normalize_template_name,
)


@dataclass(frozen=True, slots=True)
class RenderedScenario:
    settings: ElspethSettings
    settings_yaml: str
    settings_sha256: str
    fixture_sha256: str
    input_paths: Mapping[str, Path]
    output_paths: Mapping[str, Path]
    fault_marker: Path

    @property
    def output_path(self) -> Path:
        """Return the sole declared artifact for legacy single-output callers."""

        if len(self.output_paths) != 1:
            raise ValueError(f"scenario declares multiple output artifacts: {tuple(self.output_paths)!r}")
        return next(iter(self.output_paths.values()))


@dataclass(frozen=True, slots=True)
class BuiltScenario:
    rendered: RenderedScenario
    bundle: PluginBundle
    graph: ExecutionGraph
    config: PipelineConfig
    graph_evidence: GraphEvidence


def _require_exact_template_bindings(
    fixture_template: str,
    *,
    section: str,
    declared_names: tuple[str, ...],
    token_prefix: str,
) -> None:
    raw = yaml.safe_load(fixture_template)
    if not isinstance(raw, dict) or not isinstance(raw.get(section), dict):
        raise ValueError(f"DAG scenario fixture must declare a {section} mapping")
    configured = cast(dict[object, object], raw[section])
    configured_names = tuple(str(name) for name in configured)
    subject = "source" if section == "sources" else "sink"
    if configured_names != declared_names:
        raise ValueError(
            f"DAG scenario declared {subject} names must exactly match fixture {section}: "
            f"declared={declared_names!r}, configured={configured_names!r}"
        )
    for name in declared_names:
        entry = configured.get(name)
        options = entry.get("options") if isinstance(entry, dict) else None
        configured_path = options.get("path") if isinstance(options, dict) else None
        expected_token = f"${{{token_prefix}_{normalize_template_name(name)}}}"
        if configured_path != expected_token:
            raise ValueError(
                f"DAG scenario trusted {token_prefix} token must configure declared {subject} {name!r}: "
                f"expected {expected_token!r}, got {configured_path!r}"
            )


def compute_fixture_sha256(case: HarnessCaseSpec) -> str:
    """Hash canonical YAML and every sorted source binding with names and bytes."""

    digest = hashlib.sha256(resolve_fixture_path(case.fixture).read_bytes())
    for source_name, relative_path in case.input_fixtures.items():
        fixture_bytes = resolve_fixture_path(relative_path).read_bytes()
        for component in (source_name.encode(), relative_path.encode(), fixture_bytes):
            digest.update(b"\0")
            digest.update(len(component).to_bytes(8, "big"))
            digest.update(component)
    return digest.hexdigest()


def render_settings(case: HarnessCaseSpec, tmp_path: Path) -> RenderedScenario:
    """Resolve and load one trusted corpus fixture without environment expansion."""

    fixture_path = resolve_fixture_path(case.fixture)
    fixture_bytes = fixture_path.read_bytes()
    fixture_template = fixture_bytes.decode("utf-8")
    _require_exact_template_bindings(
        fixture_template,
        section="sources",
        declared_names=tuple(case.input_fixtures),
        token_prefix="input",
    )
    _require_exact_template_bindings(
        fixture_template,
        section="sinks",
        declared_names=tuple(case.output_artifacts),
        token_prefix="output",
    )

    input_paths = {name: resolve_fixture_path(path) for name, path in case.input_fixtures.items()}
    output_paths = {name: tmp_path / filename for name, filename in case.output_artifacts.items()}
    fault_marker = tmp_path / "fault-triggered.marker"
    substitutions = {
        **{f"input_{normalize_template_name(name)}": json.dumps(str(path)) for name, path in input_paths.items()},
        **{f"output_{normalize_template_name(name)}": json.dumps(str(path)) for name, path in output_paths.items()},
        "fault_marker": json.dumps(str(fault_marker)),
    }
    rendered = Template(fixture_template).substitute(substitutions)
    if "${" in rendered:
        raise ValueError(f"Unresolved DAG scenario template variable in {fixture_path}")
    settings = load_settings_from_yaml_string(rendered)
    source_paths = {
        source_name: Path(source.options["path"]).resolve() for source_name, source in settings.sources.items() if "path" in source.options
    }
    if tuple(source_paths) != tuple(input_paths):
        raise ValueError(
            "DAG scenario source names must exactly match declared input_fixtures: "
            f"declared={tuple(input_paths)!r}, configured={tuple(source_paths)!r}"
        )
    if source_paths != input_paths:
        raise ValueError(
            "DAG scenario source path bindings must exactly match declared input_fixtures: "
            f"declared={input_paths!r}, configured={source_paths!r}"
        )

    sink_paths = {sink_name: Path(sink.options["path"]).resolve() for sink_name, sink in settings.sinks.items() if "path" in sink.options}
    resolved_output_paths = {name: path.resolve() for name, path in output_paths.items()}
    if tuple(sink_paths) != tuple(resolved_output_paths):
        raise ValueError(
            "DAG scenario sink names must exactly match declared output_artifacts: "
            f"declared={tuple(resolved_output_paths)!r}, configured={tuple(sink_paths)!r}"
        )
    if sink_paths != resolved_output_paths:
        raise ValueError(
            "DAG scenario sink path bindings must exactly match declared output_artifacts: "
            f"declared={resolved_output_paths!r}, configured={sink_paths!r}"
        )

    return RenderedScenario(
        settings=settings,
        settings_yaml=rendered,
        settings_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        fixture_sha256=compute_fixture_sha256(case),
        input_paths=MappingProxyType(input_paths),
        output_paths=MappingProxyType(output_paths),
        fault_marker=fault_marker,
    )


def build_scenario(
    rendered: RenderedScenario,
    *,
    purpose: SinkEffectExecutionPurpose = SinkEffectExecutionPurpose.FRESH,
) -> BuiltScenario:
    """Build and validate a scenario through the production assembly sequence."""

    settings = rendered.settings
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=purpose)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    if purpose is SinkEffectExecutionPurpose.RESUME:
        for sink_name, sink in execution_sinks.items():
            if not sink.supports_resume:
                raise ValueError(f"DAG scenario sink {sink_name!r} does not support resume")
            sink.configure_for_resume()
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=purpose,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
    )
    graph.validate()
    graph.validate_edge_compatibility()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    node_type_counts = Counter(node.node_type.value for node in graph.get_nodes())
    graph_evidence = GraphEvidence(
        accepted=True,
        node_count=len(graph.get_nodes()),
        edge_count=len(graph.get_edges()),
        node_type_counts=tuple(
            GraphNodeTypeCount(node_type=cast(GraphNodeType, node_type), count=count)
            for node_type, count in sorted(node_type_counts.items())
        ),
        edge_labels=tuple(sorted(edge.label for edge in graph.get_edges())),
        topology_hash=CheckpointCompatibilityValidator().compute_full_topology_hash(graph),
    )
    return BuiltScenario(rendered, bundle, graph, config, graph_evidence)


def _stable_node_key(record: Mapping[str, Any]) -> str:
    node_type = str(record["node_type"])
    prefix = "config_gate" if node_type == "gate" else node_type
    node_id = str(record["node_id"])
    match = re.fullmatch(rf"{re.escape(prefix)}_(.+)_[0-9a-f]{{12}}(?:_[0-9]+)?", node_id)
    if match is None:
        raise AssertionError(f"DAG corpus cannot derive stable identity for {node_type} node {node_id!r}")
    return f"{node_type}:{match.group(1)}"


def _stable_projection(records: list[dict[str, Any]]) -> StableRunProjection:
    """Normalize one public durable/export view without retaining run-local IDs."""

    node_records = [record for record in records if record.get("record_type") == "node"]
    node_keys = {str(record["node_id"]): _stable_node_key(record) for record in node_records}
    edge_records = [record for record in records if record.get("record_type") == "edge"]
    edges = {str(record["edge_id"]): record for record in edge_records}

    rows_by_id: dict[str, str] = {}
    rows: list[StableRowProjection] = []
    for record in (record for record in records if record.get("record_type") == "row"):
        source_key = node_keys[str(record["source_node_id"])]
        source_name = source_key.split(":", 1)[1]
        source_row_index = int(record["source_row_index"])
        key = f"{source_name}:{source_row_index}"
        rows_by_id[str(record["row_id"])] = key
        rows.append(
            StableRowProjection(
                key=key,
                source_name=source_name,
                source_row_index=source_row_index,
                ingest_sequence=int(record["ingest_sequence"]),
                source_data_hash=str(record["source_data_hash"]),
            )
        )

    token_records_by_row: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "token"):
        token_records_by_row[str(record["row_id"])].append(record)
    token_keys: dict[str, str] = {}
    for row_id, token_records in token_records_by_row.items():
        token_records.sort(
            key=lambda record: (
                str(record.get("branch_name") or ""),
                int(record.get("step_in_pipeline") or 0),
            )
        )
        signatures = [
            (
                record.get("branch_name"),
                record.get("step_in_pipeline"),
            )
            for record in token_records
        ]
        if len(signatures) != len(set(signatures)):
            raise AssertionError(f"DAG corpus tokens lack a stable ordering for row {rows_by_id[row_id]!r}")
        for ordinal, record in enumerate(token_records):
            token_keys[str(record["token_id"])] = f"{rows_by_id[row_id]}#{ordinal}"

    parents_by_token: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "token_parent"):
        parents_by_token[str(record["token_id"])].append((int(record["ordinal"]), str(record["parent_token_id"])))
    tokens = tuple(
        StableTokenProjection(
            key=stable_key,
            row_key=rows_by_id[str(record["row_id"])],
            parents=tuple(token_keys[parent_id] for _ordinal, parent_id in sorted(parents_by_token[str(record["token_id"])])),
        )
        for record in (record for record in records if record.get("record_type") == "token")
        for stable_key in (token_keys[str(record["token_id"])],)
    )

    state_records = [record for record in records if record.get("record_type") == "node_state"]
    state_keys = {str(record["state_id"]): record for record in state_records}
    node_states = tuple(
        StableNodeStateProjection(
            key=(
                f"{token_keys[str(record['token_id'])]}|{node_keys[str(record['node_id'])]}|"
                f"{int(record['step_index'])}|{int(record['attempt'])}"
            ),
            token_key=token_keys[str(record["token_id"])],
            node_key=node_keys[str(record["node_id"])],
            step_index=int(record["step_index"]),
            attempt=int(record["attempt"]),
            status=str(record["status"]),
        )
        for record in state_records
    )

    routes: list[StableRouteProjection] = []
    for record in (record for record in records if record.get("record_type") == "routing_event"):
        state = state_keys[str(record["state_id"])]
        edge_id = record.get("edge_id")
        if edge_id is None:
            raise AssertionError("DAG corpus exact route projection requires a durable edge_id")
        edge = edges[str(edge_id)]
        token_key = token_keys[str(state["token_id"])]
        from_node_key = node_keys[str(state["node_id"])]
        to_node_key = node_keys[str(edge["to_node_id"])]
        ordinal = int(record["ordinal"])
        routes.append(
            StableRouteProjection(
                key=f"{token_key}|{from_node_key}|{ordinal}|{to_node_key}",
                token_key=token_key,
                from_node_key=from_node_key,
                to_node_key=to_node_key,
                label=str(edge["label"]),
                mode=str(record["mode"]),
                ordinal=ordinal,
            )
        )

    completed_outcomes: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "token_outcome"):
        if bool(record["completed"]):
            completed_outcomes[str(record["token_id"])].append(record)
    terminal_dispositions: list[StableTerminalDisposition] = []
    for token_id, token_key in token_keys.items():
        outcomes = completed_outcomes[token_id]
        if len(outcomes) != 1:
            raise AssertionError(f"DAG corpus exact projection requires one terminal disposition for {token_key!r}")
        outcome = outcomes[0]
        terminal_dispositions.append(
            StableTerminalDisposition(
                key=token_key,
                token_key=token_key,
                outcome=str(outcome["outcome"]),
                path=str(outcome["path"]),
                sink_name=cast(str | None, outcome.get("sink_name")),
            )
        )

    scheduler_events: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "scheduler_event"):
        scheduler_events[str(record["work_item_id"])].append(record)
    work_by_token_node: defaultdict[tuple[str, str], list[tuple[str, list[dict[str, Any]]]]] = defaultdict(list)
    for work_item_id, work_events in scheduler_events.items():
        first = work_events[0]
        token_key = token_keys[str(first["token_id"])]
        raw_node_id = first.get("node_id")
        node_key = node_keys[str(raw_node_id)] if raw_node_id is not None else "scheduler:unbound"
        work_by_token_node[(token_key, node_key)].append((work_item_id, work_events))
    scheduler_work: list[StableSchedulerWorkProjection] = []
    for (token_key, node_key), items in work_by_token_node.items():
        if len(items) != 1:
            raise AssertionError(f"DAG corpus scheduler work lacks a stable ordering for {token_key!r} at {node_key!r}")
        _work_item_id, unordered_events = items[0]
        ordered_events: list[dict[str, Any]] = []
        remaining = list(unordered_events)
        current_status: object = None
        while remaining:
            candidates = [event for event in remaining if event.get("from_status") == current_status]
            if len(candidates) != 1:
                raise AssertionError(f"DAG corpus scheduler events do not form one stable transition chain for {token_key!r}")
            event = candidates[0]
            ordered_events.append(event)
            remaining.remove(event)
            current_status = event["to_status"]
        scheduler_work.append(
            StableSchedulerWorkProjection(
                key=f"{token_key}|{node_key}|0",
                token_key=token_key,
                node_key=node_key,
                transitions=tuple(f"{event['event_type']}:{event['to_status']}" for event in ordered_events),
                final_status=str(ordered_events[-1]["to_status"]),
            )
        )

    return StableRunProjection(
        rows=tuple(sorted(rows, key=lambda row: row.key)),
        tokens=tuple(sorted(tokens, key=lambda token: token.key)),
        node_states=tuple(sorted(node_states, key=lambda state: state.key)),
        routes=tuple(sorted(routes, key=lambda route: route.key)),
        terminal_dispositions=tuple(sorted(terminal_dispositions, key=lambda disposition: disposition.key)),
        scheduler_work=tuple(sorted(scheduler_work, key=lambda work: work.key)),
    )


def _public_durable_records(db: LandscapeDB, *, run_id: str, payload_store: FilesystemPayloadStore) -> list[dict[str, Any]]:
    repositories = RecorderFactory.read_only(db, payload_store=payload_store)
    records: list[dict[str, Any]] = []
    for node in repositories.data_flow.get_nodes(run_id):
        records.append({"record_type": "node", "node_id": node.node_id, "node_type": node.node_type.value})
    for edge in repositories.data_flow.get_edges(run_id):
        records.append(
            {
                "record_type": "edge",
                "edge_id": edge.edge_id,
                "from_node_id": edge.from_node_id,
                "to_node_id": edge.to_node_id,
                "label": edge.label,
            }
        )
    for row in repositories.query.get_rows(run_id):
        records.append(
            {
                "record_type": "row",
                "row_id": row.row_id,
                "source_node_id": row.source_node_id,
                "source_row_index": row.source_row_index,
                "ingest_sequence": row.ingest_sequence,
                "source_data_hash": row.source_data_hash,
            }
        )
    tokens = repositories.query.get_all_tokens_for_run(run_id)
    for token in tokens:
        records.append(
            {
                "record_type": "token",
                "token_id": token.token_id,
                "row_id": token.row_id,
                "step_in_pipeline": token.step_in_pipeline,
                "branch_name": token.branch_name,
                "fork_group_id": token.fork_group_id,
                "join_group_id": token.join_group_id,
                "expand_group_id": token.expand_group_id,
            }
        )
    for parent in repositories.query.get_all_token_parents_for_run(run_id):
        records.append(
            {
                "record_type": "token_parent",
                "token_id": parent.token_id,
                "parent_token_id": parent.parent_token_id,
                "ordinal": parent.ordinal,
            }
        )
    for state in repositories.query.get_all_node_states_for_run(run_id):
        records.append(
            {
                "record_type": "node_state",
                "state_id": state.state_id,
                "token_id": state.token_id,
                "node_id": state.node_id,
                "step_index": state.step_index,
                "attempt": state.attempt,
                "status": state.status.value,
            }
        )
    for route_event in repositories.query.get_all_routing_events_for_run(run_id):
        records.append(
            {
                "record_type": "routing_event",
                "state_id": route_event.state_id,
                "edge_id": route_event.edge_id,
                "ordinal": route_event.ordinal,
                "mode": route_event.mode.value,
            }
        )
    for outcome in repositories.query.get_all_token_outcomes_for_run(run_id):
        records.append(
            {
                "record_type": "token_outcome",
                "token_id": outcome.token_id,
                "outcome": outcome.outcome.value if outcome.outcome is not None else None,
                "path": outcome.path.value,
                "completed": outcome.completed,
                "sink_name": outcome.sink_name,
            }
        )
    for scheduler_event in repositories.query.get_scheduler_events(run_id=run_id):
        records.append(
            {
                "record_type": "scheduler_event",
                "work_item_id": scheduler_event.work_item_id,
                "token_id": scheduler_event.token_id,
                "node_id": scheduler_event.node_id,
                "event_type": scheduler_event.event_type.value,
                "from_status": scheduler_event.from_status.value if scheduler_event.from_status is not None else None,
                "to_status": scheduler_event.to_status.value,
            }
        )
    return records


def _sink_outputs(rendered: RenderedScenario) -> tuple[SinkOutputProjection, ...]:
    outputs: list[SinkOutputProjection] = []
    for sink_name, output_path in rendered.output_paths.items():
        if not output_path.is_file():
            raise AssertionError(f"DAG corpus sink {sink_name!r} did not produce {output_path.name!r}")
        rows = tuple(
            json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
            for line in output_path.read_text(encoding="utf-8").splitlines()
        )
        outputs.append(SinkOutputProjection(sink_name=sink_name, rows=rows))
    return tuple(outputs)


def _audit_evidence(records: list[dict[str, Any]], *, portable_projection: StableRunProjection | None = None) -> AuditEvidence:
    counts = Counter(str(record["record_type"]) for record in records)
    return AuditEvidence(
        attempted=True,
        total_records=len(records),
        record_counts=tuple(AuditRecordCount(record_type=record_type, count=count) for record_type, count in sorted(counts.items())),
        source_operation_count=sum(
            1 for record in records if record.get("record_type") == "operation" and record.get("operation_type") == "source_load"
        ),
        portable_projection=portable_projection,
    )


def _run_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    rendered = render_settings(case, tmp_path)
    built = build_scenario(rendered)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        result = Orchestrator(db).run(
            built.config,
            graph=built.graph,
            settings=built.rendered.settings,
            payload_store=payload_store,
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
        sink_outputs = _sink_outputs(rendered)
        durable_projection = _stable_projection(_public_durable_records(db, run_id=result.run_id, payload_store=payload_store))
        records = list(LandscapeExporter(db).export_run(result.run_id))
        portable_projection = _stable_projection(records)
        if durable_projection != portable_projection:
            raise AssertionError("DAG corpus public durable query and portable export projections differ")
        audit = _audit_evidence(records, portable_projection=portable_projection)
        result_data = result.to_dict()
        return ScenarioRunEvidence(
            schema_version=1,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=rendered.settings_sha256),
            graph=built.graph_evidence,
            runtime=RuntimeEvidence(
                attempted=True,
                run_id=result.run_id,
                status=str(result_data["status"]),
                rows_processed=result_data["rows_processed"],
                rows_succeeded=result_data["rows_succeeded"],
                rows_failed=result_data["rows_failed"],
                output_rows=sum(len(output.rows) for output in sink_outputs),
                sink_outputs=sink_outputs,
                durable_projection=durable_projection,
            ),
            audit=audit,
            recovery=RecoveryEvidence(
                attempted=False,
                database_reopened=False,
                can_resume=False,
                source_replayed=False,
                checkpoint_removed=False,
            ),
            completed_stages=("config", "build", "runtime", "audit"),
        )
    finally:
        db.close()


def _build_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    rendered = render_settings(case, tmp_path)
    built = build_scenario(rendered)
    return ScenarioRunEvidence(
        schema_version=1,
        scenario_id=scenario.id,
        case_id=case.id,
        fixture_sha256=rendered.fixture_sha256,
        config=ConfigEvidence(loaded=True, settings_sha256=rendered.settings_sha256),
        graph=built.graph_evidence,
        runtime=RuntimeEvidence(
            attempted=False,
            run_id=None,
            status=None,
            rows_processed=0,
            rows_succeeded=0,
            rows_failed=0,
            output_rows=0,
        ),
        audit=AuditEvidence(
            attempted=False,
            total_records=0,
            record_counts=(),
            source_operation_count=0,
        ),
        recovery=RecoveryEvidence(
            attempted=False,
            database_reopened=False,
            checkpoint_id=None,
            checkpoint_sequence=None,
            can_resume=False,
            source_replayed=False,
            checkpoint_removed=False,
        ),
        completed_stages=("config", "build"),
    )


def _require_exact_eof_crash(orchestrator: Orchestrator, built: BuiltScenario, payload_store: FilesystemPayloadStore) -> None:
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    try:
        orchestrator.run(
            built.config,
            graph=built.graph,
            settings=built.rendered.settings,
            payload_store=payload_store,
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    except RuntimeError as exc:
        if str(exc) != "injected DAG corpus EOF flush crash":
            raise
    else:
        raise AssertionError("DAG recovery corpus run did not inject the EOF flush crash")


def _assert_terminal_recovery_state(
    db: LandscapeDB,
    *,
    run_id: str,
    checkpoint_id: str,
    resume_node_id: str,
    payload_store: FilesystemPayloadStore,
) -> None:
    repositories = RecorderFactory.read_only(db, payload_store=payload_store)
    run = repositories.run_lifecycle.get_run(run_id)
    if run is None or run.status is not RunStatus.COMPLETED:
        raise AssertionError(f"DAG recovery corpus did not persist a completed run: {run!r}")
    source_records = repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
    if not source_records or any(record.lifecycle_state != "exhausted" for record in source_records.values()):
        raise AssertionError(f"DAG recovery corpus sources lost their exhausted state: {source_records!r}")

    tokens = repositories.query.get_all_tokens_for_run(run_id)
    outcomes = repositories.query.get_all_token_outcomes_for_run(run_id)
    latest_outcomes = {outcome.token_id: outcome for outcome in outcomes}
    token_ids = {token.token_id for token in tokens}
    if (
        not token_ids
        or set(latest_outcomes) != token_ids
        or not all(outcome.completed and outcome.outcome is not None for outcome in latest_outcomes.values())
    ):
        raise AssertionError(
            "DAG recovery corpus requires every token's latest exported outcome to be terminal: "
            f"tokens={sorted(token_ids)!r}, latest_outcomes={latest_outcomes!r}"
        )

    with db.connection() as conn:
        work_statuses = (
            conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id)).scalars().all()
        )
        resumed_markers = (
            conn.execute(
                select(node_states_table.c.state_id).where(
                    node_states_table.c.run_id == run_id,
                    node_states_table.c.node_id == resume_node_id,
                    node_states_table.c.attempt > 0,
                    node_states_table.c.status == "completed",
                    node_states_table.c.resume_checkpoint_id == checkpoint_id,
                )
            )
            .scalars()
            .all()
        )
    if not work_statuses or set(work_statuses) != {"terminal"}:
        raise AssertionError(f"DAG recovery corpus left non-terminal scheduler work: {work_statuses!r}")
    if not resumed_markers:
        raise AssertionError("DAG recovery corpus requires a completed resumed node-state attempt carrying the checkpoint marker")


def _recovery_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    db_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"
    initial_rendered = render_settings(case, tmp_path)
    initial_built = build_scenario(initial_rendered)
    initial_store = FilesystemPayloadStore(payload_root)
    initial_db = LandscapeDB(db_url)
    initial_checkpoint_manager = CheckpointManager(initial_db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    initial_orchestrator = Orchestrator(
        initial_db,
        checkpoint_manager=initial_checkpoint_manager,
        checkpoint_config=checkpoint_config,
    )

    try:
        _require_exact_eof_crash(initial_orchestrator, initial_built, initial_store)
        initial_repositories = RecorderFactory.read_only(initial_db, payload_store=initial_store)
        runs = initial_repositories.run_lifecycle.list_runs()
        if len(runs) != 1:
            raise AssertionError(f"DAG recovery corpus expected exactly one failed run, got {len(runs)}")
        failed_run = runs[0]
        if failed_run.status is not RunStatus.FAILED:
            raise AssertionError(f"DAG recovery corpus expected failed run, got {failed_run.status.value!r}")
        run_id = failed_run.run_id
        source_records = initial_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        if not source_records or any(record.lifecycle_state != "exhausted" for record in source_records.values()):
            raise AssertionError(f"DAG recovery corpus sources were not exhausted before the crash: {source_records!r}")
        checkpoint = initial_checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None:
            raise AssertionError("DAG recovery corpus crash did not preserve a checkpoint")
        if checkpoint.upstream_topology_hash != initial_built.graph_evidence.topology_hash:
            raise AssertionError("DAG recovery corpus checkpoint topology does not match the initial graph")
        checkpoint_id = checkpoint.checkpoint_id
        checkpoint_sequence = checkpoint.sequence_number
        checkpoint_topology_hash = checkpoint.upstream_topology_hash
    finally:
        initial_db.close()

    del initial_orchestrator, initial_checkpoint_manager, initial_repositories
    del initial_built, initial_rendered, initial_store, failed_run, source_records, checkpoint, runs

    reopened_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        reopened_store = FilesystemPayloadStore(payload_root)
        reopened_checkpoint_manager = CheckpointManager(reopened_db)
        reopened_checkpoint = reopened_checkpoint_manager.get_latest_checkpoint(run_id)
        if reopened_checkpoint is None:
            raise AssertionError("DAG recovery corpus checkpoint disappeared across database reopen")
        if (
            reopened_checkpoint.checkpoint_id,
            reopened_checkpoint.sequence_number,
            reopened_checkpoint.upstream_topology_hash,
        ) != (checkpoint_id, checkpoint_sequence, checkpoint_topology_hash):
            raise AssertionError("DAG recovery corpus checkpoint changed across database reopen")

        fresh_rendered = render_settings(case, tmp_path)
        fresh_built = build_scenario(fresh_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        if fresh_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("DAG recovery corpus fresh graph does not match the persisted checkpoint topology")
        recovery = RecoveryManager(reopened_db, reopened_checkpoint_manager)
        resume_check = recovery.can_resume(run_id, fresh_built.graph)
        if not resume_check.can_resume:
            raise AssertionError(f"DAG recovery corpus run is not resumable: {resume_check.reason}")
        resume_point = recovery.get_resume_point(run_id, fresh_built.graph)
        if resume_point is None:
            raise AssertionError("DAG recovery corpus did not produce a public resume point")
        if resume_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError("DAG recovery corpus resume point does not use the reopened checkpoint")

        result = Orchestrator(
            reopened_db,
            checkpoint_manager=reopened_checkpoint_manager,
            checkpoint_config=checkpoint_config,
        ).resume(
            resume_point,
            fresh_built.config,
            fresh_built.graph,
            payload_store=reopened_store,
            settings=fresh_rendered.settings,
        )
        if result.run_id != run_id:
            raise AssertionError(f"DAG recovery corpus resumed the wrong run: expected {run_id!r}, got {result.run_id!r}")
        output_rows = [json.loads(line) for line in fresh_rendered.output_path.read_text(encoding="utf-8").splitlines()]
        if output_rows != [{"value": 60, "count": 3}]:
            raise AssertionError(f"DAG recovery corpus emitted unexpected output: {output_rows!r}")

        records = list(LandscapeExporter(reopened_db).export_run(run_id))
        audit = _audit_evidence(records)
        if audit.source_operation_count != 1:
            raise AssertionError(f"DAG recovery corpus replayed its source: source_load count={audit.source_operation_count}")
        aggregation_node_ids = tuple(str(node_id) for node_id in fresh_built.graph.get_aggregation_id_map().values())
        if len(aggregation_node_ids) != 1:
            raise AssertionError(f"DAG recovery corpus expected one aggregation node, got {aggregation_node_ids!r}")
        _assert_terminal_recovery_state(
            reopened_db,
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            resume_node_id=aggregation_node_ids[0],
            payload_store=reopened_store,
        )
        if reopened_checkpoint_manager.get_latest_checkpoint(run_id) is not None:
            raise AssertionError("DAG recovery corpus retained a checkpoint after successful resume")
        if not fresh_rendered.fault_marker.is_file():
            raise AssertionError("DAG recovery corpus fault marker is missing after resume")

        result_data = result.to_dict()
        return ScenarioRunEvidence(
            schema_version=1,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=fresh_rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=fresh_rendered.settings_sha256),
            graph=fresh_built.graph_evidence,
            runtime=RuntimeEvidence(
                attempted=True,
                run_id=result.run_id,
                status=str(result_data["status"]),
                rows_processed=result_data["rows_processed"],
                rows_succeeded=result_data["rows_succeeded"],
                rows_failed=result_data["rows_failed"],
                output_rows=len(output_rows),
            ),
            audit=audit,
            recovery=RecoveryEvidence(
                attempted=True,
                database_reopened=True,
                checkpoint_id=checkpoint_id,
                checkpoint_sequence=checkpoint_sequence,
                can_resume=True,
                source_replayed=False,
                checkpoint_removed=True,
            ),
            completed_stages=("config", "build", "runtime", "audit", "recovery"),
        )
    finally:
        reopened_db.close()


def run_scenario_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    """Execute a declared case through the workflow implemented for this task."""

    if case.workflow == "build":
        return _build_case(scenario, case, tmp_path)
    if case.workflow == "run":
        return _run_case(scenario, case, tmp_path)
    return _recovery_case(scenario, case, tmp_path)
