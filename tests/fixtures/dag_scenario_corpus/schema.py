"""Pydantic contracts for declared and observed DAG scenario evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StringConstraints,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

NonEmpty = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
IssueId = Annotated[str, StringConstraints(strict=True, pattern=r"^elspeth-[0-9a-f]{10}$")]
Count = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, ge=1)]

CellStatus = Literal["pass", "partial", "fail", "unknown", "not_applicable"]
Dimension = Literal[
    "config",
    "build",
    "contracts",
    "runtime",
    "audit",
    "recovery",
    "concurrency",
    "freeform",
    "guided",
    "round_trip",
    "scale",
]
EvidenceKind = Literal["harness", "pytest", "document", "decision"]
Stage = Literal["config", "build", "runtime", "audit", "recovery"]
Workflow = Literal["run", "recovery", "build"]
GraphNodeType = Literal["aggregation", "coalesce", "gate", "queue", "sink", "source", "transform"]

EXPECTED_DIMENSIONS: tuple[Dimension, ...] = (
    "config",
    "build",
    "contracts",
    "runtime",
    "audit",
    "recovery",
    "concurrency",
    "freeform",
    "guided",
    "round_trip",
    "scale",
)

EXPECTED_SCENARIOS: tuple[tuple[str, str], ...] = (
    ("linear", "Linear source → transform → sink"),
    ("multiple-independent-sources", "Multiple independent sources"),
    ("multi-source-queue-fan-in", "Multi-source queue fan-in"),
    ("conditional-routing", "Conditional routing, including missing and error destinations"),
    ("fork-multiple-terminals-partial-failure", "Fork to multiple terminals with partial failure"),
    ("fork-coalesce-policies", "Fork and coalesce across every completion policy and merge strategy"),
    ("sequential-nested-fork-coalesce", "Sequential or nested forks and coalesces"),
    ("parallel-coalesces", "Parallel coalesces"),
    ("aggregation-immutable-batch", "Aggregation, batch closure, and immutable membership"),
    ("row-expansion-parent-child-recovery", "Row expansion with parent/child identity and recovery"),
    ("row-union-interleave", "Row union or interleave, whether supported or consistently rejected"),
    ("retry-quarantine-discard-routed-errors", "Retry, quarantine, discard, and routed error handling"),
    ("sink-write-pending-redrive", "Sink write and pending-sink redrive"),
    ("checkpoint-deterministic-resume", "Checkpoint and deterministic resume"),
    (
        "multi-worker-lease-reclaim-late-completion",
        "Multi-worker execution, lease expiry, reclaim, and late completion",
    ),
)


class ClosedModel(BaseModel):
    """Immutable model whose declared fields are the complete contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReference(ClosedModel):
    id: NonEmpty
    kind: EvidenceKind
    locator: NonEmpty
    claim: NonEmpty
    stages: tuple[Stage, ...] = ()

    @property
    def executable(self) -> bool:
        return self.kind in ("harness", "pytest")


class EvidenceCell(ClosedModel):
    status: CellStatus
    evidence: tuple[NonEmpty, ...] = ()
    reason: NonEmpty | None = None
    owner_issue: IssueId | None = None
    exit_gate: NonEmpty | None = None

    @model_validator(mode="after")
    def _validate_status_shape(self) -> Self:
        if self.status == "pass":
            if not self.evidence:
                raise ValueError("pass status requires non-empty evidence")
            if self.reason is not None or self.owner_issue is not None or self.exit_gate is not None:
                raise ValueError("pass status forbids reason, owner_issue, and exit_gate")
        elif self.status in ("partial", "fail", "unknown"):
            if self.reason is None or self.owner_issue is None or self.exit_gate is None:
                raise ValueError("partial, fail, and unknown statuses require reason, owner_issue, and exit_gate")
        else:
            if self.reason is None:
                raise ValueError("not_applicable status requires reason")
            if self.evidence or self.owner_issue is not None or self.exit_gate is not None:
                raise ValueError("not_applicable status forbids evidence, owner_issue, and exit_gate")
        return self


class SummaryRunExpectation(ClosedModel):
    kind: Literal["summary"]
    status: Literal["completed", "completed_with_failures", "empty"]
    output_rows: Count
    required_audit_record_types: tuple[NonEmpty, ...]


class AuditRecordCount(ClosedModel):
    record_type: NonEmpty
    count: Count


class SinkOutputProjection(ClosedModel):
    sink_name: NonEmpty
    rows: tuple[NonEmpty, ...]

    @field_validator("rows")
    @classmethod
    def _require_canonical_json_rows(cls, rows: tuple[str, ...]) -> tuple[str, ...]:
        import json

        for row in rows:
            try:
                parsed = json.loads(row)
            except json.JSONDecodeError as exc:
                raise ValueError("sink output rows must be valid JSON") from exc
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            if canonical != row:
                raise ValueError("sink output rows must use canonical JSON")
        return rows


class StableRowProjection(ClosedModel):
    key: NonEmpty
    source_name: NonEmpty
    source_row_index: Count
    ingest_sequence: Count
    source_data_hash: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class StableTokenProjection(ClosedModel):
    key: NonEmpty
    row_key: NonEmpty
    parents: tuple[NonEmpty, ...]

    @field_validator("parents")
    @classmethod
    def _require_sorted_parents(cls, parents: tuple[str, ...]) -> tuple[str, ...]:
        if parents != tuple(sorted(set(parents))):
            raise ValueError("token parents must be unique and sorted")
        return parents


class StableNodeStateProjection(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    node_key: NonEmpty
    step_index: Count
    attempt: Count
    status: Literal["open", "pending", "completed", "failed"]
    context_after: NonEmpty | None = None
    error: NonEmpty | None = None

    @field_validator("context_after", "error")
    @classmethod
    def _require_canonical_json_object(cls, value: str | None, info: ValidationInfo) -> str | None:
        import json

        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{info.field_name} must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{info.field_name} must be a JSON object")
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        if canonical != value:
            raise ValueError(f"{info.field_name} must use canonical JSON")
        return value


class StableRouteProjection(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    from_node_key: NonEmpty
    to_node_key: NonEmpty
    label: NonEmpty
    mode: Literal["move", "copy", "divert"]
    ordinal: Count


class StableTerminalDisposition(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    outcome: Literal["success", "failure", "transient"]
    path: Literal[
        "default_flow",
        "gate_routed",
        "gate_discarded",
        "on_error_routed",
        "filter_dropped",
        "coalesced",
        "unrouted",
        "quarantined_at_source",
        "sink_fallback_to_failsink",
        "sink_discarded",
        "fork_parent",
        "expand_parent",
        "batch_consumed",
    ]
    sink_name: NonEmpty | None


class StableSchedulerWorkProjection(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    node_key: NonEmpty
    transitions: tuple[NonEmpty, ...]
    final_status: Literal["ready", "leased", "blocked", "pending_sink", "terminal", "failed"]


StableKeyedProjection = (
    StableRowProjection
    | StableTokenProjection
    | StableNodeStateProjection
    | StableRouteProjection
    | StableTerminalDisposition
    | StableSchedulerWorkProjection
)


StableAuditRecordType = Literal[
    "artifact",
    "call",
    "edge",
    "manifest",
    "node",
    "operation",
    "run",
    "sink_effect",
    "sink_effect_attempt",
    "sink_effect_member",
    "sink_effect_stream",
]


class StableAuditRecordProjection(ClosedModel):
    """Exact stable material and relationships for one claimed audit record."""

    key: NonEmpty
    record_type: StableAuditRecordType
    material: NonEmpty
    references: tuple[NonEmpty, ...] = ()

    @field_validator("material")
    @classmethod
    def _require_canonical_json_material(cls, material: str) -> str:
        import json

        try:
            parsed = json.loads(material)
        except json.JSONDecodeError as exc:
            raise ValueError("stable audit material must be valid JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("stable audit material must be a non-empty JSON object")
        if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != material:
            raise ValueError("stable audit material must use canonical JSON")
        return material

    @field_validator("references")
    @classmethod
    def _require_sorted_references(cls, references: tuple[str, ...]) -> tuple[str, ...]:
        if references != tuple(sorted(set(references))):
            raise ValueError("stable audit references must be unique and sorted")
        return references


def _require_unique_sorted_keys(
    label: str,
    values: tuple[StableKeyedProjection | StableAuditRecordProjection, ...],
) -> None:
    keys = tuple(value.key for value in values)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ValueError(f"{label} must contain unique sorted keys")


def _validate_projected_token_parent_graph(
    tokens: tuple[StableTokenProjection, ...],
    dispositions: tuple[StableTerminalDisposition, ...],
) -> None:
    parents_by_token = {token.key: token.parents for token in tokens}
    children_by_token: dict[str, list[str]] = {token.key: [] for token in tokens}
    remaining_parents = {token.key: len(token.parents) for token in tokens}
    for token in tokens:
        for parent in token.parents:
            children_by_token[parent].append(token.key)

    ready = [token.key for token in tokens if remaining_parents[token.key] == 0]
    visited = 0
    for token_key in ready:
        visited += 1
        for child in children_by_token[token_key]:
            remaining_parents[child] -= 1
            if remaining_parents[child] == 0:
                ready.append(child)
    if visited != len(tokens):
        raise ValueError("projected token parent graph must be acyclic")

    fork_parent_keys = tuple(
        disposition.token_key for disposition in dispositions if disposition.outcome == "transient" and disposition.path == "fork_parent"
    )
    if any(not children_by_token[token_key] for token_key in fork_parent_keys):
        raise ValueError("transient fork_parent token must parent a projected child token")

    terminal_reachable = {disposition.token_key for disposition in dispositions if disposition.outcome in ("success", "failure")}
    if tokens and not terminal_reachable:
        raise ValueError("non-empty projection must contain a terminal success or failure outcome")
    pending = sorted(terminal_reachable, reverse=True)
    while pending:
        token_key = pending.pop()
        for parent in parents_by_token[token_key]:
            if parent not in terminal_reachable:
                terminal_reachable.add(parent)
                pending.append(parent)
    if any(token_key not in terminal_reachable for token_key in fork_parent_keys):
        raise ValueError("transient fork_parent token must reach a terminal descendant")


class StableRunProjection(ClosedModel):
    rows: tuple[StableRowProjection, ...]
    tokens: tuple[StableTokenProjection, ...]
    node_states: tuple[StableNodeStateProjection, ...]
    routes: tuple[StableRouteProjection, ...]
    terminal_dispositions: tuple[StableTerminalDisposition, ...]
    scheduler_work: tuple[StableSchedulerWorkProjection, ...]
    audit_records: tuple[StableAuditRecordProjection, ...]

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        for label, values in (
            ("rows", self.rows),
            ("tokens", self.tokens),
            ("node states", self.node_states),
            ("routes", self.routes),
            ("terminal dispositions", self.terminal_dispositions),
            ("scheduler work", self.scheduler_work),
            ("audit records", self.audit_records),
        ):
            _require_unique_sorted_keys(label, values)

        row_keys = {row.key for row in self.rows}
        token_keys = {token.key for token in self.tokens}
        if any(token.row_key not in row_keys for token in self.tokens):
            raise ValueError("every token must reference a projected row")
        if any(parent not in token_keys or parent == token.key for token in self.tokens for parent in token.parents):
            raise ValueError("token parents must reference distinct projected tokens")
        if any(state.token_key not in token_keys for state in self.node_states):
            raise ValueError("every node state must reference a projected token")
        if any(route.token_key not in token_keys for route in self.routes):
            raise ValueError("every route must reference a projected token")
        if {disposition.token_key for disposition in self.terminal_dispositions} != token_keys:
            raise ValueError("terminal dispositions must exactly cover tokens")
        _validate_projected_token_parent_graph(self.tokens, self.terminal_dispositions)
        if any(work.token_key not in token_keys for work in self.scheduler_work):
            raise ValueError("every scheduler work item must reference a projected token")
        return self


class ExpectedRunError(ClosedModel):
    exception_type: Literal["CoalesceCollisionError"]


class RunExpectation(ClosedModel):
    kind: Literal["exact"]
    status: Literal["completed", "completed_with_failures", "empty", "failed"]
    expected_error: ExpectedRunError | None = None
    sink_outputs: tuple[SinkOutputProjection, ...]
    rows_processed: Count
    rows_succeeded: Count
    rows_failed: Count
    projection: StableRunProjection
    audit_record_counts: tuple[AuditRecordCount, ...]
    source_operation_count: Count

    @model_validator(mode="after")
    def _validate_exact_counts(self) -> Self:
        if self.expected_error is not None and self.status != "failed":
            raise ValueError("expected_error requires status=failed")
        sink_names = tuple(output.sink_name for output in self.sink_outputs)
        if sink_names != tuple(sorted(sink_names)) or len(set(sink_names)) != len(sink_names):
            raise ValueError("sink_outputs must contain unique sorted sink names")
        record_types = tuple(record.record_type for record in self.audit_record_counts)
        if record_types != tuple(sorted(record_types)) or len(set(record_types)) != len(record_types):
            raise ValueError("audit_record_counts must contain unique sorted record types")
        if self.rows_processed != len(self.projection.rows):
            raise ValueError("rows_processed must equal projected row count")
        return self


DeclaredRunExpectation = Annotated[RunExpectation | SummaryRunExpectation, Field(discriminator="kind")]


class GraphNodeTypeCount(ClosedModel):
    node_type: GraphNodeType
    count: PositiveCount


def _validate_exact_graph_shape(
    *,
    node_count: int,
    edge_count: int,
    node_type_counts: tuple[GraphNodeTypeCount, ...],
    edge_labels: tuple[str, ...],
) -> None:
    if node_count <= 0 or edge_count <= 0:
        raise ValueError("accepted graph shape requires positive node_count and edge_count")
    node_types = tuple(item.node_type for item in node_type_counts)
    if node_types != tuple(sorted(node_types)) or len(set(node_types)) != len(node_types):
        raise ValueError("node_type_counts must contain unique node types in sorted order")
    if sum(item.count for item in node_type_counts) != node_count:
        raise ValueError("node_type_counts must sum exactly to node_count")
    if not {"source", "sink"}.issubset(node_types):
        raise ValueError("accepted graph shape requires at least one source and one sink")
    if edge_labels != tuple(sorted(edge_labels)):
        raise ValueError("edge_labels must be sorted")
    if len(edge_labels) != edge_count:
        raise ValueError("edge_labels must contain exactly edge_count labels")


class BuildExpectation(ClosedModel):
    node_count: Count
    edge_count: Count
    node_type_counts: tuple[GraphNodeTypeCount, ...]
    edge_labels: tuple[NonEmpty, ...]

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        _validate_exact_graph_shape(
            node_count=self.node_count,
            edge_count=self.edge_count,
            node_type_counts=self.node_type_counts,
            edge_labels=self.edge_labels,
        )
        return self


class OutputArtifactExpectation(ClosedModel):
    filename: NonEmpty
    presence: Literal["required", "absent"] = "required"

    @field_validator("filename")
    @classmethod
    def _require_safe_relative_leaf(cls, filename: str) -> str:
        path = Path(filename)
        if path.is_absolute() or len(path.parts) != 1 or path.name != filename or filename in (".", ".."):
            raise ValueError(f"output artifact must be a safe relative leaf filename: {filename!r}")
        return filename


def normalize_template_name(name: str) -> str:
    """Return the deterministic token component for one declared node name."""

    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not normalized:
        raise ValueError(f"node name {name!r} has no usable normalized template token")
    return normalized


class HarnessCaseSpec(ClosedModel):
    id: NonEmpty
    workflow: Workflow
    fixture: NonEmpty
    input_fixtures: Mapping[NonEmpty, NonEmpty]
    output_artifacts: Mapping[NonEmpty, OutputArtifactExpectation]
    expected: DeclaredRunExpectation | BuildExpectation

    @field_validator("input_fixtures")
    @classmethod
    def _freeze_input_fixtures(cls, fixtures: Mapping[str, str]) -> Mapping[str, str]:
        items = tuple(fixtures.items())
        if not items:
            raise ValueError("input_fixtures must not be empty")
        if items != tuple(sorted(items)):
            raise ValueError("input_fixtures must be sorted by source name")
        if len({path for _name, path in items}) != len(items):
            raise ValueError("input_fixtures must use distinct fixture paths")
        return MappingProxyType(dict(items))

    @field_serializer("input_fixtures")
    def _serialize_input_fixtures(self, fixtures: Mapping[str, str]) -> dict[str, str]:
        return dict(fixtures)

    @field_validator("output_artifacts", mode="before")
    @classmethod
    def _normalize_output_artifacts(cls, artifacts: object) -> dict[str, OutputArtifactExpectation]:
        if not isinstance(artifacts, Mapping):
            raise ValueError("output_artifacts must be a mapping")
        items = tuple(artifacts.items())
        if not items:
            raise ValueError("output_artifacts must not be empty")
        if items != tuple(sorted(items)):
            raise ValueError("output_artifacts must be sorted by sink name")
        normalized = {
            str(name): OutputArtifactExpectation.model_validate(
                {"filename": value, "presence": "required"} if isinstance(value, str) else value
            )
            for name, value in items
        }
        if len({artifact.filename for artifact in normalized.values()}) != len(items):
            raise ValueError("output_artifacts must use unique filenames")
        return normalized

    @field_validator("output_artifacts")
    @classmethod
    def _freeze_output_artifacts(
        cls,
        artifacts: Mapping[str, OutputArtifactExpectation],
    ) -> Mapping[str, OutputArtifactExpectation]:
        return MappingProxyType(dict(artifacts))

    @field_serializer("output_artifacts")
    def _serialize_output_artifacts(
        self,
        artifacts: Mapping[str, OutputArtifactExpectation],
    ) -> dict[str, dict[str, str]]:
        return {name: {"filename": artifact.filename, "presence": artifact.presence} for name, artifact in artifacts.items()}

    @model_validator(mode="after")
    def _validate_workflow_expectation(self) -> Self:
        if self.workflow == "build":
            if not isinstance(self.expected, BuildExpectation):
                raise ValueError("build workflow requires BuildExpectation")
        elif not isinstance(self.expected, (RunExpectation, SummaryRunExpectation)):
            raise ValueError(f"{self.workflow} workflow requires a run expectation")

        input_tokens = {normalize_template_name(name) for name in self.input_fixtures}
        if len(input_tokens) != len(self.input_fixtures):
            raise ValueError("normalized template token collision between input source names")
        output_tokens = {normalize_template_name(name) for name in self.output_artifacts}
        if len(output_tokens) != len(self.output_artifacts):
            raise ValueError("normalized template token collision between output sink names")
        if input_tokens & output_tokens:
            raise ValueError("input/output template token collision between source and sink names")
        return self


class ScenarioSpec(ClosedModel):
    id: NonEmpty
    ordinal: Annotated[int, Field(strict=True, ge=1, le=15)]
    title: NonEmpty
    cases: tuple[HarnessCaseSpec, ...] = ()
    dimensions: Mapping[Dimension, EvidenceCell]

    @field_validator("dimensions")
    @classmethod
    def _freeze_dimensions(cls, dimensions: Mapping[Dimension, EvidenceCell]) -> Mapping[Dimension, EvidenceCell]:
        return MappingProxyType(dict(dimensions))

    @field_serializer("dimensions")
    def _serialize_dimensions(self, dimensions: Mapping[Dimension, EvidenceCell]) -> dict[Dimension, EvidenceCell]:
        return dict(dimensions)


class ScenarioManifest(ClosedModel):
    schema_version: Literal[1]
    criteria_ref: NonEmpty
    evidence: tuple[EvidenceReference, ...]
    scenarios: tuple[ScenarioSpec, ...]

    @property
    def verdict(self) -> Literal["complete", "not_complete"]:
        if all(cell.status in ("pass", "not_applicable") for scenario in self.scenarios for cell in scenario.dimensions.values()):
            return "complete"
        return "not_complete"


class ConfigEvidence(ClosedModel):
    loaded: StrictBool
    settings_sha256: NonEmpty


class GraphEvidence(ClosedModel):
    accepted: StrictBool
    node_count: Count | None = None
    edge_count: Count | None = None
    node_type_counts: tuple[GraphNodeTypeCount, ...] | None = None
    edge_labels: tuple[NonEmpty, ...] | None = None
    topology_hash: NonEmpty | None = None
    rejection_type: NonEmpty | None = None
    rejection_message: NonEmpty | None = None

    @model_validator(mode="after")
    def _validate_graph_shape(self) -> Self:
        graph_facts = (self.node_count, self.edge_count, self.node_type_counts, self.edge_labels, self.topology_hash)
        rejection_facts = (self.rejection_type, self.rejection_message)
        if self.accepted:
            if (
                self.node_count is None
                or self.edge_count is None
                or self.node_type_counts is None
                or self.edge_labels is None
                or self.topology_hash is None
                or any(value is not None for value in rejection_facts)
            ):
                raise ValueError("accepted graph requires all graph facts and forbids rejection facts")
            _validate_exact_graph_shape(
                node_count=self.node_count,
                edge_count=self.edge_count,
                node_type_counts=self.node_type_counts,
                edge_labels=self.edge_labels,
            )
        elif any(value is not None for value in graph_facts) or any(value is None for value in rejection_facts):
            raise ValueError("rejected graph requires both rejection facts and forbids graph facts")
        return self


class RuntimeEvidence(ClosedModel):
    kind: Literal["unattempted", "summary", "exact"] = "summary"
    attempted: StrictBool
    run_id: NonEmpty | None = None
    status: NonEmpty | None = None
    rows_processed: Count = 0
    rows_succeeded: Count = 0
    rows_failed: Count = 0
    output_rows: Count = 0
    sink_outputs: tuple[SinkOutputProjection, ...] = ()
    durable_projection: StableRunProjection | None = None

    @model_validator(mode="before")
    @classmethod
    def _supply_discriminator(cls, data: object) -> object:
        if isinstance(data, dict) and "kind" not in data:
            data = dict(data)
            data["kind"] = "exact" if data.get("durable_projection") is not None else "summary" if data.get("attempted") else "unattempted"
        return data

    @model_validator(mode="after")
    def _validate_runtime_shape(self) -> Self:
        if self.kind == "unattempted" and self.attempted:
            raise ValueError("unattempted runtime kind requires attempted=false")
        if self.kind != "unattempted" and not self.attempted:
            raise ValueError("attempted runtime kind requires attempted=true")
        if self.attempted:
            if self.run_id is None or self.status is None:
                raise ValueError("attempted runtime requires run_id and status")
        elif (
            self.run_id is not None
            or self.status is not None
            or any(count != 0 for count in (self.rows_processed, self.rows_succeeded, self.rows_failed, self.output_rows))
        ):
            raise ValueError("unattempted runtime forbids run identity, status, and non-zero counters")
        if (self.durable_projection is None) != (not self.sink_outputs):
            raise ValueError("runtime sink outputs and durable projection must be declared together")
        if self.kind == "exact" and self.durable_projection is None:
            raise ValueError("exact runtime kind requires exact projection evidence")
        if self.kind != "exact" and self.durable_projection is not None:
            raise ValueError("non-exact runtime kind forbids exact projection evidence")
        if self.kind == "exact":
            assert self.durable_projection is not None
            sink_names = tuple(output.sink_name for output in self.sink_outputs)
            if sink_names != tuple(sorted(sink_names)) or len(set(sink_names)) != len(sink_names):
                raise ValueError("runtime sink outputs must contain unique sorted sink names")
            if self.rows_processed != len(self.durable_projection.rows):
                raise ValueError("runtime rows_processed must equal projected row count")
            if self.output_rows != sum(len(output.rows) for output in self.sink_outputs):
                raise ValueError("runtime output_rows must equal exact sink output row count")
        if not self.attempted and (self.sink_outputs or self.durable_projection is not None):
            raise ValueError("unattempted runtime forbids exact projection evidence")
        return self

    @model_serializer(mode="wrap")
    def _serialize_runtime(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.kind != "exact":
            for field in ("kind", "sink_outputs", "durable_projection"):
                data.pop(field)
        return data


class AuditEvidence(ClosedModel):
    kind: Literal["unattempted", "summary", "exact"] = "summary"
    attempted: StrictBool
    total_records: Count
    record_counts: tuple[AuditRecordCount, ...]
    source_operation_count: Count
    portable_projection: StableRunProjection | None = None

    @model_validator(mode="before")
    @classmethod
    def _supply_discriminator(cls, data: object) -> object:
        if isinstance(data, dict) and "kind" not in data:
            data = dict(data)
            data["kind"] = "exact" if data.get("portable_projection") is not None else "summary" if data.get("attempted") else "unattempted"
        return data

    @model_validator(mode="after")
    def _validate_audit_shape(self) -> Self:
        if self.kind == "unattempted" and self.attempted:
            raise ValueError("unattempted audit kind requires attempted=false")
        if self.kind != "unattempted" and not self.attempted:
            raise ValueError("attempted audit kind requires attempted=true")
        if not self.attempted and (self.total_records != 0 or self.record_counts or self.source_operation_count != 0):
            raise ValueError("unattempted audit forbids non-zero or non-empty records")
        record_types = tuple(record.record_type for record in self.record_counts)
        if record_types != tuple(sorted(record_types)) or len(set(record_types)) != len(record_types):
            raise ValueError("audit record counts must contain unique sorted record types")
        if self.total_records != sum(record.count for record in self.record_counts):
            raise ValueError("audit total_records must equal the sum of record counts")
        if not self.attempted and self.portable_projection is not None:
            raise ValueError("unattempted audit forbids exact projection evidence")
        if self.kind == "exact" and self.portable_projection is None:
            raise ValueError("exact audit kind requires exact projection evidence")
        if self.kind != "exact" and self.portable_projection is not None:
            raise ValueError("non-exact audit kind forbids exact projection evidence")
        return self

    @model_serializer(mode="wrap")
    def _serialize_audit(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.kind != "exact":
            for field in ("kind", "portable_projection"):
                data.pop(field)
        return data


class RecoveryEvidence(ClosedModel):
    attempted: StrictBool
    database_reopened: StrictBool
    checkpoint_id: NonEmpty | None = None
    checkpoint_sequence: Count | None = None
    can_resume: StrictBool
    source_replayed: StrictBool
    checkpoint_removed: StrictBool

    @model_validator(mode="after")
    def _validate_recovery_shape(self) -> Self:
        if self.attempted:
            if self.checkpoint_id is None or self.checkpoint_sequence is None:
                raise ValueError("attempted recovery requires checkpoint identity")
        elif (
            self.checkpoint_id is not None
            or self.checkpoint_sequence is not None
            or self.database_reopened
            or self.can_resume
            or self.source_replayed
            or self.checkpoint_removed
        ):
            raise ValueError("unattempted recovery forbids checkpoint identity and true result flags")
        return self


class ScenarioRunEvidence(ClosedModel):
    schema_version: Literal[1]
    scenario_id: NonEmpty
    case_id: NonEmpty
    fixture_sha256: NonEmpty
    config: ConfigEvidence
    graph: GraphEvidence
    runtime: RuntimeEvidence
    audit: AuditEvidence
    recovery: RecoveryEvidence
    completed_stages: tuple[Stage, ...]

    @model_validator(mode="after")
    def _validate_exact_views(self) -> Self:
        if self.runtime.kind == "exact":
            if self.audit.kind != "exact":
                raise ValueError("exact runtime requires exact audit evidence")
            if self.runtime.durable_projection != self.audit.portable_projection:
                raise ValueError("exact durable and portable projections must match")
        elif self.audit.kind == "exact":
            raise ValueError("exact audit evidence requires exact runtime evidence")
        return self
