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
RecoveryKind = Literal["eof_aggregation", "parallel_sink_finalization"]
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
    values: tuple[StableKeyedProjection | StableAuditRecordProjection | SemanticTokenProjection, ...],
) -> None:
    keys = tuple(value.key for value in values)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ValueError(f"{label} must contain unique sorted keys")


def _validate_projected_token_parent_graph(
    tokens: tuple[StableTokenProjection, ...],
    dispositions: tuple[StableTerminalDisposition, ...],
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
        if any(parent.parent_key not in token_keys or parent.parent_key == token.key for token in self.tokens for parent in token.parents):
            raise ValueError("token parents must reference distinct projected tokens")
        if any(state.token_key not in token_keys for state in self.node_states):
            raise ValueError("every node state must reference a projected token")
        if any(route.token_key not in token_keys for route in self.routes):
            raise ValueError("every route must reference a projected token")
        disposition_token_keys = tuple(disposition.token_key for disposition in self.terminal_dispositions)
        if len(disposition_token_keys) != len(token_keys) or set(disposition_token_keys) != token_keys:
            raise ValueError("terminal dispositions must exactly cover tokens one-to-one")
        _validate_projected_token_parent_graph(self.tokens, self.terminal_dispositions)
        _validate_completed_coalesce_lineage(
            self.tokens,
            self.node_states,
            self.terminal_dispositions,
            self.audit_records,
        )
        if any(work.token_key not in token_keys for work in self.scheduler_work):
            raise ValueError("every scheduler work item must reference a projected token")
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

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        for label, values in (
            ("rows", self.rows),
            ("tokens", self.tokens),
            ("node states", self.node_states),
            ("routes", self.routes),
            ("terminal dispositions", self.terminal_dispositions),
            ("scheduler work", self.scheduler_work),
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
        _validate_projected_token_parent_graph(semantic_as_raw, self.terminal_dispositions)
        if any(state.token_key not in token_keys for state in self.node_states):
            raise ValueError("every semantic node state must reference a projected token")
        if any(route.token_key not in token_keys for route in self.routes):
            raise ValueError("every semantic route must reference a projected token")
        if any(work.token_key not in token_keys for work in self.scheduler_work):
            raise ValueError("every semantic scheduler work item must reference a projected token")
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
            or self.sink_finalization is not None
        ):
            raise ValueError("unattempted recovery forbids checkpoint identity and true result flags")
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
                "token_outcome": len(projection.terminal_dispositions),
                "token_parent": sum(len(token.parents) for token in projection.tokens),
                "scheduler_event": sum(len(work.transitions) for work in projection.scheduler_work),
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
