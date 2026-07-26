"""Pydantic contracts for declared and observed DAG scenario evidence."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_serializer, field_validator, model_validator

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


class RunExpectation(ClosedModel):
    status: Literal["completed", "completed_with_failures", "empty"]
    output_rows: Count
    required_audit_record_types: tuple[NonEmpty, ...]


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
    node_types = tuple(item.node_type for item in node_type_counts)
    if node_types != tuple(sorted(node_types)) or len(set(node_types)) != len(node_types):
        raise ValueError("node_type_counts must contain unique node types in sorted order")
    if sum(item.count for item in node_type_counts) != node_count:
        raise ValueError("node_type_counts must sum exactly to node_count")
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


class HarnessCaseSpec(ClosedModel):
    id: NonEmpty
    workflow: Workflow
    fixture: NonEmpty
    input_fixture: NonEmpty
    expected: RunExpectation | BuildExpectation

    @model_validator(mode="after")
    def _validate_workflow_expectation(self) -> Self:
        if self.workflow == "build":
            if not isinstance(self.expected, BuildExpectation):
                raise ValueError("build workflow requires BuildExpectation")
        elif not isinstance(self.expected, RunExpectation):
            raise ValueError(f"{self.workflow} workflow requires RunExpectation")
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
    attempted: StrictBool
    run_id: NonEmpty | None = None
    status: NonEmpty | None = None
    rows_processed: Count = 0
    rows_succeeded: Count = 0
    rows_failed: Count = 0
    output_rows: Count = 0

    @model_validator(mode="after")
    def _validate_runtime_shape(self) -> Self:
        if self.attempted:
            if self.run_id is None or self.status is None:
                raise ValueError("attempted runtime requires run_id and status")
        elif (
            self.run_id is not None
            or self.status is not None
            or any(count != 0 for count in (self.rows_processed, self.rows_succeeded, self.rows_failed, self.output_rows))
        ):
            raise ValueError("unattempted runtime forbids run identity, status, and non-zero counters")
        return self


class AuditRecordCount(ClosedModel):
    record_type: NonEmpty
    count: Count


class AuditEvidence(ClosedModel):
    attempted: StrictBool
    total_records: Count
    record_counts: tuple[AuditRecordCount, ...]
    source_operation_count: Count

    @model_validator(mode="after")
    def _validate_audit_shape(self) -> Self:
        if not self.attempted and (self.total_records != 0 or self.record_counts or self.source_operation_count != 0):
            raise ValueError("unattempted audit forbids non-zero or non-empty records")
        return self


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
