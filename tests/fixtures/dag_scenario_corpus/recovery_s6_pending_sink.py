"""Topology-specific S6 evidence built on the generic sink-boundary runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame, path_branch_name
from elspeth.core.landscape.schema import (
    node_states_table,
    rows_table,
    sink_effect_attempts_table,
    sink_effects_table,
    token_lineage_frames_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.dag_scenario_corpus.harness import (
    SinkBoundaryInterruptedContext,
    run_sink_boundary_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioRunEvidence,
    ScenarioSpec,
)

S6_PENDING_SINK_CASE_ID = "require-all-nested-reopen-after-coalesce"
_SCENARIO_ID = "fork-coalesce-policies"


@dataclass(frozen=True, slots=True)
class S6PendingSinkRecoveryProof:
    """Exact pre-reopen S6 facts paired with generic public recovery evidence."""

    evidence: ScenarioRunEvidence
    checkpoint_sequence_before: int
    checkpoint_topology_hash_before: str
    row_ids_before: tuple[str, ...]
    token_ids_before: tuple[str, ...]
    parent_links_before: tuple[tuple[str, str, int], ...]
    completed_coalesce_state_ids_before: tuple[str, ...]
    merged_token_id_before: str
    effect_id_before: str
    artifact_id_before: str
    effect_generation_before: int
    attempts_before: tuple[tuple[int, str, str], ...]
    output_absent_before: bool


@dataclass(frozen=True, slots=True)
class _InterruptedS6Facts:
    checkpoint_sequence: int
    checkpoint_topology_hash: str
    row_ids: tuple[str, ...]
    token_ids: tuple[str, ...]
    parent_links: tuple[tuple[str, str, int], ...]
    completed_coalesce_state_ids: tuple[str, ...]
    merged_token_id: str
    effect_id: str
    artifact_id: str
    effect_generation: int
    attempts: tuple[tuple[int, str, str], ...]
    output_absent: bool


def build_s6_pending_sink_case() -> tuple[ScenarioSpec, HarnessCaseSpec]:
    """Return the maintained S6 recovery declaration."""

    return next(
        (scenario, case)
        for scenario, case in iter_harness_cases(load_manifest())
        if (scenario.id, case.id)
        == (
            _SCENARIO_ID,
            S6_PENDING_SINK_CASE_ID,
        )
    )


def _capture_interrupted_s6_state(
    context: SinkBoundaryInterruptedContext,
) -> _InterruptedS6Facts:
    if (context.scenario.id, context.case.id) != (
        _SCENARIO_ID,
        S6_PENDING_SINK_CASE_ID,
    ):
        raise AssertionError("S6 pending-sink verifier received the wrong corpus case")
    if context.checkpoint_sequence != 0:
        raise AssertionError(f"S6 pending-sink recovery requires checkpoint sequence 0, got {context.checkpoint_sequence}")
    if context.checkpoint_topology_hash != context.built.graph_evidence.topology_hash:
        raise AssertionError("S6 pending-sink checkpoint does not bind the built topology")

    coalesce_ids = {str(name): str(node_id) for name, node_id in context.built.graph.get_coalesce_id_map().items()}
    sink_ids = {str(name): str(node_id) for name, node_id in context.built.graph.get_sink_id_map().items()}
    if tuple(coalesce_ids) != ("merge_paths",) or tuple(sink_ids) != ("output",):
        raise AssertionError(f"S6 pending-sink graph identity changed: coalesces={coalesce_ids!r}, sinks={sink_ids!r}")
    coalesce_id = coalesce_ids["merge_paths"]
    terminal_work_node_ids = {
        str(node_id)
        for node_id in (
            *context.built.graph.get_config_gate_id_map().values(),
            *context.built.graph.get_transform_id_map().values(),
        )
    }
    if len(terminal_work_node_ids) != 4:
        raise AssertionError(f"S6 pending-sink graph must expose one gate and three transforms: {terminal_work_node_ids!r}")

    with context.database.connection() as conn:
        row_ids = tuple(
            str(row_id)
            for row_id in conn.execute(
                select(rows_table.c.row_id).where(rows_table.c.run_id == context.run_id).order_by(rows_table.c.row_id)
            ).scalars()
        )
        tokens = tuple(
            conn.execute(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.join_group_id,
                )
                .where(tokens_table.c.run_id == context.run_id)
                .order_by(tokens_table.c.token_id)
            ).mappings()
        )
        parent_links = tuple(
            (
                str(row["token_id"]),
                str(row["parent_token_id"]),
                int(row["ordinal"]),
            )
            for row in conn.execute(
                select(
                    token_parents_table.c.token_id,
                    token_parents_table.c.parent_token_id,
                    token_parents_table.c.ordinal,
                )
                .where(token_parents_table.c.run_id == context.run_id)
                .order_by(
                    token_parents_table.c.token_id,
                    token_parents_table.c.ordinal,
                    token_parents_table.c.parent_token_id,
                )
            ).mappings()
        )
        coalesce_states = tuple(
            conn.execute(
                select(
                    node_states_table.c.state_id,
                    node_states_table.c.token_id,
                    node_states_table.c.attempt,
                    node_states_table.c.status,
                    node_states_table.c.context_after_json,
                    node_states_table.c.resume_checkpoint_id,
                )
                .where(
                    node_states_table.c.run_id == context.run_id,
                    node_states_table.c.node_id == coalesce_id,
                )
                .order_by(node_states_table.c.state_id)
            ).mappings()
        )
        raw_effect = (
            conn.execute(
                select(
                    sink_effects_table.c.effect_id,
                    sink_effects_table.c.artifact_id,
                    sink_effects_table.c.sink_node_id,
                    sink_effects_table.c.state,
                    sink_effects_table.c.generation,
                    sink_effects_table.c.publication_performed,
                ).where(
                    sink_effects_table.c.run_id == context.run_id,
                    sink_effects_table.c.effect_id == context.interrupted_effect.effect_id,
                )
            )
            .mappings()
            .one()
        )
        raw_attempts = tuple(
            conn.execute(
                select(
                    sink_effect_attempts_table.c.generation,
                    sink_effect_attempts_table.c.action,
                    sink_effect_attempts_table.c.state,
                )
                .where(sink_effect_attempts_table.c.effect_id == context.interrupted_effect.effect_id)
                .order_by(
                    sink_effect_attempts_table.c.started_at,
                    sink_effect_attempts_table.c.attempt_id,
                )
            ).mappings()
        )

    token_ids = tuple(str(token["token_id"]) for token in tokens)
    if (len(row_ids), len(token_ids), len(parent_links)) != (1, 5, 6):
        raise AssertionError(
            "S6 pending-sink recovery requires 1 row/5 tokens/6 parent links "
            f"before reopen, got {len(row_ids)}/{len(token_ids)}/{len(parent_links)}"
        )
    if set(context.token_ids) != set(token_ids):
        raise AssertionError("S6 pending-sink generic token inventory differs from the direct table")
    if {str(token["row_id"]) for token in tokens} != set(row_ids):
        raise AssertionError("S6 pending-sink tokens do not all bind the sole source row")

    parents_by_token: dict[str, list[tuple[str, int]]] = {}
    for token_id, parent_token_id, ordinal in parent_links:
        parents_by_token.setdefault(token_id, []).append((parent_token_id, ordinal))
    merged_candidates = tuple(token_id for token_id, parents in parents_by_token.items() if len(parents) == 3)
    if len(merged_candidates) != 1:
        raise AssertionError(f"S6 pending-sink recovery cannot identify one three-parent merged token: {parent_links!r}")
    merged_token_id = merged_candidates[0]
    merged_token = next(token for token in tokens if str(token["token_id"]) == merged_token_id)
    with context.database.connection() as conn:
        merged_token_frames = tuple(
            conn.execute(
                select(
                    token_lineage_frames_table.c.kind,
                    token_lineage_frames_table.c.group_id,
                    token_lineage_frames_table.c.member_key,
                )
                .where(
                    token_lineage_frames_table.c.run_id == context.run_id,
                    token_lineage_frames_table.c.token_id == merged_token_id,
                )
                .order_by(token_lineage_frames_table.c.depth)
            ).mappings()
        )
    merged_token_lineage_path = tuple(
        LineageFrame(kind=FrameKind(row["kind"]), group_id=str(row["group_id"]), member_key=str(row["member_key"]))
        for row in merged_token_frames
    )
    if path_branch_name(merged_token_lineage_path) is not None or merged_token["join_group_id"] is None:
        raise AssertionError("S6 pending-sink merged token lacks its exact join identity")
    if context.interrupted_effect.member_token_ids != (merged_token_id,):
        raise AssertionError("S6 pending-sink effect must contain only the completed coalesce token")

    if len(coalesce_states) != 3:
        raise AssertionError(f"S6 pending-sink recovery requires one completed state per consumed branch: {coalesce_states!r}")
    coalesce_contexts = {str(state["context_after_json"]) for state in coalesce_states}
    if len(coalesce_contexts) != 1:
        raise AssertionError(f"S6 completed coalesce states do not share exact context: {coalesce_states!r}")
    coalesce_context_raw = next(iter(coalesce_contexts))
    if not isinstance(coalesce_context_raw, str):
        raise AssertionError("S6 pending-sink completed coalesce lacks durable context")
    coalesce_context = json.loads(coalesce_context_raw)
    if (
        any(state["status"] != "completed" for state in coalesce_states)
        or any(state["attempt"] != 0 for state in coalesce_states)
        or any(state["resume_checkpoint_id"] is not None for state in coalesce_states)
        or coalesce_context.get("policy") != "require_all"
        or coalesce_context.get("merge_strategy") != "nested"
        or set(coalesce_context.get("branches_arrived", ())) != {"path_a", "path_b", "path_c"}
        or coalesce_context.get("branches_lost") != {}
    ):
        raise AssertionError(f"S6 pending-sink recovery did not durably complete the exact coalesce: {coalesce_states!r}")

    pending_work = tuple(work for work in context.work if work.status == "pending_sink")
    terminal_work = tuple(work for work in context.work if work.status == "terminal")
    if (
        len(context.work) != 5
        or len(pending_work) != 1
        or len(terminal_work) != 4
        or pending_work[0].token_id != merged_token_id
        or pending_work[0].pending_sink_name != "output"
        or pending_work[0].pending_outcome != "success"
        or pending_work[0].pending_path != "coalesced"
        or pending_work[0].node_id is not None
        or pending_work[0].row_payload_state != "live"
        or pending_work[0].pending_error_hash is not None
        or pending_work[0].pending_error_message is not None
        or any(work.row_payload_state != "purged" for work in terminal_work)
        or {work.node_id for work in terminal_work} != terminal_work_node_ids
        or any(
            work.pending_sink_name is not None
            or work.pending_outcome is not None
            or work.pending_path is not None
            or work.pending_error_hash is not None
            or work.pending_error_message is not None
            for work in terminal_work
        )
        or any(work.attempt != 1 for work in context.work)
        or {work.token_id for work in context.work} != set(token_ids)
    ):
        raise AssertionError(f"S6 pending-sink recovery has the wrong exact durable work state: {context.work!r}")

    attempts = tuple((int(attempt["generation"]), str(attempt["action"]), str(attempt["state"])) for attempt in raw_attempts)
    if attempts != (
        (1, "inspect", "returned"),
        (2, "reconcile", "returned"),
        (2, "commit", "intent"),
    ):
        raise AssertionError(f"S6 pending-sink recovery has the wrong pre-reopen attempts: {attempts!r}")
    if (
        str(raw_effect["effect_id"]) != context.interrupted_effect.effect_id
        or str(raw_effect["artifact_id"]) != context.interrupted_effect.artifact_id
        or str(raw_effect["sink_node_id"]) != sink_ids["output"]
        or raw_effect["state"] != "in_flight"
        or raw_effect["generation"] != 2
        or raw_effect["publication_performed"] is not None
    ):
        raise AssertionError(f"S6 pending-sink recovery has the wrong in-flight effect: {raw_effect!r}")
    output_absent = not context.rendered.output_paths["output"].exists()
    if not output_absent:
        raise AssertionError("S6 pending-sink output exists before public resume")

    return _InterruptedS6Facts(
        checkpoint_sequence=context.checkpoint_sequence,
        checkpoint_topology_hash=context.checkpoint_topology_hash,
        row_ids=row_ids,
        token_ids=tuple(sorted(token_ids)),
        parent_links=parent_links,
        completed_coalesce_state_ids=tuple(sorted(str(state["state_id"]) for state in coalesce_states)),
        merged_token_id=merged_token_id,
        effect_id=str(raw_effect["effect_id"]),
        artifact_id=str(raw_effect["artifact_id"]),
        effect_generation=int(raw_effect["generation"]),
        attempts=attempts,
        output_absent=output_absent,
    )


def run_s6_pending_sink_recovery(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> S6PendingSinkRecoveryProof:
    """Run the exact S6 branch through the shared fresh-object recovery path."""

    interrupted: list[_InterruptedS6Facts] = []

    def capture(context: SinkBoundaryInterruptedContext) -> None:
        interrupted.append(_capture_interrupted_s6_state(context))

    evidence = run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=capture,
    )
    if len(interrupted) != 1:
        raise AssertionError(f"S6 pending-sink verifier ran {len(interrupted)} times instead of once")
    facts = interrupted[0]
    return S6PendingSinkRecoveryProof(
        evidence=evidence,
        checkpoint_sequence_before=facts.checkpoint_sequence,
        checkpoint_topology_hash_before=facts.checkpoint_topology_hash,
        row_ids_before=facts.row_ids,
        token_ids_before=facts.token_ids,
        parent_links_before=facts.parent_links,
        completed_coalesce_state_ids_before=facts.completed_coalesce_state_ids,
        merged_token_id_before=facts.merged_token_id,
        effect_id_before=facts.effect_id,
        artifact_id_before=facts.artifact_id,
        effect_generation_before=facts.effect_generation,
        attempts_before=facts.attempts,
        output_absent_before=facts.output_absent,
    )
