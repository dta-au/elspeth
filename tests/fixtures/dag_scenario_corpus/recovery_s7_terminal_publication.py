"""Exact S7 recovery evidence at the terminal-publication sink boundary.

This helper deliberately proves only recovery after both sequential coalesces
have completed and the source is exhausted.  It does not claim a production
seam between the two coalesces.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    node_states_table,
    rows_table,
    sink_effect_attempts_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioRunEvidence,
    ScenarioSpec,
    SummaryRunExpectation,
)


@dataclass(frozen=True, slots=True)
class S7TokenIdentity:
    """Raw durable token identity and lineage grouping."""

    token_id: str
    row_id: str
    step_in_pipeline: int | None
    branch_name: str | None
    fork_group_id: str | None
    join_group_id: str | None
    expand_group_id: str | None


@dataclass(frozen=True, slots=True)
class S7ParentIdentity:
    """One exact durable parent edge, including its ordinal."""

    token_id: str
    parent_token_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class S7CoalesceStateIdentity:
    """One completed coalesce state whose raw identity must survive resume."""

    coalesce_name: str
    node_id: str
    state_id: str
    token_id: str
    step_index: int
    attempt: int
    status: str
    context_after_json: str


@dataclass(frozen=True, slots=True)
class S7CommitAttemptIdentity:
    """The exact commit INTENT interrupted at BEFORE_EFFECT."""

    attempt_id: str
    effect_id: str
    generation: int
    action: str
    call_kind: str
    request_hash: str
    state: str


@dataclass(frozen=True, slots=True)
class S7LineageSnapshot:
    """Raw lineage material compared before and after public resume."""

    row_ids: tuple[str, ...]
    token_rows: tuple[S7TokenIdentity, ...]
    parent_links: tuple[S7ParentIdentity, ...]
    coalesce_states: tuple[S7CoalesceStateIdentity, ...]


@dataclass(frozen=True, slots=True)
class S7InterruptedSnapshot(S7LineageSnapshot):
    """S7-specific durable facts while the initial database is still open."""

    source_names_exhausted: tuple[str, ...]
    checkpoint_sequence: int
    checkpoint_topology_hash: str
    coalesce_node_names: tuple[str, ...]
    work_status_counts: tuple[tuple[str, int], ...]
    effect_id: str
    effect_state: str
    effect_member_token_ids: tuple[str, ...]
    commit_attempt: S7CommitAttemptIdentity
    output_absent: bool


@dataclass(frozen=True, slots=True)
class S7TerminalPublicationRecovery:
    """Evidence packet with an explicit no-overclaim recovery ceiling."""

    evidence: ScenarioRunEvidence
    interrupted: S7InterruptedSnapshot
    resumed: S7LineageSnapshot
    resumed_commit_attempts: tuple[S7CommitAttemptIdentity, ...]
    between_merge_recovery_proven: Literal[False] = False
    recovery_promotion_ceiling: Literal["incomplete"] = "incomplete"


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssertionError(f"S7 recovery {field} must be non-empty text, got {value!r}")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _required_text(value, field="optional durable identity")


def _lineage_snapshot(
    database: LandscapeDB,
    *,
    run_id: str,
    coalesce_ids_by_name: tuple[tuple[str, str], ...],
) -> S7LineageSnapshot:
    coalesce_name_by_id = {node_id: name for name, node_id in coalesce_ids_by_name}
    with database.connection() as conn:
        row_ids = tuple(
            _required_text(row_id, field="row_id")
            for row_id in conn.execute(
                select(rows_table.c.row_id).where(rows_table.c.run_id == run_id).order_by(rows_table.c.row_id)
            ).scalars()
        )
        token_rows = tuple(
            S7TokenIdentity(
                token_id=_required_text(row["token_id"], field="token_id"),
                row_id=_required_text(row["row_id"], field="token row_id"),
                step_in_pipeline=(None if row["step_in_pipeline"] is None else int(row["step_in_pipeline"])),
                branch_name=_optional_text(row["branch_name"]),
                fork_group_id=_optional_text(row["fork_group_id"]),
                join_group_id=_optional_text(row["join_group_id"]),
                expand_group_id=_optional_text(row["expand_group_id"]),
            )
            for row in conn.execute(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.step_in_pipeline,
                    tokens_table.c.branch_name,
                    tokens_table.c.fork_group_id,
                    tokens_table.c.join_group_id,
                    tokens_table.c.expand_group_id,
                )
                .where(tokens_table.c.run_id == run_id)
                .order_by(tokens_table.c.token_id)
            ).mappings()
        )
        parent_links = tuple(
            S7ParentIdentity(
                token_id=_required_text(row["token_id"], field="parent token_id"),
                parent_token_id=_required_text(row["parent_token_id"], field="parent parent_token_id"),
                ordinal=int(row["ordinal"]),
            )
            for row in conn.execute(
                select(
                    token_parents_table.c.token_id,
                    token_parents_table.c.parent_token_id,
                    token_parents_table.c.ordinal,
                )
                .where(token_parents_table.c.run_id == run_id)
                .order_by(
                    token_parents_table.c.token_id,
                    token_parents_table.c.ordinal,
                    token_parents_table.c.parent_token_id,
                )
            ).mappings()
        )
        coalesce_states = tuple(
            S7CoalesceStateIdentity(
                coalesce_name=coalesce_name_by_id[_required_text(row["node_id"], field="coalesce node_id")],
                node_id=_required_text(row["node_id"], field="coalesce node_id"),
                state_id=_required_text(row["state_id"], field="coalesce state_id"),
                token_id=_required_text(row["token_id"], field="coalesce token_id"),
                step_index=int(row["step_index"]),
                attempt=int(row["attempt"]),
                status=_required_text(row["status"], field="coalesce status"),
                context_after_json=_required_text(row["context_after_json"], field="coalesce context_after_json"),
            )
            for row in conn.execute(
                select(
                    node_states_table.c.node_id,
                    node_states_table.c.state_id,
                    node_states_table.c.token_id,
                    node_states_table.c.step_index,
                    node_states_table.c.attempt,
                    node_states_table.c.status,
                    node_states_table.c.context_after_json,
                )
                .where(
                    node_states_table.c.run_id == run_id,
                    node_states_table.c.node_id.in_(tuple(coalesce_name_by_id)),
                )
                .order_by(
                    node_states_table.c.node_id,
                    node_states_table.c.token_id,
                    node_states_table.c.step_index,
                    node_states_table.c.attempt,
                    node_states_table.c.state_id,
                )
            ).mappings()
        )
    return S7LineageSnapshot(
        row_ids=row_ids,
        token_rows=token_rows,
        parent_links=parent_links,
        coalesce_states=coalesce_states,
    )


def _validate_s7_case(scenario: ScenarioSpec, case: HarnessCaseSpec) -> None:
    if scenario.id != "sequential-nested-fork-coalesce":
        raise AssertionError(f"S7 recovery requires its exact scenario, got {scenario.id!r}")
    if (
        case.workflow != "recovery"
        or case.recovery_kind != "sink_boundary"
        or case.recovery_fault is None
        or case.recovery_fault.seam != "before_effect"
        or case.recovery_fault.sink_name != "output"
        or not isinstance(case.expected, SummaryRunExpectation)
        or case.expected.status != "completed"
        or case.expected.output_rows != 3
    ):
        raise AssertionError("S7 recovery requires its declared output BEFORE_EFFECT terminal-publication case")


def run_s7_terminal_publication_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> S7TerminalPublicationRecovery:
    """Run the S7 terminal-publication interruption through public recovery."""

    _validate_s7_case(scenario, case)
    interrupted_snapshots: list[S7InterruptedSnapshot] = []

    def verify_interrupted_s7_state(
        context: corpus_harness.SinkBoundaryInterruptedContext,
    ) -> None:
        if (context.built.graph_evidence.node_count, context.built.graph_evidence.edge_count) != (6, 9):
            raise AssertionError("S7 recovery requires the exact six-node/nine-edge graph")
        if context.checkpoint_sequence != 0:
            raise AssertionError(f"S7 recovery requires the source-boundary checkpoint sequence 0, got {context.checkpoint_sequence}")
        coalesce_ids_by_name = tuple(
            sorted((str(name), str(node_id)) for name, node_id in context.built.graph.get_coalesce_id_map().items())
        )
        if tuple(name for name, _node_id in coalesce_ids_by_name) != ("merge_a", "merge_b"):
            raise AssertionError(f"S7 recovery requires the two sequential coalesces, got {coalesce_ids_by_name!r}")

        lineage = _lineage_snapshot(
            context.database,
            run_id=context.run_id,
            coalesce_ids_by_name=coalesce_ids_by_name,
        )
        if (len(lineage.row_ids), len(lineage.token_rows), len(lineage.parent_links)) != (3, 21, 24):
            raise AssertionError(
                "S7 recovery requires 3 rows/21 tokens/24 parents before reopen: "
                f"{len(lineage.row_ids)}/{len(lineage.token_rows)}/{len(lineage.parent_links)}"
            )
        if (
            len(lineage.coalesce_states) != 12
            or Counter(state.coalesce_name for state in lineage.coalesce_states) != {"merge_a": 6, "merge_b": 6}
            or any(state.status != "completed" for state in lineage.coalesce_states)
        ):
            raise AssertionError(f"S7 recovery did not durably complete both sequential coalesces: {lineage.coalesce_states!r}")

        work_status_counts = tuple(sorted(Counter(item.status for item in context.work).items()))
        if work_status_counts != (("pending_sink", 3), ("terminal", 18)):
            raise AssertionError(f"S7 recovery interrupted the wrong scheduler work state: {work_status_counts!r}")
        effect = context.interrupted_effect
        if (
            len(context.effects) != 1
            or effect.state != "in_flight"
            or len(effect.member_token_ids) != 3
            or len(set(effect.member_token_ids)) != 3
        ):
            raise AssertionError(f"S7 recovery requires one ordered three-member in-flight effect: {context.effects!r}")

        with context.database.connection() as conn:
            attempt_rows = tuple(
                conn.execute(
                    select(
                        sink_effect_attempts_table.c.attempt_id,
                        sink_effect_attempts_table.c.effect_id,
                        sink_effect_attempts_table.c.generation,
                        sink_effect_attempts_table.c.action,
                        sink_effect_attempts_table.c.call_kind,
                        sink_effect_attempts_table.c.request_hash,
                        sink_effect_attempts_table.c.state,
                    )
                    .where(sink_effect_attempts_table.c.effect_id == effect.effect_id)
                    .order_by(
                        sink_effect_attempts_table.c.started_at,
                        sink_effect_attempts_table.c.attempt_id,
                    )
                ).mappings()
            )
        commit_intents = tuple(row for row in attempt_rows if row["action"] == "commit" and row["state"] == "intent")
        if len(attempt_rows) != 3 or len(commit_intents) != 1:
            raise AssertionError(f"S7 recovery requires one commit INTENT among three preparation attempts: {attempt_rows!r}")
        if {(str(row["action"]), str(row["state"])) for row in attempt_rows} != {
            ("inspect", "returned"),
            ("reconcile", "returned"),
            ("commit", "intent"),
        }:
            raise AssertionError(f"S7 recovery observed unexpected pre-publication attempts: {attempt_rows!r}")
        commit_row = commit_intents[0]
        commit_attempt = S7CommitAttemptIdentity(
            attempt_id=_required_text(commit_row["attempt_id"], field="commit attempt_id"),
            effect_id=_required_text(commit_row["effect_id"], field="commit effect_id"),
            generation=int(commit_row["generation"]),
            action=_required_text(commit_row["action"], field="commit action"),
            call_kind=_required_text(commit_row["call_kind"], field="commit call_kind"),
            request_hash=_required_text(commit_row["request_hash"], field="commit request_hash"),
            state=_required_text(commit_row["state"], field="commit state"),
        )
        output_absent = not context.rendered.output_paths["output"].exists()
        if not output_absent:
            raise AssertionError("S7 recovery published its output before database reopen")
        interrupted_snapshots.append(
            S7InterruptedSnapshot(
                row_ids=lineage.row_ids,
                token_rows=lineage.token_rows,
                parent_links=lineage.parent_links,
                coalesce_states=lineage.coalesce_states,
                source_names_exhausted=context.source_names_exhausted,
                checkpoint_sequence=context.checkpoint_sequence,
                checkpoint_topology_hash=context.checkpoint_topology_hash,
                coalesce_node_names=tuple(name for name, _node_id in coalesce_ids_by_name),
                work_status_counts=work_status_counts,
                effect_id=effect.effect_id,
                effect_state=effect.state,
                effect_member_token_ids=effect.member_token_ids,
                commit_attempt=commit_attempt,
                output_absent=True,
            )
        )

    evidence = corpus_harness.run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=verify_interrupted_s7_state,
    )
    if len(interrupted_snapshots) != 1:
        raise AssertionError(f"S7 recovery requires exactly one interrupted-state snapshot, got {interrupted_snapshots!r}")
    interrupted = interrupted_snapshots[0]
    proof = evidence.recovery.sink_boundary
    if proof is None:
        raise AssertionError("S7 recovery lost its generic sink-boundary proof")
    if (
        proof.source_names_exhausted_before != ("primary",)
        or proof.checkpoint_topology_hash != interrupted.checkpoint_topology_hash
        or len(proof.token_ids_before) != 21
        or len(proof.work_before) != 21
        or len(proof.work_after) != 21
        or proof.effect_member_count_before != 3
        or proof.publication_count_before != 0
        or proof.publication_count_after != 1
        or not proof.durable_identity_reused
        or not proof.durable_export_parity
    ):
        raise AssertionError(f"S7 recovery generic proof does not match the exact terminal-publication boundary: {proof!r}")

    reopened_database = LandscapeDB.from_url(
        f"sqlite:///{tmp_path / 'audit.db'}",
        create_tables=False,
    )
    try:
        coalesce_ids_by_name = tuple(sorted({(state.coalesce_name, state.node_id) for state in interrupted.coalesce_states}))
        resumed = _lineage_snapshot(
            reopened_database,
            run_id=_required_text(evidence.runtime.run_id, field="resumed run_id"),
            coalesce_ids_by_name=coalesce_ids_by_name,
        )
        with reopened_database.connection() as conn:
            resumed_commit_attempts = tuple(
                S7CommitAttemptIdentity(
                    attempt_id=_required_text(row["attempt_id"], field="resumed commit attempt_id"),
                    effect_id=_required_text(row["effect_id"], field="resumed commit effect_id"),
                    generation=int(row["generation"]),
                    action=_required_text(row["action"], field="resumed commit action"),
                    call_kind=_required_text(row["call_kind"], field="resumed commit call_kind"),
                    request_hash=_required_text(row["request_hash"], field="resumed commit request_hash"),
                    state=_required_text(row["state"], field="resumed commit state"),
                )
                for row in conn.execute(
                    select(
                        sink_effect_attempts_table.c.attempt_id,
                        sink_effect_attempts_table.c.effect_id,
                        sink_effect_attempts_table.c.generation,
                        sink_effect_attempts_table.c.action,
                        sink_effect_attempts_table.c.call_kind,
                        sink_effect_attempts_table.c.request_hash,
                        sink_effect_attempts_table.c.state,
                    )
                    .where(
                        sink_effect_attempts_table.c.effect_id == interrupted.effect_id,
                        sink_effect_attempts_table.c.action == "commit",
                    )
                    .order_by(
                        sink_effect_attempts_table.c.started_at,
                        sink_effect_attempts_table.c.attempt_id,
                    )
                ).mappings()
            )
    finally:
        reopened_database.close()
    if resumed != S7LineageSnapshot(
        row_ids=interrupted.row_ids,
        token_rows=interrupted.token_rows,
        parent_links=interrupted.parent_links,
        coalesce_states=interrupted.coalesce_states,
    ):
        raise AssertionError("S7 recovery reminted or changed exact coalesce/lineage identities")
    if (
        len(resumed_commit_attempts) != 2
        or tuple(attempt.state for attempt in resumed_commit_attempts) != ("response_lost", "returned")
        or resumed_commit_attempts[0].attempt_id != interrupted.commit_attempt.attempt_id
        or len({attempt.attempt_id for attempt in resumed_commit_attempts}) != 2
        or any(attempt.effect_id != interrupted.effect_id for attempt in resumed_commit_attempts)
    ):
        raise AssertionError(
            "S7 recovery must retain the interrupted commit as response_lost "
            f"and return exactly one fresh commit: {resumed_commit_attempts!r}"
        )

    return S7TerminalPublicationRecovery(
        evidence=evidence,
        interrupted=interrupted,
        resumed=resumed,
        resumed_commit_attempts=resumed_commit_attempts,
    )
