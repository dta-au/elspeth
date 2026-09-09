"""Recovery oracle for the two independent source roots scenario."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from elspeth.core.landscape.schema import (
    artifacts_table,
    operations_table,
    rows_table,
    run_sources_table,
    token_outcomes_table,
)
from tests.fixtures.dag_scenario_corpus.harness import (
    SinkBoundaryInterruptedContext,
    run_sink_boundary_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioRunEvidence,
    ScenarioSpec,
    SinkOutputProjection,
    SummaryRunExpectation,
)

_EXPECTED_SOURCE_ROWS = (
    (
        "orders",
        0,
        0,
        "19b33c29a9a46e8935271aed5ccd77ab3b9d4be9ef78e2267548874cd61726b2",
    ),
    (
        "orders",
        1,
        1,
        "ffeb1fb12db8f2d5c55f05b744bde7f0e0858990ceb9202d9ec418045aa0fd8d",
    ),
    (
        "orders",
        2,
        2,
        "d7c1d668deaae7fb498b23776bd8b3a5f898bf11e8b73cd42bd1d415f0baf4c9",
    ),
    (
        "refunds",
        0,
        3,
        "804d9ca070c77a3af1dd0dac3d6fd6a2f664c563275ae090ba958e3725944256",
    ),
    (
        "refunds",
        1,
        4,
        "67b60e8bb29435c1ba474ce8707630b6bc49770f88a13c3060d307d8b219c784",
    ),
    (
        "refunds",
        2,
        5,
        "f0df28eb4c45da93c8c9c0124c72449bbf5627ca1166126dac2103c6f3067d9a",
    ),
)

_EXPECTED_OUTPUT = SinkOutputProjection(
    sink_name="output",
    rows=(
        '{"id":1,"value":10}',
        '{"id":2,"value":20}',
        '{"id":3,"value":30}',
        '{"id":101,"value":-5}',
        '{"id":102,"value":-10}',
        '{"id":103,"value":-15}',
    ),
)


def _require_independent_roots_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
) -> None:
    if scenario.id != "multiple-independent-sources":
        raise AssertionError("independent-roots recovery requires the multiple-independent-sources scenario")
    if case.id != "independent-roots-reopen-resume":
        raise AssertionError("independent-roots recovery requires its exact declared recovery case")
    if tuple(case.input_fixtures) != ("orders", "refunds"):
        raise AssertionError("independent-roots recovery requires declared orders then refunds source bindings")
    if tuple(case.output_artifacts) != ("output",):
        raise AssertionError("independent-roots recovery requires the sole declared output sink")
    if (
        case.workflow != "recovery"
        or case.recovery_kind != "sink_boundary"
        or case.recovery_fault is None
        or case.recovery_fault.model_dump(mode="json")
        != {
            "kind": "sink_effect",
            "seam": "before_effect",
            "sink_name": "output",
            "occurrence": 1,
        }
    ):
        raise AssertionError("independent-roots recovery requires the declared first-output sink-effect BEFORE_EFFECT fault")
    if not isinstance(case.expected, SummaryRunExpectation) or case.expected.status != "completed" or case.expected.output_rows != 6:
        raise AssertionError("independent-roots recovery requires its six-row completed summary")


def _verify_interrupted_independent_roots(
    context: SinkBoundaryInterruptedContext,
) -> None:
    if context.source_names_exhausted != ("orders", "refunds"):
        raise AssertionError("independent-roots recovery did not exhaust exactly both declared roots")
    if (
        context.built.graph_evidence.node_count,
        context.built.graph_evidence.edge_count,
    ) != (3, 2):
        raise AssertionError("independent-roots recovery changed its two-root, one-sink topology")
    if context.checkpoint_sequence != 0 or context.checkpoint_topology_hash != context.built.graph_evidence.topology_hash:
        raise AssertionError("independent-roots recovery retained the wrong topology checkpoint")

    source_rows_join = rows_table.join(
        run_sources_table,
        (rows_table.c.run_id == run_sources_table.c.run_id) & (rows_table.c.source_node_id == run_sources_table.c.source_node_id),
    )
    source_operations_join = operations_table.join(
        run_sources_table,
        (operations_table.c.run_id == run_sources_table.c.run_id) & (operations_table.c.node_id == run_sources_table.c.source_node_id),
    )
    with context.database.connection() as connection:
        rows = tuple(
            connection.execute(
                select(
                    rows_table.c.row_id,
                    run_sources_table.c.source_name,
                    rows_table.c.source_row_index,
                    rows_table.c.ingest_sequence,
                    rows_table.c.source_data_hash,
                )
                .select_from(source_rows_join)
                .where(rows_table.c.run_id == context.run_id)
                .order_by(rows_table.c.ingest_sequence)
            ).mappings()
        )
        source_operations = tuple(
            connection.execute(
                select(
                    run_sources_table.c.source_name,
                    operations_table.c.operation_id,
                    operations_table.c.status,
                    operations_table.c.completed_at,
                    operations_table.c.sink_effect_id,
                )
                .select_from(source_operations_join)
                .where(
                    operations_table.c.run_id == context.run_id,
                    operations_table.c.operation_type == "source_load",
                )
                .order_by(run_sources_table.c.source_name)
            ).mappings()
        )
        artifact_ids = tuple(
            connection.execute(select(artifacts_table.c.artifact_id).where(artifacts_table.c.run_id == context.run_id)).scalars()
        )
        outcome_ids = tuple(
            connection.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.run_id == context.run_id)).scalars()
        )

    durable_source_rows = tuple(
        (
            str(row["source_name"]),
            int(row["source_row_index"]),
            int(row["ingest_sequence"]),
            str(row["source_data_hash"]),
        )
        for row in rows
    )
    if durable_source_rows != _EXPECTED_SOURCE_ROWS:
        raise AssertionError(f"independent-roots recovery durable rows changed declared source order or material: {durable_source_rows!r}")
    row_ids = tuple(str(row["row_id"]) for row in rows)
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(context.interrupted_effect.member_row_ids):
        raise AssertionError("independent-roots recovery effect members do not cover the six durable row identities")

    if (
        tuple(str(operation["source_name"]) for operation in source_operations) != ("orders", "refunds")
        or len({str(operation["operation_id"]) for operation in source_operations}) != 2
        or any(
            operation["status"] != "completed" or operation["completed_at"] is None or operation["sink_effect_id"] is not None
            for operation in source_operations
        )
    ):
        raise AssertionError("independent-roots recovery requires exactly one completed source-load operation per declared root")

    if len(context.token_ids) != 6 or set(context.interrupted_effect.member_token_ids) != set(context.token_ids):
        raise AssertionError("independent-roots recovery effect does not contain exactly the six durable token identities")
    if (
        context.effects != (context.interrupted_effect,)
        or context.interrupted_effect.sink_name != "output"
        or context.interrupted_effect.state != "in_flight"
        or len(context.interrupted_effect.member_token_ids) != 6
    ):
        raise AssertionError("independent-roots recovery requires one pending six-member output effect")
    if len(context.work) != 6 or any(
        work.status != "pending_sink"
        or work.row_payload_state != "live"
        or work.pending_sink_name != "output"
        or work.pending_outcome != "success"
        or work.pending_path != "default_flow"
        or work.pending_error_hash is not None
        or work.pending_error_message is not None
        for work in context.work
    ):
        raise AssertionError("independent-roots recovery requires six exact live pending-sink work rows")
    if {work.token_id for work in context.work} != set(context.token_ids) or {work.row_id for work in context.work} != set(row_ids):
        raise AssertionError("independent-roots recovery scheduler work does not cover the durable token and row identities")
    if artifact_ids or outcome_ids:
        raise AssertionError("independent-roots recovery published an artifact or terminalized an outcome before reopen")


def _verify_resumed_independent_roots(
    evidence: ScenarioRunEvidence,
) -> None:
    if evidence.runtime.sink_outputs != (_EXPECTED_OUTPUT,):
        raise AssertionError("independent-roots recovery emitted the wrong canonical six-row output")
    if evidence.audit.source_operation_count != 2:
        raise AssertionError("independent-roots recovery replayed or omitted a source load")
    projection = evidence.runtime.durable_projection
    if projection is None:
        raise AssertionError("independent-roots recovery lacks an exact durable projection")
    if (
        tuple(
            (
                row.source_name,
                row.source_row_index,
                row.ingest_sequence,
                row.source_data_hash,
            )
            for row in projection.rows
        )
        != _EXPECTED_SOURCE_ROWS
    ):
        raise AssertionError("independent-roots recovery changed its exact final durable rows")
    if (
        len(projection.tokens) != 6
        or any(token.parents for token in projection.tokens)
        or len(projection.terminal_dispositions) != 6
        or {
            (
                disposition.outcome,
                disposition.path,
                disposition.sink_name,
            )
            for disposition in projection.terminal_dispositions
        }
        != {("success", "default_flow", "output")}
        or len(projection.scheduler_work) != 6
        or {work.final_status for work in projection.scheduler_work} != {"terminal"}
    ):
        raise AssertionError("independent-roots recovery did not terminalize exactly the six root tokens, work items, and outcomes")

    proof = evidence.recovery.sink_boundary
    if (
        proof is None
        or proof.source_names_exhausted_before != ("orders", "refunds")
        or len(proof.token_ids_before) != 6
        or proof.token_ids_after != proof.token_ids_before
        or proof.effect_count_before != 1
        or proof.effect_count_after != 1
        or proof.effect_member_count_before != 6
        or proof.effects_before[0].effect_id != proof.effects_after[0].effect_id
        or proof.effects_before[0].artifact_id != proof.effects_after[0].artifact_id
        or proof.effects_before[0].member_token_ids != proof.effects_after[0].member_token_ids
        or proof.artifact_count_before != 0
        or proof.publication_count_before != 0
        or proof.artifact_count_after != 1
        or proof.publication_count_after != 1
        or proof.resume_marker_count != 1
        or proof.resume_marker_entry_point != "resume"
        or not evidence.recovery.checkpoint_removed
        or not proof.durable_identity_reused
        or not proof.durable_export_parity
    ):
        raise AssertionError("independent-roots recovery lost exact public-resume identity, publication, checkpoint, or parity evidence")


def run_independent_roots_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    """Interrupt the first output effect, reopen, and resume both roots."""

    _require_independent_roots_case(scenario, case)
    evidence = run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=_verify_interrupted_independent_roots,
    )
    _verify_resumed_independent_roots(evidence)
    return evidence
