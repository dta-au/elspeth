"""Pydantic contracts for declared and observed DAG scenario evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
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
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]

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
RecoveryKind = Literal[
    "eof_aggregation",
    "expansion_child_enqueue",
    "parallel_sink_finalization",
    "pending_sink_redrive",
    "sink_boundary",
    "terminal_resume_idempotence",
]
RecoveryFaultKind = Literal["sink_effect"]
RecoveryFaultSeam = Literal["before_effect"]
GraphNodeType = Literal["aggregation", "coalesce", "gate", "queue", "row_union", "sink", "source", "transform"]

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
    resumed_full_projection_sha256: Sha256 | None = None


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


class StableParentProjection(ClosedModel):
    ordinal: Count
    parent_key: NonEmpty


class StableTokenProjection(ClosedModel):
    key: NonEmpty
    row_key: NonEmpty
    parents: tuple[StableParentProjection, ...]
    branch_name: NonEmpty | None = None

    @field_validator("parents")
    @classmethod
    def _require_ordered_unique_parents(
        cls,
        parents: tuple[StableParentProjection, ...],
    ) -> tuple[StableParentProjection, ...]:
        parent_keys = tuple(parent.parent_key for parent in parents)
        ordinals = tuple(parent.ordinal for parent in parents)
        if len(parent_keys) != len(set(parent_keys)):
            raise ValueError("token parent keys must be unique")
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("token parent ordinals must be unique")
        if ordinals != tuple(sorted(ordinals)):
            raise ValueError("token parents must be sorted by durable ordinal")
        return parents


class SemanticTokenProjection(ClosedModel):
    """Order-insensitive runtime lineage; never an audit/recovery identity."""

    key: NonEmpty
    row_key: NonEmpty
    parent_set: tuple[NonEmpty, ...]
    branch_name: NonEmpty | None = None

    @field_validator("parent_set")
    @classmethod
    def _require_sorted_parent_set(cls, parents: tuple[str, ...]) -> tuple[str, ...]:
        if parents != tuple(sorted(set(parents))):
            raise ValueError("semantic token parent_set must be unique and sorted")
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
    error_hash: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{16}$")] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class StableIntermediateOutcomeProjection(ClosedModel):
    """One durable non-terminal BUFFERED outcome, separate from token terminality."""

    key: NonEmpty
    token_key: NonEmpty
    ordinal: Count
    path: Literal["buffered"]
    batch_key: NonEmpty

    @model_validator(mode="after")
    def _validate_stable_key(self) -> Self:
        if self.key != f"{self.token_key}|buffered|{self.ordinal:08d}":
            raise ValueError("intermediate outcome key must exactly encode token, path, and ordinal")
        return self


class StableSchedulerWorkProjection(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    node_key: NonEmpty
    transitions: tuple[NonEmpty, ...]
    final_status: Literal["ready", "leased", "blocked", "pending_sink", "terminal", "failed"]


class StableBatchMemberProjection(ClosedModel):
    ordinal: Count
    token_key: NonEmpty


class StableBatchProjection(ClosedModel):
    """One terminal aggregation batch with its immutable ordered membership."""

    key: NonEmpty
    aggregation_node_key: NonEmpty
    attempt: Count
    status: Literal["draft", "executing", "completed", "failed"]
    trigger_type: Literal["count", "timeout", "condition", "end_of_source"] | None
    trigger_reason: NonEmpty | None
    members: tuple[StableBatchMemberProjection, ...]

    @model_validator(mode="after")
    def _validate_members(self) -> Self:
        ordinals = tuple(member.ordinal for member in self.members)
        token_keys = tuple(member.token_key for member in self.members)
        if ordinals != tuple(range(len(self.members))):
            raise ValueError("batch member ordinals must be dense from zero")
        if len(token_keys) != len(set(token_keys)):
            raise ValueError("batch member token keys must be unique")
        if self.status in ("completed", "failed") and not self.members:
            raise ValueError("terminal batch projection requires immutable members")
        if self.trigger_type is None and self.trigger_reason is not None:
            raise ValueError("batch trigger_reason requires trigger_type")
        return self


class StableExpansionChildProjection(ClosedModel):
    ordinal: Count
    token_key: NonEmpty


class StableExpansionProjection(ClosedModel):
    """Stable expansion identity derived from one durable parent and child set."""

    key: NonEmpty
    parent_token_key: NonEmpty
    expected_child_count: PositiveCount
    children: tuple[StableExpansionChildProjection, ...]

    @model_validator(mode="after")
    def _validate_children(self) -> Self:
        if len(self.children) != self.expected_child_count:
            raise ValueError("expansion children must exactly match expected_child_count")
        ordinals = tuple(child.ordinal for child in self.children)
        token_keys = tuple(child.token_key for child in self.children)
        if ordinals != tuple(range(self.expected_child_count)):
            raise ValueError("expansion child ordinals must be dense from zero")
        if len(token_keys) != len(set(token_keys)):
            raise ValueError("expansion child token keys must be unique")
        return self


def _require_canonical_json_object_or_none(value: str | None, info: ValidationInfo) -> str | None:
    import json

    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{info.field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{info.field_name} must be a JSON object")
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != value:
        raise ValueError(f"{info.field_name} must use canonical JSON")
    return value


class StableValidationErrorProjection(ClosedModel):
    key: NonEmpty
    node_key: NonEmpty
    row_key: NonEmpty | None
    row_hash: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    row_data: NonEmpty | None
    error: NonEmpty
    schema_mode: Literal["fixed", "flexible", "observed", "parse"]
    destination: NonEmpty
    violation_type: NonEmpty | None
    original_field_name: NonEmpty | None
    normalized_field_name: NonEmpty | None
    expected_type: NonEmpty | None
    actual_type: NonEmpty | None

    _canonical_row_data = field_validator("row_data")(_require_canonical_json_object_or_none)


class StableTransformErrorProjection(ClosedModel):
    key: NonEmpty
    token_key: NonEmpty
    transform_node_key: NonEmpty
    row_hash: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    row_data: NonEmpty | None
    error_details: NonEmpty
    destination: NonEmpty

    _canonical_row_data = field_validator("row_data")(_require_canonical_json_object_or_none)
    _canonical_error_details = field_validator("error_details")(_require_canonical_json_object_or_none)


StableKeyedProjection = (
    StableRowProjection
    | StableTokenProjection
    | StableNodeStateProjection
    | StableRouteProjection
    | StableTerminalDisposition
    | StableIntermediateOutcomeProjection
    | StableSchedulerWorkProjection
    | StableBatchProjection
    | StableExpansionProjection
    | StableValidationErrorProjection
    | StableTransformErrorProjection
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
    values: Sequence[
        StableKeyedProjection
        | StableAuditRecordProjection
        | SemanticTokenProjection
        | TerminalNodeStateProjection
        | TerminalSchedulerWorkProjection
        | TerminalBatchProjection
    ],
) -> None:
    keys = tuple(value.key for value in values)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ValueError(f"{label} must contain unique sorted keys")


def _validate_projected_token_parent_graph(
    tokens: tuple[StableTokenProjection, ...],
    dispositions: tuple[StableTerminalDisposition, ...],
    batches: tuple[StableBatchProjection, ...] = (),
) -> None:
    parents_by_token = {token.key: tuple(parent.parent_key for parent in token.parents) for token in tokens}
    children_by_token: dict[str, list[str]] = {token.key: [] for token in tokens}
    remaining_parents = {token.key: len(token.parents) for token in tokens}
    for token in tokens:
        for parent in token.parents:
            children_by_token[parent.parent_key].append(token.key)

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
    terminal_reachable.update(
        member.token_key
        for batch in batches
        if batch.status == "completed"
        for member in batch.members
        if member.token_key in parents_by_token
    )
    if tokens and not terminal_reachable:
        raise ValueError("non-empty projection must contain a terminal success or failure outcome")
    pending = sorted(terminal_reachable, reverse=True)
    while pending:
        token_key = pending.pop()
        for parent_key in parents_by_token[token_key]:
            if parent_key not in terminal_reachable:
                terminal_reachable.add(parent_key)
                pending.append(parent_key)
    if any(token_key not in terminal_reachable for token_key in fork_parent_keys):
        raise ValueError("transient fork_parent token must reach a terminal descendant")


def _validate_completed_coalesce_lineage(
    tokens: tuple[StableTokenProjection, ...],
    node_states: tuple[StableNodeStateProjection, ...],
    dispositions: tuple[StableTerminalDisposition, ...],
    audit_records: tuple[StableAuditRecordProjection, ...],
) -> None:
    """Tie completed barrier evidence to the exact consumed-parent lineage."""
    import json

    token_by_key = {token.key: token for token in tokens}
    disposition_by_token = {disposition.token_key: disposition for disposition in dispositions}
    audit_by_key = {record.key: record for record in audit_records}
    completed_groups: dict[tuple[str, str], list[StableNodeStateProjection]] = {}
    for state in node_states:
        if state.status != "completed" or not state.node_key.startswith("coalesce:"):
            continue
        token = token_by_key[state.token_key]
        completed_groups.setdefault((state.node_key, token.row_key), []).append(state)

    claimed_merged_tokens: set[str] = set()
    for (node_key, row_key), states in completed_groups.items():
        contexts = {state.context_after for state in states}
        if None in contexts or len(contexts) != 1:
            raise ValueError("completed coalesce token states must share one exact context_after")
        context_after = next(iter(contexts))
        assert context_after is not None
        context = json.loads(context_after)
        branches_arrived = context.get("branches_arrived")
        expected_branches = context.get("expected_branches")
        arrival_order = context.get("arrival_order")
        branches_lost = context.get("branches_lost")
        if (
            not isinstance(branches_arrived, list)
            or not all(isinstance(branch, str) and branch for branch in branches_arrived)
            or len(set(branches_arrived)) != len(branches_arrived)
        ):
            raise ValueError("completed coalesce context must declare unique branches_arrived")
        if (
            not isinstance(expected_branches, list)
            or not all(isinstance(branch, str) and branch for branch in expected_branches)
            or len(set(expected_branches)) != len(expected_branches)
            or not set(branches_arrived).issubset(expected_branches)
        ):
            raise ValueError("completed coalesce context must declare valid expected_branches")
        if (
            not isinstance(arrival_order, list)
            or any(
                not isinstance(arrival, dict) or arrival.get("branch") != branch
                for arrival, branch in zip(arrival_order, branches_arrived, strict=False)
            )
            or len(arrival_order) != len(branches_arrived)
        ):
            raise ValueError("completed coalesce arrival_order must exactly match branches_arrived")
        if not isinstance(branches_lost, dict) or not set(branches_lost).issubset(set(expected_branches) - set(branches_arrived)):
            raise ValueError("completed coalesce branches_lost must be non-arrived expected branches")

        consumed_token_keys = tuple(sorted(state.token_key for state in states))
        if len(branches_arrived) != len(consumed_token_keys):
            raise ValueError("completed coalesce branches_arrived must exactly cover consumed token states")
        consumed_branch_names = tuple(token_by_key[token_key].branch_name for token_key in consumed_token_keys)
        if (
            any(branch_name is None for branch_name in consumed_branch_names)
            or len(set(consumed_branch_names)) != len(consumed_branch_names)
            or set(consumed_branch_names) != set(branches_arrived)
        ):
            raise ValueError("completed coalesce arrived branches must bind exactly to consumed upstream tokens")
        if any(
            (
                disposition_by_token[token_key].outcome,
                disposition_by_token[token_key].path,
                disposition_by_token[token_key].sink_name,
            )
            != ("success", "coalesced", None)
            for token_key in consumed_token_keys
        ):
            raise ValueError("completed coalesce consumed tokens must be internal successful coalesced dispositions")
        merged_tokens = tuple(
            token
            for token in tokens
            if token.row_key == row_key and tuple(sorted(parent.parent_key for parent in token.parents)) == consumed_token_keys
        )
        if len(merged_tokens) != 1:
            raise ValueError(f"completed coalesce {node_key} consumed token set must exactly parent one merged token")
        merged_token = merged_tokens[0]
        if merged_token.key in claimed_merged_tokens:
            raise ValueError("one merged token cannot satisfy multiple completed coalesce groups")
        claimed_merged_tokens.add(merged_token.key)
        merged_disposition = disposition_by_token[merged_token.key]
        disposition_shape = (
            merged_disposition.outcome,
            merged_disposition.path,
            merged_disposition.sink_name,
        )
        if disposition_shape[:2] != ("success", "coalesced") and disposition_shape != (
            "transient",
            "fork_parent",
            None,
        ):
            raise ValueError("completed coalesce merged token must have a successful coalesced disposition or be an internal fork parent")

        node_record = audit_by_key.get(f"node|{node_key}")
        if node_record is None or node_record.record_type != "node":
            raise ValueError("completed coalesce requires its exact stable node audit record")
        node_material = json.loads(node_record.material)
        config = node_material.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("branches"), dict):
            raise ValueError("completed coalesce node audit record requires exact branch config")
        configured_branches = config["branches"]
        if tuple(expected_branches) != tuple(sorted(configured_branches)):
            raise ValueError("completed coalesce expected_branches must exactly match node config")
        policy = context.get("policy")
        merge_strategy = context.get("merge_strategy")
        if policy != config.get("policy") or merge_strategy != config.get("merge"):
            raise ValueError("completed coalesce policy and merge strategy must exactly match node config")
        if policy == "require_all" and set(branches_arrived) != set(expected_branches):
            raise ValueError("completed require_all coalesce requires every expected branch")
        if policy == "first" and len(branches_arrived) != 1:
            raise ValueError("completed first coalesce requires exactly one arrived branch")
        if policy == "quorum":
            quorum_count = config.get("quorum_count")
            if isinstance(quorum_count, bool) or not isinstance(quorum_count, int) or len(branches_arrived) < quorum_count:
                raise ValueError("completed quorum coalesce must meet its configured quorum_count")
        if policy == "best_effort" and not branches_arrived:
            raise ValueError("completed best_effort coalesce requires at least one arrived branch")
        if policy not in ("require_all", "first", "quorum", "best_effort"):
            raise ValueError("completed coalesce has unsupported policy evidence")
        if policy in ("require_all", "quorum", "best_effort") and set(branches_lost) != set(expected_branches) - set(branches_arrived):
            raise ValueError("completed coalesce branches_lost must exactly complement arrived branches")
        if merge_strategy == "select" and config.get("select_branch") not in branches_arrived:
            raise ValueError("completed select coalesce requires its selected branch to have arrived")
        arrived_set = set(branches_arrived)
        origins = context.get("union_field_origins")
        if origins is not None and (
            not isinstance(origins, dict) or not all(isinstance(branch, str) and branch in arrived_set for branch in origins.values())
        ):
            raise ValueError("completed union provenance origins must reference arrived branches")
        collisions = context.get("union_field_collisions")
        if collisions is not None and (
            not isinstance(collisions, dict)
            or not all(
                isinstance(branches, list)
                and bool(branches)
                and all(isinstance(branch, str) and branch in arrived_set for branch in branches)
                and len(set(branches)) == len(branches)
                for branches in collisions.values()
            )
        ):
            raise ValueError("completed union collision provenance must reference unique arrived branches")
        collision_values = context.get("union_field_collision_values")
        if collision_values is not None and (
            not isinstance(collision_values, dict)
            or not all(
                isinstance(values, list)
                and bool(values)
                and all(
                    isinstance(value, list)
                    and len(value) == 2
                    and isinstance(value[0], str)
                    and value[0] in arrived_set
                    and isinstance(value[1], dict)
                    for value in values
                )
                and len({value[0] for value in values}) == len(values)
                for values in collision_values.values()
            )
        ):
            raise ValueError("completed union collision values must reference unique arrived branches")


def _derive_projected_terminal_counts(projection: StableRunProjection | SemanticRuntimeProjection) -> tuple[int, int, int]:
    """Mirror the production run-counter projection from exact token outcomes.

    Processing is counted once per source-row identity that reaches any terminal
    outcome. Success counts public terminal outcomes so one forked row may publish
    successfully to more than one sink; consumed coalesce inputs do not independently
    count as successes. Failure remains a per-terminal-token tally, matching the
    production coalesce failure accounting.
    """
    row_key_by_token = {token.key: token.row_key for token in projection.tokens}
    terminal = tuple(disposition for disposition in projection.terminal_dispositions if disposition.outcome in ("success", "failure"))
    processed_rows = {row_key_by_token[disposition.token_key] for disposition in terminal}
    processed_rows.update(row_key_by_token[member.token_key] for batch in projection.batches for member in batch.members)
    successful_terminals = sum(
        1
        for disposition in terminal
        if disposition.outcome == "success" and not (disposition.path == "coalesced" and disposition.sink_name is None)
    )
    failed_tokens = sum(disposition.outcome == "failure" for disposition in terminal)
    return len(processed_rows), successful_terminals, failed_tokens


class StableRunProjection(ClosedModel):
    rows: tuple[StableRowProjection, ...]
    tokens: tuple[StableTokenProjection, ...]
    node_states: tuple[StableNodeStateProjection, ...]
    routes: tuple[StableRouteProjection, ...]
    terminal_dispositions: tuple[StableTerminalDisposition, ...]
    intermediate_outcomes: tuple[StableIntermediateOutcomeProjection, ...] = ()
    scheduler_work: tuple[StableSchedulerWorkProjection, ...]
    batches: tuple[StableBatchProjection, ...] = ()
    expansions: tuple[StableExpansionProjection, ...] = ()
    validation_errors: tuple[StableValidationErrorProjection, ...] = ()
    transform_errors: tuple[StableTransformErrorProjection, ...] = ()
    audit_records: tuple[StableAuditRecordProjection, ...]

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        for label, values in (
            ("rows", self.rows),
            ("tokens", self.tokens),
            ("node states", self.node_states),
            ("routes", self.routes),
            ("terminal dispositions", self.terminal_dispositions),
            ("intermediate outcomes", self.intermediate_outcomes),
            ("scheduler work", self.scheduler_work),
            ("batches", self.batches),
            ("expansions", self.expansions),
            ("validation errors", self.validation_errors),
            ("transform errors", self.transform_errors),
            ("audit records", self.audit_records),
        ):
            _require_unique_sorted_keys(label, values)

        row_keys = {row.key for row in self.rows}
        token_keys = {token.key for token in self.tokens}
        if any(token.row_key not in row_keys for token in self.tokens):
            raise ValueError("every token must reference a projected row")
        if any(parent.parent_key not in token_keys or parent.parent_key == token.key for token in self.tokens for parent in token.parents):
            raise ValueError("token parents must reference distinct projected tokens")
        if any(state.token_key not in token_keys for state in self.node_states):
            raise ValueError("every node state must reference a projected token")
        if any(route.token_key not in token_keys for route in self.routes):
            raise ValueError("every route must reference a projected token")
        disposition_token_keys = tuple(disposition.token_key for disposition in self.terminal_dispositions)
        if len(disposition_token_keys) != len(token_keys) or set(disposition_token_keys) != token_keys:
            raise ValueError("terminal dispositions must exactly cover tokens one-to-one")
        _validate_projected_token_parent_graph(self.tokens, self.terminal_dispositions, self.batches)
        _validate_completed_coalesce_lineage(
            self.tokens,
            self.node_states,
            self.terminal_dispositions,
            self.audit_records,
        )
        if any(work.token_key not in token_keys for work in self.scheduler_work):
            raise ValueError("every scheduler work item must reference a projected token")
        if any(outcome.token_key not in token_keys for outcome in self.intermediate_outcomes):
            raise ValueError("intermediate outcomes must reference projected tokens")
        if any(member.token_key not in token_keys for batch in self.batches for member in batch.members):
            raise ValueError("batch members must reference projected tokens")
        batch_by_key = {batch.key: batch for batch in self.batches}
        if any(outcome.batch_key not in batch_by_key for outcome in self.intermediate_outcomes):
            raise ValueError("intermediate outcomes must reference projected batches")
        intermediate_by_token: dict[str, list[StableIntermediateOutcomeProjection]] = {}
        for outcome in self.intermediate_outcomes:
            intermediate_by_token.setdefault(outcome.token_key, []).append(outcome)
            member_keys = {member.token_key for member in batch_by_key[outcome.batch_key].members}
            if outcome.token_key not in member_keys:
                raise ValueError("intermediate outcome token must belong to its projected batch")
        if any(
            tuple(outcome.ordinal for outcome in outcomes) != tuple(range(len(outcomes))) for outcomes in intermediate_by_token.values()
        ):
            raise ValueError("intermediate outcome ordinals must be dense from zero per token")
        disposition_by_token = {disposition.token_key: disposition for disposition in self.terminal_dispositions}
        for batch in self.batches:
            if batch.status != "completed":
                continue
            if any(
                (
                    disposition_by_token[member.token_key].outcome,
                    disposition_by_token[member.token_key].path,
                    disposition_by_token[member.token_key].sink_name,
                )
                != ("transient", "batch_consumed", None)
                for member in batch.members
            ):
                raise ValueError("completed batch members must have exact transient batch_consumed dispositions")
        token_by_key = {token.key: token for token in self.tokens}
        expansion_children: set[str] = set()
        for expansion in self.expansions:
            if expansion.parent_token_key not in token_keys:
                raise ValueError("expansion parent must reference a projected token")
            parent_disposition = disposition_by_token[expansion.parent_token_key]
            if (parent_disposition.outcome, parent_disposition.path, parent_disposition.sink_name) != (
                "transient",
                "expand_parent",
                None,
            ):
                raise ValueError("expansion parent must have exact transient expand_parent disposition")
            for child in expansion.children:
                if child.token_key not in token_keys or child.token_key in expansion_children:
                    raise ValueError("expansion children must reference distinct projected tokens")
                expansion_children.add(child.token_key)
                token = token_by_key[child.token_key]
                if tuple((parent.ordinal, parent.parent_key) for parent in token.parents) != ((child.ordinal, expansion.parent_token_key),):
                    raise ValueError("expansion child lineage must exactly bind parent and ordinal")
        row_keys_with_none = row_keys | {None}
        if any(error.row_key not in row_keys_with_none for error in self.validation_errors):
            raise ValueError("validation errors must reference projected rows when row identity is present")
        if any(error.token_key not in token_keys for error in self.transform_errors):
            raise ValueError("transform errors must reference projected tokens")
        return self


class SemanticRuntimeProjection(ClosedModel):
    """Exact runtime behavior with explicitly order-insensitive parent sets.

    This projection deliberately excludes audit records and therefore cannot
    establish audit or recovery completeness. Raw ``StableRunProjection``
    values retain durable parent ordinals and sink-effect lineage identity.
    """

    rows: tuple[StableRowProjection, ...]
    tokens: tuple[SemanticTokenProjection, ...]
    node_states: tuple[StableNodeStateProjection, ...]
    routes: tuple[StableRouteProjection, ...]
    terminal_dispositions: tuple[StableTerminalDisposition, ...]
    scheduler_work: tuple[StableSchedulerWorkProjection, ...]
    intermediate_outcomes: tuple[StableIntermediateOutcomeProjection, ...] = Field(default=(), exclude_if=lambda value: not value)
    batches: tuple[StableBatchProjection, ...] = Field(default=(), exclude_if=lambda value: not value)
    expansions: tuple[StableExpansionProjection, ...] = Field(default=(), exclude_if=lambda value: not value)
    validation_errors: tuple[StableValidationErrorProjection, ...] = Field(default=(), exclude_if=lambda value: not value)
    transform_errors: tuple[StableTransformErrorProjection, ...] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        for label, values in (
            ("rows", self.rows),
            ("tokens", self.tokens),
            ("node states", self.node_states),
            ("routes", self.routes),
            ("terminal dispositions", self.terminal_dispositions),
            ("scheduler work", self.scheduler_work),
            ("intermediate outcomes", self.intermediate_outcomes),
            ("batches", self.batches),
            ("expansions", self.expansions),
            ("validation errors", self.validation_errors),
            ("transform errors", self.transform_errors),
        ):
            _require_unique_sorted_keys(label, values)

        row_keys = {row.key for row in self.rows}
        token_keys = {token.key for token in self.tokens}
        if any(token.row_key not in row_keys for token in self.tokens):
            raise ValueError("every semantic token must reference a projected row")
        if any(parent not in token_keys or parent == token.key for token in self.tokens for parent in token.parent_set):
            raise ValueError("semantic token parent_set must reference distinct projected tokens")
        disposition_token_keys = tuple(disposition.token_key for disposition in self.terminal_dispositions)
        if len(disposition_token_keys) != len(token_keys) or set(disposition_token_keys) != token_keys:
            raise ValueError("semantic terminal dispositions must exactly cover tokens one-to-one")
        semantic_as_raw = tuple(
            StableTokenProjection(
                key=token.key,
                row_key=token.row_key,
                parents=tuple(
                    StableParentProjection(ordinal=ordinal, parent_key=parent_key) for ordinal, parent_key in enumerate(token.parent_set)
                ),
                branch_name=token.branch_name,
            )
            for token in self.tokens
        )
        _validate_projected_token_parent_graph(semantic_as_raw, self.terminal_dispositions, self.batches)
        if any(state.token_key not in token_keys for state in self.node_states):
            raise ValueError("every semantic node state must reference a projected token")
        if any(route.token_key not in token_keys for route in self.routes):
            raise ValueError("every semantic route must reference a projected token")
        if any(work.token_key not in token_keys for work in self.scheduler_work):
            raise ValueError("every semantic scheduler work item must reference a projected token")
        if any(outcome.token_key not in token_keys for outcome in self.intermediate_outcomes):
            raise ValueError("semantic intermediate outcomes must reference projected tokens")
        if any(member.token_key not in token_keys for batch in self.batches for member in batch.members):
            raise ValueError("semantic batch members must reference projected tokens")
        batch_by_key = {batch.key: batch for batch in self.batches}
        if any(outcome.batch_key not in batch_by_key for outcome in self.intermediate_outcomes):
            raise ValueError("semantic intermediate outcomes must reference projected batches")
        intermediate_by_token: dict[str, list[StableIntermediateOutcomeProjection]] = {}
        for outcome in self.intermediate_outcomes:
            intermediate_by_token.setdefault(outcome.token_key, []).append(outcome)
            member_keys = {member.token_key for member in batch_by_key[outcome.batch_key].members}
            if outcome.token_key not in member_keys:
                raise ValueError("semantic intermediate outcome token must belong to its projected batch")
        if any(
            tuple(outcome.ordinal for outcome in outcomes) != tuple(range(len(outcomes))) for outcomes in intermediate_by_token.values()
        ):
            raise ValueError("semantic intermediate outcome ordinals must be dense from zero per token")
        disposition_by_token = {disposition.token_key: disposition for disposition in self.terminal_dispositions}
        for batch in self.batches:
            if batch.status != "completed":
                continue
            if any(
                (
                    disposition_by_token[member.token_key].outcome,
                    disposition_by_token[member.token_key].path,
                    disposition_by_token[member.token_key].sink_name,
                )
                != ("transient", "batch_consumed", None)
                for member in batch.members
            ):
                raise ValueError("semantic completed batch members must have exact transient batch_consumed dispositions")
        token_by_key = {token.key: token for token in self.tokens}
        expansion_children: set[str] = set()
        for expansion in self.expansions:
            if expansion.parent_token_key not in token_keys:
                raise ValueError("semantic expansion parent must reference a projected token")
            parent_disposition = disposition_by_token[expansion.parent_token_key]
            if (parent_disposition.outcome, parent_disposition.path, parent_disposition.sink_name) != (
                "transient",
                "expand_parent",
                None,
            ):
                raise ValueError("semantic expansion parent must have exact transient expand_parent disposition")
            for child in expansion.children:
                if child.token_key not in token_keys or child.token_key in expansion_children:
                    raise ValueError("semantic expansion children must reference distinct projected tokens")
                expansion_children.add(child.token_key)
                if token_by_key[child.token_key].parent_set != (expansion.parent_token_key,):
                    raise ValueError("semantic expansion child lineage must exactly bind its parent")
        row_keys_with_none = row_keys | {None}
        if any(error.row_key not in row_keys_with_none for error in self.validation_errors):
            raise ValueError("semantic validation errors must reference projected rows when row identity is present")
        if any(error.token_key not in token_keys for error in self.transform_errors):
            raise ValueError("semantic transform errors must reference projected tokens")
        return self


class TerminalNodeStateProjection(ClosedModel):
    """Final semantic node state with retry-attempt history removed."""

    key: NonEmpty
    token_key: NonEmpty
    node_key: NonEmpty
    step_index: Count
    status: Literal["completed"]
    context_after: NonEmpty | None = None

    @field_validator("context_after")
    @classmethod
    def _require_canonical_context(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _require_canonical_json_object_or_none(value, info)

    @model_validator(mode="after")
    def _validate_key(self) -> Self:
        if self.key != f"{self.token_key}|{self.node_key}|{self.step_index}":
            raise ValueError("terminal node-state key must exactly encode token, node, and step")
        return self


class TerminalSchedulerWorkProjection(ClosedModel):
    """Final semantic scheduler state with transition history removed."""

    key: NonEmpty
    token_key: NonEmpty
    node_key: NonEmpty
    final_status: Literal["terminal"]

    @model_validator(mode="after")
    def _validate_key(self) -> Self:
        if self.key != f"{self.token_key}|{self.node_key}":
            raise ValueError("terminal scheduler-work key must exactly encode token and node")
        return self


class TerminalBatchProjection(ClosedModel):
    """Completed batch semantics independent of failed/retried batch identity."""

    key: NonEmpty
    aggregation_node_key: NonEmpty
    trigger_type: Literal["count", "timeout", "condition", "end_of_source"]
    trigger_reason: NonEmpty | None
    member_token_keys: tuple[NonEmpty, ...]

    @model_validator(mode="after")
    def _validate_members(self) -> Self:
        if not self.member_token_keys or len(self.member_token_keys) != len(set(self.member_token_keys)):
            raise ValueError("terminal batch member tokens must be non-empty and unique")
        expected_key = "|".join(
            (
                self.aggregation_node_key,
                self.trigger_type,
                str(self.trigger_reason),
                *self.member_token_keys,
            )
        )
        if self.key != expected_key:
            raise ValueError("terminal batch key must exactly encode aggregation, trigger, and member sequence")
        return self


class TerminalEquivalenceProjection(ClosedModel):
    """Terminal runtime meaning, deliberately excluding retry/audit history."""

    rows: tuple[StableRowProjection, ...]
    tokens: tuple[SemanticTokenProjection, ...]
    terminal_node_states: tuple[TerminalNodeStateProjection, ...]
    routes: tuple[StableRouteProjection, ...]
    terminal_dispositions: tuple[StableTerminalDisposition, ...]
    terminal_scheduler_work: tuple[TerminalSchedulerWorkProjection, ...]
    completed_batches: tuple[TerminalBatchProjection, ...] = ()
    sink_outputs: tuple[SinkOutputProjection, ...]
    rows_processed: Count
    rows_succeeded: Count
    rows_failed: Count
    output_rows: Count

    @model_validator(mode="after")
    def _validate_terminal_projection(self) -> Self:
        for label, values in (
            ("rows", self.rows),
            ("tokens", self.tokens),
            ("terminal node states", self.terminal_node_states),
            ("routes", self.routes),
            ("terminal dispositions", self.terminal_dispositions),
            ("terminal scheduler work", self.terminal_scheduler_work),
            ("completed batches", self.completed_batches),
        ):
            _require_unique_sorted_keys(label, values)
        sink_names = tuple(output.sink_name for output in self.sink_outputs)
        if sink_names != tuple(sorted(sink_names)) or len(sink_names) != len(set(sink_names)):
            raise ValueError("terminal sink outputs must contain unique sorted sink names")

        row_keys = {row.key for row in self.rows}
        token_keys = {token.key for token in self.tokens}
        if any(token.row_key not in row_keys for token in self.tokens):
            raise ValueError("terminal tokens must reference projected rows")
        if any(parent not in token_keys or parent == token.key for token in self.tokens for parent in token.parent_set):
            raise ValueError("terminal token parent_set must reference distinct projected tokens")
        disposition_token_keys = tuple(disposition.token_key for disposition in self.terminal_dispositions)
        if len(disposition_token_keys) != len(token_keys) or set(disposition_token_keys) != token_keys:
            raise ValueError("terminal dispositions must exactly cover terminal tokens one-to-one")
        semantic_as_raw = tuple(
            StableTokenProjection(
                key=token.key,
                row_key=token.row_key,
                parents=tuple(
                    StableParentProjection(ordinal=ordinal, parent_key=parent_key) for ordinal, parent_key in enumerate(token.parent_set)
                ),
                branch_name=token.branch_name,
            )
            for token in self.tokens
        )
        _validate_projected_token_parent_graph(semantic_as_raw, self.terminal_dispositions)
        for values in (self.terminal_node_states, self.routes, self.terminal_scheduler_work):
            if any(value.token_key not in token_keys for value in values):
                raise ValueError("terminal runtime records must reference projected tokens")
        if any(member not in token_keys for batch in self.completed_batches for member in batch.member_token_keys):
            raise ValueError("terminal batch members must reference projected tokens")

        row_key_by_token = {token.key: token.row_key for token in self.tokens}
        terminal = tuple(disposition for disposition in self.terminal_dispositions if disposition.outcome in ("success", "failure"))
        processed_rows = {row_key_by_token[disposition.token_key] for disposition in terminal}
        processed_rows.update(row_key_by_token[token_key] for batch in self.completed_batches for token_key in batch.member_token_keys)
        succeeded = sum(
            1
            for disposition in terminal
            if disposition.outcome == "success" and not (disposition.path == "coalesced" and disposition.sink_name is None)
        )
        failed = sum(disposition.outcome == "failure" for disposition in terminal)
        if (self.rows_processed, self.rows_succeeded, self.rows_failed) != (len(processed_rows), succeeded, failed):
            raise ValueError("terminal counters must match projected terminal behavior")
        if self.output_rows != sum(len(output.rows) for output in self.sink_outputs):
            raise ValueError("terminal output_rows must equal exact sink output row count")
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
        projected_processed, projected_succeeded, projected_failed = _derive_projected_terminal_counts(self.projection)
        if self.rows_processed != projected_processed:
            raise ValueError("rows_processed must equal distinct projected rows with terminal outcomes")
        if self.rows_succeeded != projected_succeeded:
            raise ValueError("rows_succeeded must equal projected successful terminal publications")
        if self.rows_failed != projected_failed:
            raise ValueError("rows_failed must equal projected failed terminal dispositions")
        return self


class SemanticProjectionCounts(ClosedModel):
    rows: Count
    tokens: Count
    parent_links: Count
    node_states: Count
    routes: Count
    terminal_dispositions: Count
    scheduler_work: Count
    intermediate_outcomes: Count = Field(default=0, exclude_if=lambda value: value == 0)
    batches: Count = Field(default=0, exclude_if=lambda value: value == 0)
    batch_members: Count = Field(default=0, exclude_if=lambda value: value == 0)
    expansions: Count = Field(default=0, exclude_if=lambda value: value == 0)
    expansion_children: Count = Field(default=0, exclude_if=lambda value: value == 0)
    validation_errors: Count = Field(default=0, exclude_if=lambda value: value == 0)
    transform_errors: Count = Field(default=0, exclude_if=lambda value: value == 0)


class SemanticRunExpectation(ClosedModel):
    """Exact runtime oracle that makes no raw audit-identity claim."""

    kind: Literal["semantic_runtime"]
    status: Literal["completed", "completed_with_failures", "empty"]
    sink_outputs: tuple[SinkOutputProjection, ...]
    rows_processed: Count
    rows_succeeded: Count
    rows_failed: Count
    projection_sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    projection_counts: SemanticProjectionCounts
    audit_record_counts: tuple[AuditRecordCount, ...]
    source_operation_count: Count

    @model_validator(mode="after")
    def _validate_exact_counts(self) -> Self:
        sink_names = tuple(output.sink_name for output in self.sink_outputs)
        if sink_names != tuple(sorted(sink_names)) or len(set(sink_names)) != len(sink_names):
            raise ValueError("sink_outputs must contain unique sorted sink names")
        record_types = tuple(record.record_type for record in self.audit_record_counts)
        if record_types != tuple(sorted(record_types)) or len(set(record_types)) != len(record_types):
            raise ValueError("audit_record_counts must contain unique sorted record types")
        return self


DeclaredRunExpectation = Annotated[
    RunExpectation | SemanticRunExpectation | SummaryRunExpectation,
    Field(discriminator="kind"),
]


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


class RecoveryFaultDeclaration(ClosedModel):
    """Exact production seam used to interrupt one declared recovery case."""

    kind: Literal["sink_effect"]
    seam: Literal["before_effect"]
    sink_name: NonEmpty
    occurrence: Literal[1] = 1


def normalize_template_name(name: str) -> str:
    """Return the deterministic token component for one declared node name."""

    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not normalized:
        raise ValueError(f"node name {name!r} has no usable normalized template token")
    return normalized


class HarnessCaseSpec(ClosedModel):
    id: NonEmpty
    workflow: Workflow
    recovery_kind: RecoveryKind | None = Field(default=None, exclude_if=lambda value: value is None)
    recovery_fault: RecoveryFaultDeclaration | None = Field(default=None, exclude_if=lambda value: value is None)
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
        if isinstance(self.expected, SemanticRunExpectation) and self.workflow != "run":
            raise ValueError("semantic_runtime expectation is valid only for the run workflow")
        if self.workflow == "recovery" and self.recovery_kind is None:
            raise ValueError("recovery workflow requires recovery_kind")
        if self.workflow != "recovery" and self.recovery_kind is not None:
            raise ValueError("recovery_kind is valid only for the recovery workflow")
        if self.recovery_kind == "sink_boundary" and self.recovery_fault is None:
            raise ValueError("sink-boundary recovery requires recovery_fault")
        if self.recovery_kind != "sink_boundary" and self.recovery_fault is not None:
            raise ValueError("recovery_fault is valid only for sink-boundary recovery")
        if self.recovery_kind == "terminal_resume_idempotence":
            if not isinstance(self.expected, SummaryRunExpectation) or self.expected.resumed_full_projection_sha256 is None:
                raise ValueError("terminal-resume recovery requires a pinned full-history hash")
        elif isinstance(self.expected, SummaryRunExpectation) and self.expected.resumed_full_projection_sha256 is not None:
            raise ValueError("resumed full-history hash is valid only for terminal-resume recovery")
        if self.workflow == "build":
            if not isinstance(self.expected, BuildExpectation):
                raise ValueError("build workflow requires BuildExpectation")
        elif not isinstance(self.expected, (RunExpectation, SemanticRunExpectation, SummaryRunExpectation)):
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
    schema_version: Literal[2]
    criteria_ref: NonEmpty
    evidence: tuple[EvidenceReference, ...]
    scenarios: tuple[ScenarioSpec, ...]

    @model_validator(mode="after")
    def _semantic_runtime_cannot_claim_audit_or_recovery(self) -> Self:
        evidence_by_id = {reference.id: reference for reference in self.evidence}
        for scenario in self.scenarios:
            semantic_locators = {f"{scenario.id}:{case.id}" for case in scenario.cases if isinstance(case.expected, SemanticRunExpectation)}
            if not semantic_locators:
                continue
            semantic_evidence_ids = {reference.id for reference in evidence_by_id.values() if reference.locator in semantic_locators}
            for dimension in ("audit", "recovery"):
                cell = scenario.dimensions.get(dimension)
                if cell is not None and cell.status == "pass" and semantic_evidence_ids.intersection(cell.evidence):
                    raise ValueError(f"semantic_runtime expectation cannot satisfy an {dimension} pass")
            for reference in evidence_by_id.values():
                if reference.locator in semantic_locators and any(
                    stage not in ("config", "build", "runtime") for stage in reference.stages
                ):
                    raise ValueError("semantic_runtime harness evidence cannot claim audit or recovery stages")
        return self

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
    observed_error: ExpectedRunError | None = None

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
        if self.observed_error is not None and self.status != "failed":
            raise ValueError("observed_error requires status=failed")
        if self.observed_error is not None and self.kind != "exact":
            raise ValueError("observed_error requires kind=exact")
        if self.sink_outputs and self.durable_projection is None:
            raise ValueError("runtime sink outputs require a durable projection")
        if self.kind == "exact" and self.durable_projection is None:
            raise ValueError("exact runtime kind requires exact projection evidence")
        if self.kind != "exact" and self.durable_projection is not None:
            raise ValueError("non-exact runtime kind forbids exact projection evidence")
        if self.kind == "exact":
            assert self.durable_projection is not None
            sink_names = tuple(output.sink_name for output in self.sink_outputs)
            if sink_names != tuple(sorted(sink_names)) or len(set(sink_names)) != len(sink_names):
                raise ValueError("runtime sink outputs must contain unique sorted sink names")
            projected_processed, projected_succeeded, projected_failed = _derive_projected_terminal_counts(self.durable_projection)
            if self.rows_processed != projected_processed:
                raise ValueError("runtime rows_processed must equal distinct projected rows with terminal outcomes")
            if self.rows_succeeded != projected_succeeded:
                raise ValueError("runtime rows_succeeded must equal projected successful terminal publications")
            if self.rows_failed != projected_failed:
                raise ValueError("runtime rows_failed must equal projected failed terminal dispositions")
            if self.output_rows != sum(len(output.rows) for output in self.sink_outputs):
                raise ValueError("runtime output_rows must equal exact sink output row count")
        if not self.attempted and (self.sink_outputs or self.durable_projection is not None or self.observed_error is not None):
            raise ValueError("unattempted runtime forbids exact projection evidence")
        return self

    @model_serializer(mode="wrap")
    def _serialize_runtime(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.kind != "exact":
            for field in ("kind", "sink_outputs", "durable_projection", "observed_error"):
                data.pop(field)
        return data


class PortableExportUnavailableByPolicy(ClosedModel):
    run_status: Literal["failed"]
    exception_type: Literal["ValueError"]
    reason: Literal["Audit export requires an immutable export-terminal run"]


class AuditEvidence(ClosedModel):
    kind: Literal["unattempted", "summary", "exact", "unavailable_by_policy"] = "summary"
    attempted: StrictBool
    total_records: Count
    record_counts: tuple[AuditRecordCount, ...]
    source_operation_count: Count
    portable_projection: StableRunProjection | None = None
    portable_export_unavailable: PortableExportUnavailableByPolicy | None = None

    @model_validator(mode="before")
    @classmethod
    def _supply_discriminator(cls, data: object) -> object:
        if isinstance(data, dict) and "kind" not in data:
            data = dict(data)
            if data.get("portable_projection") is not None:
                data["kind"] = "exact"
            elif data.get("portable_export_unavailable") is not None:
                data["kind"] = "unavailable_by_policy"
            else:
                data["kind"] = "summary" if data.get("attempted") else "unattempted"
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
        if not self.attempted and (self.portable_projection is not None or self.portable_export_unavailable is not None):
            raise ValueError("unattempted audit forbids exact projection evidence")
        if self.kind == "exact" and self.portable_projection is None:
            raise ValueError("exact audit kind requires exact projection evidence")
        if self.kind != "exact" and self.portable_projection is not None:
            raise ValueError("non-exact audit kind forbids exact projection evidence")
        if self.kind == "unavailable_by_policy" and self.portable_export_unavailable is None:
            raise ValueError("unavailable_by_policy audit kind requires exact public-export refusal evidence")
        if self.kind == "unavailable_by_policy" and self.total_records == 0:
            raise ValueError("unavailable_by_policy requires non-empty durable audit evidence")
        if self.kind != "unavailable_by_policy" and self.portable_export_unavailable is not None:
            raise ValueError("only unavailable_by_policy audit kind accepts public-export refusal evidence")
        return self

    @model_serializer(mode="wrap")
    def _serialize_audit(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.kind in ("summary", "unattempted"):
            for field in ("kind", "portable_projection", "portable_export_unavailable"):
                data.pop(field)
        elif self.kind == "exact":
            data.pop("portable_export_unavailable")
        else:
            data.pop("portable_projection")
        return data


class ParallelSinkFinalizationRecoveryEvidence(ClosedModel):
    """Exact asymmetric sink-finalization proof; never a held-barrier claim."""

    fault_seam: Literal["after_finalize_before_response"]
    fault_count: Literal[1]
    first_sink: NonEmpty
    second_sink: NonEmpty
    source_exhausted_before: Literal[True]
    completed_coalesces_before: Literal[2]
    first_sink_rows_before: Literal[3]
    first_effect_id_before: NonEmpty
    first_effect_id_after: NonEmpty
    first_artifact_id_before: NonEmpty
    first_artifact_id_after: NonEmpty
    first_attempt_ids_before: tuple[NonEmpty, ...]
    first_attempt_ids_after: tuple[NonEmpty, ...]
    first_effect_unchanged: Literal[True]
    first_artifact_unchanged: Literal[True]
    first_attempts_unchanged: Literal[True]
    first_sink_republished: Literal[False]
    second_effect_absent_before: Literal[True]
    second_artifact_absent_before: Literal[True]
    second_attempt_count_before: Literal[0]
    second_effect_id_after: NonEmpty
    second_artifact_id_after: NonEmpty
    second_attempt_ids_after: tuple[NonEmpty, ...]
    final_output_rows: Literal[6]
    durable_export_parity: Literal[True]
    held_barrier_proven: Literal[False]

    @model_validator(mode="after")
    def _validate_stable_identity(self) -> Self:
        if self.first_sink == self.second_sink:
            raise ValueError("parallel sink-finalization recovery requires distinct sinks")
        if self.first_effect_id_before != self.first_effect_id_after:
            raise ValueError("first sink effect identity must be stable across resume")
        if self.first_artifact_id_before != self.first_artifact_id_after:
            raise ValueError("first sink artifact identity must be stable across resume")
        if self.first_attempt_ids_before != self.first_attempt_ids_after:
            raise ValueError("first sink attempt identities must be stable across resume")
        for label, attempt_ids in (
            ("first sink", self.first_attempt_ids_after),
            ("second sink", self.second_attempt_ids_after),
        ):
            if len(attempt_ids) != 3 or attempt_ids != tuple(sorted(set(attempt_ids))):
                raise ValueError(f"{label} recovery requires three unique sorted attempt identities")
        if self.first_effect_id_after == self.second_effect_id_after:
            raise ValueError("parallel sinks must use distinct effect identities")
        if self.first_artifact_id_after == self.second_artifact_id_after:
            raise ValueError("parallel sinks must use distinct artifact identities")
        return self


class AggregationEOFRecoveryEvidence(ClosedModel):
    """Exact immutable-batch identity across an EOF-flush public resume."""

    fault_seam: Literal["eof_flush_before_transform_result"]
    fault_count: Literal[1]
    source_exhausted_before: Literal[True]
    original_batch_id_before: NonEmpty
    original_batch_id_after: NonEmpty
    recovery_batch_id_after: NonEmpty
    member_token_ids_before: tuple[NonEmpty, ...]
    member_token_ids_after: tuple[NonEmpty, ...]
    original_batch_identity_preserved: Literal[True]
    member_identity_reused: Literal[True]
    membership_unchanged: Literal[True]
    result_token_absent_before: Literal[True]
    sink_effect_absent_before: Literal[True]
    final_batches: tuple[StableBatchProjection, ...]
    final_output_rows: Literal[1]
    final_output_json: Literal['{"count":3,"value":60}']
    durable_export_parity: Literal[True]
    provisional_until_deferred_platform_rebase: Literal[True]

    @model_validator(mode="after")
    def _validate_identity_reuse(self) -> Self:
        if self.original_batch_id_before != self.original_batch_id_after:
            raise ValueError("EOF aggregation recovery must preserve the original failed batch identity")
        if self.recovery_batch_id_after == self.original_batch_id_after:
            raise ValueError("EOF aggregation recovery must record a distinct retry batch attempt")
        if self.member_token_ids_before != self.member_token_ids_after:
            raise ValueError("EOF aggregation recovery must preserve immutable ordered batch membership")
        if len(self.member_token_ids_after) != 3 or len(set(self.member_token_ids_after)) != 3:
            raise ValueError("EOF aggregation recovery requires exactly three unique batch members")
        if tuple((batch.attempt, batch.status) for batch in self.final_batches) != ((0, "failed"), (1, "completed")):
            raise ValueError("EOF aggregation recovery requires exact failed-attempt then completed-retry batches")
        if any(len(batch.members) != 3 for batch in self.final_batches):
            raise ValueError("EOF aggregation recovery requires exact three-member membership on both attempts")
        final_member_keys = tuple(tuple(member.token_key for member in batch.members) for batch in self.final_batches)
        if final_member_keys[0] != final_member_keys[1]:
            raise ValueError("EOF aggregation recovery retry batches must reuse exact stable member identity and order")
        return self


class ArtifactByteDigest(ClosedModel):
    path: NonEmpty
    sha256: Sha256


class TerminalResumeIdempotenceEvidence(ClosedModel):
    """Exact terminal equivalence and zero-mutation refusal of a second resume."""

    fault_seam: Literal["eof_flush_before_transform_result"]
    fault_count: Literal[1]
    source_exhausted_before: Literal[True]
    resumed_run_id: NonEmpty
    control_terminal_projection: TerminalEquivalenceProjection
    resumed_terminal_projection: TerminalEquivalenceProjection
    terminal_projection_equal: Literal[True]
    fresh_object_lifetimes: Literal[4]
    resumed_full_projection_sha256: Sha256
    second_resume_error_type: Literal["NonResumableRunError"]
    second_resume_error_run_id: NonEmpty
    second_resume_error_reason: Literal["Run is terminal (status 'completed'); successful terminal runs are immutable"]
    database_sha256_before: Sha256
    database_sha256_after: Sha256
    durable_records_sha256_before: Sha256
    durable_records_sha256_after: Sha256
    portable_export_sha256_before: Sha256
    portable_export_sha256_after: Sha256
    output_tree_sha256_before: Sha256
    output_tree_sha256_after: Sha256
    artifact_digests_before: tuple[ArtifactByteDigest, ...]
    artifact_digests_after: tuple[ArtifactByteDigest, ...]
    zero_mutation: Literal[True]
    provisional_until_deferred_platform_rebase: Literal[True]

    @model_validator(mode="after")
    def _validate_equivalence_and_no_mutation(self) -> Self:
        if self.control_terminal_projection != self.resumed_terminal_projection:
            raise ValueError("control and resumed terminal projections must be exactly equal")
        if self.second_resume_error_run_id != self.resumed_run_id:
            raise ValueError("second resume refusal must identify the same completed run")
        for before, after, label in (
            (self.database_sha256_before, self.database_sha256_after, "database bytes"),
            (self.durable_records_sha256_before, self.durable_records_sha256_after, "durable records"),
            (self.portable_export_sha256_before, self.portable_export_sha256_after, "portable export"),
            (self.output_tree_sha256_before, self.output_tree_sha256_after, "output tree"),
            (self.artifact_digests_before, self.artifact_digests_after, "artifact bytes"),
        ):
            if before != after:
                raise ValueError(f"second resume refusal must preserve byte-identical {label}")
        paths = tuple(digest.path for digest in self.artifact_digests_before)
        if not paths or paths != tuple(sorted(set(paths))):
            raise ValueError("artifact byte digests must contain unique sorted paths")
        return self


class ExpansionChildEnqueueRecoveryEvidence(ClosedModel):
    """Exact expansion identity after durable child handoff and public resume."""

    fault_seam: Literal["after_source_exhausted_before_sink_flush"]
    fault_count: Literal[1]
    source_exhausted_before: Literal[True]
    parent_token_ids_before: tuple[NonEmpty, ...]
    parent_token_ids_after: tuple[NonEmpty, ...]
    child_token_ids_before: tuple[NonEmpty, ...]
    child_token_ids_after: tuple[NonEmpty, ...]
    expand_group_ids_before: tuple[NonEmpty, ...]
    expand_group_ids_after: tuple[NonEmpty, ...]
    scheduler_work_ids_before: tuple[NonEmpty, ...]
    scheduler_work_ids_after: tuple[NonEmpty, ...]
    parent_scheduler_work_ids_before: tuple[NonEmpty, ...]
    parent_scheduler_work_ids_after: tuple[NonEmpty, ...]
    child_scheduler_work_ids_before: tuple[NonEmpty, ...]
    child_scheduler_work_ids_after: tuple[NonEmpty, ...]
    parent_identity_unchanged: Literal[True]
    child_identity_unchanged: Literal[True]
    group_identity_unchanged: Literal[True]
    scheduler_identity_unchanged: Literal[True]
    pending_children_before: Literal[6]
    sink_effect_absent_before: Literal[True]
    artifact_absent_before: Literal[True]
    final_expansions: tuple[StableExpansionProjection, ...]
    final_output_rows: Literal[6]
    durable_export_parity: Literal[True]
    provisional_until_deferred_platform_rebase: Literal[True]

    @model_validator(mode="after")
    def _validate_identity_reuse(self) -> Self:
        equality_checks = (
            (self.parent_token_ids_before, self.parent_token_ids_after, "parent"),
            (self.child_token_ids_before, self.child_token_ids_after, "child"),
            (self.expand_group_ids_before, self.expand_group_ids_after, "group"),
            (self.scheduler_work_ids_before, self.scheduler_work_ids_after, "scheduler"),
            (self.parent_scheduler_work_ids_before, self.parent_scheduler_work_ids_after, "parent scheduler"),
            (self.child_scheduler_work_ids_before, self.child_scheduler_work_ids_after, "child scheduler"),
        )
        for before, after, label in equality_checks:
            if before != after:
                raise ValueError(f"expansion recovery must preserve {label} identities")
            if len(after) != len(set(after)):
                raise ValueError(f"expansion recovery requires unique {label} identities")
        if (len(self.parent_token_ids_after), len(self.child_token_ids_after), len(self.expand_group_ids_after)) != (3, 6, 3):
            raise ValueError("expansion recovery requires exactly 3 parents, 6 children, and 3 groups")
        if len(self.scheduler_work_ids_after) != 9:
            raise ValueError("expansion recovery requires exactly nine stable scheduler work identities")
        if set(self.parent_token_ids_after) & set(self.child_token_ids_after):
            raise ValueError("expansion recovery parent and child token identities must be disjoint")
        if (len(self.parent_scheduler_work_ids_after), len(self.child_scheduler_work_ids_after)) != (3, 6):
            raise ValueError("expansion recovery requires exactly 3 parent and 6 child scheduler identities")
        if set(self.parent_scheduler_work_ids_after) & set(self.child_scheduler_work_ids_after) or set(
            self.parent_scheduler_work_ids_after
        ) | set(self.child_scheduler_work_ids_after) != set(self.scheduler_work_ids_after):
            raise ValueError("expansion recovery scheduler identities must exactly partition parent and child work")
        child_counts = tuple(expansion.expected_child_count for expansion in self.final_expansions)
        if child_counts != (2, 1, 3):
            raise ValueError("expansion recovery requires exact 2/1/3 child groups")
        return self


class PendingSinkRedriveRecoveryEvidence(ClosedModel):
    """Exact TS-04/TS-06 bundle identity across expiry and public resume."""

    fault_seam: Literal["before_sink_effect_reservation"]
    fault_count: Literal[1]
    source_exhausted_before: Literal[True]
    work_item_id_before: NonEmpty
    work_item_id_claimed: NonEmpty
    work_item_id_after: NonEmpty
    token_id_before: NonEmpty
    token_id_claimed: NonEmpty
    token_id_after: NonEmpty
    row_id_before: NonEmpty
    row_id_claimed: NonEmpty
    row_id_after: NonEmpty
    row_payload_hash_before: NonEmpty
    row_payload_hash_claimed: NonEmpty
    row_payload_hash_after: NonEmpty
    pending_sink_name_before: NonEmpty
    pending_sink_name_claimed: NonEmpty
    pending_sink_name_after: NonEmpty
    pending_outcome_before: Literal["success"]
    pending_outcome_claimed: Literal["success"]
    pending_outcome_after: Literal["success"]
    pending_path_before: Literal["default_flow"]
    pending_path_claimed: Literal["default_flow"]
    pending_path_after: Literal["default_flow"]
    pending_error_hash_before: None = None
    pending_error_hash_claimed: None = None
    pending_error_hash_after: None = None
    pending_error_message_before: None = None
    pending_error_message_claimed: None = None
    pending_error_message_after: None = None
    scheduler_attempt_before: Literal[1]
    scheduler_attempt_claimed: Literal[1]
    scheduler_attempt_after: Literal[1]
    lease_owner_before: NonEmpty
    lease_cleared_before_reclaim: Literal[True]
    reclaimed_by_fresh_owner: Literal[True]
    reclaimed_lease_owner_after: NonEmpty
    expired_lease_recovery_events: Literal[1]
    recover_event_work_item_id: NonEmpty
    recover_event_token_id: NonEmpty
    recover_event_from_status: Literal["leased"]
    recover_event_to_status: Literal["pending_sink"]
    recover_event_from_attempt: Literal[1]
    recover_event_to_attempt: Literal[1]
    recover_event_from_lease_owner: NonEmpty
    recover_event_to_lease_owner: None = None
    sink_effects_before: Literal[0]
    artifacts_before: Literal[0]
    sink_effects_after: Literal[1]
    sink_effect_members_after: Literal[1]
    sink_effect_attempts_after: Literal[3]
    artifacts_after: Literal[1]
    publications_after: Literal[1]
    effect_id_after: NonEmpty
    member_effect_id_after: NonEmpty
    attempt_effect_ids_after: tuple[NonEmpty, ...]
    artifact_id_after: NonEmpty
    artifact_effect_id_after: NonEmpty
    effect_attempt_ids_after: tuple[NonEmpty, ...]
    terminal_outcome: Literal["success"]
    terminal_work_status: Literal["terminal"]
    final_output_rows: Literal[1]
    durable_export_parity: Literal[True]
    provisional_until_deferred_platform_rebase: Literal[True]

    @model_validator(mode="after")
    def _validate_exact_bundle_identity(self) -> Self:
        identity_triples = (
            (self.work_item_id_before, self.work_item_id_claimed, self.work_item_id_after, "work item"),
            (self.token_id_before, self.token_id_claimed, self.token_id_after, "token"),
            (self.row_id_before, self.row_id_claimed, self.row_id_after, "row"),
            (self.row_payload_hash_before, self.row_payload_hash_claimed, self.row_payload_hash_after, "row payload"),
            (self.pending_sink_name_before, self.pending_sink_name_claimed, self.pending_sink_name_after, "sink name"),
            (self.pending_outcome_before, self.pending_outcome_claimed, self.pending_outcome_after, "outcome"),
            (self.pending_path_before, self.pending_path_claimed, self.pending_path_after, "path"),
        )
        for before, claimed, after, label in identity_triples:
            if before != claimed or claimed != after:
                raise ValueError(f"pending-sink redrive must preserve exact {label} identity across pending, claim, and recovery")
        if self.recover_event_work_item_id != self.work_item_id_before or self.recover_event_token_id != self.token_id_before:
            raise ValueError("pending-sink recovery event must identify the exact recovered work item and token")
        if self.recover_event_from_lease_owner != self.lease_owner_before:
            raise ValueError("pending-sink recovery event must clear the exact expired lease owner")
        if self.reclaimed_lease_owner_after == self.lease_owner_before:
            raise ValueError("pending-sink redrive must be reclaimed by a fresh lease owner")
        if self.member_effect_id_after != self.effect_id_after:
            raise ValueError("pending-sink redrive member must retain the sole effect identity")
        if self.attempt_effect_ids_after != (self.effect_id_after,) * 3:
            raise ValueError("pending-sink redrive attempts must retain the sole effect identity")
        if self.artifact_effect_id_after != self.effect_id_after:
            raise ValueError("pending-sink redrive artifact must retain the sole effect identity")
        if len(self.effect_attempt_ids_after) != 3 or self.effect_attempt_ids_after != tuple(sorted(set(self.effect_attempt_ids_after))):
            raise ValueError("pending-sink redrive requires three unique sorted sink-effect attempt identities")
        return self


class SinkBoundaryWorkProjection(ClosedModel):
    """Durable scheduler identity on either side of a sink-boundary resume."""

    work_item_id: NonEmpty
    token_id: NonEmpty
    row_id: NonEmpty
    row_payload_sha256: Sha256
    row_payload_state: Literal["live", "purged"]
    row_payload_anchor_sha256: Sha256 | None
    node_id: NonEmpty | None
    attempt: Count
    status: NonEmpty
    pending_sink_name: NonEmpty | None
    pending_outcome: NonEmpty | None
    pending_path: NonEmpty | None
    pending_error_hash: NonEmpty | None
    pending_error_message: NonEmpty | None

    @model_validator(mode="after")
    def _validate_payload_lifecycle(self) -> Self:
        if self.row_payload_state == "live" and self.row_payload_anchor_sha256 is not None:
            raise ValueError("live scheduler payload cannot carry a purge anchor")
        if self.row_payload_state == "purged" and self.row_payload_anchor_sha256 is None:
            raise ValueError("purged scheduler payload requires its durable anchor")
        return self


class SinkBoundaryEffectProjection(ClosedModel):
    """Stable effect, artifact, and ordered member identity."""

    effect_id: NonEmpty
    sink_name: NonEmpty
    sink_node_id: NonEmpty
    artifact_id: NonEmpty
    state: Literal["reserved", "prepared", "in_flight", "finalized"]
    member_token_ids: tuple[NonEmpty, ...]
    member_row_ids: tuple[NonEmpty, ...]

    @model_validator(mode="after")
    def _validate_members(self) -> Self:
        if not self.member_token_ids or len(self.member_token_ids) != len(self.member_row_ids):
            raise ValueError("sink-boundary effect requires aligned non-empty token and row members")
        if len(self.member_token_ids) != len(set(self.member_token_ids)):
            raise ValueError("sink-boundary effect member token identities must be unique")
        return self


class SinkBoundaryRecoveryEvidence(ClosedModel):
    """Exact generic proof for a pre-publication sink-effect interruption."""

    fault: RecoveryFaultDeclaration
    fault_count: Literal[1]
    initial_run_status: Literal["failed"]
    source_names_exhausted_before: tuple[NonEmpty, ...]
    checkpoint_topology_hash: Sha256
    fresh_topology_hash: Sha256
    lease_live_before_close: Literal[True]
    token_ids_before: tuple[NonEmpty, ...]
    token_ids_after: tuple[NonEmpty, ...]
    work_before: tuple[SinkBoundaryWorkProjection, ...]
    work_after: tuple[SinkBoundaryWorkProjection, ...]
    effects_before: tuple[SinkBoundaryEffectProjection, ...]
    effects_after: tuple[SinkBoundaryEffectProjection, ...]
    effect_count_before: PositiveCount
    effect_member_count_before: PositiveCount
    artifact_count_before: Count
    publication_count_before: Count
    effect_count_after: PositiveCount
    artifact_count_after: PositiveCount
    publication_count_after: PositiveCount
    resume_marker_count: Literal[1]
    resume_marker_event_type: Literal["leader_acquire"]
    resume_marker_entry_point: Literal["resume"]
    resume_marker_worker_id: NonEmpty
    resume_marker_leader_epoch: PositiveCount
    durable_identity_reused: Literal[True]
    durable_export_parity: Literal[True]
    provisional_until_deferred_platform_rebase: Literal[True]

    @model_validator(mode="after")
    def _validate_exact_identity_reuse(self) -> Self:
        if self.checkpoint_topology_hash != self.fresh_topology_hash:
            raise ValueError("sink-boundary recovery fresh topology must equal the checkpoint topology")
        if not self.source_names_exhausted_before or self.source_names_exhausted_before != tuple(
            sorted(set(self.source_names_exhausted_before))
        ):
            raise ValueError("sink-boundary recovery requires unique sorted exhausted source names")
        if (
            not self.token_ids_before
            or self.token_ids_before != tuple(sorted(set(self.token_ids_before)))
            or self.token_ids_after != self.token_ids_before
        ):
            raise ValueError("sink-boundary recovery must preserve unique sorted token identities")
        if not self.work_before or tuple(item.work_item_id for item in self.work_before) != tuple(
            sorted({item.work_item_id for item in self.work_before})
        ):
            raise ValueError("sink-boundary recovery requires unique sorted scheduler work identities")
        if tuple(item.work_item_id for item in self.work_after) != tuple(item.work_item_id for item in self.work_before):
            raise ValueError("sink-boundary recovery must preserve scheduler work identities")

        def work_identity(item: SinkBoundaryWorkProjection) -> tuple[object, ...]:
            return (
                item.work_item_id,
                item.token_id,
                item.row_id,
                item.node_id,
                item.attempt,
                item.pending_sink_name,
                item.pending_outcome,
                item.pending_path,
                item.pending_error_hash,
                item.pending_error_message,
            )

        if tuple(map(work_identity, self.work_after)) != tuple(map(work_identity, self.work_before)):
            raise ValueError("sink-boundary recovery must preserve exact scheduler work material")
        if any(item.status not in {"pending_sink", "terminal"} for item in self.work_before):
            raise ValueError("sink-boundary recovery permits only pending-sink or already-terminal work before reopen")
        if not any(item.status == "pending_sink" and item.pending_sink_name == self.fault.sink_name for item in self.work_before):
            raise ValueError("sink-boundary recovery requires durable pending work for the declared sink before reopen")
        if any(item.status != "terminal" for item in self.work_after):
            raise ValueError("sink-boundary recovery requires terminal scheduler work after resume")
        for before_work, after_work in zip(self.work_before, self.work_after, strict=True):
            if before_work.status == "pending_sink":
                if before_work.row_payload_state != "live" or after_work.row_payload_state != "purged":
                    raise ValueError(
                        "sink-boundary recovery must transition pending-sink payloads from live material to a typed purge witness"
                    )
                expected_anchor = hashlib.sha256(before_work.token_id.encode()).hexdigest()
                if after_work.row_payload_anchor_sha256 != expected_anchor:
                    raise ValueError("sink-boundary recovery pending-sink purge anchor must equal the token identity hash")
            elif (
                before_work.row_payload_state != "purged"
                or after_work.row_payload_state != "purged"
                or before_work.row_payload_sha256 != after_work.row_payload_sha256
                or before_work.row_payload_anchor_sha256 != after_work.row_payload_anchor_sha256
            ):
                raise ValueError("sink-boundary recovery must preserve already-terminal scheduler purge witnesses exactly")
        if len(self.effects_before) != self.effect_count_before or len(self.effects_after) != self.effect_count_after:
            raise ValueError("sink-boundary recovery effect counts must match exact effect projections")
        before_effect_ids = tuple(effect.effect_id for effect in self.effects_before)
        after_effect_ids = tuple(effect.effect_id for effect in self.effects_after)
        if before_effect_ids != tuple(sorted(set(before_effect_ids))) or after_effect_ids != tuple(sorted(set(after_effect_ids))):
            raise ValueError("sink-boundary recovery requires unique sorted effect identities")
        interrupted = tuple(
            effect for effect in self.effects_before if effect.sink_name == self.fault.sink_name and effect.state == "in_flight"
        )
        if len(interrupted) != 1:
            raise ValueError("sink-boundary recovery must select exactly one declared in-flight sink effect")
        if len(interrupted[0].member_token_ids) != self.effect_member_count_before:
            raise ValueError("sink-boundary recovery member count must match the interrupted effect")
        effects_after = {effect.effect_id: effect for effect in self.effects_after}
        for before_effect in self.effects_before:
            after_effect = effects_after.get(before_effect.effect_id)
            if after_effect is None or after_effect.state != "finalized":
                raise ValueError("sink-boundary recovery must finalize the original effect identity")
            if (
                before_effect.sink_name,
                before_effect.sink_node_id,
                before_effect.artifact_id,
                before_effect.member_token_ids,
                before_effect.member_row_ids,
            ) != (
                after_effect.sink_name,
                after_effect.sink_node_id,
                after_effect.artifact_id,
                after_effect.member_token_ids,
                after_effect.member_row_ids,
            ):
                raise ValueError("sink-boundary recovery must preserve exact effect and member identity")
        if any(effect.state != "finalized" for effect in self.effects_after):
            raise ValueError("sink-boundary recovery requires every final sink effect to be finalized")
        if self.artifact_count_after < self.artifact_count_before or self.publication_count_after < self.publication_count_before:
            raise ValueError("sink-boundary recovery final artifact/publication counts cannot shrink")
        return self


class RecoveryEvidence(ClosedModel):
    attempted: StrictBool
    database_reopened: StrictBool
    checkpoint_id: NonEmpty | None = None
    checkpoint_sequence: Count | None = None
    can_resume: StrictBool
    source_replayed: StrictBool
    checkpoint_removed: StrictBool
    sink_finalization: ParallelSinkFinalizationRecoveryEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    aggregation_eof: AggregationEOFRecoveryEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    expansion_child_enqueue: ExpansionChildEnqueueRecoveryEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    pending_sink_redrive: PendingSinkRedriveRecoveryEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    sink_boundary: SinkBoundaryRecoveryEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    terminal_resume_idempotence: TerminalResumeIdempotenceEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_recovery_shape(self) -> Self:
        seam_proofs = (
            self.sink_finalization,
            self.aggregation_eof,
            self.expansion_child_enqueue,
            self.pending_sink_redrive,
            self.sink_boundary,
            self.terminal_resume_idempotence,
        )
        if sum(proof is not None for proof in seam_proofs) > 1:
            raise ValueError("recovery evidence permits at most one seam-specific proof")
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
            or self.sink_finalization is not None
            or self.aggregation_eof is not None
            or self.expansion_child_enqueue is not None
            or self.pending_sink_redrive is not None
            or self.sink_boundary is not None
            or self.terminal_resume_idempotence is not None
        ):
            raise ValueError("unattempted recovery forbids checkpoint identity and true result flags")
        if any(proof is not None for proof in seam_proofs) and not (
            self.database_reopened and self.can_resume and not self.source_replayed and self.checkpoint_removed
        ):
            raise ValueError("seam-specific recovery proof requires successful fresh public resume flags")
        return self


class ScenarioRunEvidence(ClosedModel):
    schema_version: Literal[2]
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
            assert self.runtime.durable_projection is not None
            projection = self.runtime.durable_projection
            expected_record_counts: dict[str, int] = {
                "row": len(projection.rows),
                "token": len(projection.tokens),
                "node_state": len(projection.node_states),
                "routing_event": len(projection.routes),
                "token_outcome": len(projection.terminal_dispositions) + len(projection.intermediate_outcomes),
                "token_parent": sum(len(token.parents) for token in projection.tokens),
                "scheduler_event": sum(len(work.transitions) for work in projection.scheduler_work),
                "batch": len(projection.batches),
                "batch_member": sum(len(batch.members) for batch in projection.batches),
                "validation_error": len(projection.validation_errors),
                "transform_error": len(projection.transform_errors),
            }
            for record in projection.audit_records:
                expected_record_counts[record.record_type] = expected_record_counts.get(record.record_type, 0) + 1
            declared_record_counts = {record.record_type: record.count for record in self.audit.record_counts}
            for record_type, count in expected_record_counts.items():
                if declared_record_counts.get(record_type, 0) != count:
                    raise ValueError(f"audit record count for {record_type} must match exact durable projection")
            source_operation_count = 0
            for record in projection.audit_records:
                if record.record_type != "operation":
                    continue
                import json

                if json.loads(record.material).get("operation_type") == "source_load":
                    source_operation_count += 1
            if self.audit.source_operation_count != source_operation_count:
                raise ValueError("audit source_operation_count must match exact durable projection")
            if self.audit.kind == "exact":
                if self.runtime.observed_error is not None:
                    raise ValueError("observed expected-error runtime requires portable export unavailable_by_policy")
                if self.runtime.durable_projection != self.audit.portable_projection:
                    raise ValueError("exact durable and portable projections must match")
            elif self.audit.kind == "unavailable_by_policy":
                if self.runtime.status != "failed" or self.runtime.observed_error is None:
                    raise ValueError("portable export unavailable_by_policy requires failed runtime with observed error")
                assert self.audit.portable_export_unavailable is not None
                if self.audit.portable_export_unavailable.run_status != self.runtime.status:
                    raise ValueError("portable export refusal status must match runtime status")
            else:
                raise ValueError("exact runtime requires exact or unavailable_by_policy audit evidence")
        elif self.audit.kind in ("exact", "unavailable_by_policy"):
            raise ValueError("exact audit evidence requires exact runtime evidence")
        return self
