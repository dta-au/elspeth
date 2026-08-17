"""Production-path execution harness for the maintained DAG scenario corpus."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import patch

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
from elspeth.contracts.errors import CoalesceCollisionError
from elspeth.contracts.hashing import canonical_json as contract_canonical_json
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.config import ElspethSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB, LandscapeExporter, RecorderFactory
from elspeth.core.landscape.execution.sink_effect_reservation import SinkEffectReservation
from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    RunSourceLifecycleState,
    artifacts_table,
    batch_members_table,
    batches_table,
    calls_table,
    edges_table,
    node_states_table,
    nodes_table,
    operations_table,
    routing_events_table,
    rows_table,
    run_coordination_events_table,
    runs_table,
    scheduler_events_table,
    sink_effect_attempts_table,
    sink_effect_members_table,
    sink_effect_streams_table,
    sink_effects_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
    transform_errors_table,
    validation_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
)
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.engine.orchestrator.run_status import derive_terminal_status_from_audit
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle, instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from tests.fixtures.dag_scenario_corpus.loader import resolve_fixture_path
from tests.fixtures.dag_scenario_corpus.schema import (
    AggregationEOFRecoveryEvidence,
    ArtifactByteDigest,
    AuditEvidence,
    AuditRecordCount,
    ConfigEvidence,
    ExpansionChildEnqueueRecoveryEvidence,
    GraphEvidence,
    GraphNodeType,
    GraphNodeTypeCount,
    HarnessCaseSpec,
    OutputArtifactExpectation,
    ParallelSinkFinalizationRecoveryEvidence,
    PendingSinkRedriveRecoveryEvidence,
    PortableExportUnavailableByPolicy,
    RecoveryEvidence,
    RunExpectation,
    RuntimeEvidence,
    ScenarioRunEvidence,
    ScenarioSpec,
    SemanticProjectionCounts,
    SemanticRuntimeProjection,
    SemanticTokenProjection,
    SinkBoundaryEffectProjection,
    SinkBoundaryRecoveryEvidence,
    SinkBoundaryWorkProjection,
    SinkOutputProjection,
    StableAuditRecordProjection,
    StableAuditRecordType,
    StableBatchMemberProjection,
    StableBatchProjection,
    StableExpansionChildProjection,
    StableExpansionProjection,
    StableIntermediateOutcomeProjection,
    StableNodeStateProjection,
    StableParentProjection,
    StableRouteProjection,
    StableRowProjection,
    StableRunProjection,
    StableSchedulerWorkProjection,
    StableTerminalDisposition,
    StableTokenProjection,
    StableTransformErrorProjection,
    StableValidationErrorProjection,
    SummaryRunExpectation,
    TerminalBatchProjection,
    TerminalEquivalenceProjection,
    TerminalNodeStateProjection,
    TerminalResumeIdempotenceEvidence,
    TerminalSchedulerWorkProjection,
    normalize_template_name,
)

EXPECTED_RUN_ERROR_TYPES: Mapping[str, type[BaseException]] = MappingProxyType({"CoalesceCollisionError": CoalesceCollisionError})


@dataclass(frozen=True, slots=True)
class RenderedScenario:
    settings: ElspethSettings
    settings_yaml: str
    settings_sha256: str
    fixture_sha256: str
    input_paths: Mapping[str, Path]
    output_paths: Mapping[str, Path]
    output_expectations: Mapping[str, OutputArtifactExpectation]
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


@dataclass(frozen=True, slots=True)
class SinkBoundaryInterruptedContext:
    """Ephemeral, read-only verification context for the interrupted database.

    The context is valid only while ``before_reopen_verifier`` is executing.
    Topology helpers may issue direct-table reads through ``database`` but must
    not mutate it or retain any runtime object after the callback returns.
    """

    database: LandscapeDB
    payload_store: FilesystemPayloadStore
    scenario: ScenarioSpec
    case: HarnessCaseSpec
    rendered: RenderedScenario
    built: BuiltScenario
    run_id: str
    checkpoint_id: str
    checkpoint_sequence: int
    checkpoint_topology_hash: str
    source_names_exhausted: tuple[str, ...]
    token_ids: tuple[str, ...]
    work: tuple[SinkBoundaryWorkProjection, ...]
    effects: tuple[SinkBoundaryEffectProjection, ...]
    interrupted_effect: SinkBoundaryEffectProjection


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
    output_root = tmp_path / "artifacts" if case.recovery_kind == "terminal_resume_idempotence" else tmp_path
    output_paths = {name: output_root / artifact.filename for name, artifact in case.output_artifacts.items()}
    runtime_root = tmp_path.resolve()
    for sink_name, output_path in output_paths.items():
        resolved_output = output_path.resolve()
        try:
            resolved_output.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError(
                f"DAG scenario sink {sink_name!r} artifact must resolve beneath runtime root {runtime_root}: {resolved_output}"
            ) from exc
        output_path.parent.mkdir(parents=True, exist_ok=True)
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
        output_expectations=MappingProxyType(dict(case.output_artifacts)),
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
        row_union_settings=list(settings.row_unions) if settings.row_unions else None,
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


def _normalize_node_state_json(raw: object, *, field: str) -> str | None:
    """Canonicalize semantic node-state JSON while masking duration-only noise."""

    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AssertionError(f"DAG corpus {field} must be JSON text or null")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"DAG corpus {field} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"DAG corpus {field} must be a JSON object")
    if field == "context_after_json":
        if "wait_duration_ms" in value:
            value["wait_duration_ms"] = "$DURATION_MS"
        arrival_order = value.get("arrival_order")
        if isinstance(arrival_order, list):
            for entry in arrival_order:
                if isinstance(entry, dict) and "arrival_offset_ms" in entry:
                    entry["arrival_offset_ms"] = "$DURATION_MS"
    return _canonical_json(value)


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
    # Schema-growth stability: the settings material is a full model dump, so
    # a settings field added AFTER a pin was authored appears as its empty
    # default in every run and would rotate every pinned
    # semantic_settings_sha256 with no semantic change to the pipeline.
    # Fields listed here entered the schema after the v1 pins; drop them when
    # they hold their type's own empty default — non-empty declarations still
    # enter the hash. NOTE: each field's empty default has its own shape
    # (`[]` for a list-typed catalog like row_unions, `{}` for a Mapping-typed
    # catalog like llm_profiles, `None` for a scalar like default_llm_profile)
    # — comparing every entry against a single literal (e.g. `== []`) would
    # silently no-op for the others and let their pins rotate anyway.
    _post_pin_empty_defaults: dict[str, object] = {
        "row_unions": [],
        "llm_profiles": {},
        "default_llm_profile": None,
    }
    for post_pin_section, empty_default in _post_pin_empty_defaults.items():
        if settings.get(post_pin_section) == empty_default:
            settings.pop(post_pin_section, None)
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


def _ordered_parent_links(
    token_id: str,
    parent_links: list[tuple[int, str]],
    token_keys: Mapping[str, str],
) -> tuple[StableParentProjection, ...]:
    """Preserve the exact durable parent ordinals without reindexing them."""

    ordered_links = tuple(sorted(parent_links))
    ordinals = tuple(ordinal for ordinal, _parent_id in ordered_links)
    parent_ids = tuple(parent_id for _ordinal, parent_id in ordered_links)
    if len(ordinals) != len(set(ordinals)):
        raise AssertionError(f"DAG corpus token {token_id!r} has duplicate durable parent ordinals {ordinals!r}")
    if len(parent_ids) != len(set(parent_ids)):
        raise AssertionError(f"DAG corpus token {token_id!r} has duplicate durable parents {parent_ids!r}")
    return tuple(StableParentProjection(ordinal=ordinal, parent_key=token_keys[parent_id]) for ordinal, parent_id in ordered_links)


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
    parents_by_token: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "token_parent"):
        parents_by_token[str(record["token_id"])].append((int(record["ordinal"]), str(record["parent_token_id"])))
    token_keys: dict[str, str] = {}
    for row_id, token_records in token_records_by_row.items():
        base_signatures = [
            (
                record.get("branch_name"),
                record.get("step_in_pipeline"),
            )
            for record in token_records
        ]
        if len(base_signatures) == len(set(base_signatures)):
            token_records.sort(
                key=lambda record: (
                    str(record.get("branch_name") or ""),
                    int(record.get("step_in_pipeline") or 0),
                )
            )
        else:

            def expansion_aware_signature(
                record: Mapping[str, Any],
                row_key: str = rows_by_id[row_id],
            ) -> tuple[object, ...]:
                links = parents_by_token.get(str(record["token_id"]), ())
                if record.get("expand_group_id") is None:
                    expansion_ordinal = -1
                elif len(links) == 1:
                    expansion_ordinal = links[0][0]
                else:
                    raise AssertionError(f"DAG corpus expanded token lacks one stable parent ordinal for row {row_key!r}")
                return (
                    str(record.get("branch_name") or ""),
                    int(record.get("step_in_pipeline") or 0),
                    expansion_ordinal,
                )

            signatures = [expansion_aware_signature(record) for record in token_records]
            if len(signatures) != len(set(signatures)):
                raise AssertionError(f"DAG corpus tokens lack a stable ordering for row {rows_by_id[row_id]!r}")
            token_records.sort(key=expansion_aware_signature)
        for ordinal, record in enumerate(token_records):
            token_keys[str(record["token_id"])] = f"{rows_by_id[row_id]}#{ordinal}"

    ordered_parents_by_token = {
        token_id: _ordered_parent_links(token_id, parent_links, token_keys) for token_id, parent_links in parents_by_token.items()
    }
    tokens = tuple(
        StableTokenProjection(
            key=stable_key,
            row_key=rows_by_id[str(record["row_id"])],
            parents=ordered_parents_by_token.get(str(record["token_id"]), ()),
            branch_name=str(record["branch_name"]) if record.get("branch_name") is not None else None,
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
            context_after=(
                _normalize_node_state_json(record.get("context_after_json"), field="context_after_json")
                if node_keys[str(record["node_id"])].startswith(("coalesce:", "row_union:"))
                else None
            ),
            error=(
                _normalize_node_state_json(record.get("error_json"), field="error_json") if record.get("error_json") is not None else None
            ),
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
                error_hash=cast(str | None, outcome.get("error_hash")),
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
        ordered_events = _ordered_scheduler_events(unordered_events, work_key=token_key)
        scheduler_work.append(
            StableSchedulerWorkProjection(
                key=f"{token_key}|{node_key}|0",
                token_key=token_key,
                node_key=node_key,
                transitions=tuple(f"{event['event_type']}:{event['to_status']}" for event in ordered_events),
                final_status=str(ordered_events[-1]["to_status"]),
            )
        )

    batch_member_records: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in (record for record in records if record.get("record_type") == "batch_member"):
        batch_member_records[str(record["batch_id"])].append(record)
    batch_key_by_id = {
        str(record["batch_id"]): f"{node_keys[str(record['aggregation_node_id'])]}|{int(record['attempt'])}"
        for record in (record for record in records if record.get("record_type") == "batch")
    }
    batches = tuple(
        StableBatchProjection(
            key=batch_key_by_id[str(record["batch_id"])],
            aggregation_node_key=node_keys[str(record["aggregation_node_id"])],
            attempt=int(record["attempt"]),
            status=str(record["status"]),
            trigger_type=cast(str | None, record.get("trigger_type")),
            trigger_reason=cast(str | None, record.get("trigger_reason")),
            members=tuple(
                StableBatchMemberProjection(
                    ordinal=int(member["ordinal"]),
                    token_key=token_keys[str(member["token_id"])],
                )
                for member in sorted(batch_member_records[str(record["batch_id"])], key=lambda member: int(member["ordinal"]))
            ),
        )
        for record in (record for record in records if record.get("record_type") == "batch")
    )

    intermediate_counts: defaultdict[str, int] = defaultdict(int)
    intermediate_outcomes: list[StableIntermediateOutcomeProjection] = []
    for record in (record for record in records if record.get("record_type") == "token_outcome"):
        if bool(record["completed"]):
            continue
        token_key = token_keys[str(record["token_id"])]
        batch_id = record.get("batch_id")
        if (
            record.get("outcome") is not None
            or record.get("path") != "buffered"
            or record.get("sink_name") is not None
            or not isinstance(batch_id, str)
            or batch_id not in batch_key_by_id
            or record.get("expand_group_id") is not None
            or record.get("expected_branches_json") is not None
            or record.get("error_hash") is not None
        ):
            raise AssertionError("DAG corpus non-terminal outcome is not an exact batch BUFFERED record")
        ordinal = intermediate_counts[token_key]
        intermediate_counts[token_key] += 1
        intermediate_outcomes.append(
            StableIntermediateOutcomeProjection(
                key=f"{token_key}|buffered|{ordinal:08d}",
                token_key=token_key,
                ordinal=ordinal,
                path="buffered",
                batch_key=batch_key_by_id[batch_id],
            )
        )

    token_records_by_id = {str(record["token_id"]): record for record in records if record.get("record_type") == "token"}
    expansions: list[StableExpansionProjection] = []
    for outcome in (record for record in records if record.get("record_type") == "token_outcome"):
        if outcome.get("path") != "expand_parent" or not bool(outcome.get("completed")):
            continue
        parent_token_id = str(outcome["token_id"])
        expand_group_id = outcome.get("expand_group_id")
        expected_raw = outcome.get("expected_branches_json")
        if not isinstance(expand_group_id, str) or not expand_group_id or not isinstance(expected_raw, str):
            raise AssertionError("DAG corpus expand_parent outcome lacks durable group and expected count")
        try:
            expected = json.loads(expected_raw)
        except json.JSONDecodeError as exc:
            raise AssertionError("DAG corpus expand_parent expected count must be valid JSON") from exc
        if not isinstance(expected, dict) or isinstance(expected.get("count"), bool) or not isinstance(expected.get("count"), int):
            raise AssertionError("DAG corpus expand_parent expected count must be an integer object field")
        children: list[StableExpansionChildProjection] = []
        for child_id, child_record in token_records_by_id.items():
            if child_record.get("expand_group_id") != expand_group_id:
                continue
            links = sorted(parents_by_token[child_id])
            if len(links) != 1 or links[0][1] != parent_token_id:
                raise AssertionError("DAG corpus expanded child lacks exact durable parent linkage")
            children.append(StableExpansionChildProjection(ordinal=links[0][0], token_key=token_keys[child_id]))
        parent_key = token_keys[parent_token_id]
        expansions.append(
            StableExpansionProjection(
                key=f"expand|{parent_key}",
                parent_token_key=parent_key,
                expected_child_count=int(expected["count"]),
                children=tuple(sorted(children, key=lambda child: child.ordinal)),
            )
        )

    validation_error_groups: defaultdict[tuple[str, str], int] = defaultdict(int)
    validation_errors: list[StableValidationErrorProjection] = []
    for record in (record for record in records if record.get("record_type") == "validation_error"):
        raw_node_id = record.get("node_id")
        node_key = node_keys[str(raw_node_id)] if raw_node_id is not None else "source:unbound"
        raw_row_id = record.get("row_id")
        row_key = rows_by_id.get(str(raw_row_id)) if raw_row_id is not None else None
        group = (row_key or "unbound", node_key)
        attempt = validation_error_groups[group]
        validation_error_groups[group] += 1
        row_data = record.get("row_data_json")
        validation_errors.append(
            StableValidationErrorProjection(
                key=f"{group[0]}|{node_key}|{attempt}",
                node_key=node_key,
                row_key=row_key,
                row_hash=str(record["row_hash"]),
                row_data=_normalize_node_state_json(row_data, field="row_data") if row_data is not None else None,
                error=str(record["error"]),
                schema_mode=str(record["schema_mode"]),
                destination=str(record["destination"]),
                violation_type=cast(str | None, record.get("violation_type")),
                original_field_name=cast(str | None, record.get("original_field_name")),
                normalized_field_name=cast(str | None, record.get("normalized_field_name")),
                expected_type=cast(str | None, record.get("expected_type")),
                actual_type=cast(str | None, record.get("actual_type")),
            )
        )

    transform_errors: list[StableTransformErrorProjection] = []
    for record in (record for record in records if record.get("record_type") == "transform_error"):
        token_key = token_keys[str(record["token_id"])]
        transform_node_key = node_keys[str(record["transform_id"])]
        error_details = record.get("error_details_json")
        if not isinstance(error_details, str):
            raise AssertionError("DAG corpus transform error lacks canonical error details")
        row_data = record.get("row_data_json")
        transform_errors.append(
            StableTransformErrorProjection(
                key=f"{token_key}|{transform_node_key}",
                token_key=token_key,
                transform_node_key=transform_node_key,
                row_hash=str(record["row_hash"]),
                row_data=_normalize_node_state_json(row_data, field="row_data") if row_data is not None else None,
                error_details=_normalize_node_state_json(error_details, field="error_details"),
                destination=str(record["destination"]),
            )
        )

    return StableRunProjection(
        rows=tuple(sorted(rows, key=lambda row: row.key)),
        tokens=tuple(sorted(tokens, key=lambda token: token.key)),
        node_states=tuple(sorted(node_states, key=lambda state: state.key)),
        routes=tuple(sorted(routes, key=lambda route: route.key)),
        terminal_dispositions=tuple(sorted(terminal_dispositions, key=lambda disposition: disposition.key)),
        intermediate_outcomes=tuple(sorted(intermediate_outcomes, key=lambda outcome: outcome.key)),
        scheduler_work=tuple(sorted(scheduler_work, key=lambda work: work.key)),
        batches=tuple(sorted(batches, key=lambda batch: batch.key)),
        expansions=tuple(sorted(expansions, key=lambda expansion: expansion.key)),
        validation_errors=tuple(sorted(validation_errors, key=lambda error: error.key)),
        transform_errors=tuple(sorted(transform_errors, key=lambda error: error.key)),
        audit_records=_stable_audit_records(
            records,
            source=source,
            node_keys=node_keys,
            row_keys=rows_by_id,
            token_keys=token_keys,
            state_keys=state_keys,
        ),
    )


def semantic_runtime_projection(projection: StableRunProjection) -> SemanticRuntimeProjection:
    """Return order-insensitive runtime facts without weakening raw audit identity."""

    semantic_states: list[StableNodeStateProjection] = []
    for state in projection.node_states:
        context_after = state.context_after
        if context_after is not None:
            context = json.loads(context_after)
            if context.get("policy") == "require_all":
                branches_arrived = context.get("branches_arrived")
                if isinstance(branches_arrived, list):
                    context["branches_arrived"] = sorted(branches_arrived)
                arrival_order = context.get("arrival_order")
                if isinstance(arrival_order, list):
                    context["arrival_order"] = sorted(
                        arrival_order,
                        key=lambda entry: str(entry.get("branch")) if isinstance(entry, dict) else _canonical_json(entry),
                    )
                context_after = _canonical_json(context)
        semantic_states.append(state.model_copy(update={"context_after": context_after}))

    return SemanticRuntimeProjection(
        rows=projection.rows,
        tokens=tuple(
            SemanticTokenProjection(
                key=token.key,
                row_key=token.row_key,
                parent_set=tuple(sorted(parent.parent_key for parent in token.parents)),
                branch_name=token.branch_name,
            )
            for token in projection.tokens
        ),
        node_states=tuple(semantic_states),
        routes=projection.routes,
        terminal_dispositions=tuple(
            disposition.model_copy(update={"error_hash": None}) for disposition in projection.terminal_dispositions
        ),
        scheduler_work=projection.scheduler_work,
        intermediate_outcomes=projection.intermediate_outcomes,
        batches=projection.batches,
        expansions=projection.expansions,
        validation_errors=projection.validation_errors,
        transform_errors=projection.transform_errors,
    )


def semantic_runtime_projection_sha256(projection: SemanticRuntimeProjection) -> str:
    """Bind the complete runtime-only semantic projection."""

    return hashlib.sha256(contract_canonical_json(projection.model_dump(mode="json")).encode("utf-8")).hexdigest()


def stable_run_projection_sha256(
    projection: StableRunProjection,
    *,
    runtime_root: Path,
    settings: ElspethSettings,
) -> str:
    """Bind complete durable history after normalizing only the ephemeral runtime root."""

    root_text = str(runtime_root.resolve())
    runtime_token = "$DAG_CORPUS_RUNTIME_ROOT"

    def normalize_root(value: object) -> object:
        if isinstance(value, dict):
            return {str(key): normalize_root(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize_root(item) for item in value]
        if isinstance(value, str):
            return value.replace(root_text, runtime_token)
        return value

    node_key_replacements: dict[str, str] = {}
    for record in projection.audit_records:
        if record.record_type != "node" or not record.key.startswith("node|"):
            continue
        raw_node_key = record.key.removeprefix("node|")
        prefix, separator, observed_suffix = raw_node_key.rpartition("@")
        node_material = json.loads(record.material)
        node_config = node_material.get("config") if isinstance(node_material, dict) else None
        if not separator or not prefix or not isinstance(node_config, dict):
            raise AssertionError(f"full-history pin cannot validate observed node identity: {raw_node_key!r}")
        expected_suffix = hashlib.sha256(_canonical_json(node_config).encode()).hexdigest()[:12]
        if observed_suffix != expected_suffix:
            raise AssertionError(
                "observed node identity suffix differs from its pre-normalization semantic config identity: "
                f"node={raw_node_key!r}, expected={expected_suffix!r}, observed={observed_suffix!r}"
            )
        if root_text not in _canonical_json(node_config):
            continue
        normalized_config = normalize_root(node_config)
        normalized_suffix = hashlib.sha256(_canonical_json(normalized_config).encode()).hexdigest()[:12]
        node_key_replacements[raw_node_key] = f"{prefix}@{normalized_suffix}"

    persisted_settings_shape = json.loads(contract_canonical_json(settings.model_dump(mode="json")))
    semantic_settings = _semantic_run_settings(persisted_settings_shape)
    expected_observed_semantic_settings = hashlib.sha256(_canonical_json(semantic_settings).encode()).hexdigest()
    run_audit_records = tuple(record for record in projection.audit_records if record.record_type == "run")
    if len(run_audit_records) != 1:
        raise AssertionError(f"full-history pin requires exactly one run audit record: {len(run_audit_records)}")
    run_material = json.loads(run_audit_records[0].material)
    observed_semantic_settings = run_material.get("semantic_settings_sha256") if isinstance(run_material, dict) else None
    if observed_semantic_settings != expected_observed_semantic_settings:
        raise AssertionError(
            "observed semantic settings hash differs from the fresh settings supplied for full-history normalization: "
            f"expected={expected_observed_semantic_settings!r}, observed={observed_semantic_settings!r}"
        )

    normalized_semantic_settings = hashlib.sha256(_canonical_json(normalize_root(semantic_settings)).encode()).hexdigest()

    def normalize(value: object) -> object:
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, str):
            return value
        normalized = value.replace(root_text, runtime_token)
        for raw_node_key, normalized_node_key in sorted(node_key_replacements.items(), key=lambda item: len(item[0]), reverse=True):
            normalized = normalized.replace(raw_node_key, normalized_node_key)
        if normalized.startswith("{"):
            try:
                material = json.loads(normalized)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(material, dict) and "semantic_settings_sha256" in material:
                    material["semantic_settings_sha256"] = normalized_semantic_settings
                    normalized = contract_canonical_json(material)
        return normalized

    normalized_projection = normalize(projection.model_dump(mode="json"))
    return hashlib.sha256(contract_canonical_json(normalized_projection).encode("utf-8")).hexdigest()


def semantic_runtime_projection_counts(projection: SemanticRuntimeProjection) -> SemanticProjectionCounts:
    return SemanticProjectionCounts(
        rows=len(projection.rows),
        tokens=len(projection.tokens),
        parent_links=sum(len(token.parent_set) for token in projection.tokens),
        node_states=len(projection.node_states),
        routes=len(projection.routes),
        terminal_dispositions=len(projection.terminal_dispositions),
        scheduler_work=len(projection.scheduler_work),
        intermediate_outcomes=len(projection.intermediate_outcomes),
        batches=len(projection.batches),
        batch_members=sum(len(batch.members) for batch in projection.batches),
        expansions=len(projection.expansions),
        expansion_children=sum(len(expansion.children) for expansion in projection.expansions),
        validation_errors=len(projection.validation_errors),
        transform_errors=len(projection.transform_errors),
    )


def terminal_equivalence_projection(
    projection: StableRunProjection,
    *,
    sink_outputs: tuple[SinkOutputProjection, ...],
    rows_processed: int,
    rows_succeeded: int,
    rows_failed: int,
) -> TerminalEquivalenceProjection:
    """Project terminal meaning while retaining the full history separately."""

    semantic = semantic_runtime_projection(projection)
    terminal_states: dict[str, StableNodeStateProjection] = {}
    for state in semantic.node_states:
        if state.status != "completed":
            continue
        key = f"{state.token_key}|{state.node_key}|{state.step_index}"
        current = terminal_states.get(key)
        if current is None or state.attempt > current.attempt:
            terminal_states[key] = state

    terminal_work: dict[str, StableSchedulerWorkProjection] = {}
    for work in semantic.scheduler_work:
        if work.final_status != "terminal":
            raise AssertionError(f"terminal equivalence cannot hide non-terminal scheduler work: {work!r}")
        key = f"{work.token_key}|{work.node_key}"
        terminal_work[key] = work

    completed_batches = tuple(
        sorted(
            (
                TerminalBatchProjection(
                    key="|".join(
                        (
                            batch.aggregation_node_key,
                            str(batch.trigger_type),
                            str(batch.trigger_reason),
                            *tuple(member.token_key for member in batch.members),
                        )
                    ),
                    aggregation_node_key=batch.aggregation_node_key,
                    trigger_type=cast(Any, batch.trigger_type),
                    trigger_reason=batch.trigger_reason,
                    member_token_keys=tuple(member.token_key for member in batch.members),
                )
                for batch in semantic.batches
                if batch.status == "completed"
            ),
            key=lambda batch: batch.key,
        )
    )
    if not completed_batches and semantic.batches:
        raise AssertionError("terminal equivalence requires a completed batch whenever batch history exists")

    return TerminalEquivalenceProjection(
        rows=semantic.rows,
        tokens=semantic.tokens,
        terminal_node_states=tuple(
            TerminalNodeStateProjection(
                key=key,
                token_key=state.token_key,
                node_key=state.node_key,
                step_index=state.step_index,
                status="completed",
                context_after=state.context_after,
            )
            for key, state in sorted(terminal_states.items())
        ),
        routes=semantic.routes,
        terminal_dispositions=semantic.terminal_dispositions,
        terminal_scheduler_work=tuple(
            TerminalSchedulerWorkProjection(
                key=key,
                token_key=work.token_key,
                node_key=work.node_key,
                final_status="terminal",
            )
            for key, work in sorted(terminal_work.items())
        ),
        completed_batches=completed_batches,
        sink_outputs=sink_outputs,
        rows_processed=rows_processed,
        rows_succeeded=rows_succeeded,
        rows_failed=rows_failed,
        output_rows=sum(len(output.rows) for output in sink_outputs),
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(contract_canonical_json(value).encode("utf-8")).hexdigest()


def _artifact_byte_digests(rendered: RenderedScenario) -> tuple[ArtifactByteDigest, ...]:
    digests = tuple(
        ArtifactByteDigest(
            path=rendered.output_expectations[sink_name].filename,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for sink_name, path in sorted(rendered.output_paths.items())
    )
    paths = tuple(digest.path for digest in digests)
    if paths != tuple(sorted(set(paths))):
        raise AssertionError(f"DAG corpus output artifact paths must be unique and sorted: {paths!r}")
    return digests


def _output_tree_sha256(rendered: RenderedScenario) -> str:
    roots = {path.parent.resolve() for path in rendered.output_paths.values()}
    if len(roots) != 1:
        raise AssertionError(f"terminal-resume outputs must share one designated artifact root: {sorted(map(str, roots))!r}")
    root = next(iter(roots))
    if not root.is_dir():
        raise AssertionError(f"terminal-resume artifact root is missing: {root}")
    material: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise AssertionError(f"terminal-resume artifact tree forbids symlinks: {relative_path}")
        if path.is_dir():
            material.append({"kind": "directory", "path": relative_path})
        elif path.is_file():
            material.append(
                {
                    "kind": "file",
                    "path": relative_path,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        else:
            raise AssertionError(f"terminal-resume artifact tree contains an unsupported entry: {relative_path}")
    return _canonical_sha256(material)


def _ordered_scheduler_events(
    events: list[dict[str, Any]],
    *,
    work_key: str,
) -> tuple[dict[str, Any], ...]:
    """Recover exactly one complete scheduler transition chain.

    Recovery may legitimately re-enter a status (for example, a sink work item
    moves from ``leased`` to ``pending_sink`` and is later leased again).  A
    greedy status lookup cannot distinguish the two outgoing ``leased`` events.
    Search the small per-work-item event graph instead, accepting it only when
    exactly one ordering consumes every event.
    """

    if not events:
        raise AssertionError(f"DAG corpus scheduler events do not form exactly one complete transition chain for {work_key!r}")

    complete_chains: list[tuple[int, ...]] = []

    def search(
        current_status: object,
        current_attempt: object,
        current_lease_owner: object,
        remaining: tuple[int, ...],
        prefix: tuple[int, ...],
    ) -> None:
        if len(complete_chains) > 1:
            return
        if not remaining:
            complete_chains.append(prefix)
            return
        for index in remaining:
            event = events[index]
            if (
                event.get("from_status") != current_status
                or event.get("from_attempt") != current_attempt
                or event.get("from_lease_owner") != current_lease_owner
            ):
                continue
            search(
                event.get("to_status"),
                event.get("to_attempt"),
                event.get("to_lease_owner"),
                tuple(candidate for candidate in remaining if candidate != index),
                (*prefix, index),
            )

    search(None, None, None, tuple(range(len(events))), ())
    if len(complete_chains) != 1:
        raise AssertionError(f"DAG corpus scheduler events do not form exactly one complete transition chain for {work_key!r}")
    return tuple(events[index] for index in complete_chains[0])


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


def _validate_durable_sink_effect_attempt_call_material(
    *,
    effect_id: str,
    attempt: Mapping[str, object],
    call: Mapping[str, object],
) -> None:
    """Validate one attempt against its call and closed semantic evidence."""

    source = "durable"
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
    _require_material_equal(
        source=source,
        record_type="sink_effect_attempt",
        field="request_hash",
        actual=attempt.get("request_hash"),
        expected=call.get("request_hash"),
    )
    attempt_state = attempt.get("state")
    if attempt_state == "returned":
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="returned call.status",
            actual=call.get("status"),
            expected="success",
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="evidence_hash/call.response_hash",
            actual=attempt.get("evidence_hash"),
            expected=call.get("response_hash"),
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="returned call.error_json",
            actual=call.get("error_json"),
            expected=None,
        )
    elif attempt_state == "response_lost":
        expected_response_lost_evidence = contract_canonical_json({"classification": "response_lost"})
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="response-lost semantic evidence",
            actual=raw_evidence,
            expected=expected_response_lost_evidence,
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="response-lost call.status",
            actual=call.get("status"),
            expected="error",
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="response-lost call.response_hash",
            actual=call.get("response_hash"),
            expected=None,
        )
        _require_material_equal(
            source=source,
            record_type="sink_effect_attempt",
            field="response-lost call.error_json",
            actual=call.get("error_json"),
            expected=raw_evidence,
        )
    else:
        raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: unsupported terminal state {attempt_state!r}")


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
        operation = operation_by_effect.get(effect_id)
        if operation is None:
            raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: {effect_id} has no sink-effect operation")
        call = calls.get((operation.get("operation_id"), attempt_index_value))
        if call is None:
            raise AssertionError(f"DAG corpus durable sink_effect_attempt integrity: {effect_id} has no matching operation call")
        _validate_durable_sink_effect_attempt_call_material(
            effect_id=effect_id,
            attempt=attempt,
            call=call,
        )


_DURABLE_EXPORT_PARITY_SCHEMA: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "run",
        ("run_id",),
        ("status", "canonical_version", "config_hash", "settings", "reproducibility_grade"),
    ),
    (
        "node",
        ("node_id",),
        (
            "plugin_name",
            "node_type",
            "plugin_version",
            "source_file_hash",
            "determinism",
            "config_hash",
            "config",
            "schema_hash",
            "schema_mode",
            "schema_fields",
            "sequence_in_pipeline",
        ),
    ),
    ("edge", ("edge_id",), ("from_node_id", "to_node_id", "label", "default_mode")),
    (
        "operation",
        ("operation_id",),
        (
            "node_id",
            "operation_type",
            "sink_effect_id",
            "status",
            "error_message",
            "input_data_ref",
            "input_data_hash",
            "output_data_ref",
            "output_data_hash",
        ),
    ),
    (
        "call",
        ("call_id",),
        (
            "state_id",
            "operation_id",
            "call_index",
            "call_type",
            "status",
            "request_hash",
            "response_hash",
            "resolved_prompt_template_hash",
            "request_ref",
            "response_ref",
            "error_json",
        ),
    ),
    (
        "artifact",
        ("artifact_id",),
        (
            "sink_node_id",
            "producer_kind",
            "produced_by_state_id",
            "sink_effect_id",
            "artifact_type",
            "path_or_uri",
            "content_hash",
            "size_bytes",
            "idempotency_key",
            "publication_performed",
            "publication_evidence_kind",
        ),
    ),
    (
        "sink_effect_stream",
        ("stream_id",),
        (
            "sink_node_id",
            "role",
            "requested_target_hash",
            "next_sequence",
            "tail_effect_id",
            "head_effect_id",
            "head_descriptor_hash",
        ),
    ),
    (
        "sink_effect",
        ("effect_id",),
        (
            "sink_node_id",
            "role",
            "state",
            "protocol_version",
            "input_kind",
            "config_hash",
            "membership_or_manifest_hash",
            "group_payload_hash",
            "artifact_id",
            "artifact_idempotency_key",
            "inspection_mode",
            "inspection_attempt_id",
            "plan_hash",
            "descriptor_mode",
            "expected_descriptor_hash",
            "precondition_hash",
            "lease_owner",
            "generation",
            "reconcile_kind",
            "reconcile_evidence_hash",
            "result_descriptor_hash",
            "publication_performed",
            "publication_evidence_kind",
            "primary_effect_id",
            "stream_id",
            "stream_sequence",
            "predecessor_effect_id",
        ),
    ),
    (
        "sink_effect_member",
        ("effect_id", "ordinal"),
        (
            "sink_node_id",
            "role",
            "token_id",
            "row_id",
            "ingest_sequence",
            "lineage_hash",
            "payload_hash",
            "primary_effect_id",
            "prepared_disposition",
            "reason_hash",
            "member_effect_id",
            "member_state",
            "descriptor_hash",
            "evidence_hash",
        ),
    ),
    (
        "sink_effect_attempt",
        ("effect_id", "attempt_index"),
        (
            "attempt_id",
            "member_ordinal",
            "generation",
            "action",
            "call_kind",
            "request_hash",
            "state",
            "evidence_hash",
        ),
    ),
    (
        "row",
        ("row_id",),
        ("source_node_id", "source_row_index", "ingest_sequence", "source_data_hash"),
    ),
    (
        "token",
        ("token_id",),
        ("row_id", "step_in_pipeline", "branch_name", "fork_group_id", "join_group_id", "expand_group_id"),
    ),
    ("token_parent", ("token_id", "parent_token_id"), ("ordinal",)),
    (
        "node_state",
        ("state_id",),
        ("token_id", "node_id", "step_index", "attempt", "status", "context_after_json", "error_json"),
    ),
    ("routing_event", ("event_id",), ("state_id", "edge_id", "ordinal", "mode")),
    (
        "token_outcome",
        ("outcome_id",),
        (
            "token_id",
            "outcome",
            "path",
            "completed",
            "sink_name",
            "batch_id",
            "expand_group_id",
            "expected_branches_json",
            "error_hash",
        ),
    ),
    (
        "scheduler_event",
        ("event_id",),
        (
            "work_item_id",
            "token_id",
            "node_id",
            "event_type",
            "from_status",
            "to_status",
            "from_attempt",
            "to_attempt",
            "from_lease_owner",
            "to_lease_owner",
        ),
    ),
    (
        "batch",
        ("batch_id",),
        ("aggregation_node_id", "attempt", "status", "trigger_type", "trigger_reason"),
    ),
    ("batch_member", ("batch_id", "token_id"), ("ordinal",)),
    (
        "validation_error",
        ("error_id",),
        (
            "node_id",
            "row_id",
            "row_hash",
            "row_data_json",
            "error",
            "schema_mode",
            "destination",
            "violation_type",
            "original_field_name",
            "normalized_field_name",
            "expected_type",
            "actual_type",
        ),
    ),
    (
        "transform_error",
        ("error_id",),
        ("token_id", "transform_id", "row_hash", "row_data_json", "error_details_json", "destination"),
    ),
)


def _validate_portable_material_matches_durable(durable_records: list[dict[str, Any]], portable_records: list[dict[str, Any]]) -> None:
    for record_type, key_fields, fields in _DURABLE_EXPORT_PARITY_SCHEMA:
        durable = _record_index(durable_records, record_type=record_type, key_fields=key_fields, source="durable")
        portable = _record_index(portable_records, record_type=record_type, key_fields=key_fields, source="portable")
        if durable.keys() != portable.keys():
            raise AssertionError(f"DAG corpus portable {record_type} integrity: record identities differ from durable data")
        for key, durable_record in durable.items():
            portable_record = portable[key]
            for field in fields:
                if field not in durable_record:
                    raise AssertionError(f"DAG corpus durable {record_type} integrity: selected field {field!r} is missing")
                if field not in portable_record:
                    raise AssertionError(f"DAG corpus portable {record_type} integrity: selected field {field!r} is missing")
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
    """Project claimed export material directly from persisted table rows.

    This is deliberately independent of ``LandscapeExporter`` and its
    ``_iter_records`` serializer.  The field lists below are the maintained
    durable/export parity contract; raw error rows and volatile timestamps are
    intentionally normalized out before comparison.
    """

    _ = payload_store

    def decode_json(raw: object, *, label: str) -> object:
        if not isinstance(raw, str):
            raise AssertionError(f"DAG corpus durable {label} must be JSON text")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"DAG corpus durable {label} must be valid JSON") from exc

    def fetch(
        table: Any,
        fields: tuple[str, ...],
        *order_by: Any,
    ) -> list[Mapping[str, Any]]:
        query = select(*(table.c[field] for field in fields)).where(table.c.run_id == run_id)
        if order_by:
            query = query.order_by(*order_by)
        return [cast(Mapping[str, Any], row) for row in connection.execute(query).mappings()]

    def project(record_type: str, row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        return {"record_type": record_type, **{field: row[field] for field in fields}}

    records: list[dict[str, Any]] = []
    with db.connection() as connection:
        run_fields = ("run_id", "status", "canonical_version", "config_hash", "settings_json", "reproducibility_grade")
        run_rows = fetch(runs_table, run_fields)
        if len(run_rows) != 1:
            raise AssertionError(f"DAG corpus durable run integrity: expected one run, got {len(run_rows)}")
        run_row = run_rows[0]
        records.append(
            {
                "record_type": "run",
                "run_id": run_row["run_id"],
                "status": run_row["status"],
                "canonical_version": run_row["canonical_version"],
                "config_hash": run_row["config_hash"],
                "settings": decode_json(run_row["settings_json"], label="run.settings_json"),
                "reproducibility_grade": run_row["reproducibility_grade"],
            }
        )

        node_fields = (
            "run_id",
            "node_id",
            "plugin_name",
            "node_type",
            "plugin_version",
            "source_file_hash",
            "determinism",
            "config_hash",
            "config_json",
            "schema_hash",
            "schema_mode",
            "schema_fields_json",
            "sequence_in_pipeline",
        )
        for row in fetch(
            nodes_table,
            node_fields,
            nodes_table.c.sequence_in_pipeline.nullslast(),
            nodes_table.c.registered_at,
            nodes_table.c.node_id,
        ):
            records.append(
                {
                    "record_type": "node",
                    "run_id": row["run_id"],
                    "node_id": row["node_id"],
                    "plugin_name": row["plugin_name"],
                    "node_type": row["node_type"],
                    "plugin_version": row["plugin_version"],
                    "source_file_hash": row["source_file_hash"],
                    "determinism": row["determinism"],
                    "config_hash": row["config_hash"],
                    "config": decode_json(row["config_json"], label=f"node {row['node_id']}.config_json"),
                    "schema_hash": row["schema_hash"],
                    "schema_mode": row["schema_mode"],
                    "schema_fields": (
                        None
                        if row["schema_fields_json"] is None
                        else decode_json(row["schema_fields_json"], label=f"node {row['node_id']}.schema_fields_json")
                    ),
                    "sequence_in_pipeline": row["sequence_in_pipeline"],
                }
            )

        edge_fields = ("run_id", "edge_id", "from_node_id", "to_node_id", "label", "default_mode")
        records.extend(
            project("edge", row, edge_fields) for row in fetch(edges_table, edge_fields, edges_table.c.created_at, edges_table.c.edge_id)
        )

        operation_fields = (
            "run_id",
            "operation_id",
            "node_id",
            "operation_type",
            "sink_effect_id",
            "status",
            "error_message",
            "input_data_ref",
            "input_data_hash",
            "output_data_ref",
            "output_data_hash",
        )
        records.extend(
            project("operation", row, operation_fields)
            for row in fetch(
                operations_table,
                operation_fields,
                operations_table.c.started_at,
                operations_table.c.operation_id,
            )
        )

        call_fields = (
            "call_id",
            "state_id",
            "operation_id",
            "call_index",
            "call_type",
            "status",
            "request_hash",
            "response_hash",
            "resolved_prompt_template_hash",
            "request_ref",
            "response_ref",
            "error_json",
        )
        call_scope = calls_table.c.operation_id.in_(
            select(operations_table.c.operation_id).where(operations_table.c.run_id == run_id)
        ) | calls_table.c.state_id.in_(select(node_states_table.c.state_id).where(node_states_table.c.run_id == run_id))
        call_rows = connection.execute(
            select(*(calls_table.c[field] for field in call_fields))
            .where(call_scope)
            .order_by(calls_table.c.operation_id, calls_table.c.state_id, calls_table.c.call_index)
        ).mappings()
        records.extend({"record_type": "call", "run_id": run_id, **dict(row)} for row in call_rows)

        stream_fields = (
            "run_id",
            "stream_id",
            "sink_node_id",
            "role",
            "requested_target_hash",
            "next_sequence",
            "tail_effect_id",
            "head_effect_id",
            "head_descriptor_hash",
        )
        records.extend(
            project("sink_effect_stream", row, stream_fields)
            for row in fetch(sink_effect_streams_table, stream_fields, sink_effect_streams_table.c.stream_id)
        )

        effect_fields = (
            "run_id",
            "effect_id",
            "sink_node_id",
            "role",
            "state",
            "protocol_version",
            "input_kind",
            "config_hash",
            "membership_or_manifest_hash",
            "group_payload_hash",
            "artifact_id",
            "artifact_idempotency_key",
            "inspection_mode",
            "inspection_attempt_id",
            "plan_hash",
            "descriptor_mode",
            "expected_descriptor_hash",
            "precondition_hash",
            "lease_owner",
            "generation",
            "reconcile_kind",
            "reconcile_evidence_hash",
            "result_descriptor_hash",
            "publication_performed",
            "publication_evidence_kind",
            "primary_effect_id",
            "stream_id",
            "stream_sequence",
            "predecessor_effect_id",
            "plan_json",
        )
        for row in fetch(
            sink_effects_table,
            effect_fields,
            sink_effects_table.c.stream_id,
            sink_effects_table.c.stream_sequence,
            sink_effects_table.c.effect_id,
        ):
            record = project("sink_effect", row, effect_fields[:-1])
            record["_plan_json"] = row["plan_json"]
            records.append(record)

        member_fields = (
            "run_id",
            "effect_id",
            "ordinal",
            "sink_node_id",
            "role",
            "token_id",
            "row_id",
            "ingest_sequence",
            "lineage_hash",
            "payload_hash",
            "primary_effect_id",
            "prepared_disposition",
            "reason_hash",
            "member_effect_id",
            "member_state",
            "descriptor_hash",
            "evidence_hash",
        )
        records.extend(
            project("sink_effect_member", row, member_fields)
            for row in fetch(
                sink_effect_members_table,
                member_fields,
                sink_effect_members_table.c.effect_id,
                sink_effect_members_table.c.ordinal,
            )
        )

        attempt_fields = (
            "attempt_id",
            "effect_id",
            "member_ordinal",
            "generation",
            "action",
            "call_kind",
            "request_hash",
            "state",
            "evidence_hash",
            "evidence_json",
        )
        attempt_rows = connection.execute(
            select(*(sink_effect_attempts_table.c[field] for field in attempt_fields))
            .where(
                sink_effect_attempts_table.c.effect_id.in_(
                    select(sink_effects_table.c.effect_id).where(sink_effects_table.c.run_id == run_id)
                )
            )
            .order_by(
                sink_effect_attempts_table.c.effect_id,
                sink_effect_attempts_table.c.started_at,
                sink_effect_attempts_table.c.attempt_id,
            )
        ).mappings()
        attempt_index_by_effect: defaultdict[str, int] = defaultdict(int)
        for attempt_row in attempt_rows:
            effect_id = str(attempt_row["effect_id"])
            attempt_index = attempt_index_by_effect[effect_id]
            attempt_index_by_effect[effect_id] += 1
            record = {
                "record_type": "sink_effect_attempt",
                "run_id": run_id,
                **{field: attempt_row[field] for field in attempt_fields[:-1]},
                "attempt_index": attempt_index,
                "_evidence_json": attempt_row["evidence_json"],
            }
            records.append(record)

        artifact_fields = (
            "run_id",
            "artifact_id",
            "sink_node_id",
            "produced_by_state_id",
            "sink_effect_id",
            "artifact_type",
            "path_or_uri",
            "content_hash",
            "size_bytes",
            "idempotency_key",
            "publication_performed",
            "publication_evidence_kind",
        )
        for row in fetch(
            artifacts_table,
            artifact_fields,
            artifacts_table.c.created_at,
            artifacts_table.c.artifact_id,
        ):
            producer_kind = "node_state" if row["produced_by_state_id"] is not None else "sink_effect"
            records.append({"record_type": "artifact", **dict(row), "producer_kind": producer_kind})

        row_fields = ("run_id", "row_id", "source_node_id", "source_row_index", "ingest_sequence", "source_data_hash")
        records.extend(
            project("row", row, row_fields) for row in fetch(rows_table, row_fields, rows_table.c.ingest_sequence, rows_table.c.row_id)
        )

        token_fields = (
            "run_id",
            "token_id",
            "row_id",
            "step_in_pipeline",
            "branch_name",
            "fork_group_id",
            "join_group_id",
            "expand_group_id",
        )
        records.extend(
            project("token", row, token_fields)
            for row in fetch(tokens_table, token_fields, tokens_table.c.row_id, tokens_table.c.created_at, tokens_table.c.token_id)
        )

        parent_fields = ("run_id", "token_id", "parent_token_id", "ordinal")
        records.extend(
            project("token_parent", row, parent_fields)
            for row in fetch(
                token_parents_table,
                parent_fields,
                token_parents_table.c.token_id,
                token_parents_table.c.ordinal,
                token_parents_table.c.parent_token_id,
            )
        )

        state_fields = (
            "run_id",
            "state_id",
            "token_id",
            "node_id",
            "step_index",
            "attempt",
            "status",
            "context_after_json",
            "error_json",
        )
        records.extend(
            project("node_state", row, state_fields)
            for row in fetch(
                node_states_table,
                state_fields,
                node_states_table.c.token_id,
                node_states_table.c.step_index,
                node_states_table.c.attempt,
                node_states_table.c.state_id,
            )
        )

        route_fields = ("run_id", "event_id", "state_id", "edge_id", "ordinal", "mode")
        records.extend(
            project("routing_event", row, route_fields)
            for row in fetch(
                routing_events_table,
                route_fields,
                routing_events_table.c.state_id,
                routing_events_table.c.ordinal,
                routing_events_table.c.event_id,
            )
        )

        outcome_fields = (
            "run_id",
            "outcome_id",
            "token_id",
            "outcome",
            "path",
            "completed",
            "sink_name",
            "batch_id",
            "expand_group_id",
            "expected_branches_json",
            "error_hash",
        )
        for row in fetch(
            token_outcomes_table,
            outcome_fields,
            token_outcomes_table.c.token_id,
            token_outcomes_table.c.recorded_at,
            token_outcomes_table.c.outcome_id,
        ):
            record = project("token_outcome", row, outcome_fields)
            record["completed"] = bool(record["completed"])
            records.append(record)

        scheduler_fields = (
            "run_id",
            "event_id",
            "work_item_id",
            "token_id",
            "node_id",
            "event_type",
            "from_status",
            "to_status",
            "from_attempt",
            "to_attempt",
            "from_lease_owner",
            "to_lease_owner",
        )
        records.extend(
            project("scheduler_event", row, scheduler_fields)
            for row in fetch(
                scheduler_events_table,
                scheduler_fields,
                scheduler_events_table.c.token_id,
                scheduler_events_table.c.recorded_at,
                scheduler_events_table.c.event_id,
            )
        )

        batch_fields = (
            "run_id",
            "batch_id",
            "aggregation_node_id",
            "attempt",
            "status",
            "trigger_type",
            "trigger_reason",
        )
        records.extend(
            project("batch", row, batch_fields)
            for row in fetch(batches_table, batch_fields, batches_table.c.created_at, batches_table.c.batch_id)
        )

        batch_member_fields = ("run_id", "batch_id", "token_id", "ordinal")
        records.extend(
            project("batch_member", row, batch_member_fields)
            for row in fetch(
                batch_members_table,
                batch_member_fields,
                batch_members_table.c.batch_id,
                batch_members_table.c.ordinal,
                batch_members_table.c.token_id,
            )
        )

        validation_fields = (
            "run_id",
            "error_id",
            "node_id",
            "row_id",
            "row_hash",
            "error",
            "schema_mode",
            "destination",
            "violation_type",
            "original_field_name",
            "normalized_field_name",
            "expected_type",
            "actual_type",
        )
        records.extend(
            {
                **project("validation_error", row, validation_fields),
                "row_data_json": None,
            }
            for row in fetch(
                validation_errors_table,
                validation_fields,
                validation_errors_table.c.created_at,
                validation_errors_table.c.error_id,
            )
        )

        transform_fields = (
            "run_id",
            "error_id",
            "token_id",
            "transform_id",
            "row_hash",
            "error_details_json",
            "destination",
        )
        records.extend(
            {
                **project("transform_error", row, transform_fields),
                "row_data_json": None,
            }
            for row in fetch(
                transform_errors_table,
                transform_fields,
                transform_errors_table.c.created_at,
                transform_errors_table.c.error_id,
            )
        )

    records.append(
        {
            "record_type": "manifest",
            "chunk_count": 1,
            "derivation_version": "audit-export-derivation-v1",
            "export_format": "json",
            "hash_algorithm": "sha256",
            "record_chain_algorithm": "sha256_concat_record_sha256_v1",
            "record_count": len(records),
            "schema": "elspeth.audit-export-manifest.v2",
            "signature_algorithm": "unsigned",
            "signature_key_id": "UNSIGNED",
            "source_status": run_row["status"],
        }
    )
    return records


def _sink_outputs(rendered: RenderedScenario) -> tuple[SinkOutputProjection, ...]:
    outputs: list[SinkOutputProjection] = []
    for sink_name, output_path in rendered.output_paths.items():
        expectation = rendered.output_expectations[sink_name]
        if not output_path.is_file():
            if expectation.presence == "required":
                raise AssertionError(f"DAG corpus sink {sink_name!r} did not produce {output_path.name!r}")
            continue
        if expectation.presence == "absent":
            raise AssertionError(f"DAG corpus sink {sink_name!r} produced intentionally absent artifact {output_path.name!r}")
        rows = tuple(
            json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
            for line in output_path.read_text(encoding="utf-8").splitlines()
        )
        outputs.append(SinkOutputProjection(sink_name=sink_name, rows=rows))
    return tuple(outputs)


def _audit_evidence(
    records: list[dict[str, Any]],
    *,
    portable_projection: StableRunProjection | None = None,
    portable_export_unavailable: PortableExportUnavailableByPolicy | None = None,
) -> AuditEvidence:
    counts = Counter(str(record["record_type"]) for record in records)
    return AuditEvidence(
        attempted=True,
        total_records=len(records),
        record_counts=tuple(AuditRecordCount(record_type=record_type, count=count) for record_type, count in sorted(counts.items())),
        source_operation_count=sum(
            1 for record in records if record.get("record_type") == "operation" and record.get("operation_type") == "source_load"
        ),
        portable_projection=portable_projection,
        portable_export_unavailable=portable_export_unavailable,
    )


def _run_expected_error_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    expected = case.expected
    if not isinstance(expected, RunExpectation) or expected.expected_error is None:
        raise AssertionError("expected-error runner requires an exact run expectation with expected_error")
    expected_type = EXPECTED_RUN_ERROR_TYPES[expected.expected_error.exception_type]
    rendered = render_settings(case, tmp_path)
    built = build_scenario(rendered)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        try:
            Orchestrator(db).run(
                built.config,
                graph=built.graph,
                settings=built.rendered.settings,
                payload_store=payload_store,
                openrouter_catalog_sha256=catalog_sha256,
                openrouter_catalog_source=catalog_source,
            )
        except expected_type as exc:
            if type(exc) is not expected_type:
                raise AssertionError(f"DAG corpus expected exact {expected_type.__name__}, got subclass {type(exc).__name__}") from exc
        else:
            raise AssertionError(f"DAG corpus expected exact {expected_type.__name__}, but production run returned")

        sink_outputs = _sink_outputs(rendered)
        repositories = RecorderFactory.read_only(db, payload_store=payload_store)
        runs = repositories.run_lifecycle.list_runs()
        if len(runs) != 1:
            raise AssertionError(f"DAG expected-error corpus expected exactly one persisted run, got {len(runs)}")
        failed_run = runs[0]
        if failed_run.status is not RunStatus.FAILED:
            raise AssertionError(f"DAG expected-error corpus expected failed run, got {failed_run.status.value!r}")

        counter_factory = RecorderFactory(db, payload_store=payload_store)
        _derived_status, counters = derive_terminal_status_from_audit(counter_factory, failed_run.run_id)
        durable_records = _public_durable_records(db, run_id=failed_run.run_id, payload_store=payload_store)
        _validate_durable_sink_effect_material(durable_records)
        durable_projection = _stable_projection(durable_records, source="durable")

        export_reason = "Audit export requires an immutable export-terminal run"
        try:
            list(LandscapeExporter(db).export_run(failed_run.run_id))
        except ValueError as export_exc:
            if type(export_exc) is not ValueError or str(export_exc) != export_reason:
                raise
        else:
            raise AssertionError("DAG expected-error corpus failed run unexpectedly allowed portable export")
        audit = _audit_evidence(
            durable_records,
            portable_export_unavailable=PortableExportUnavailableByPolicy(
                run_status="failed",
                exception_type="ValueError",
                reason=export_reason,
            ),
        )
        return ScenarioRunEvidence(
            schema_version=2,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=rendered.settings_sha256),
            graph=built.graph_evidence,
            runtime=RuntimeEvidence(
                attempted=True,
                run_id=failed_run.run_id,
                status=failed_run.status.value,
                rows_processed=counters.rows_processed,
                rows_succeeded=counters.rows_succeeded,
                rows_failed=counters.rows_failed,
                output_rows=sum(len(output.rows) for output in sink_outputs),
                sink_outputs=sink_outputs,
                durable_projection=durable_projection,
                observed_error=expected.expected_error,
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


def _run_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    if isinstance(case.expected, RunExpectation) and case.expected.expected_error is not None:
        return _run_expected_error_case(scenario, case, tmp_path)
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
            schema_version=2,
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
        schema_version=2,
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


def _assert_expected_terminal_run_status(
    *,
    actual_status: RunStatus | None,
    expected_status: RunStatus,
) -> None:
    """Require the exact terminal status declared by a generic recovery case."""

    if expected_status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY}:
        raise AssertionError(f"DAG recovery corpus expected a terminal run status, got {expected_status!r}")
    if actual_status is not expected_status:
        raise AssertionError(f"DAG recovery corpus expected terminal run status {expected_status.value!r}, but persisted {actual_status!r}")


def _assert_all_tokens_and_work_terminal(
    db: LandscapeDB,
    *,
    run_id: str,
    payload_store: FilesystemPayloadStore,
    expected_run_status: RunStatus = RunStatus.COMPLETED,
) -> None:
    repositories = RecorderFactory.read_only(db, payload_store=payload_store)
    run = repositories.run_lifecycle.get_run(run_id)
    _assert_expected_terminal_run_status(
        actual_status=None if run is None else run.status,
        expected_status=expected_run_status,
    )
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
        work_statuses = tuple(
            conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id)).scalars()
        )
    if not work_statuses or set(work_statuses) != {"terminal"}:
        raise AssertionError(f"DAG recovery corpus left non-terminal scheduler work: {work_statuses!r}")


def _exact_recovery_views(
    db: LandscapeDB,
    *,
    run_id: str,
    payload_store: FilesystemPayloadStore,
) -> tuple[StableRunProjection, AuditEvidence]:
    durable_records = _public_durable_records(db, run_id=run_id, payload_store=payload_store)
    _validate_durable_sink_effect_material(durable_records)
    durable_projection = _stable_projection(durable_records, source="durable recovery")
    portable_records = list(LandscapeExporter(db).export_run(run_id))
    _validate_portable_material_matches_durable(durable_records, portable_records)
    _validate_portable_manifest(portable_records)
    portable_projection = _stable_projection(portable_records, source="portable recovery")
    if durable_projection != portable_projection:
        raise AssertionError("DAG recovery corpus public durable query and portable export projections differ")
    return durable_projection, _audit_evidence(portable_records, portable_projection=portable_projection)


def _aggregation_identity_snapshot(
    db: LandscapeDB,
    *,
    run_id: str,
) -> tuple[tuple[tuple[str, int, str, tuple[str, ...]], ...], int, int]:
    with db.connection() as conn:
        batch_rows = tuple(
            conn.execute(
                select(batches_table.c.batch_id, batches_table.c.attempt, batches_table.c.status)
                .where(batches_table.c.run_id == run_id)
                .order_by(batches_table.c.attempt)
            ).mappings()
        )
        batches = tuple(
            (
                str(row["batch_id"]),
                int(row["attempt"]),
                str(row["status"]),
                tuple(
                    str(value)
                    for value in conn.execute(
                        select(batch_members_table.c.token_id)
                        .where(batch_members_table.c.run_id == run_id)
                        .where(batch_members_table.c.batch_id == row["batch_id"])
                        .order_by(batch_members_table.c.ordinal)
                    ).scalars()
                ),
            )
            for row in batch_rows
        )
        token_count = len(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).all())
        effect_count = len(conn.execute(select(sink_effects_table.c.effect_id).where(sink_effects_table.c.run_id == run_id)).all())
    return batches, token_count, effect_count


def _expansion_identity_snapshot(
    db: LandscapeDB,
    *,
    run_id: str,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, str, str], ...],
    int,
    int,
]:
    with db.connection() as conn:
        parent_rows = tuple(
            conn.execute(
                select(token_outcomes_table.c.token_id, token_outcomes_table.c.expand_group_id)
                .join(tokens_table, tokens_table.c.token_id == token_outcomes_table.c.token_id)
                .join(rows_table, rows_table.c.row_id == tokens_table.c.row_id)
                .where(token_outcomes_table.c.run_id == run_id)
                .where(token_outcomes_table.c.completed == 1)
                .where(token_outcomes_table.c.path == "expand_parent")
                .order_by(rows_table.c.ingest_sequence)
            ).mappings()
        )
        parent_ids = tuple(str(row["token_id"]) for row in parent_rows)
        group_ids = tuple(str(row["expand_group_id"]) for row in parent_rows)
        child_ids: list[str] = []
        for parent_id, group_id in zip(parent_ids, group_ids, strict=True):
            child_ids.extend(
                str(value)
                for value in conn.execute(
                    select(token_parents_table.c.token_id)
                    .join(tokens_table, tokens_table.c.token_id == token_parents_table.c.token_id)
                    .where(token_parents_table.c.run_id == run_id)
                    .where(token_parents_table.c.parent_token_id == parent_id)
                    .where(tokens_table.c.expand_group_id == group_id)
                    .order_by(token_parents_table.c.ordinal)
                ).scalars()
            )
        work_rows = tuple(
            conn.execute(
                select(
                    token_work_items_table.c.work_item_id,
                    token_work_items_table.c.token_id,
                    token_work_items_table.c.status,
                )
                .where(token_work_items_table.c.run_id == run_id)
                .order_by(token_work_items_table.c.work_item_id)
            ).mappings()
        )
        effect_count = len(conn.execute(select(sink_effects_table.c.effect_id).where(sink_effects_table.c.run_id == run_id)).all())
        artifact_count = len(conn.execute(select(artifacts_table.c.artifact_id).where(artifacts_table.c.run_id == run_id)).all())
    return (
        parent_ids,
        tuple(child_ids),
        group_ids,
        tuple((str(row["work_item_id"]), str(row["token_id"]), str(row["status"])) for row in work_rows),
        effect_count,
        artifact_count,
    )


def _partition_expansion_work(
    work_items: tuple[tuple[str, str, str], ...],
    *,
    parent_token_ids: tuple[str, ...],
    child_token_ids: tuple[str, ...],
    parent_status: str,
    child_status: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bind every expansion token to exactly one work identity and status."""

    parent_set = set(parent_token_ids)
    child_set = set(child_token_ids)
    work_ids = tuple(work_id for work_id, _token_id, _status in work_items)
    work_token_ids = tuple(token_id for _work_id, token_id, _status in work_items)
    if (
        len(parent_set) != len(parent_token_ids)
        or len(child_set) != len(child_token_ids)
        or parent_set & child_set
        or len(work_ids) != len(set(work_ids))
        or len(work_token_ids) != len(set(work_token_ids))
        or set(work_token_ids) != parent_set | child_set
    ):
        raise AssertionError("expansion recovery corpus lacks an exact parent/child scheduler status partition")

    by_token = {token_id: (work_id, status) for work_id, token_id, status in work_items}
    if any(by_token[token_id][1] != parent_status for token_id in parent_token_ids) or any(
        by_token[token_id][1] != child_status for token_id in child_token_ids
    ):
        raise AssertionError("expansion recovery corpus lacks an exact parent/child scheduler status partition")
    return (
        tuple(by_token[token_id][0] for token_id in parent_token_ids),
        tuple(by_token[token_id][0] for token_id in child_token_ids),
    )


def _eof_aggregation_recovery_case(scenario: ScenarioSpec, case: HarnessCaseSpec, tmp_path: Path) -> ScenarioRunEvidence:
    prove_terminal_idempotence = case.recovery_kind == "terminal_resume_idempotence"
    control_terminal_projection: TerminalEquivalenceProjection | None = None
    if prove_terminal_idempotence:
        control_root = tmp_path / "control"
        control_root.mkdir()
        control_rendered = render_settings(case, tmp_path)
        control_rendered.fault_marker.parent.mkdir(parents=True, exist_ok=True)
        control_rendered.fault_marker.touch(exist_ok=False)
        control_built = build_scenario(control_rendered)
        control_store = FilesystemPayloadStore(control_root / "payloads")
        control_db = LandscapeDB(f"sqlite:///{control_root / 'audit.db'}")
        control_checkpoint_manager = CheckpointManager(control_db)
        control_checkpoint_config = RuntimeCheckpointConfig.from_settings(control_rendered.settings.checkpoint)
        try:
            control_catalog_sha256, control_catalog_source = read_openrouter_catalog_snapshot_id()
            control_result = Orchestrator(
                control_db,
                checkpoint_manager=control_checkpoint_manager,
                checkpoint_config=control_checkpoint_config,
            ).run(
                control_built.config,
                graph=control_built.graph,
                settings=control_rendered.settings,
                payload_store=control_store,
                openrouter_catalog_sha256=control_catalog_sha256,
                openrouter_catalog_source=control_catalog_source,
            )
            control_projection, control_audit = _exact_recovery_views(
                control_db,
                run_id=control_result.run_id,
                payload_store=control_store,
            )
            if control_audit.source_operation_count != 1:
                raise AssertionError(f"terminal-equivalence control must load its source once: {control_audit.source_operation_count}")
            control_result_data = control_result.to_dict()
            control_terminal_projection = terminal_equivalence_projection(
                control_projection,
                sink_outputs=_sink_outputs(control_rendered),
                rows_processed=control_result_data["rows_processed"],
                rows_succeeded=control_result_data["rows_succeeded"],
                rows_failed=control_result_data["rows_failed"],
            )
        finally:
            control_db.close()
        for output_path in control_rendered.output_paths.values():
            output_path.unlink()
        control_rendered.fault_marker.unlink()

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
        batches_before, token_count_before, effect_count_before = _aggregation_identity_snapshot(
            initial_db,
            run_id=run_id,
        )
        if len(batches_before) != 1:
            raise AssertionError(f"EOF aggregation recovery requires one failed pre-resume batch, got {batches_before!r}")
        original_batch_id_before, batch_attempt_before, batch_status_before, member_token_ids_before = batches_before[0]
        if (
            batch_attempt_before,
            batch_status_before,
            len(member_token_ids_before),
            token_count_before,
            effect_count_before,
        ) != (0, "failed", 3, 3, 0):
            raise AssertionError(
                "EOF aggregation recovery requires one failed attempt with three immutable members, no result token, "
                f"and no sink effect before resume: batch={batches_before!r}, tokens={token_count_before}, effects={effect_count_before}"
            )
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
        fresh_checkpoint_config = RuntimeCheckpointConfig.from_settings(fresh_rendered.settings.checkpoint)
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
            checkpoint_config=fresh_checkpoint_config,
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

        sink_outputs = _sink_outputs(fresh_rendered)
        expected_sink_outputs = (SinkOutputProjection(sink_name="output", rows=('{"count":3,"value":60}',)),)
        if sink_outputs != expected_sink_outputs:
            raise AssertionError(f"EOF aggregation recovery emitted unexpected canonical sink output: {sink_outputs!r}")
        durable_projection, audit = _exact_recovery_views(
            reopened_db,
            run_id=run_id,
            payload_store=reopened_store,
        )
        if audit.source_operation_count != 1:
            raise AssertionError(f"DAG recovery corpus replayed its source: source_load count={audit.source_operation_count}")
        batches_after, token_count_after, effect_count_after = _aggregation_identity_snapshot(
            reopened_db,
            run_id=run_id,
        )
        if (token_count_after, effect_count_after) != (4, 1):
            raise AssertionError(
                "EOF aggregation recovery requires one reused aggregate result and one sink effect: "
                f"tokens={token_count_after}, effects={effect_count_after}"
            )
        if len(batches_after) != 2:
            raise AssertionError(f"EOF aggregation recovery requires failed and completed batch attempts: {batches_after!r}")
        original_batch_id_after, original_attempt_after, original_status_after, original_members_after = batches_after[0]
        recovery_batch_id_after, recovery_attempt_after, recovery_status_after, member_token_ids_after = batches_after[1]
        if (
            original_batch_id_after != original_batch_id_before
            or original_members_after != member_token_ids_before
            or member_token_ids_after != member_token_ids_before
            or (original_attempt_after, original_status_after, recovery_attempt_after, recovery_status_after)
            != (0, "failed", 1, "completed")
            or recovery_batch_id_after == original_batch_id_after
        ):
            raise AssertionError("EOF aggregation recovery changed original identity, member identity, or retry-attempt semantics")
        final_batches = tuple(sorted(durable_projection.batches, key=lambda batch: batch.attempt))
        if len(final_batches) != 2:
            raise AssertionError(f"EOF aggregation recovery projected unexpected batches: {durable_projection.batches!r}")
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
        terminal_idempotence: TerminalResumeIdempotenceEvidence | None = None
        aggregation_eof: AggregationEOFRecoveryEvidence | None = None
        if prove_terminal_idempotence:
            if control_terminal_projection is None:
                raise AssertionError("terminal-resume proof lost its fresh-run control projection")
            resumed_terminal_projection = terminal_equivalence_projection(
                durable_projection,
                sink_outputs=sink_outputs,
                rows_processed=result_data["rows_processed"],
                rows_succeeded=result_data["rows_succeeded"],
                rows_failed=result_data["rows_failed"],
            )
            if control_terminal_projection != resumed_terminal_projection:
                raise AssertionError(
                    "fresh control and resumed terminal behavior differ: "
                    f"control={control_terminal_projection.model_dump(mode='json')!r}, "
                    f"resumed={resumed_terminal_projection.model_dump(mode='json')!r}"
                )

            durable_records_before = _public_durable_records(
                reopened_db,
                run_id=run_id,
                payload_store=reopened_store,
            )
            portable_records_before = list(LandscapeExporter(reopened_db).export_run(run_id))
            durable_records_sha256_before = _canonical_sha256(durable_records_before)
            portable_export_sha256_before = _canonical_sha256(portable_records_before)
            output_tree_sha256_before = _output_tree_sha256(fresh_rendered)
            artifact_digests_before = _artifact_byte_digests(fresh_rendered)
            resumed_full_projection_sha256 = stable_run_projection_sha256(
                durable_projection,
                runtime_root=tmp_path,
                settings=fresh_rendered.settings,
            )
            if not isinstance(case.expected, SummaryRunExpectation) or case.expected.resumed_full_projection_sha256 is None:
                raise AssertionError("terminal-resume case lost its manifest-pinned full-history hash")
            if resumed_full_projection_sha256 != case.expected.resumed_full_projection_sha256:
                raise AssertionError(
                    "resumed full durable history differs from the manifest pin: "
                    f"expected={case.expected.resumed_full_projection_sha256}, observed={resumed_full_projection_sha256}"
                )

            reopened_db.close()
            database_path = tmp_path / "audit.db"
            database_sha256_before = hashlib.sha256(database_path.read_bytes()).hexdigest()

            second_rendered = render_settings(case, tmp_path)
            second_built = build_scenario(second_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
            if second_built.graph_evidence.topology_hash != checkpoint_topology_hash:
                raise AssertionError("second fresh resume graph does not match the persisted checkpoint topology")
            second_db = LandscapeDB.from_url(db_url, create_tables=False)
            second_store = FilesystemPayloadStore(payload_root)
            second_checkpoint_manager = CheckpointManager(second_db)
            second_checkpoint_config = RuntimeCheckpointConfig.from_settings(second_rendered.settings.checkpoint)
            try:
                try:
                    Orchestrator(
                        second_db,
                        checkpoint_manager=second_checkpoint_manager,
                        checkpoint_config=second_checkpoint_config,
                    ).resume(
                        resume_point,
                        second_built.config,
                        second_built.graph,
                        payload_store=second_store,
                        settings=second_rendered.settings,
                    )
                except NonResumableRunError as exc:
                    if type(exc) is not NonResumableRunError:
                        raise AssertionError(
                            f"second public resume must raise the exact NonResumableRunError type; observed={type(exc).__qualname__}"
                        ) from exc
                    second_resume_error = exc
                else:
                    raise AssertionError("second public resume unexpectedly admitted a completed run")
            finally:
                second_db.close()

            database_sha256_after = hashlib.sha256(database_path.read_bytes()).hexdigest()
            output_tree_sha256_after = _output_tree_sha256(second_rendered)
            artifact_digests_after = _artifact_byte_digests(second_rendered)
            after_db = LandscapeDB.from_url(db_url, create_tables=False)
            try:
                durable_records_after = _public_durable_records(
                    after_db,
                    run_id=run_id,
                    payload_store=second_store,
                )
                portable_records_after = list(LandscapeExporter(after_db).export_run(run_id))
                after_audit = _audit_evidence(
                    portable_records_after,
                    portable_projection=_stable_projection(portable_records_after, source="post-refusal portable export"),
                )
            finally:
                after_db.close()
            if after_audit.source_operation_count != 1:
                raise AssertionError(f"second public resume replayed the source: source_load count={after_audit.source_operation_count}")

            expected_terminal_reason = "Run is terminal (status 'completed'); successful terminal runs are immutable"
            if second_resume_error.reason != expected_terminal_reason:
                raise AssertionError(
                    "second public resume returned an unexpected terminal refusal reason: "
                    f"expected={expected_terminal_reason!r}, observed={second_resume_error.reason!r}"
                )
            terminal_idempotence = TerminalResumeIdempotenceEvidence(
                fault_seam="eof_flush_before_transform_result",
                fault_count=1,
                source_exhausted_before=True,
                resumed_run_id=run_id,
                control_terminal_projection=control_terminal_projection,
                resumed_terminal_projection=resumed_terminal_projection,
                terminal_projection_equal=True,
                fresh_object_lifetimes=4,
                resumed_full_projection_sha256=resumed_full_projection_sha256,
                second_resume_error_type="NonResumableRunError",
                second_resume_error_run_id=second_resume_error.run_id,
                second_resume_error_reason="Run is terminal (status 'completed'); successful terminal runs are immutable",
                database_sha256_before=database_sha256_before,
                database_sha256_after=database_sha256_after,
                durable_records_sha256_before=durable_records_sha256_before,
                durable_records_sha256_after=_canonical_sha256(durable_records_after),
                portable_export_sha256_before=portable_export_sha256_before,
                portable_export_sha256_after=_canonical_sha256(portable_records_after),
                output_tree_sha256_before=output_tree_sha256_before,
                output_tree_sha256_after=output_tree_sha256_after,
                artifact_digests_before=artifact_digests_before,
                artifact_digests_after=artifact_digests_after,
                zero_mutation=True,
                provisional_until_deferred_platform_rebase=True,
            )
        else:
            aggregation_eof = AggregationEOFRecoveryEvidence(
                fault_seam="eof_flush_before_transform_result",
                fault_count=1,
                source_exhausted_before=True,
                original_batch_id_before=original_batch_id_before,
                original_batch_id_after=original_batch_id_after,
                recovery_batch_id_after=recovery_batch_id_after,
                member_token_ids_before=member_token_ids_before,
                member_token_ids_after=member_token_ids_after,
                original_batch_identity_preserved=True,
                member_identity_reused=True,
                membership_unchanged=True,
                result_token_absent_before=True,
                sink_effect_absent_before=True,
                final_batches=final_batches,
                final_output_rows=1,
                final_output_json='{"count":3,"value":60}',
                durable_export_parity=True,
                provisional_until_deferred_platform_rebase=True,
            )
        return ScenarioRunEvidence(
            schema_version=2,
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
                sink_outputs=sink_outputs,
                durable_projection=durable_projection,
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
                aggregation_eof=aggregation_eof,
                terminal_resume_idempotence=terminal_idempotence,
            ),
            completed_stages=("config", "build", "runtime", "audit", "recovery"),
        )
    finally:
        reopened_db.close()


def _expansion_child_enqueue_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    db_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"
    initial_rendered = render_settings(case, tmp_path)
    initial_built = build_scenario(initial_rendered)
    initial_store = FilesystemPayloadStore(payload_root)
    initial_db = LandscapeDB(db_url)
    initial_checkpoint_manager = CheckpointManager(initial_db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    record_run_source = RunLifecycleRepository.record_run_source
    observed_faults: list[str] = []

    def fail_after_source_exhausted(
        repository: RunLifecycleRepository,
        **kwargs: Any,
    ) -> None:
        record_run_source(repository, **kwargs)
        # ``lifecycle_state`` is declared ``str | RunSourceLifecycleState``.
        # The enum is a ``StrEnum``, so this one comparison covers both arms
        # of the union and binds the owned enum instead of a magic string.
        lifecycle = kwargs["lifecycle_state"]
        if lifecycle == RunSourceLifecycleState.EXHAUSTED and not observed_faults:
            observed_faults.append("after_source_exhausted_before_sink_flush")
            raise RuntimeError("injected DAG corpus expansion crash before sink flush")

    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        try:
            with patch.object(RunLifecycleRepository, "record_run_source", new=fail_after_source_exhausted):
                Orchestrator(
                    initial_db,
                    checkpoint_manager=initial_checkpoint_manager,
                    checkpoint_config=checkpoint_config,
                ).run(
                    initial_built.config,
                    graph=initial_built.graph,
                    settings=initial_rendered.settings,
                    payload_store=initial_store,
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                )
        except RuntimeError as exc:
            if str(exc) != "injected DAG corpus expansion crash before sink flush":
                raise
        else:
            raise AssertionError("expansion recovery corpus did not reach the post-exhaustion pre-sink fault seam")
        if observed_faults != ["after_source_exhausted_before_sink_flush"]:
            raise AssertionError(f"expansion recovery corpus reached unexpected fault seams: {observed_faults!r}")

        initial_repositories = RecorderFactory.read_only(initial_db, payload_store=initial_store)
        runs = initial_repositories.run_lifecycle.list_runs()
        if len(runs) != 1 or runs[0].status is not RunStatus.FAILED:
            raise AssertionError(f"expansion recovery corpus expected one failed run, got {runs!r}")
        run_id = runs[0].run_id
        source_records = initial_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        if not source_records or any(record.lifecycle_state != "exhausted" for record in source_records.values()):
            raise AssertionError(f"expansion recovery corpus source was not exhausted before the fault: {source_records!r}")
        checkpoint = initial_checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None or checkpoint.upstream_topology_hash != initial_built.graph_evidence.topology_hash:
            raise AssertionError("expansion recovery corpus did not preserve its exact topology checkpoint")
        checkpoint_id = checkpoint.checkpoint_id
        checkpoint_sequence = checkpoint.sequence_number
        checkpoint_topology_hash = checkpoint.upstream_topology_hash
        (
            parent_token_ids_before,
            child_token_ids_before,
            expand_group_ids_before,
            work_items_before,
            effect_count_before,
            artifact_count_before,
        ) = _expansion_identity_snapshot(initial_db, run_id=run_id)
        parent_work_ids_before, child_work_ids_before = _partition_expansion_work(
            work_items_before,
            parent_token_ids=parent_token_ids_before,
            child_token_ids=child_token_ids_before,
            parent_status="terminal",
            child_status="pending_sink",
        )
        scheduler_work_ids_before = tuple(sorted((*parent_work_ids_before, *child_work_ids_before)))
        if (
            len(parent_token_ids_before),
            len(child_token_ids_before),
            len(expand_group_ids_before),
            len(scheduler_work_ids_before),
        ) != (3, 6, 3, 9):
            raise AssertionError(
                "expansion recovery corpus requires 3 parents/6 children/3 groups/9 work identities before resume: "
                f"{len(parent_token_ids_before)}/{len(child_token_ids_before)}/"
                f"{len(expand_group_ids_before)}/{len(scheduler_work_ids_before)}"
            )
        if effect_count_before != 0 or artifact_count_before != 0:
            raise AssertionError(
                "expansion recovery corpus crossed the sink boundary before its fault: "
                f"effects={effect_count_before}, artifacts={artifact_count_before}"
            )
    finally:
        initial_db.close()

    del initial_repositories, initial_store, runs, source_records, checkpoint
    del initial_built, initial_rendered

    reopened_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        reopened_store = FilesystemPayloadStore(payload_root)
        reopened_checkpoint_manager = CheckpointManager(reopened_db)
        reopened_checkpoint = reopened_checkpoint_manager.get_latest_checkpoint(run_id)
        if reopened_checkpoint is None or (
            reopened_checkpoint.checkpoint_id,
            reopened_checkpoint.sequence_number,
            reopened_checkpoint.upstream_topology_hash,
        ) != (checkpoint_id, checkpoint_sequence, checkpoint_topology_hash):
            raise AssertionError("expansion recovery corpus checkpoint changed across fresh-object reopen")

        fresh_rendered = render_settings(case, tmp_path)
        fresh_built = build_scenario(fresh_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        fresh_checkpoint_config = RuntimeCheckpointConfig.from_settings(fresh_rendered.settings.checkpoint)
        if fresh_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("expansion recovery corpus fresh graph changed topology")
        recovery = RecoveryManager(reopened_db, reopened_checkpoint_manager)
        resume_check = recovery.can_resume(run_id, fresh_built.graph)
        if not resume_check.can_resume:
            raise AssertionError(f"expansion recovery corpus was not immediately resumable: {resume_check.reason}")
        resume_point = recovery.get_resume_point(run_id, fresh_built.graph)
        if resume_point is None or resume_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError("expansion recovery corpus did not use its public reopened resume point")

        result = Orchestrator(
            reopened_db,
            checkpoint_manager=reopened_checkpoint_manager,
            checkpoint_config=fresh_checkpoint_config,
        ).resume(
            resume_point,
            fresh_built.config,
            fresh_built.graph,
            payload_store=reopened_store,
            settings=fresh_rendered.settings,
        )
        result_data = result.to_dict()
        if result.run_id != run_id or (
            result_data["status"],
            result_data["rows_processed"],
            result_data["rows_succeeded"],
            result_data["rows_failed"],
        ) != ("completed", 3, 6, 0):
            raise AssertionError(f"expansion recovery corpus returned the wrong final result: {result_data!r}")

        sink_outputs = _sink_outputs(fresh_rendered)
        expected_sink_outputs = (
            SinkOutputProjection(
                sink_name="output",
                rows=(
                    '{"item":{"qty":2,"sku":"A1"},"item_index":0,"order_id":1}',
                    '{"item":{"qty":1,"sku":"B2"},"item_index":1,"order_id":1}',
                    '{"item":{"qty":5,"sku":"C3"},"item_index":0,"order_id":2}',
                    '{"item":{"qty":1,"sku":"A1"},"item_index":0,"order_id":3}',
                    '{"item":{"qty":3,"sku":"D4"},"item_index":1,"order_id":3}',
                    '{"item":{"qty":2,"sku":"E5"},"item_index":2,"order_id":3}',
                ),
            ),
        )
        if sink_outputs != expected_sink_outputs:
            raise AssertionError(f"expansion recovery corpus emitted unexpected outputs: {sink_outputs!r}")

        _assert_all_tokens_and_work_terminal(reopened_db, run_id=run_id, payload_store=reopened_store)
        durable_projection, audit = _exact_recovery_views(
            reopened_db,
            run_id=run_id,
            payload_store=reopened_store,
        )
        if audit.source_operation_count != 1:
            raise AssertionError(f"expansion recovery corpus replayed its source: source_load count={audit.source_operation_count}")
        (
            parent_token_ids_after,
            child_token_ids_after,
            expand_group_ids_after,
            work_items_after,
            effect_count_after,
            artifact_count_after,
        ) = _expansion_identity_snapshot(reopened_db, run_id=run_id)
        parent_work_ids_after, child_work_ids_after = _partition_expansion_work(
            work_items_after,
            parent_token_ids=parent_token_ids_after,
            child_token_ids=child_token_ids_after,
            parent_status="terminal",
            child_status="terminal",
        )
        scheduler_work_ids_after = tuple(sorted((*parent_work_ids_after, *child_work_ids_after)))
        if (
            parent_token_ids_after != parent_token_ids_before
            or child_token_ids_after != child_token_ids_before
            or expand_group_ids_after != expand_group_ids_before
            or parent_work_ids_after != parent_work_ids_before
            or child_work_ids_after != child_work_ids_before
        ):
            raise AssertionError("expansion recovery corpus reminted durable expansion or scheduler identity")
        if (effect_count_after, artifact_count_after) != (1, 1):
            raise AssertionError(
                "expansion recovery corpus did not reach exact terminal publication state: "
                f"effects={effect_count_after}, artifacts={artifact_count_after}"
            )
        final_expansions = tuple(sorted(durable_projection.expansions, key=lambda item: item.parent_token_key))
        if tuple(item.expected_child_count for item in final_expansions) != (2, 1, 3):
            raise AssertionError(f"expansion recovery corpus projected unexpected groups: {final_expansions!r}")
        if reopened_checkpoint_manager.get_latest_checkpoint(run_id) is not None:
            raise AssertionError("expansion recovery corpus retained a checkpoint after successful resume")

        return ScenarioRunEvidence(
            schema_version=2,
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
                output_rows=sum(len(output.rows) for output in sink_outputs),
                sink_outputs=sink_outputs,
                durable_projection=durable_projection,
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
                expansion_child_enqueue=ExpansionChildEnqueueRecoveryEvidence(
                    fault_seam="after_source_exhausted_before_sink_flush",
                    fault_count=1,
                    source_exhausted_before=True,
                    parent_token_ids_before=parent_token_ids_before,
                    parent_token_ids_after=parent_token_ids_after,
                    child_token_ids_before=child_token_ids_before,
                    child_token_ids_after=child_token_ids_after,
                    expand_group_ids_before=expand_group_ids_before,
                    expand_group_ids_after=expand_group_ids_after,
                    scheduler_work_ids_before=scheduler_work_ids_before,
                    scheduler_work_ids_after=scheduler_work_ids_after,
                    parent_scheduler_work_ids_before=parent_work_ids_before,
                    parent_scheduler_work_ids_after=parent_work_ids_after,
                    child_scheduler_work_ids_before=child_work_ids_before,
                    child_scheduler_work_ids_after=child_work_ids_after,
                    parent_identity_unchanged=True,
                    child_identity_unchanged=True,
                    group_identity_unchanged=True,
                    scheduler_identity_unchanged=True,
                    pending_children_before=6,
                    sink_effect_absent_before=True,
                    artifact_absent_before=True,
                    final_expansions=final_expansions,
                    final_output_rows=6,
                    durable_export_parity=True,
                    provisional_until_deferred_platform_rebase=True,
                ),
            ),
            completed_stages=("config", "build", "runtime", "audit", "recovery"),
        )
    finally:
        reopened_db.close()


def _sink_effect_snapshot(
    db: LandscapeDB,
    *,
    run_id: str,
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    with db.connection() as conn:
        effects = tuple(
            sorted(
                (dict(row) for row in conn.execute(select(sink_effects_table).where(sink_effects_table.c.run_id == run_id)).mappings()),
                key=lambda row: str(row["effect_id"]),
            )
        )
        effect_ids = tuple(str(effect["effect_id"]) for effect in effects)
        attempts = (
            tuple(
                sorted(
                    (
                        dict(row)
                        for row in conn.execute(
                            select(sink_effect_attempts_table).where(sink_effect_attempts_table.c.effect_id.in_(effect_ids))
                        ).mappings()
                    ),
                    key=lambda row: str(row["attempt_id"]),
                )
            )
            if effect_ids
            else ()
        )
        members = (
            tuple(
                sorted(
                    (
                        dict(row)
                        for row in conn.execute(
                            select(sink_effect_members_table).where(sink_effect_members_table.c.effect_id.in_(effect_ids))
                        ).mappings()
                    ),
                    key=lambda row: (str(row["effect_id"]), int(row["ordinal"])),
                )
            )
            if effect_ids
            else ()
        )
        artifacts = tuple(
            sorted(
                (dict(row) for row in conn.execute(select(artifacts_table).where(artifacts_table.c.run_id == run_id)).mappings()),
                key=lambda row: str(row["artifact_id"]),
            )
        )
    return effects, attempts, members, artifacts


def _sink_boundary_work_snapshot(
    db: LandscapeDB,
    *,
    run_id: str,
) -> tuple[SinkBoundaryWorkProjection, ...]:
    """Project scheduler identity and material without retaining ORM/runtime objects."""

    with db.connection() as conn:
        rows = tuple(
            conn.execute(
                select(token_work_items_table)
                .where(token_work_items_table.c.run_id == run_id)
                .order_by(token_work_items_table.c.work_item_id)
            ).mappings()
        )

    def optional_text(value: object) -> str | None:
        return None if value is None else str(value)

    projections: list[SinkBoundaryWorkProjection] = []
    for row in rows:
        payload = row["row_payload_json"]
        if not isinstance(payload, str) or not payload:
            raise AssertionError("sink-boundary recovery scheduler work lacks a durable row payload")
        try:
            decoded_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AssertionError("sink-boundary recovery scheduler work payload is not valid JSON") from exc
        if not isinstance(decoded_payload, dict):
            raise AssertionError("sink-boundary recovery scheduler work payload is not an object")
        if decoded_payload.get("row_payload") == "purged":
            if set(decoded_payload) != {"payload_hash", "row_payload"}:
                raise AssertionError("sink-boundary recovery scheduler purge witness has unexpected fields")
            row_payload_state = "purged"
            row_payload_anchor_sha256 = decoded_payload.get("payload_hash")
        else:
            if set(decoded_payload) != {"contract", "row"}:
                raise AssertionError("sink-boundary recovery live scheduler payload lacks row and contract material")
            row_payload_state = "live"
            row_payload_anchor_sha256 = None
        projections.append(
            SinkBoundaryWorkProjection(
                work_item_id=str(row["work_item_id"]),
                token_id=str(row["token_id"]),
                row_id=str(row["row_id"]),
                row_payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                row_payload_state=row_payload_state,
                row_payload_anchor_sha256=row_payload_anchor_sha256,
                node_id=optional_text(row["node_id"]),
                attempt=int(row["attempt"]),
                status=str(row["status"]),
                pending_sink_name=optional_text(row["pending_sink_name"]),
                pending_outcome=optional_text(row["pending_outcome"]),
                pending_path=optional_text(row["pending_path"]),
                pending_error_hash=optional_text(row["pending_error_hash"]),
                pending_error_message=optional_text(row["pending_error_message"]),
            )
        )
    return tuple(projections)


def _required_sink_boundary_sql_text(value: object, *, field: str) -> str:
    """Return one exact non-empty SQL identity without coercion or trimming."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise AssertionError(f"sink-boundary recovery {field} must be non-empty SQL text, got {value!r}")
    return value


def _sink_boundary_effect_projection(
    effects: tuple[dict[str, Any], ...],
    members: tuple[dict[str, Any], ...],
    *,
    sink_names_by_node_id: Mapping[str, str],
) -> tuple[SinkBoundaryEffectProjection, ...]:
    """Bind each durable effect to its declared sink and ordered member identities."""

    validated_members = tuple(
        (
            _required_sink_boundary_sql_text(member.get("effect_id"), field="member.effect_id"),
            _required_sink_boundary_sql_text(member.get("token_id"), field="member.token_id"),
            _required_sink_boundary_sql_text(member.get("row_id"), field="member.row_id"),
        )
        for member in members
    )
    projections: list[SinkBoundaryEffectProjection] = []
    for effect in effects:
        effect_id = _required_sink_boundary_sql_text(effect.get("effect_id"), field="effect.effect_id")
        sink_node_id = _required_sink_boundary_sql_text(effect.get("sink_node_id"), field="effect.sink_node_id")
        artifact_id = _required_sink_boundary_sql_text(effect.get("artifact_id"), field="effect.artifact_id")
        sink_name = sink_names_by_node_id.get(sink_node_id)
        if sink_name is None:
            raise AssertionError(f"sink-boundary recovery effect targets an undeclared sink node: {sink_node_id!r}")
        effect_members = tuple(member for member in validated_members if member[0] == effect_id)
        projections.append(
            SinkBoundaryEffectProjection(
                effect_id=effect_id,
                sink_name=sink_name,
                sink_node_id=sink_node_id,
                artifact_id=artifact_id,
                state=str(effect["state"]),
                member_token_ids=tuple(member[1] for member in effect_members),
                member_row_ids=tuple(member[2] for member in effect_members),
            )
        )
    return tuple(projections)


def _single_pending_sink_work_snapshot(
    db: LandscapeDB,
    *,
    run_id: str,
    require_live_payload: bool = True,
) -> dict[str, Any]:
    """Read and validate the sole scheduler identity for the one-row redrive case."""

    with db.connection() as conn:
        rows = tuple(conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == run_id)).mappings())
    if len(rows) != 1:
        raise AssertionError(f"pending-sink recovery requires exactly one scheduler work row, got {len(rows)}")
    snapshot = dict(rows[0])
    payload = snapshot["row_payload_json"]
    if not isinstance(payload, str) or payload == "":
        raise AssertionError("pending-sink recovery work does not carry a non-empty durable row payload")
    if require_live_payload:
        restored = TokenSchedulerRepository.deserialize_row_payload(payload)
        if restored.to_dict() != {"id": 1, "value": 10}:
            raise AssertionError(f"pending-sink recovery restored unexpected row payload: {restored.to_dict()!r}")
    snapshot["row_payload_hash"] = hashlib.sha256(payload.encode()).hexdigest()
    return snapshot


def _pending_sink_redrive_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    """Exercise TS-04 then TS-06 through three fresh production lifetimes."""

    db_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"
    clock = MockClock(start=1_750_000_000.0)
    initial_rendered = render_settings(case, tmp_path)
    initial_built = build_scenario(initial_rendered)
    initial_store = FilesystemPayloadStore(payload_root)
    initial_db = LandscapeDB(db_url)
    initial_checkpoint_manager = CheckpointManager(initial_db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    record_run_source = RunLifecycleRepository.record_run_source
    setup_faults: list[str] = []

    def stop_after_source_exhausted(repository: RunLifecycleRepository, **kwargs: Any) -> None:
        record_run_source(repository, **kwargs)
        # See ``fail_after_source_exhausted``: ``RunSourceLifecycleState`` is a
        # ``StrEnum``, so one comparison covers both arms of the declared
        # ``str | RunSourceLifecycleState`` union.
        lifecycle = kwargs["lifecycle_state"]
        if lifecycle == RunSourceLifecycleState.EXHAUSTED and not setup_faults:
            setup_faults.append("after_source_exhausted_before_sink_flush")
            raise RuntimeError("injected DAG corpus pending-sink setup crash")

    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        try:
            with patch.object(RunLifecycleRepository, "record_run_source", new=stop_after_source_exhausted):
                Orchestrator(
                    initial_db,
                    checkpoint_manager=initial_checkpoint_manager,
                    checkpoint_config=checkpoint_config,
                    clock=clock,
                ).run(
                    initial_built.config,
                    graph=initial_built.graph,
                    settings=initial_rendered.settings,
                    payload_store=initial_store,
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                )
        except RuntimeError as exc:
            if str(exc) != "injected DAG corpus pending-sink setup crash":
                raise
        else:
            raise AssertionError("pending-sink recovery setup did not stop before sink flush")
        if setup_faults != ["after_source_exhausted_before_sink_flush"]:
            raise AssertionError(f"pending-sink recovery setup reached unexpected seams: {setup_faults!r}")

        initial_repositories = RecorderFactory.read_only(initial_db, payload_store=initial_store)
        runs = initial_repositories.run_lifecycle.list_runs()
        if len(runs) != 1 or runs[0].status is not RunStatus.FAILED:
            raise AssertionError(f"pending-sink recovery expected one failed setup run, got {runs!r}")
        run_id = runs[0].run_id
        source_records = initial_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        if not source_records or any(record.lifecycle_state != "exhausted" for record in source_records.values()):
            raise AssertionError(f"pending-sink recovery source was not exhausted before setup stop: {source_records!r}")
        checkpoint = initial_checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None or checkpoint.upstream_topology_hash != initial_built.graph_evidence.topology_hash:
            raise AssertionError("pending-sink recovery setup did not retain its exact topology checkpoint")
        checkpoint_id = checkpoint.checkpoint_id
        checkpoint_sequence = checkpoint.sequence_number
        checkpoint_topology_hash = checkpoint.upstream_topology_hash
        pending_before_claim = _single_pending_sink_work_snapshot(initial_db, run_id=run_id)
        if (
            pending_before_claim["status"],
            pending_before_claim["pending_sink_name"],
            pending_before_claim["pending_outcome"],
            pending_before_claim["pending_path"],
            pending_before_claim["pending_error_hash"],
            pending_before_claim["pending_error_message"],
            pending_before_claim["attempt"],
            pending_before_claim["lease_expires_at"],
        ) != ("pending_sink", "output", "success", "default_flow", None, None, 1, None):
            raise AssertionError(f"pending-sink recovery setup retained the wrong complete bundle: {pending_before_claim!r}")
        initial_effects = RecorderFactory(initial_db, payload_store=initial_store).execution.sink_effects.get_effects_for_run(run_id)
        initial_artifacts = initial_repositories.execution.get_artifacts(run_id)
        if initial_effects or initial_artifacts or initial_rendered.output_path.exists():
            raise AssertionError("pending-sink recovery setup crossed the sink-effect reservation boundary")
    finally:
        initial_db.close()

    del initial_repositories, initial_store, runs, source_records, checkpoint
    del initial_built, initial_rendered

    first_resume_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        first_resume_store = FilesystemPayloadStore(payload_root)
        first_resume_checkpoint_manager = CheckpointManager(first_resume_db)
        first_resume_rendered = render_settings(case, tmp_path)
        first_resume_built = build_scenario(first_resume_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        if first_resume_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("pending-sink first fresh graph changed topology")
        first_recovery = RecoveryManager(first_resume_db, first_resume_checkpoint_manager)
        first_check = first_recovery.can_resume(run_id, first_resume_built.graph)
        first_point = first_recovery.get_resume_point(run_id, first_resume_built.graph)
        if not first_check.can_resume or first_point is None or first_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError(f"pending-sink first public resume was unavailable: {first_check.reason}")

        original_reserve = SinkEffectReservation.reserve
        reservation_faults: list[str] = []

        def stop_before_reservation(reservation: SinkEffectReservation, request: Any) -> Any:
            if not reservation_faults:
                reservation_faults.append("before_sink_effect_reservation")
                raise RuntimeError("injected DAG corpus crash before sink-effect reservation")
            return original_reserve(reservation, request)

        try:
            with patch.object(SinkEffectReservation, "reserve", new=stop_before_reservation):
                Orchestrator(
                    first_resume_db,
                    checkpoint_manager=first_resume_checkpoint_manager,
                    checkpoint_config=checkpoint_config,
                    clock=clock,
                ).resume(
                    first_point,
                    first_resume_built.config,
                    first_resume_built.graph,
                    payload_store=first_resume_store,
                    settings=first_resume_rendered.settings,
                )
        except RuntimeError as exc:
            if str(exc) != "injected DAG corpus crash before sink-effect reservation":
                raise
        else:
            raise AssertionError("pending-sink first resume did not reach the reservation fault")
        if reservation_faults != ["before_sink_effect_reservation"]:
            raise AssertionError(f"pending-sink recovery reached unexpected reservation seams: {reservation_faults!r}")

        leased_before_recovery = _single_pending_sink_work_snapshot(first_resume_db, run_id=run_id)
        if leased_before_recovery["status"] != "leased" or leased_before_recovery["lease_owner"] in (None, ""):
            raise AssertionError(f"pending-sink first resume did not retain a claimed sink-redrive lease: {leased_before_recovery!r}")
        if leased_before_recovery["lease_expires_at"] is None:
            raise AssertionError("pending-sink first resume did not retain a bounded sink-redrive lease")
        claim_preserved_fields = (
            "work_item_id",
            "token_id",
            "row_id",
            "row_payload_hash",
            "pending_sink_name",
            "pending_outcome",
            "pending_path",
            "pending_error_hash",
            "pending_error_message",
            "attempt",
        )
        if any(pending_before_claim[field] != leased_before_recovery[field] for field in claim_preserved_fields):
            raise AssertionError("pending-sink TS-04 claim changed the complete durable bundle or scheduler identity")
        first_factory = RecorderFactory(first_resume_db, payload_store=first_resume_store)
        effects_before = first_factory.execution.sink_effects.get_effects_for_run(run_id)
        artifacts_before = first_factory.execution.get_artifacts(run_id)
        if effects_before or artifacts_before or first_resume_rendered.output_path.exists():
            raise AssertionError("pending-sink reservation fault wrote an effect, artifact, or publication")
        if first_resume_checkpoint_manager.get_latest_checkpoint(run_id) is None:
            raise AssertionError("pending-sink reservation fault removed the resumable checkpoint")
    finally:
        first_resume_db.close()

    del first_resume_store, first_resume_built, first_resume_rendered, first_recovery, first_check, first_point, first_factory

    clock.advance(360.0)
    final_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        final_store = FilesystemPayloadStore(payload_root)
        final_checkpoint_manager = CheckpointManager(final_db)
        final_rendered = render_settings(case, tmp_path)
        final_built = build_scenario(final_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        if final_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("pending-sink final fresh graph changed topology")
        final_recovery = RecoveryManager(final_db, final_checkpoint_manager)
        final_check = final_recovery.can_resume(run_id, final_built.graph)
        final_point = final_recovery.get_resume_point(run_id, final_built.graph)
        if not final_check.can_resume or final_point is None or final_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError(f"pending-sink final public resume was unavailable: {final_check.reason}")

        original_claim_pending_sink = TokenSchedulerRepository.claim_pending_sink
        recovered_before_reclaim: list[dict[str, Any]] = []

        def capture_recovered_bundle(repository: TokenSchedulerRepository, **kwargs: Any) -> Any:
            if not recovered_before_reclaim:
                candidate = _single_pending_sink_work_snapshot(final_db, run_id=run_id)
                if candidate["status"] == "pending_sink":
                    recovered_before_reclaim.append(candidate)
            return original_claim_pending_sink(repository, **kwargs)

        with patch.object(TokenSchedulerRepository, "claim_pending_sink", new=capture_recovered_bundle):
            result = Orchestrator(
                final_db,
                checkpoint_manager=final_checkpoint_manager,
                checkpoint_config=checkpoint_config,
                clock=clock,
            ).resume(
                final_point,
                final_built.config,
                final_built.graph,
                payload_store=final_store,
                settings=final_rendered.settings,
            )
        if len(recovered_before_reclaim) != 1:
            raise AssertionError(f"pending-sink final resume did not expose one ownerless recovered bundle: {recovered_before_reclaim!r}")
        recovered_bundle = recovered_before_reclaim[0]
        preserved_fields = (
            "work_item_id",
            "token_id",
            "row_id",
            "row_payload_hash",
            "pending_sink_name",
            "pending_outcome",
            "pending_path",
            "pending_error_hash",
            "pending_error_message",
            "attempt",
        )
        if any(leased_before_recovery[field] != recovered_bundle[field] for field in preserved_fields):
            raise AssertionError("pending-sink expiry recovery changed the durable bundle or scheduler identity")
        if recovered_bundle["lease_owner"] is not None or recovered_bundle["lease_expires_at"] is not None:
            raise AssertionError("pending-sink expiry recovery did not clear the former lease before reclaim")

        result_data = result.to_dict()
        if result.run_id != run_id or (
            result_data["status"],
            result_data["rows_processed"],
            result_data["rows_succeeded"],
            result_data["rows_failed"],
        ) != ("completed", 1, 1, 0):
            raise AssertionError(f"pending-sink recovery returned the wrong final result: {result_data!r}")
        sink_outputs = _sink_outputs(final_rendered)
        expected_sink_outputs = (SinkOutputProjection(sink_name="output", rows=('{"id":1,"value":10}',)),)
        if sink_outputs != expected_sink_outputs:
            raise AssertionError(f"pending-sink recovery emitted unexpected output: {sink_outputs!r}")

        final_factory = RecorderFactory(final_db, payload_store=final_store)
        effects_after = final_factory.execution.sink_effects.get_effects_for_run(run_id)
        members_after = final_factory.execution.sink_effects.get_members_for_run(run_id)
        attempts_after = final_factory.execution.sink_effects.get_attempts_for_run(run_id)
        artifacts_after = final_factory.execution.get_artifacts(run_id)
        if (
            len(effects_after),
            len(members_after),
            len(attempts_after),
            len(artifacts_after),
            sum(effect.publication_performed is True for effect in effects_after),
        ) != (1, 1, 3, 1, 1):
            raise AssertionError("pending-sink recovery did not produce exactly one effect/member/artifact/publication and three attempts")
        effect = effects_after[0]
        artifact = artifacts_after[0]
        if artifact.sink_effect_id != effect.effect_id or artifact.artifact_id != effect.artifact_id:
            raise AssertionError("pending-sink recovery artifact is not linked to the sole finalized effect")
        if members_after[0].token_id != str(leased_before_recovery["token_id"]):
            raise AssertionError("pending-sink recovery effect member changed token identity")

        with final_db.connection() as conn:
            recovery_events = tuple(
                conn.execute(
                    select(scheduler_events_table)
                    .where(scheduler_events_table.c.run_id == run_id)
                    .where(scheduler_events_table.c.event_type == "recover_expired_lease")
                ).mappings()
            )
            claim_events = tuple(
                conn.execute(
                    select(scheduler_events_table)
                    .where(scheduler_events_table.c.run_id == run_id)
                    .where(scheduler_events_table.c.event_type == "claim_pending_sink")
                    .order_by(scheduler_events_table.c.recorded_at, scheduler_events_table.c.event_id)
                ).mappings()
            )
        if len(recovery_events) != 1:
            raise AssertionError(f"pending-sink recovery requires one RECOVER_EXPIRED_LEASE event, got {recovery_events!r}")
        recovery_event = recovery_events[0]
        if (
            recovery_event["work_item_id"],
            recovery_event["token_id"],
            recovery_event["from_status"],
            recovery_event["to_status"],
            recovery_event["from_attempt"],
            recovery_event["to_attempt"],
            recovery_event["from_lease_owner"],
            recovery_event["to_lease_owner"],
        ) != (
            leased_before_recovery["work_item_id"],
            leased_before_recovery["token_id"],
            "leased",
            "pending_sink",
            1,
            1,
            leased_before_recovery["lease_owner"],
            None,
        ):
            raise AssertionError(f"pending-sink recovery event changed subtype identity: {dict(recovery_event)!r}")
        if len(claim_events) != 2:
            raise AssertionError(f"pending-sink recovery did not claim the same exact bundle twice: {claim_events!r}")
        first_claim, fresh_claim = claim_events
        if any(
            event["work_item_id"] != leased_before_recovery["work_item_id"]
            or event["token_id"] != leased_before_recovery["token_id"]
            or event["from_status"] != "pending_sink"
            or event["to_status"] != "leased"
            or event["from_attempt"] != 1
            or event["to_attempt"] != 1
            for event in claim_events
        ):
            raise AssertionError(f"pending-sink recovery claim events changed the exact bundle subtype: {claim_events!r}")
        fresh_owner = fresh_claim["to_lease_owner"]
        if (
            first_claim["to_lease_owner"] != leased_before_recovery["lease_owner"]
            or fresh_claim["from_lease_owner"] is not None
            or not isinstance(fresh_owner, str)
            or fresh_owner == ""
            or fresh_owner == leased_before_recovery["lease_owner"]
        ):
            raise AssertionError(f"pending-sink recovery did not clear and reclaim under a fresh lease owner: {claim_events!r}")

        if (
            members_after[0].effect_id != effect.effect_id
            or tuple(attempt.effect_id for attempt in attempts_after) != (effect.effect_id,) * 3
            or artifact.sink_effect_id != effect.effect_id
        ):
            raise AssertionError("pending-sink recovery effect, member, attempts, and artifact split identity")

        _assert_all_tokens_and_work_terminal(final_db, run_id=run_id, payload_store=final_store)
        final_work = _single_pending_sink_work_snapshot(final_db, run_id=run_id, require_live_payload=False)
        if final_work["work_item_id"] != leased_before_recovery["work_item_id"] or final_work["status"] != "terminal":
            raise AssertionError("pending-sink recovery did not terminalize the original work identity")
        final_outcome = RecorderFactory.read_only(final_db, payload_store=final_store).data_flow.get_token_outcome(
            str(leased_before_recovery["token_id"])
        )
        if final_outcome is None or final_outcome.outcome is None or final_outcome.outcome.value != "success":
            raise AssertionError(f"pending-sink recovery did not retain the terminal success outcome: {final_outcome!r}")
        durable_projection, audit = _exact_recovery_views(final_db, run_id=run_id, payload_store=final_store)
        if audit.source_operation_count != 1:
            raise AssertionError(f"pending-sink recovery replayed its source: source_load count={audit.source_operation_count}")
        if final_checkpoint_manager.get_latest_checkpoint(run_id) is not None:
            raise AssertionError("pending-sink recovery retained its checkpoint after successful resume")

        attempt_ids = tuple(sorted(attempt.attempt_id for attempt in attempts_after))
        return ScenarioRunEvidence(
            schema_version=2,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=final_rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=final_rendered.settings_sha256),
            graph=final_built.graph_evidence,
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
                attempted=True,
                database_reopened=True,
                checkpoint_id=checkpoint_id,
                checkpoint_sequence=checkpoint_sequence,
                can_resume=True,
                source_replayed=False,
                checkpoint_removed=True,
                pending_sink_redrive=PendingSinkRedriveRecoveryEvidence(
                    fault_seam="before_sink_effect_reservation",
                    fault_count=1,
                    source_exhausted_before=True,
                    work_item_id_before=str(pending_before_claim["work_item_id"]),
                    work_item_id_claimed=str(leased_before_recovery["work_item_id"]),
                    work_item_id_after=str(recovered_bundle["work_item_id"]),
                    token_id_before=str(pending_before_claim["token_id"]),
                    token_id_claimed=str(leased_before_recovery["token_id"]),
                    token_id_after=str(recovered_bundle["token_id"]),
                    row_id_before=str(pending_before_claim["row_id"]),
                    row_id_claimed=str(leased_before_recovery["row_id"]),
                    row_id_after=str(recovered_bundle["row_id"]),
                    row_payload_hash_before=str(pending_before_claim["row_payload_hash"]),
                    row_payload_hash_claimed=str(leased_before_recovery["row_payload_hash"]),
                    row_payload_hash_after=str(recovered_bundle["row_payload_hash"]),
                    pending_sink_name_before=str(pending_before_claim["pending_sink_name"]),
                    pending_sink_name_claimed=str(leased_before_recovery["pending_sink_name"]),
                    pending_sink_name_after=str(recovered_bundle["pending_sink_name"]),
                    pending_outcome_before="success",
                    pending_outcome_claimed="success",
                    pending_outcome_after="success",
                    pending_path_before="default_flow",
                    pending_path_claimed="default_flow",
                    pending_path_after="default_flow",
                    pending_error_hash_before=None,
                    pending_error_hash_claimed=None,
                    pending_error_hash_after=None,
                    pending_error_message_before=None,
                    pending_error_message_claimed=None,
                    pending_error_message_after=None,
                    scheduler_attempt_before=1,
                    scheduler_attempt_claimed=1,
                    scheduler_attempt_after=1,
                    lease_owner_before=str(leased_before_recovery["lease_owner"]),
                    lease_cleared_before_reclaim=True,
                    reclaimed_by_fresh_owner=True,
                    reclaimed_lease_owner_after=fresh_owner,
                    expired_lease_recovery_events=1,
                    recover_event_work_item_id=str(recovery_event["work_item_id"]),
                    recover_event_token_id=str(recovery_event["token_id"]),
                    recover_event_from_status="leased",
                    recover_event_to_status="pending_sink",
                    recover_event_from_attempt=1,
                    recover_event_to_attempt=1,
                    recover_event_from_lease_owner=str(recovery_event["from_lease_owner"]),
                    recover_event_to_lease_owner=None,
                    sink_effects_before=0,
                    artifacts_before=0,
                    sink_effects_after=1,
                    sink_effect_members_after=1,
                    sink_effect_attempts_after=3,
                    artifacts_after=1,
                    publications_after=1,
                    effect_id_after=effect.effect_id,
                    member_effect_id_after=members_after[0].effect_id,
                    attempt_effect_ids_after=tuple(attempt.effect_id for attempt in attempts_after),
                    artifact_id_after=artifact.artifact_id,
                    artifact_effect_id_after=artifact.sink_effect_id,
                    effect_attempt_ids_after=attempt_ids,
                    terminal_outcome="success",
                    terminal_work_status="terminal",
                    final_output_rows=1,
                    durable_export_parity=True,
                    provisional_until_deferred_platform_rebase=True,
                ),
            ),
            completed_stages=("config", "build", "runtime", "audit", "recovery"),
        )
    finally:
        final_db.close()


def _parallel_sink_finalization_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    db_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"
    initial_rendered = render_settings(case, tmp_path)
    if tuple(initial_rendered.settings.sinks) != ("left", "right"):
        raise AssertionError("parallel sink-finalization recovery requires declared sink order ('left', 'right')")
    initial_built = build_scenario(initial_rendered)
    if (initial_built.graph_evidence.node_count, initial_built.graph_evidence.edge_count) != (6, 8):
        raise AssertionError("parallel sink-finalization recovery requires the exact six-node/eight-edge graph")
    initial_store = FilesystemPayloadStore(payload_root)
    initial_db = LandscapeDB(db_url)
    initial_checkpoint_manager = CheckpointManager(initial_db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    observed_seams: list[SinkEffectExecutionSeam] = []

    def fail_after_first_finalize(_coordinator: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
        observed_seams.append(seam)
        if seam is SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE:
            raise SinkEffectInjectedFault(seam)

    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        with patch.object(SinkEffectCoordinator, "_fault", new=fail_after_first_finalize):
            try:
                Orchestrator(
                    initial_db,
                    checkpoint_manager=initial_checkpoint_manager,
                    checkpoint_config=checkpoint_config,
                ).run(
                    initial_built.config,
                    graph=initial_built.graph,
                    settings=initial_rendered.settings,
                    payload_store=initial_store,
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                )
            except SinkEffectInjectedFault as exc:
                if exc.seam is not SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE:
                    raise AssertionError(f"parallel sink-finalization recovery reached the wrong fault seam: {exc.seam.value}") from exc
            else:
                raise AssertionError("parallel sink-finalization recovery did not inject the first-sink finalization fault")
        expected_seams = (
            SinkEffectExecutionSeam.BEFORE_RESERVATION,
            SinkEffectExecutionSeam.AFTER_RESERVATION,
            SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM,
            SinkEffectExecutionSeam.AFTER_INSPECTION,
            SinkEffectExecutionSeam.AFTER_PLAN_CAS,
            SinkEffectExecutionSeam.BEFORE_EFFECT,
            SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN,
            SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE,
            SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE,
        )
        if tuple(observed_seams) != expected_seams:
            raise AssertionError(f"parallel sink-finalization recovery reached unexpected seams: {observed_seams!r}")
        finalize_fault_count = observed_seams.count(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
        if finalize_fault_count != 1:
            raise AssertionError(f"parallel sink-finalization recovery reached the finalization fault {finalize_fault_count} times")

        initial_repositories = RecorderFactory.read_only(initial_db, payload_store=initial_store)
        runs = initial_repositories.run_lifecycle.list_runs()
        if len(runs) != 1 or runs[0].status is not RunStatus.FAILED:
            raise AssertionError(f"parallel sink-finalization recovery expected one failed run, got {runs!r}")
        run_id = runs[0].run_id
        source_records = initial_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        if not source_records or any(record.lifecycle_state != "exhausted" for record in source_records.values()):
            raise AssertionError(f"parallel sink-finalization recovery source was not exhausted: {source_records!r}")
        checkpoint = initial_checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None or checkpoint.upstream_topology_hash != initial_built.graph_evidence.topology_hash:
            raise AssertionError("parallel sink-finalization recovery did not preserve its exact topology checkpoint")
        checkpoint_id = checkpoint.checkpoint_id
        checkpoint_sequence = checkpoint.sequence_number
        checkpoint_topology_hash = checkpoint.upstream_topology_hash

        sink_ids = {str(name): str(node_id) for name, node_id in initial_built.graph.get_sink_id_map().items()}
        coalesce_ids = {str(node_id) for node_id in initial_built.graph.get_coalesce_id_map().values()}
        if tuple(sink_ids) != ("left", "right") or len(coalesce_ids) != 2:
            raise AssertionError(f"parallel sink-finalization recovery graph identity is wrong: {sink_ids!r}, {coalesce_ids!r}")
        with initial_db.connection() as conn:
            row_count = conn.execute(select(rows_table.c.row_id).where(rows_table.c.run_id == run_id)).scalars().all()
            token_count = conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).scalars().all()
            parent_count = (
                conn.execute(select(token_parents_table.c.token_id).where(token_parents_table.c.run_id == run_id)).scalars().all()
            )
            coalesce_states = (
                conn.execute(
                    select(node_states_table.c.node_id, node_states_table.c.status).where(
                        node_states_table.c.run_id == run_id,
                        node_states_table.c.node_id.in_(coalesce_ids),
                    )
                )
                .mappings()
                .all()
            )
            sink_states = (
                conn.execute(
                    select(node_states_table.c.node_id, node_states_table.c.status).where(
                        node_states_table.c.run_id == run_id,
                        node_states_table.c.node_id.in_(tuple(sink_ids.values())),
                    )
                )
                .mappings()
                .all()
            )
        if (len(row_count), len(token_count), len(parent_count)) != (3, 21, 24):
            raise AssertionError(
                "parallel sink-finalization recovery requires 3 rows/21 tokens/24 parents before resume: "
                f"{len(row_count)}/{len(token_count)}/{len(parent_count)}"
            )
        if {str(state["node_id"]) for state in coalesce_states} != coalesce_ids or any(
            state["status"] != "completed" for state in coalesce_states
        ):
            raise AssertionError(f"parallel sink-finalization recovery did not complete both coalesces: {coalesce_states!r}")
        if len(sink_states) != 3 or any(state["node_id"] != sink_ids["left"] or state["status"] != "completed" for state in sink_states):
            raise AssertionError(f"parallel sink-finalization recovery reached the wrong pre-resume sink states: {sink_states!r}")

        before_effects, before_attempts, before_members, before_artifacts = _sink_effect_snapshot(initial_db, run_id=run_id)
        if len(before_effects) != 1 or before_effects[0]["sink_node_id"] != sink_ids["left"]:
            raise AssertionError(f"parallel sink-finalization recovery expected only the left effect: {before_effects!r}")
        first_effect_before = before_effects[0]
        first_effect_id = str(first_effect_before["effect_id"])
        first_artifact_id = str(first_effect_before["artifact_id"])
        if (
            first_effect_before["state"] != "finalized"
            or first_effect_before["publication_performed"] is not True
            or first_effect_before["lease_owner"] is not None
            or len(before_artifacts) != 1
            or before_artifacts[0]["artifact_id"] != first_artifact_id
            or before_artifacts[0]["publication_performed"] is not True
        ):
            raise AssertionError("parallel sink-finalization recovery did not durably finalize the first artifact")
        first_attempts_before = tuple(attempt for attempt in before_attempts if attempt["effect_id"] == first_effect_id)
        first_members_before = tuple(member for member in before_members if member["effect_id"] == first_effect_id)
        if (
            {str(attempt["action"]) for attempt in first_attempts_before} != {"inspect", "reconcile", "commit"}
            or any(attempt["state"] != "returned" for attempt in first_attempts_before)
            or len(first_members_before) != 3
            or any(member["member_state"] != "finalized" for member in first_members_before)
        ):
            raise AssertionError("parallel sink-finalization recovery first effect is not exactly finalized")
        left_path = initial_rendered.output_paths["left"]
        right_path = initial_rendered.output_paths["right"]
        left_bytes_before = left_path.read_bytes()
        if len(left_bytes_before.splitlines()) != 3 or right_path.exists():
            raise AssertionError("parallel sink-finalization recovery published anything beyond the exact first three-row artifact")
    finally:
        initial_db.close()

    del initial_repositories, initial_store, runs, source_records, checkpoint
    del initial_built, initial_rendered

    reopened_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        reopened_store = FilesystemPayloadStore(payload_root)
        reopened_checkpoint_manager = CheckpointManager(reopened_db)
        reopened_checkpoint = reopened_checkpoint_manager.get_latest_checkpoint(run_id)
        if reopened_checkpoint is None or (
            reopened_checkpoint.checkpoint_id,
            reopened_checkpoint.sequence_number,
            reopened_checkpoint.upstream_topology_hash,
        ) != (checkpoint_id, checkpoint_sequence, checkpoint_topology_hash):
            raise AssertionError("parallel sink-finalization recovery checkpoint changed across fresh-object reopen")

        fresh_rendered = render_settings(case, tmp_path)
        fresh_built = build_scenario(fresh_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        if fresh_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("parallel sink-finalization recovery fresh graph changed topology")
        recovery = RecoveryManager(reopened_db, reopened_checkpoint_manager)
        resume_check = recovery.can_resume(run_id, fresh_built.graph)
        if not resume_check.can_resume:
            raise AssertionError(f"parallel sink-finalization recovery was not immediately resumable: {resume_check.reason}")
        resume_point = recovery.get_resume_point(run_id, fresh_built.graph)
        if resume_point is None or resume_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError("parallel sink-finalization recovery did not use its public reopened resume point")

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
        result_data = result.to_dict()
        if result.run_id != run_id or (
            result_data["status"],
            result_data["rows_processed"],
            result_data["rows_succeeded"],
            result_data["rows_failed"],
        ) != ("completed", 3, 6, 0):
            raise AssertionError(f"parallel sink-finalization recovery returned the wrong final result: {result_data!r}")

        sink_outputs = _sink_outputs(fresh_rendered)
        expected_outputs = tuple(
            SinkOutputProjection(
                sink_name=sink_name,
                rows=tuple(
                    json.dumps(
                        {
                            f"{sink_name}_a": {"id": row_id, "value": row_id * 10},
                            f"{sink_name}_b": {"id": row_id, "value": row_id * 10},
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for row_id in (1, 2, 3)
                ),
            )
            for sink_name in ("left", "right")
        )
        if sink_outputs != expected_outputs:
            raise AssertionError(f"parallel sink-finalization recovery emitted unexpected outputs: {sink_outputs!r}")

        after_effects, after_attempts, after_members, after_artifacts = _sink_effect_snapshot(reopened_db, run_id=run_id)
        effects_by_sink = {str(effect["sink_node_id"]): effect for effect in after_effects}
        artifacts_by_sink = {str(artifact["sink_node_id"]): artifact for artifact in after_artifacts}
        if (
            len(after_effects) != 2
            or len(after_artifacts) != 2
            or set(effects_by_sink) != set(sink_ids.values())
            or set(artifacts_by_sink) != set(sink_ids.values())
        ):
            raise AssertionError("parallel sink-finalization recovery did not finish exactly two sink effects and artifacts")
        first_effect_after = effects_by_sink[sink_ids["left"]]
        first_artifact_after = artifacts_by_sink[sink_ids["left"]]
        first_attempts_after = tuple(attempt for attempt in after_attempts if attempt["effect_id"] == first_effect_id)
        first_members_after = tuple(member for member in after_members if member["effect_id"] == first_effect_id)
        if (
            first_effect_after != first_effect_before
            or first_artifact_after != before_artifacts[0]
            or first_attempts_after != first_attempts_before
            or first_members_after != first_members_before
            or fresh_rendered.output_paths["left"].read_bytes() != left_bytes_before
        ):
            raise AssertionError("parallel sink-finalization recovery mutated or republished the finalized first effect")

        second_effect = effects_by_sink[sink_ids["right"]]
        second_effect_id = str(second_effect["effect_id"])
        second_artifact = artifacts_by_sink[sink_ids["right"]]
        second_attempts = tuple(attempt for attempt in after_attempts if attempt["effect_id"] == second_effect_id)
        second_members = tuple(member for member in after_members if member["effect_id"] == second_effect_id)
        if (
            second_effect["state"] != "finalized"
            or second_effect["publication_performed"] is not True
            or second_effect["lease_owner"] is not None
            or second_artifact["sink_effect_id"] != second_effect_id
            or second_artifact["publication_performed"] is not True
            or {str(attempt["action"]) for attempt in second_attempts} != {"inspect", "reconcile", "commit"}
            or any(attempt["state"] != "returned" for attempt in second_attempts)
            or len(second_members) != 3
            or any(member["member_state"] != "finalized" for member in second_members)
        ):
            raise AssertionError("parallel sink-finalization recovery did not drive exactly the absent second effect")

        final_repositories = RecorderFactory.read_only(reopened_db, payload_store=reopened_store)
        final_run = final_repositories.run_lifecycle.get_run(run_id)
        final_source_records = final_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        tokens = final_repositories.query.get_all_tokens_for_run(run_id)
        outcomes = final_repositories.query.get_all_token_outcomes_for_run(run_id)
        latest_outcomes = {outcome.token_id: outcome for outcome in outcomes}
        token_ids = {token.token_id for token in tokens}
        with reopened_db.connection() as conn:
            work_statuses = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id)).scalars().all()
            )
            final_row_count = conn.execute(select(rows_table.c.row_id).where(rows_table.c.run_id == run_id)).scalars().all()
            final_parent_count = (
                conn.execute(select(token_parents_table.c.token_id).where(token_parents_table.c.run_id == run_id)).scalars().all()
            )
            final_state_count = (
                conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.run_id == run_id)).scalars().all()
            )
        if final_run is None or final_run.status is not RunStatus.COMPLETED:
            raise AssertionError(f"parallel sink-finalization recovery did not complete the run: {final_run!r}")
        if not final_source_records or any(record.lifecycle_state != "exhausted" for record in final_source_records.values()):
            raise AssertionError("parallel sink-finalization recovery lost source exhaustion")
        if (
            len(final_row_count),
            len(token_ids),
            len(final_parent_count),
            len(final_state_count),
            len(work_statuses),
        ) != (3, 21, 24, 24, 21):
            raise AssertionError("parallel sink-finalization recovery changed exact DAG cardinalities")
        if (
            len(outcomes) != len(token_ids)
            or set(latest_outcomes) != token_ids
            or not all(outcome.completed and outcome.outcome is not None for outcome in latest_outcomes.values())
        ):
            raise AssertionError("parallel sink-finalization recovery left non-terminal token outcomes")
        if set(work_statuses) != {"terminal"}:
            raise AssertionError(f"parallel sink-finalization recovery left non-terminal work: {work_statuses!r}")
        if reopened_checkpoint_manager.get_latest_checkpoint(run_id) is not None:
            raise AssertionError("parallel sink-finalization recovery retained its checkpoint")

        durable_records = _public_durable_records(reopened_db, run_id=run_id, payload_store=reopened_store)
        _validate_durable_sink_effect_material(durable_records)
        durable_projection = _stable_projection(durable_records, source="durable")
        portable_records = list(LandscapeExporter(reopened_db).export_run(run_id))
        _validate_portable_material_matches_durable(durable_records, portable_records)
        _validate_portable_manifest(portable_records)
        portable_projection = _stable_projection(portable_records, source="portable")
        if durable_projection != portable_projection:
            raise AssertionError("parallel sink-finalization recovery durable and portable projections differ")
        audit = _audit_evidence(portable_records, portable_projection=portable_projection)
        if audit.source_operation_count != 1:
            raise AssertionError(f"parallel sink-finalization recovery replayed its source: {audit.source_operation_count}")

        first_attempt_ids_before = tuple(sorted(str(attempt["attempt_id"]) for attempt in first_attempts_before))
        first_attempt_ids_after = tuple(sorted(str(attempt["attempt_id"]) for attempt in first_attempts_after))
        second_attempt_ids = tuple(sorted(str(attempt["attempt_id"]) for attempt in second_attempts))
        return ScenarioRunEvidence(
            schema_version=2,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=fresh_rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=fresh_rendered.settings_sha256),
            graph=fresh_built.graph_evidence,
            runtime=RuntimeEvidence(
                attempted=True,
                run_id=run_id,
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
                attempted=True,
                database_reopened=True,
                checkpoint_id=checkpoint_id,
                checkpoint_sequence=checkpoint_sequence,
                can_resume=True,
                source_replayed=False,
                checkpoint_removed=True,
                sink_finalization=ParallelSinkFinalizationRecoveryEvidence(
                    fault_seam="after_finalize_before_response",
                    fault_count=1,
                    first_sink="left",
                    second_sink="right",
                    source_exhausted_before=True,
                    completed_coalesces_before=2,
                    first_sink_rows_before=3,
                    first_effect_id_before=first_effect_id,
                    first_effect_id_after=str(first_effect_after["effect_id"]),
                    first_artifact_id_before=first_artifact_id,
                    first_artifact_id_after=str(first_artifact_after["artifact_id"]),
                    first_attempt_ids_before=first_attempt_ids_before,
                    first_attempt_ids_after=first_attempt_ids_after,
                    first_effect_unchanged=True,
                    first_artifact_unchanged=True,
                    first_attempts_unchanged=True,
                    first_sink_republished=False,
                    second_effect_absent_before=True,
                    second_artifact_absent_before=True,
                    second_attempt_count_before=0,
                    second_effect_id_after=second_effect_id,
                    second_artifact_id_after=str(second_artifact["artifact_id"]),
                    second_attempt_ids_after=second_attempt_ids,
                    final_output_rows=6,
                    durable_export_parity=True,
                    held_barrier_proven=False,
                ),
            ),
            completed_stages=("config", "build", "runtime", "audit", "recovery"),
        )
    finally:
        reopened_db.close()


def run_sink_boundary_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    *,
    before_reopen_verifier: Callable[[SinkBoundaryInterruptedContext], None] | None = None,
) -> ScenarioRunEvidence:
    """Interrupt one declared sink before publication, then reopen and resume.

    This is the common extension point for topology-specific recovery helpers.
    The optional verifier receives the still-open interrupted database after
    common invariants pass and before every initial runtime object is
    discarded. It can assert exact topology-specific pre-reopen facts without
    adding central dispatch. Manifest-driven calls use the generic default.
    """

    if case.workflow != "recovery" or case.recovery_kind != "sink_boundary" or case.recovery_fault is None:
        raise AssertionError("sink-boundary recovery requires its exact declared workflow, kind, and fault")
    if not isinstance(case.expected, SummaryRunExpectation):
        raise AssertionError("sink-boundary recovery requires a summary run expectation")

    fault = case.recovery_fault
    target_seam = SinkEffectExecutionSeam(fault.seam)
    database_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"
    initial_rendered = render_settings(case, tmp_path)
    if fault.sink_name not in initial_rendered.settings.sinks:
        raise AssertionError(f"sink-boundary recovery fault names undeclared sink {fault.sink_name!r}")
    initial_built = build_scenario(initial_rendered)
    sink_ids = {str(name): str(node_id) for name, node_id in initial_built.graph.get_sink_id_map().items()}
    sink_names_by_node_id = {node_id: name for name, node_id in sink_ids.items()}
    target_sink_node_id = sink_ids.get(fault.sink_name)
    if target_sink_node_id is None:
        raise AssertionError(f"sink-boundary recovery graph lacks declared sink {fault.sink_name!r}")

    initial_store = FilesystemPayloadStore(payload_root)
    initial_database_cell = [LandscapeDB(database_url)]

    def require_initial_database() -> LandscapeDB:
        if not initial_database_cell:
            raise AssertionError("sink-boundary recovery initial database was already discarded")
        return initial_database_cell[0]

    initial_checkpoint_manager = CheckpointManager(require_initial_database())
    initial_checkpoint_config = RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    observed_target_seams: list[SinkEffectExecutionSeam] = []
    original_coordinator_init = SinkEffectCoordinator.__init__
    corpus_lease_ttl = timedelta(seconds=3)

    def initialize_interrupted_run_coordinator(
        self: SinkEffectCoordinator,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("fault_hook") is not None:
            raise AssertionError("sink-boundary recovery initial coordinator refuses a caller-supplied fault_hook")
        kwargs.update(
            lease_ttl=corpus_lease_ttl,
            poll_interval=0.05,
            fault_hook=inject_declared_fault,
        )
        original_coordinator_init(self, *args, **kwargs)

    def inject_declared_fault(
        seam: SinkEffectExecutionSeam,
    ) -> None:
        if seam is not target_seam:
            return
        database = require_initial_database()
        with database.connection() as conn:
            active_target_effect_ids = tuple(
                str(effect_id)
                for effect_id in conn.execute(
                    select(sink_effects_table.c.effect_id).where(
                        sink_effects_table.c.state == "in_flight",
                        sink_effects_table.c.sink_node_id == target_sink_node_id,
                    )
                ).scalars()
            )
        if not active_target_effect_ids:
            return
        if len(active_target_effect_ids) != 1:
            raise AssertionError(
                f"sink-boundary recovery cannot identify one target effect at the declared seam: {active_target_effect_ids!r}"
            )
        observed_target_seams.append(seam)
        if len(observed_target_seams) == fault.occurrence:
            raise SinkEffectInjectedFault(seam)

    def initialize_resume_coordinator(
        self: SinkEffectCoordinator,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        kwargs.update(lease_ttl=corpus_lease_ttl, poll_interval=0.05)
        original_coordinator_init(self, *args, **kwargs)

    try:
        catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
        with patch.object(SinkEffectCoordinator, "__init__", new=initialize_interrupted_run_coordinator):
            try:
                Orchestrator(
                    require_initial_database(),
                    checkpoint_manager=initial_checkpoint_manager,
                    checkpoint_config=initial_checkpoint_config,
                ).run(
                    initial_built.config,
                    graph=initial_built.graph,
                    settings=initial_rendered.settings,
                    payload_store=initial_store,
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                )
            except SinkEffectInjectedFault as exc:
                if exc.seam is not target_seam:
                    raise AssertionError(f"sink-boundary recovery reached the wrong fault seam: {exc.seam.value}") from exc
            else:
                raise AssertionError("sink-boundary recovery did not inject its declared production fault")
        if tuple(observed_target_seams) != (target_seam,):
            raise AssertionError(f"sink-boundary recovery did not reach its declared sink seam exactly once: {observed_target_seams!r}")

        initial_repositories = RecorderFactory.read_only(require_initial_database(), payload_store=initial_store)
        runs = initial_repositories.run_lifecycle.list_runs()
        if len(runs) != 1 or runs[0].status is not RunStatus.FAILED:
            raise AssertionError(f"sink-boundary recovery expected one failed interrupted run, got {runs!r}")
        run_id = runs[0].run_id
        source_records = initial_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        source_names_exhausted_before = tuple(sorted(record.source_name for record in source_records.values()))
        if source_names_exhausted_before != tuple(sorted(case.input_fixtures)) or any(
            record.lifecycle_state != "exhausted" for record in source_records.values()
        ):
            raise AssertionError(f"sink-boundary recovery sources were not exactly exhausted: {source_records!r}")

        checkpoint = initial_checkpoint_manager.get_latest_checkpoint(run_id)
        if checkpoint is None or checkpoint.upstream_topology_hash != initial_built.graph_evidence.topology_hash:
            raise AssertionError("sink-boundary recovery did not retain its exact topology checkpoint")
        checkpoint_id = checkpoint.checkpoint_id
        checkpoint_sequence = checkpoint.sequence_number
        checkpoint_topology_hash = checkpoint.upstream_topology_hash
        if checkpoint_topology_hash is None:
            raise AssertionError("sink-boundary recovery checkpoint lacks a topology hash")

        with require_initial_database().connection() as conn:
            token_ids_before = tuple(
                conn.execute(
                    select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id).order_by(tokens_table.c.token_id)
                ).scalars()
            )
        token_ids_before = tuple(str(token_id) for token_id in token_ids_before)
        work_before = _sink_boundary_work_snapshot(require_initial_database(), run_id=run_id)
        before_effects, _before_attempts, before_members, before_artifacts = _sink_effect_snapshot(
            require_initial_database(),
            run_id=run_id,
        )
        effects_before = _sink_boundary_effect_projection(
            before_effects,
            before_members,
            sink_names_by_node_id=sink_names_by_node_id,
        )
        interrupted_effects = tuple(
            effect for effect in effects_before if effect.sink_node_id == target_sink_node_id and effect.state == "in_flight"
        )
        if len(interrupted_effects) != 1:
            raise AssertionError(f"sink-boundary recovery interrupted the wrong durable effect: {effects_before!r}")
        interrupted_effect = interrupted_effects[0]
        pending_token_ids = {
            item.token_id for item in work_before if item.status == "pending_sink" and item.pending_sink_name == fault.sink_name
        }
        if set(interrupted_effect.member_token_ids) != pending_token_ids:
            raise AssertionError("sink-boundary recovery effect members do not equal the declared sink's pending work")
        publication_count_before = sum(effect["publication_performed"] is True for effect in before_effects)
        interrupted_artifacts = tuple(
            artifact for artifact in before_artifacts if str(artifact["sink_effect_id"]) == interrupted_effect.effect_id
        )
        interrupted_raw_effect = next(effect for effect in before_effects if str(effect["effect_id"]) == interrupted_effect.effect_id)
        if interrupted_artifacts or interrupted_raw_effect["publication_performed"] is not None:
            raise AssertionError("sink-boundary recovery published its declared sink before the fault")
        if initial_rendered.output_paths[fault.sink_name].exists():
            raise AssertionError("sink-boundary recovery created the declared sink artifact before reopen")
        lease_expires_at = interrupted_raw_effect["lease_expires_at"]
        if not isinstance(lease_expires_at, datetime):
            raise AssertionError("sink-boundary recovery interrupted effect lacks a live lease")
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if lease_expires_at <= datetime.now(UTC):
            raise AssertionError("sink-boundary recovery interrupted effect lease expired before initial close")
        if before_reopen_verifier is not None:
            before_reopen_verifier(
                SinkBoundaryInterruptedContext(
                    database=require_initial_database(),
                    payload_store=initial_store,
                    scenario=scenario,
                    case=case,
                    rendered=initial_rendered,
                    built=initial_built,
                    run_id=run_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_sequence=checkpoint_sequence,
                    checkpoint_topology_hash=checkpoint_topology_hash,
                    source_names_exhausted=source_names_exhausted_before,
                    token_ids=token_ids_before,
                    work=work_before,
                    effects=effects_before,
                    interrupted_effect=interrupted_effect,
                )
            )
        with require_initial_database().connection() as conn:
            lease_expires_at_before_close = conn.execute(
                select(sink_effects_table.c.lease_expires_at).where(
                    sink_effects_table.c.effect_id == interrupted_effect.effect_id,
                )
            ).scalar_one()
        if not isinstance(lease_expires_at_before_close, datetime):
            raise AssertionError("sink-boundary recovery interrupted effect lost its lease before initial close")
        if lease_expires_at_before_close.tzinfo is None:
            lease_expires_at_before_close = lease_expires_at_before_close.replace(tzinfo=UTC)
        if lease_expires_at_before_close <= datetime.now(UTC):
            raise AssertionError("sink-boundary recovery verifier outlived the interrupted effect's live lease")
    finally:
        initial_database = require_initial_database()
        initial_database.close()

    initial_database_cell.clear()
    del initial_database
    del initial_repositories, initial_store, runs, source_records, checkpoint
    del initial_checkpoint_manager, initial_checkpoint_config, initial_built, initial_rendered

    reopened_db = LandscapeDB.from_url(database_url, create_tables=False)
    try:
        reopened_store = FilesystemPayloadStore(payload_root)
        reopened_checkpoint_manager = CheckpointManager(reopened_db)
        reopened_checkpoint = reopened_checkpoint_manager.get_latest_checkpoint(run_id)
        if reopened_checkpoint is None or (
            reopened_checkpoint.checkpoint_id,
            reopened_checkpoint.sequence_number,
            reopened_checkpoint.upstream_topology_hash,
        ) != (checkpoint_id, checkpoint_sequence, checkpoint_topology_hash):
            raise AssertionError("sink-boundary recovery checkpoint changed across fresh-object reopen")

        fresh_rendered = render_settings(case, tmp_path)
        fresh_built = build_scenario(fresh_rendered, purpose=SinkEffectExecutionPurpose.RESUME)
        if fresh_built.graph_evidence.topology_hash != checkpoint_topology_hash:
            raise AssertionError("sink-boundary recovery fresh graph changed checkpoint topology")
        fresh_sink_ids = {str(name): str(node_id) for name, node_id in fresh_built.graph.get_sink_id_map().items()}
        if fresh_sink_ids != sink_ids:
            raise AssertionError("sink-boundary recovery fresh graph reminted sink node identity")

        recovery = RecoveryManager(reopened_db, reopened_checkpoint_manager)
        resume_check = recovery.can_resume(run_id, fresh_built.graph)
        if not resume_check.can_resume:
            raise AssertionError(f"sink-boundary recovery was not publicly resumable: {resume_check.reason}")
        resume_point = recovery.get_resume_point(run_id, fresh_built.graph)
        if resume_point is None or resume_point.checkpoint.checkpoint_id != checkpoint_id:
            raise AssertionError("sink-boundary recovery did not use its public reopened resume point")

        fresh_checkpoint_config = RuntimeCheckpointConfig.from_settings(fresh_rendered.settings.checkpoint)
        with patch.object(SinkEffectCoordinator, "__init__", new=initialize_resume_coordinator):
            result = Orchestrator(
                reopened_db,
                checkpoint_manager=reopened_checkpoint_manager,
                checkpoint_config=fresh_checkpoint_config,
            ).resume(
                resume_point,
                fresh_built.config,
                fresh_built.graph,
                payload_store=reopened_store,
                settings=fresh_rendered.settings,
            )
        result_data = result.to_dict()
        if result.run_id != run_id or result_data["status"] != case.expected.status:
            raise AssertionError(f"sink-boundary recovery returned the wrong final result: {result_data!r}")

        sink_outputs = _sink_outputs(fresh_rendered)
        output_rows = sum(len(output.rows) for output in sink_outputs)
        if output_rows != case.expected.output_rows:
            raise AssertionError(f"sink-boundary recovery emitted {output_rows} rows, expected {case.expected.output_rows}")
        _assert_all_tokens_and_work_terminal(
            reopened_db,
            run_id=run_id,
            payload_store=reopened_store,
            expected_run_status=RunStatus(case.expected.status),
        )
        final_repositories = RecorderFactory.read_only(reopened_db, payload_store=reopened_store)
        final_source_records = final_repositories.run_lifecycle.get_run_source_lifecycle_records(run_id)
        if tuple(sorted(record.source_name for record in final_source_records.values())) != tuple(sorted(case.input_fixtures)) or any(
            record.lifecycle_state != "exhausted" for record in final_source_records.values()
        ):
            raise AssertionError("sink-boundary recovery lost exact source exhaustion")

        with reopened_db.connection() as conn:
            token_ids_after = tuple(
                str(token_id)
                for token_id in conn.execute(
                    select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id).order_by(tokens_table.c.token_id)
                ).scalars()
            )
            coordination_rows = tuple(
                conn.execute(
                    select(
                        run_coordination_events_table.c.event_type,
                        run_coordination_events_table.c.worker_id,
                        run_coordination_events_table.c.leader_epoch,
                        run_coordination_events_table.c.context_json,
                    )
                    .where(
                        run_coordination_events_table.c.run_id == run_id,
                        run_coordination_events_table.c.event_type == "leader_acquire",
                    )
                    .order_by(run_coordination_events_table.c.seq)
                ).mappings()
            )
        resume_markers = tuple(row for row in coordination_rows if json.loads(str(row["context_json"])).get("entry_point") == "resume")
        if token_ids_after != token_ids_before:
            raise AssertionError("sink-boundary recovery reminted durable token identity")
        if len(resume_markers) != 1:
            raise AssertionError(f"sink-boundary recovery requires exactly one durable resume leadership marker: {resume_markers!r}")
        resume_marker = resume_markers[0]
        resume_marker_worker_id = str(resume_marker["worker_id"])
        resume_marker_leader_epoch = resume_marker["leader_epoch"]
        if not resume_marker_worker_id or not isinstance(resume_marker_leader_epoch, int) or resume_marker_leader_epoch < 1:
            raise AssertionError(f"sink-boundary recovery persisted a corrupt resume marker: {resume_marker!r}")

        work_after = _sink_boundary_work_snapshot(reopened_db, run_id=run_id)
        after_effects, _after_attempts, after_members, after_artifacts = _sink_effect_snapshot(
            reopened_db,
            run_id=run_id,
        )
        effects_after = _sink_boundary_effect_projection(
            after_effects,
            after_members,
            sink_names_by_node_id=sink_names_by_node_id,
        )
        publication_count_after = sum(effect["publication_performed"] is True for effect in after_effects)
        if reopened_checkpoint_manager.get_latest_checkpoint(run_id) is not None:
            raise AssertionError("sink-boundary recovery retained its checkpoint after successful resume")

        durable_projection, audit = _exact_recovery_views(
            reopened_db,
            run_id=run_id,
            payload_store=reopened_store,
        )
        if audit.source_operation_count != len(case.input_fixtures):
            raise AssertionError(
                "sink-boundary recovery replayed or omitted a source load: "
                f"expected {len(case.input_fixtures)}, got {audit.source_operation_count}"
            )

        return ScenarioRunEvidence(
            schema_version=2,
            scenario_id=scenario.id,
            case_id=case.id,
            fixture_sha256=fresh_rendered.fixture_sha256,
            config=ConfigEvidence(loaded=True, settings_sha256=fresh_rendered.settings_sha256),
            graph=fresh_built.graph_evidence,
            runtime=RuntimeEvidence(
                attempted=True,
                run_id=run_id,
                status=str(result_data["status"]),
                rows_processed=result_data["rows_processed"],
                rows_succeeded=result_data["rows_succeeded"],
                rows_failed=result_data["rows_failed"],
                output_rows=output_rows,
                sink_outputs=sink_outputs,
                durable_projection=durable_projection,
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
                sink_boundary=SinkBoundaryRecoveryEvidence(
                    fault=fault,
                    fault_count=1,
                    initial_run_status="failed",
                    source_names_exhausted_before=source_names_exhausted_before,
                    checkpoint_topology_hash=checkpoint_topology_hash,
                    fresh_topology_hash=fresh_built.graph_evidence.topology_hash,
                    lease_live_before_close=True,
                    token_ids_before=token_ids_before,
                    token_ids_after=token_ids_after,
                    work_before=work_before,
                    work_after=work_after,
                    effects_before=effects_before,
                    effects_after=effects_after,
                    effect_count_before=len(effects_before),
                    effect_member_count_before=len(interrupted_effect.member_token_ids),
                    artifact_count_before=len(before_artifacts),
                    publication_count_before=publication_count_before,
                    effect_count_after=len(effects_after),
                    artifact_count_after=len(after_artifacts),
                    publication_count_after=publication_count_after,
                    resume_marker_count=1,
                    resume_marker_event_type="leader_acquire",
                    resume_marker_entry_point="resume",
                    resume_marker_worker_id=resume_marker_worker_id,
                    resume_marker_leader_epoch=resume_marker_leader_epoch,
                    durable_identity_reused=True,
                    durable_export_parity=True,
                    provisional_until_deferred_platform_rebase=True,
                ),
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
    if case.recovery_kind == "eof_aggregation":
        return _eof_aggregation_recovery_case(scenario, case, tmp_path)
    if case.recovery_kind == "expansion_child_enqueue":
        return _expansion_child_enqueue_recovery_case(scenario, case, tmp_path)
    if case.recovery_kind == "parallel_sink_finalization":
        return _parallel_sink_finalization_recovery_case(scenario, case, tmp_path)
    if case.recovery_kind == "pending_sink_redrive":
        return _pending_sink_redrive_recovery_case(scenario, case, tmp_path)
    if case.recovery_kind == "sink_boundary":
        return run_sink_boundary_recovery_case(scenario, case, tmp_path)
    if case.recovery_kind == "terminal_resume_idempotence":
        return _eof_aggregation_recovery_case(scenario, case, tmp_path)
    raise AssertionError(f"unsupported recovery kind: {case.recovery_kind!r}")
