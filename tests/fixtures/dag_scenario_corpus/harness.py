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
from elspeth.contracts.audit_export import (
    AUDIT_EXPORT_MAX_CHUNK_BYTES,
    AUDIT_EXPORT_MAX_CHUNK_RECORDS,
    AUDIT_EXPORT_SERIALIZATION_VERSION,
    AuditExportDerivationConfig,
    derive_audit_export_bundle,
)
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.hashing import canonical_json as contract_canonical_json
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.config import ElspethSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB, LandscapeExporter, RecorderFactory
from elspeth.core.landscape.export_read_model import open_export_read_transaction
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
    StableAuditRecordProjection,
    StableAuditRecordType,
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
    runtime_root = tmp_path.resolve()
    for sink_name, output_path in output_paths.items():
        resolved_output = output_path.resolve()
        try:
            resolved_output.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError(
                f"DAG scenario sink {sink_name!r} artifact must resolve beneath runtime root {runtime_root}: {resolved_output}"
            ) from exc
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
    config = record.get("config")
    if not isinstance(config, Mapping):
        raise AssertionError(f"DAG corpus node {node_id!r} lacks material audit config")
    semantic_config = _semantic_node_config(record)
    config_fingerprint = hashlib.sha256(_canonical_json(semantic_config).encode()).hexdigest()[:12]
    return f"{node_type}:{match.group(1)}@{config_fingerprint}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _semantic_node_config(record: Mapping[str, Any]) -> dict[str, object]:
    """Normalize only the source/sink paths bound by ``render_settings``."""

    config = record.get("config")
    if not isinstance(config, Mapping):
        raise AssertionError(f"DAG corpus node {record.get('node_id')!r} lacks material audit config")
    semantic = dict(config)
    if record.get("node_type") in {"source", "sink"} and "path" in semantic:
        semantic["path"] = "$CORPUS_RUNTIME_PATH"
    return semantic


def _semantic_run_settings(raw_settings: object) -> dict[str, object]:
    if not isinstance(raw_settings, Mapping):
        raise AssertionError("DAG corpus run lacks material settings")
    settings = json.loads(json.dumps(raw_settings))
    for section in ("sources", "sinks"):
        declarations = settings.get(section)
        if not isinstance(declarations, dict):
            continue
        for declaration in declarations.values():
            if isinstance(declaration, dict) and isinstance(declaration.get("options"), dict) and "path" in declaration["options"]:
                declaration["options"]["path"] = "$CORPUS_RUNTIME_PATH"
    return cast(dict[str, object], settings)


def _stable_ref(
    refs: Mapping[str, str],
    raw_ref: object,
    *,
    source: str,
    record_type: str,
    field: str,
) -> str | None:
    if raw_ref is None:
        return None
    try:
        return refs[str(raw_ref)]
    except KeyError as exc:
        raise AssertionError(f"DAG corpus {source} {record_type} references unknown {field} {raw_ref!r}") from exc


def _stable_audit_records(
    records: list[dict[str, Any]],
    *,
    source: str,
    node_keys: Mapping[str, str],
    row_keys: Mapping[str, str],
    token_keys: Mapping[str, str],
    state_keys: Mapping[str, dict[str, Any]],
) -> tuple[StableAuditRecordProjection, ...]:
    """Project every audit family claimed by the exact F0 linear oracle."""

    audit: list[StableAuditRecordProjection] = []

    def add(record_type: StableAuditRecordType, key: str, material: Mapping[str, object], *references: str | None) -> None:
        audit.append(
            StableAuditRecordProjection(
                key=f"{record_type}|{key}",
                record_type=record_type,
                material=_canonical_json(material),
                references=tuple(sorted({reference for reference in references if reference is not None})),
            )
        )

    run_records = [record for record in records if record.get("record_type") == "run"]
    if len(run_records) != 1:
        raise AssertionError(f"DAG corpus {source} projection requires exactly one run record")
    run = run_records[0]
    settings = _semantic_run_settings(run["settings"])
    add(
        "run",
        "run",
        {
            "canonical_version": run["canonical_version"],
            "reproducibility_grade": run.get("reproducibility_grade"),
            "semantic_settings_sha256": hashlib.sha256(_canonical_json(settings).encode()).hexdigest(),
            "status": run["status"],
        },
    )

    for record in (record for record in records if record.get("record_type") == "node"):
        node_key = node_keys[str(record["node_id"])]
        semantic_config = _semantic_node_config(record)
        add(
            "node",
            node_key,
            {
                "config": semantic_config,
                "determinism": record["determinism"],
                "node_type": record["node_type"],
                "plugin_name": record["plugin_name"],
                "plugin_version": record["plugin_version"],
                "schema_fields": record.get("schema_fields"),
                "schema_hash": record.get("schema_hash"),
                "schema_mode": record.get("schema_mode"),
                "sequence_in_pipeline": record.get("sequence_in_pipeline"),
                "source_file_hash": record.get("source_file_hash"),
            },
        )

    edge_keys: dict[str, str] = {}
    for record in (record for record in records if record.get("record_type") == "edge"):
        from_key = _stable_ref(node_keys, record["from_node_id"], source=source, record_type="edge", field="from_node_id")
        to_key = _stable_ref(node_keys, record["to_node_id"], source=source, record_type="edge", field="to_node_id")
        assert from_key is not None and to_key is not None
        key = f"{from_key}|{record['label']}|{record['default_mode']}|{to_key}"
        edge_keys[str(record["edge_id"])] = key
        add("edge", key, {"default_mode": record["default_mode"], "label": record["label"]}, from_key, to_key)

    operation_keys: dict[str, str] = {}
    operation_records_by_id: dict[str, dict[str, Any]] = {}
    operation_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    for record in (record for record in records if record.get("record_type") == "operation"):
        operation_node_key = _stable_ref(node_keys, record["node_id"], source=source, record_type="operation", field="node_id")
        assert operation_node_key is not None
        identity = (operation_node_key, str(record["operation_type"]))
        ordinal = operation_counts[identity]
        operation_counts[identity] += 1
        key = f"{operation_node_key}|{record['operation_type']}|{ordinal}"
        operation_keys[str(record["operation_id"])] = key
        operation_records_by_id[str(record["operation_id"])] = record

    stream_keys: dict[str, str] = {}
    for record in (record for record in records if record.get("record_type") == "sink_effect_stream"):
        stream_node_key = _stable_ref(
            node_keys, record["sink_node_id"], source=source, record_type="sink_effect_stream", field="sink_node_id"
        )
        assert stream_node_key is not None
        key = f"{stream_node_key}|{record['role']}"
        stream_keys[str(record["stream_id"])] = key

    effect_keys: dict[str, str] = {}
    for record in (record for record in records if record.get("record_type") == "sink_effect"):
        effect_node_key = _stable_ref(node_keys, record["sink_node_id"], source=source, record_type="sink_effect", field="sink_node_id")
        assert effect_node_key is not None
        key = f"{effect_node_key}|{record['role']}|{record.get('stream_sequence')}"
        effect_keys[str(record["effect_id"])] = key

    artifact_keys: dict[str, str] = {}
    for record in (record for record in records if record.get("record_type") == "artifact"):
        effect_key = _stable_ref(
            effect_keys,
            record.get("sink_effect_id"),
            source=source,
            record_type="artifact",
            field="sink_effect_id",
        )
        state = state_keys.get(str(record.get("produced_by_state_id"))) if record.get("produced_by_state_id") is not None else None
        state_key = None
        if state is not None:
            state_key = (
                f"{token_keys[str(state['token_id'])]}|{node_keys[str(state['node_id'])]}|"
                f"{int(state['step_index'])}|{int(state['attempt'])}"
            )
        producer_key = effect_key or state_key
        if producer_key is None:
            raise AssertionError(f"DAG corpus {source} artifact has no stable producer")
        key = f"{producer_key}|{record['artifact_type']}"
        artifact_keys[str(record["artifact_id"])] = key
        sink_key = _stable_ref(node_keys, record["sink_node_id"], source=source, record_type="artifact", field="sink_node_id")
        add(
            "artifact",
            key,
            {
                "artifact_type": record["artifact_type"],
                "content_hash": record["content_hash"],
                "idempotency_witness": effect_key,
                "path_or_uri": "$CORPUS_RUNTIME_PATH",
                "producer_kind": record["producer_kind"],
                "publication_evidence_kind": record["publication_evidence_kind"],
                "publication_performed": record["publication_performed"],
                "size_bytes": record["size_bytes"],
            },
            producer_key,
            sink_key,
        )

    attempt_keys: dict[str, str] = {}
    for record in (record for record in records if record.get("record_type") == "sink_effect_attempt"):
        effect_key = _stable_ref(effect_keys, record["effect_id"], source=source, record_type="sink_effect_attempt", field="effect_id")
        assert effect_key is not None
        attempt_keys[str(record["attempt_id"])] = f"{effect_key}|{int(record['attempt_index'])}"

    calls_by_operation_index: dict[tuple[str, int], str] = {}
    for record in (record for record in records if record.get("record_type") == "call"):
        operation_key = _stable_ref(
            operation_keys,
            record.get("operation_id"),
            source=source,
            record_type="call",
            field="operation_id",
        )
        parent_key = operation_key
        if parent_key is None:
            state = state_keys.get(str(record.get("state_id")))
            if state is None:
                raise AssertionError(f"DAG corpus {source} call has no stable parent")
            parent_key = (
                f"{token_keys[str(state['token_id'])]}|{node_keys[str(state['node_id'])]}|"
                f"{int(state['step_index'])}|{int(state['attempt'])}"
            )
        key = f"{parent_key}|{int(record['call_index'])}"
        if operation_key is not None:
            calls_by_operation_index[(operation_key, int(record["call_index"]))] = key
        operation = operation_records_by_id.get(str(record.get("operation_id")))
        sink_effect_call = operation is not None and operation.get("sink_effect_id") is not None
        add(
            "call",
            key,
            {
                "call_index": record["call_index"],
                "call_type": record["call_type"],
                "error_json": record.get("error_json"),
                "request_hash": "$SINK_EFFECT_REQUEST" if sink_effect_call else record.get("request_hash"),
                "request_ref": record.get("request_ref"),
                "resolved_prompt_template_hash": record.get("resolved_prompt_template_hash"),
                "response_hash": "$SINK_EFFECT_RESPONSE" if sink_effect_call else record.get("response_hash"),
                "response_ref": record.get("response_ref"),
                "status": record["status"],
            },
            parent_key,
        )

    for record in (record for record in records if record.get("record_type") == "operation"):
        key = operation_keys[str(record["operation_id"])]
        operation_node_key = _stable_ref(node_keys, record["node_id"], source=source, record_type="operation", field="node_id")
        effect_key = _stable_ref(
            effect_keys,
            record.get("sink_effect_id"),
            source=source,
            record_type="operation",
            field="sink_effect_id",
        )
        add(
            "operation",
            key,
            {
                "error_message": record.get("error_message"),
                "input_data_hash": record.get("input_data_hash"),
                "input_data_ref": record.get("input_data_ref"),
                "operation_type": record["operation_type"],
                "output_data_hash": "$SINK_EFFECT_RESULT" if effect_key is not None else record.get("output_data_hash"),
                "output_data_ref": record.get("output_data_ref"),
                "status": record["status"],
            },
            operation_node_key,
            effect_key,
        )

    members_by_effect: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "sink_effect_member"):
        members_by_effect[str(record["effect_id"])].append(record)

    for record in (record for record in records if record.get("record_type") == "sink_effect_stream"):
        key = stream_keys[str(record["stream_id"])]
        stream_node_key = _stable_ref(
            node_keys, record["sink_node_id"], source=source, record_type="sink_effect_stream", field="sink_node_id"
        )
        head_key = _stable_ref(
            effect_keys, record.get("head_effect_id"), source=source, record_type="sink_effect_stream", field="head_effect_id"
        )
        tail_key = _stable_ref(
            effect_keys, record.get("tail_effect_id"), source=source, record_type="sink_effect_stream", field="tail_effect_id"
        )
        add(
            "sink_effect_stream",
            key,
            {
                "head_descriptor_witness": head_key,
                "next_sequence": record["next_sequence"],
                "requested_target": "$CORPUS_RUNTIME_PATH",
                "role": record["role"],
            },
            stream_node_key,
            head_key,
            tail_key,
        )

    for record in (record for record in records if record.get("record_type") == "sink_effect"):
        raw_effect_id = str(record["effect_id"])
        key = effect_keys[raw_effect_id]
        effect_node_key = _stable_ref(node_keys, record["sink_node_id"], source=source, record_type="sink_effect", field="sink_node_id")
        stream_key = _stable_ref(stream_keys, record.get("stream_id"), source=source, record_type="sink_effect", field="stream_id")
        artifact_key = _stable_ref(artifact_keys, record["artifact_id"], source=source, record_type="sink_effect", field="artifact_id")
        inspection_key = _stable_ref(
            attempt_keys,
            record.get("inspection_attempt_id"),
            source=source,
            record_type="sink_effect",
            field="inspection_attempt_id",
        )
        primary_key = _stable_ref(
            effect_keys, record.get("primary_effect_id"), source=source, record_type="sink_effect", field="primary_effect_id"
        )
        predecessor_key = _stable_ref(
            effect_keys,
            record.get("predecessor_effect_id"),
            source=source,
            record_type="sink_effect",
            field="predecessor_effect_id",
        )
        member_payload_hashes = [
            member["payload_hash"] for member in sorted(members_by_effect[raw_effect_id], key=lambda member: int(member["ordinal"]))
        ]
        add(
            "sink_effect",
            key,
            {
                "descriptor_mode": record.get("descriptor_mode"),
                "descriptor_witness": artifact_key,
                "generation": record["generation"],
                "group_payload_hash": record["group_payload_hash"],
                "input_kind": record["input_kind"],
                "inspection_mode": record.get("inspection_mode"),
                "member_payload_hashes": member_payload_hashes,
                "plan_present": record.get("plan_hash") is not None,
                "precondition_witness": stream_key,
                "protocol_version": record["protocol_version"],
                "publication_evidence_kind": record.get("publication_evidence_kind"),
                "publication_performed": record.get("publication_performed"),
                "reconcile_kind": record.get("reconcile_kind"),
                "role": record["role"],
                "state": record["state"],
                "stream_sequence": record.get("stream_sequence"),
            },
            artifact_key,
            inspection_key,
            effect_node_key,
            predecessor_key,
            primary_key,
            stream_key,
        )

    for record in (record for record in records if record.get("record_type") == "sink_effect_member"):
        effect_key = _stable_ref(effect_keys, record["effect_id"], source=source, record_type="sink_effect_member", field="effect_id")
        token_key = _stable_ref(token_keys, record["token_id"], source=source, record_type="sink_effect_member", field="token_id")
        row_key = _stable_ref(row_keys, record["row_id"], source=source, record_type="sink_effect_member", field="row_id")
        member_node_key = _stable_ref(
            node_keys, record["sink_node_id"], source=source, record_type="sink_effect_member", field="sink_node_id"
        )
        primary_key = _stable_ref(
            effect_keys, record.get("primary_effect_id"), source=source, record_type="sink_effect_member", field="primary_effect_id"
        )
        assert effect_key is not None
        key = f"{effect_key}|{int(record['ordinal'])}"
        add(
            "sink_effect_member",
            key,
            {
                "descriptor_witness": effect_key,
                "evidence_present": record.get("evidence_hash") is not None,
                "ingest_sequence": record["ingest_sequence"],
                "lineage_hash": record["lineage_hash"],
                "member_effect_bound": record.get("member_effect_id") is not None,
                "member_state": record.get("member_state"),
                "ordinal": record["ordinal"],
                "payload_hash": record["payload_hash"],
                "prepared_disposition": record.get("prepared_disposition"),
                "reason_hash": record.get("reason_hash"),
                "role": record["role"],
            },
            effect_key,
            member_node_key,
            primary_key,
            row_key,
            token_key,
        )

    sink_operation_by_effect = {
        str(record["sink_effect_id"]): operation_keys[str(record["operation_id"])]
        for record in records
        if record.get("record_type") == "operation" and record.get("sink_effect_id") is not None
    }
    for record in (record for record in records if record.get("record_type") == "sink_effect_attempt"):
        effect_key = _stable_ref(effect_keys, record["effect_id"], source=source, record_type="sink_effect_attempt", field="effect_id")
        assert effect_key is not None
        key = attempt_keys[str(record["attempt_id"])]
        operation_key = sink_operation_by_effect.get(str(record["effect_id"]))
        call_key = calls_by_operation_index.get((operation_key, int(record["attempt_index"]))) if operation_key is not None else None
        add(
            "sink_effect_attempt",
            key,
            {
                "action": record["action"],
                "attempt_index": record["attempt_index"],
                "call_kind": record["call_kind"],
                "evidence_present": record.get("evidence_hash") is not None,
                "evidence_witness": call_key,
                "generation": record["generation"],
                "member_ordinal": record.get("member_ordinal"),
                "request_witness": call_key,
                "state": record["state"],
            },
            call_key,
            effect_key,
        )

    manifest_records = [record for record in records if record.get("record_type") == "manifest"]
    if len(manifest_records) != 1:
        raise AssertionError(f"DAG corpus {source} projection requires exactly one manifest record")
    manifest = manifest_records[0]
    add(
        "manifest",
        "manifest",
        {
            "chunk_count": manifest["chunk_count"],
            "derivation_version": manifest["derivation_version"],
            "export_format": manifest["export_format"],
            "hash_algorithm": manifest["hash_algorithm"],
            "record_chain_algorithm": manifest["record_chain_algorithm"],
            "record_count": manifest["record_count"],
            "schema": manifest["schema"],
            "signature_algorithm": manifest["signature_algorithm"],
            "signature_key_id": manifest["signature_key_id"],
            "source_status": manifest["source_status"],
        },
        "run|run",
    )
    return tuple(sorted(audit, key=lambda record: record.key))


def _stable_projection(records: list[dict[str, Any]], *, source: str = "projection") -> StableRunProjection:
    """Normalize one public durable/export view without retaining run-local IDs."""

    node_records = [record for record in records if record.get("record_type") == "node"]
    node_keys = {str(record["node_id"]): _stable_node_key(record) for record in node_records}
    edge_records = [record for record in records if record.get("record_type") == "edge"]
    edges = {str(record["edge_id"]): record for record in edge_records}

    rows_by_id: dict[str, str] = {}
    rows: list[StableRowProjection] = []
    for record in (record for record in records if record.get("record_type") == "row"):
        source_key = node_keys[str(record["source_node_id"])]
        source_name = source_key.split(":", 1)[1].rsplit("@", 1)[0]
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
        audit_records=_stable_audit_records(
            records,
            source=source,
            node_keys=node_keys,
            row_keys=rows_by_id,
            token_keys=token_keys,
            state_keys=state_keys,
        ),
    )


def _record_index(
    records: list[dict[str, Any]],
    *,
    record_type: str,
    key_fields: tuple[str, ...],
    source: str,
) -> dict[tuple[object, ...], dict[str, Any]]:
    indexed: dict[tuple[object, ...], dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") != record_type:
            continue
        key = tuple(record.get(field) for field in key_fields)
        if key in indexed:
            raise AssertionError(f"DAG corpus {source} {record_type} integrity: duplicate key {key!r}")
        indexed[key] = record
    return indexed


def _require_material_equal(*, source: str, record_type: str, field: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"DAG corpus {source} {record_type} integrity: {field} differs from authoritative material")


def _sink_effect_member_id(effect_id: str, ordinal: int) -> str:
    payload = {"payload": {"effect_id": effect_id, "ordinal": ordinal}, "schema": "sink-effect-member-v1"}
    return hashlib.sha256(contract_canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_durable_sink_effect_material(records: list[dict[str, Any]]) -> None:
    """Validate stored sink-effect hashes before portable normalization.

    Request payloads are deliberately absent from both the public durable read
    model and portable export. Their hashes therefore have only the exact
    attempt-to-operation-call equality witness checked below; unlike the other
    families here, shared request-hash corruption cannot be recomputed from
    public data.
    """

    source = "durable"
    effects = _record_index(records, record_type="sink_effect", key_fields=("effect_id",), source=source)
    artifacts = _record_index(records, record_type="artifact", key_fields=("artifact_id",), source=source)
    streams = _record_index(records, record_type="sink_effect_stream", key_fields=("stream_id",), source=source)
    members = _record_index(records, record_type="sink_effect_member", key_fields=("effect_id", "ordinal"), source=source)
    attempts = _record_index(records, record_type="sink_effect_attempt", key_fields=("effect_id", "attempt_index"), source=source)
    operations = _record_index(records, record_type="operation", key_fields=("operation_id",), source=source)
    calls = _record_index(records, record_type="call", key_fields=("operation_id", "call_index"), source=source)

    operation_by_effect = {
        str(operation["sink_effect_id"]): operation for operation in operations.values() if operation.get("sink_effect_id") is not None
    }
    for (effect_id_value,), effect in effects.items():
        effect_id = str(effect_id_value)
        raw_plan = effect.get("_plan_json")
        if not isinstance(raw_plan, str):
            raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} lacks public plan material")
        try:
            plan = json.loads(raw_plan)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} plan is invalid JSON") from exc
        if not isinstance(plan, dict):
            raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} plan is not an object")
        for field in ("effect_id", "input_kind", "plan_hash", "descriptor_mode", "protocol_version"):
            _require_material_equal(
                source=source,
                record_type="sink_effect",
                field=field,
                actual=effect.get(field),
                expected=plan.get(field),
            )

        descriptor = plan.get("expected_descriptor")
        if descriptor is not None:
            if not isinstance(descriptor, dict):
                raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} expected_descriptor is not an object")
            _require_material_equal(
                source=source,
                record_type="sink_effect",
                field="expected_descriptor_hash",
                actual=effect.get("expected_descriptor_hash"),
                expected=stable_hash(descriptor),
            )
            artifact = artifacts.get((effect.get("artifact_id"),))
            if artifact is None:
                raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} references unknown artifact")
            for field in ("artifact_type", "content_hash", "path_or_uri", "size_bytes"):
                _require_material_equal(
                    source=source,
                    record_type="sink_effect",
                    field=f"artifact.{field}",
                    actual=artifact.get(field),
                    expected=descriptor.get(field),
                )

        _require_material_equal(
            source=source,
            record_type="sink_effect",
            field="precondition_hash",
            actual=effect.get("precondition_hash"),
            expected=stable_hash(
                {
                    "inspection_attempt_id": effect.get("inspection_attempt_id"),
                    "safe_evidence": plan.get("safe_evidence"),
                }
            ),
        )
        if effect.get("expected_descriptor_hash") is not None:
            _require_material_equal(
                source=source,
                record_type="sink_effect",
                field="result_descriptor_hash",
                actual=effect.get("result_descriptor_hash"),
                expected=effect.get("expected_descriptor_hash"),
            )

        stream_id = effect.get("stream_id")
        stream = streams.get((stream_id,))
        if stream is None:
            raise AssertionError(f"DAG corpus durable sink_effect integrity: {effect_id} references unknown stream")
        if stream.get("head_effect_id") == effect_id:
            _require_material_equal(
                source=source,
                record_type="sink_effect",
                field="stream.head_descriptor_hash",
                actual=stream.get("head_descriptor_hash"),
                expected=effect.get("result_descriptor_hash"),
            )

    for (effect_id_value, ordinal_value), member in members.items():
        effect_id = str(effect_id_value)
        ordinal = int(cast(int, ordinal_value))
        expected_member_id = _sink_effect_member_id(effect_id, ordinal)
        _require_material_equal(
            source=source,
            record_type="sink_effect_member",
            field="member_effect_id",
            actual=member.get("member_effect_id"),
            expected=expected_member_id,
        )
        member_effect = effects.get((effect_id,))
        if member_effect is None:
            raise AssertionError(f"DAG corpus durable sink_effect_member integrity: {effect_id} references unknown effect")
        _require_material_equal(
            source=source,
            record_type="sink_effect_member",
            field="descriptor_hash",
            actual=member.get("descriptor_hash"),
            expected=member_effect.get("result_descriptor_hash"),
        )

    for (effect_id_value, attempt_index_value), attempt in attempts.items():
        effect_id = str(effect_id_value)
        raw_evidence = attempt.get("_evidence_json")
        expected_evidence_hash = None
        if raw_evidence is not None:
            if not isinstance(raw_evidence, str):
                raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: {effect_id} evidence is not JSON text")
            expected_evidence_hash = hashlib.sha256(raw_evidence.encode("utf-8")).hexdigest()
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="evidence_hash",
            actual=attempt.get("evidence_hash"),
            expected=expected_evidence_hash,
        )
        operation = operation_by_effect.get(effect_id)
        if operation is None:
            raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: {effect_id} has no sink-effect operation")
        call = calls.get((operation.get("operation_id"), attempt_index_value))
        if call is None:
            raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: {effect_id} has no matching operation call")
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="request_hash",
            actual=attempt.get("request_hash"),
            expected=call.get("request_hash"),
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="evidence_hash/call.response_hash",
            actual=attempt.get("evidence_hash"),
            expected=call.get("response_hash"),
        )


def _validate_portable_material_matches_durable(durable_records: list[dict[str, Any]], portable_records: list[dict[str, Any]]) -> None:
    field_groups = (
        (
            "sink_effect",
            ("effect_id",),
            ("artifact_id", "expected_descriptor_hash", "result_descriptor_hash", "precondition_hash"),
        ),
        ("sink_effect_member", ("effect_id", "ordinal"), ("descriptor_hash", "member_effect_id")),
        ("sink_effect_attempt", ("effect_id", "attempt_index"), ("request_hash", "evidence_hash")),
        ("call", ("call_id",), ("request_hash", "response_hash")),
    )
    for record_type, key_fields, fields in field_groups:
        durable = _record_index(durable_records, record_type=record_type, key_fields=key_fields, source="durable")
        portable = _record_index(portable_records, record_type=record_type, key_fields=key_fields, source="portable")
        if durable.keys() != portable.keys():
            raise AssertionError(f"DAG corpus portable {record_type} integrity: record identities differ from durable data")
        for key, durable_record in durable.items():
            portable_record = portable[key]
            for field in fields:
                _require_material_equal(
                    source="portable",
                    record_type=record_type,
                    field=field,
                    actual=portable_record.get(field),
                    expected=durable_record.get(field),
                )


def _validate_portable_manifest(records: list[dict[str, Any]]) -> None:
    manifests = [record for record in records if record.get("record_type") == "manifest"]
    if len(manifests) != 1:
        raise AssertionError("DAG corpus portable manifest integrity: expected exactly one manifest")
    manifest = manifests[0]
    run_records = [record for record in records if record.get("record_type") == "run"]
    if len(run_records) != 1:
        raise AssertionError("DAG corpus portable manifest integrity: expected exactly one run")
    run = run_records[0]
    expected_completed_at = f"{run['completed_at']}Z"
    for field, expected in (
        ("run_id", run.get("run_id")),
        ("source_status", run.get("status")),
        ("source_completed_at", expected_completed_at),
        ("export_format", "json"),
        ("signature_algorithm", "unsigned"),
        ("signature_key_id", "UNSIGNED"),
    ):
        _require_material_equal(
            source="portable",
            record_type="manifest",
            field=field,
            actual=manifest.get(field),
            expected=expected,
        )
    config = AuditExportDerivationConfig(
        source_run_id=str(run["run_id"]),
        source_status=str(run["status"]),
        source_completed_at=expected_completed_at,
        export_format="json",
        exporter_version="landscape-exporter-v1",
        serialization_version=AUDIT_EXPORT_SERIALIZATION_VERSION,
        chunking_algorithm_version="record-framing-v1",
        include_raw_error_rows=False,
        per_chunk_byte_limit=AUDIT_EXPORT_MAX_CHUNK_BYTES,
        per_chunk_record_limit=AUDIT_EXPORT_MAX_CHUNK_RECORDS,
        signing_mode="unsigned",
        signer_key_id="UNSIGNED",
        signing_key=None,
    )
    expected_manifest = dict(
        derive_audit_export_bundle(
            (record for record in records if record.get("record_type") != "manifest"),
            config,
        ).final_manifest
    )
    if manifest != expected_manifest:
        mismatches = sorted(
            field for field in manifest.keys() | expected_manifest.keys() if manifest.get(field) != expected_manifest.get(field)
        )
        raise AssertionError(f"DAG corpus portable manifest integrity: fields differ from canonical derivation: {', '.join(mismatches)}")


def _public_durable_records(db: LandscapeDB, *, run_id: str, payload_store: FilesystemPayloadStore) -> list[dict[str, Any]]:
    repositories = RecorderFactory.read_only(db, payload_store=payload_store)
    with open_export_read_transaction(db.engine) as read_model:
        unsigned_records = list(LandscapeExporter(db, read_model=read_model).iter_unsigned_run_records(run_id))
        effect_plan_json = {effect.effect_id: effect.plan_json for effect in read_model.get_sink_effects_for_run(run_id)}
        attempt_evidence_json = {
            attempt.attempt_id: attempt.evidence_json for attempt in read_model.get_sink_effect_attempts_for_run(run_id)
        }
    audit_types = {
        "artifact",
        "call",
        "edge",
        "node",
        "operation",
        "run",
        "sink_effect",
        "sink_effect_attempt",
        "sink_effect_member",
        "sink_effect_stream",
    }
    records = [dict(record) for record in unsigned_records if record["record_type"] in audit_types]
    for record in records:
        if record["record_type"] == "sink_effect":
            record["_plan_json"] = effect_plan_json[str(record["effect_id"])]
        elif record["record_type"] == "sink_effect_attempt":
            record["_evidence_json"] = attempt_evidence_json[str(record["attempt_id"])]
    run = next(record for record in records if record["record_type"] == "run")
    records.append(
        {
            "record_type": "manifest",
            "chunk_count": 1,
            "derivation_version": "audit-export-derivation-v1",
            "export_format": "json",
            "hash_algorithm": "sha256",
            "record_chain_algorithm": "sha256_concat_record_sha256_v1",
            "record_count": len(unsigned_records),
            "schema": "elspeth.audit-export-manifest.v2",
            "signature_algorithm": "unsigned",
            "signature_key_id": "UNSIGNED",
            "source_status": run["status"],
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
            if rendered.settings.sinks[sink_name].plugin != "dag_corpus_always_fail_sink":
                raise AssertionError(f"DAG corpus sink {sink_name!r} did not produce {output_path.name!r}")
            continue
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
        durable_records = _public_durable_records(db, run_id=result.run_id, payload_store=payload_store)
        _validate_durable_sink_effect_material(durable_records)
        durable_projection = _stable_projection(
            durable_records,
            source="durable",
        )
        records = list(LandscapeExporter(db).export_run(result.run_id))
        _validate_portable_material_matches_durable(durable_records, records)
        _validate_portable_manifest(records)
        portable_projection = _stable_projection(records, source="portable")
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
