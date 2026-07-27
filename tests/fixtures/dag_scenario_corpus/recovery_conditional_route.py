"""Scenario-local recovery proof for conditional routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select

from elspeth.contracts.hashing import stable_hash
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    artifacts_table,
    checkpoints_table,
    edges_table,
    node_states_table,
    routing_events_table,
    rows_table,
    sink_effect_attempts_table,
    sink_effects_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioRunEvidence,
    ScenarioSpec,
)

RouteFacts = tuple[tuple[int, str, str, str], ...]
DispositionFacts = tuple[tuple[int, str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ConditionalRouteInterruptedFacts:
    source_names_exhausted: tuple[str, ...]
    sink_declaration_order: tuple[str, ...]
    topology_shape: tuple[int, int, tuple[tuple[str, int], ...], tuple[str, ...]]
    routes_by_source_row: RouteFacts
    dispositions_by_source_row: DispositionFacts
    work_by_source_row: tuple[tuple[int, str, str, str], ...]
    sink_effect_order: tuple[str, ...]
    accepted_attempts: tuple[tuple[str, str], ...]
    accepted_commit_attempt_id: str
    artifact_count: int
    output_files_exist: tuple[tuple[str, bool], ...]
    checkpoint_id: str
    checkpoint_sequence: int
    checkpoint_topology_hash: str


@dataclass(frozen=True, slots=True)
class ConditionalRouteFinalFacts:
    routes_by_source_row: RouteFacts
    dispositions_by_source_row: DispositionFacts
    sink_effect_order: tuple[str, ...]
    accepted_intent_reclassified_response_lost: bool
    accepted_response_lost_attempt_id: str
    accepted_commit_intent_count: int
    terminal_work_count: int
    terminal_outcome_count: int
    checkpoint_count: int


@dataclass(frozen=True, slots=True)
class ConditionalRouteRecoveryResult:
    scenario: ScenarioSpec
    case: HarnessCaseSpec
    evidence: ScenarioRunEvidence
    interrupted: ConditionalRouteInterruptedFacts
    final: ConditionalRouteFinalFacts


def _recovery_declaration() -> tuple[ScenarioSpec, HarnessCaseSpec]:
    return next(
        (scenario, case)
        for scenario, case in iter_harness_cases(load_manifest())
        if (scenario.id, case.id)
        == (
            "conditional-routing",
            "route-reopen-resume",
        )
    )


def _token_source_rows(connection: Any, *, run_id: str) -> dict[str, int]:
    rows = connection.execute(
        select(tokens_table.c.token_id, rows_table.c.source_row_index)
        .select_from(
            tokens_table.join(
                rows_table,
                and_(
                    rows_table.c.row_id == tokens_table.c.row_id,
                    rows_table.c.run_id == tokens_table.c.run_id,
                ),
            )
        )
        .where(tokens_table.c.run_id == run_id)
    ).all()
    return {str(token_id): int(source_row_index) for token_id, source_row_index in rows}


def _route_facts(
    connection: Any,
    *,
    run_id: str,
    sink_names_by_node_id: dict[str, str],
) -> RouteFacts:
    rows = connection.execute(
        select(
            rows_table.c.source_row_index,
            edges_table.c.label,
            routing_events_table.c.mode,
            edges_table.c.to_node_id,
        )
        .select_from(
            routing_events_table.join(
                node_states_table,
                and_(
                    node_states_table.c.state_id == routing_events_table.c.state_id,
                    node_states_table.c.run_id == routing_events_table.c.run_id,
                ),
            )
            .join(
                tokens_table,
                and_(
                    tokens_table.c.token_id == node_states_table.c.token_id,
                    tokens_table.c.run_id == node_states_table.c.run_id,
                ),
            )
            .join(
                rows_table,
                and_(
                    rows_table.c.row_id == tokens_table.c.row_id,
                    rows_table.c.run_id == tokens_table.c.run_id,
                ),
            )
            .join(
                edges_table,
                and_(
                    edges_table.c.edge_id == routing_events_table.c.edge_id,
                    edges_table.c.run_id == routing_events_table.c.run_id,
                ),
            )
        )
        .where(routing_events_table.c.run_id == run_id)
        .order_by(rows_table.c.source_row_index, routing_events_table.c.ordinal)
    ).all()
    return tuple(
        (
            int(source_row_index),
            str(label),
            str(mode),
            sink_names_by_node_id[str(to_node_id)],
        )
        for source_row_index, label, mode, to_node_id in rows
    )


def _effect_order(
    connection: Any,
    *,
    run_id: str,
    sink_names_by_node_id: dict[str, str],
) -> tuple[str, ...]:
    node_ids = connection.execute(
        select(sink_effects_table.c.sink_node_id)
        .where(sink_effects_table.c.run_id == run_id)
        .order_by(sink_effects_table.c.created_at, sink_effects_table.c.effect_id)
    ).scalars()
    return tuple(sink_names_by_node_id[str(node_id)] for node_id in node_ids)


def _interrupted_facts(
    context: corpus_harness.SinkBoundaryInterruptedContext,
) -> ConditionalRouteInterruptedFacts:
    sink_ids = {str(name): str(node_id) for name, node_id in context.built.graph.get_sink_id_map().items()}
    sink_names_by_node_id = {node_id: name for name, node_id in sink_ids.items()}
    graph = context.built.graph_evidence
    if graph.node_count is None or graph.edge_count is None or graph.node_type_counts is None or graph.edge_labels is None:
        raise AssertionError("conditional-route interrupted graph lacks its accepted topology")

    with context.database.connection() as connection:
        source_row_by_token = _token_source_rows(connection, run_id=context.run_id)
        routes = _route_facts(
            connection,
            run_id=context.run_id,
            sink_names_by_node_id=sink_names_by_node_id,
        )
        work_by_source_row = tuple(
            sorted(
                (
                    source_row_by_token[item.token_id],
                    item.status,
                    str(item.pending_sink_name),
                    item.row_payload_state,
                )
                for item in context.work
            )
        )
        dispositions = tuple(
            sorted(
                (
                    source_row_by_token[item.token_id],
                    str(item.pending_outcome),
                    str(item.pending_path),
                    str(item.pending_sink_name),
                )
                for item in context.work
            )
        )
        effect_order = _effect_order(
            connection,
            run_id=context.run_id,
            sink_names_by_node_id=sink_names_by_node_id,
        )
        attempts = tuple(
            connection.execute(
                select(
                    sink_effect_attempts_table.c.attempt_id,
                    sink_effect_attempts_table.c.action,
                    sink_effect_attempts_table.c.state,
                    sink_effect_attempts_table.c.evidence_json,
                    sink_effect_attempts_table.c.evidence_hash,
                )
                .where(sink_effect_attempts_table.c.effect_id == context.interrupted_effect.effect_id)
                .order_by(
                    sink_effect_attempts_table.c.started_at,
                    sink_effect_attempts_table.c.attempt_id,
                )
            ).mappings()
        )
        artifact_count = int(
            connection.execute(
                select(func.count()).select_from(artifacts_table).where(artifacts_table.c.run_id == context.run_id)
            ).scalar_one()
        )

    accepted_attempts = tuple((str(row["action"]), str(row["state"])) for row in attempts)
    pending_commit = tuple(row for row in attempts if row["action"] == "commit" and row["state"] == "intent")
    if len(pending_commit) != 1:
        raise AssertionError(f"conditional-route interruption requires one pre-effect commit intent: {attempts!r}")
    if pending_commit[0]["evidence_json"] is not None or pending_commit[0]["evidence_hash"] is not None:
        raise AssertionError("conditional-route pre-effect commit intent cannot carry response evidence")

    return ConditionalRouteInterruptedFacts(
        source_names_exhausted=context.source_names_exhausted,
        sink_declaration_order=tuple(str(name) for name in context.rendered.settings.sinks),
        topology_shape=(
            graph.node_count,
            graph.edge_count,
            tuple((item.node_type, item.count) for item in graph.node_type_counts),
            graph.edge_labels,
        ),
        routes_by_source_row=routes,
        dispositions_by_source_row=dispositions,
        work_by_source_row=work_by_source_row,
        sink_effect_order=effect_order,
        accepted_attempts=accepted_attempts,
        accepted_commit_attempt_id=str(pending_commit[0]["attempt_id"]),
        artifact_count=artifact_count,
        output_files_exist=tuple((name, context.rendered.output_paths[name].exists()) for name in context.rendered.settings.sinks),
        checkpoint_id=context.checkpoint_id,
        checkpoint_sequence=context.checkpoint_sequence,
        checkpoint_topology_hash=context.checkpoint_topology_hash,
    )


def _final_facts(
    tmp_path: Path,
    *,
    evidence: ScenarioRunEvidence,
) -> ConditionalRouteFinalFacts:
    proof = evidence.recovery.sink_boundary
    if proof is None:
        raise AssertionError("conditional-route recovery lacks sink-boundary proof")
    sink_names_by_node_id = {effect.sink_node_id: effect.sink_name for effect in proof.effects_after}
    database = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=False)
    try:
        with database.connection() as connection:
            source_row_by_token = _token_source_rows(connection, run_id=str(evidence.runtime.run_id))
            routes = _route_facts(
                connection,
                run_id=str(evidence.runtime.run_id),
                sink_names_by_node_id=sink_names_by_node_id,
            )
            outcomes = tuple(
                connection.execute(
                    select(
                        tokens_table.c.token_id,
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                        token_outcomes_table.c.sink_name,
                    )
                    .select_from(
                        token_outcomes_table.join(
                            tokens_table,
                            and_(
                                tokens_table.c.token_id == token_outcomes_table.c.token_id,
                                tokens_table.c.run_id == token_outcomes_table.c.run_id,
                            ),
                        )
                    )
                    .where(
                        token_outcomes_table.c.run_id == evidence.runtime.run_id,
                        token_outcomes_table.c.completed == 1,
                    )
                ).all()
            )
            dispositions = tuple(
                sorted(
                    (
                        source_row_by_token[str(token_id)],
                        str(outcome),
                        str(path),
                        str(sink_name),
                    )
                    for token_id, outcome, path, sink_name in outcomes
                )
            )
            sink_effect_order = _effect_order(
                connection,
                run_id=str(evidence.runtime.run_id),
                sink_names_by_node_id=sink_names_by_node_id,
            )
            accepted_effect_id = next(effect.effect_id for effect in proof.effects_after if effect.sink_name == "accepted")
            accepted_response_lost_attempts = tuple(
                connection.execute(
                    select(
                        sink_effect_attempts_table.c.attempt_id,
                        sink_effect_attempts_table.c.evidence_json,
                        sink_effect_attempts_table.c.evidence_hash,
                    ).where(
                        sink_effect_attempts_table.c.effect_id == accepted_effect_id,
                        sink_effect_attempts_table.c.action == "commit",
                        sink_effect_attempts_table.c.state == "response_lost",
                    )
                ).mappings()
            )
            accepted_commit_intent_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(sink_effect_attempts_table)
                    .where(
                        sink_effect_attempts_table.c.effect_id == accepted_effect_id,
                        sink_effect_attempts_table.c.action == "commit",
                        sink_effect_attempts_table.c.state == "intent",
                    )
                ).scalar_one()
            )
            terminal_work_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(token_work_items_table)
                    .where(
                        token_work_items_table.c.run_id == evidence.runtime.run_id,
                        token_work_items_table.c.status == "terminal",
                    )
                ).scalar_one()
            )
            terminal_outcome_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(token_outcomes_table)
                    .where(
                        token_outcomes_table.c.run_id == evidence.runtime.run_id,
                        token_outcomes_table.c.completed == 1,
                    )
                ).scalar_one()
            )
            checkpoint_count = int(
                connection.execute(
                    select(func.count()).select_from(checkpoints_table).where(checkpoints_table.c.run_id == evidence.runtime.run_id)
                ).scalar_one()
            )
    finally:
        database.close()

    accepted_intent_reclassified_response_lost = (
        len(accepted_response_lost_attempts) == 1
        and json.loads(str(accepted_response_lost_attempts[0]["evidence_json"])) == {"classification": "response_lost"}
        and accepted_response_lost_attempts[0]["evidence_hash"] == stable_hash({"classification": "response_lost"})
    )
    return ConditionalRouteFinalFacts(
        routes_by_source_row=routes,
        dispositions_by_source_row=dispositions,
        sink_effect_order=sink_effect_order,
        accepted_intent_reclassified_response_lost=accepted_intent_reclassified_response_lost,
        accepted_response_lost_attempt_id=str(accepted_response_lost_attempts[0]["attempt_id"]),
        accepted_commit_intent_count=accepted_commit_intent_count,
        terminal_work_count=terminal_work_count,
        terminal_outcome_count=terminal_outcome_count,
        checkpoint_count=checkpoint_count,
    )


def run_conditional_route_recovery_case(
    tmp_path: Path,
) -> ConditionalRouteRecoveryResult:
    """Interrupt accepted-sink publication, then reopen and publicly resume."""

    scenario, case = _recovery_declaration()
    interrupted: list[ConditionalRouteInterruptedFacts] = []

    def verify_before_reopen(
        context: corpus_harness.SinkBoundaryInterruptedContext,
    ) -> None:
        interrupted.append(_interrupted_facts(context))

    evidence = corpus_harness.run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=verify_before_reopen,
    )
    if len(interrupted) != 1:
        raise AssertionError("conditional-route recovery did not execute exactly one pre-reopen verifier")
    return ConditionalRouteRecoveryResult(
        scenario=scenario,
        case=case,
        evidence=evidence,
        interrupted=interrupted[0],
        final=_final_facts(tmp_path, evidence=evidence),
    )
