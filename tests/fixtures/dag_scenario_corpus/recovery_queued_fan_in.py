"""Queued fan-in topology assertions for sink-boundary recovery evidence."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import and_, select

from elspeth.contracts import NodeID, NodeType, SinkName
from elspeth.core.landscape.schema import (
    node_states_table,
    routing_events_table,
    rows_table,
    run_sources_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.harness import SinkBoundaryInterruptedContext
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioRunEvidence,
    ScenarioSpec,
    SinkOutputProjection,
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
_EXPECTED_OUTPUT_ROWS = (
    '{"id":1,"value":10}',
    '{"id":2,"value":20}',
    '{"id":3,"value":30}',
    '{"id":101,"value":-5}',
    '{"id":102,"value":-10}',
    '{"id":103,"value":-15}',
)


def _assert_exact_interrupted_fan_in(
    context: SinkBoundaryInterruptedContext,
) -> None:
    if (
        context.scenario.id,
        context.case.id,
        context.source_names_exhausted,
    ) != (
        "multi-source-queue-fan-in",
        "queued-fan-in-reopen-resume",
        ("orders", "refunds"),
    ):
        raise AssertionError("queued fan-in recovery received the wrong declaration")
    if context.rendered is not context.built.rendered:
        raise AssertionError("queued fan-in interrupted context mixed rendered objects")

    graph = context.built.graph
    queue_nodes = tuple(node for node in graph.get_nodes() if node.node_type is NodeType.QUEUE)
    if len(queue_nodes) != 1 or queue_nodes[0].plugin_name != "queue:inbound":
        raise AssertionError(f"queued fan-in recovery lacks its exact shared queue: {queue_nodes!r}")
    queue_id = queue_nodes[0].node_id
    transform_ids = graph.get_transform_id_map()
    sink_ids = graph.get_sink_id_map()
    if set(transform_ids) != {0} or set(sink_ids) != {"output"}:
        raise AssertionError("queued fan-in recovery changed its transform or sink declaration")
    transform_id = transform_ids[0]
    sink_id = sink_ids[SinkName("output")]
    source_ids = tuple(NodeID(source_id) for source_id in graph.get_sources())
    step_map = graph.get_node_step_map()
    if (
        graph.node_count,
        graph.edge_count,
        {step_map[source_id] for source_id in source_ids},
        step_map[queue_id],
        step_map[transform_id],
    ) != (5, 4, {0}, 1, 2):
        raise AssertionError("queued fan-in recovery changed its exact topology or step map")
    incoming = graph.get_incoming_edges(queue_id)
    outgoing = tuple(edge for edge in graph.get_edges() if edge.from_node == queue_id)
    if {edge.from_node for edge in incoming} != set(source_ids) or len(incoming) != 2:
        raise AssertionError("queued fan-in recovery did not route both sources into the shared queue")
    if len(outgoing) != 1 or outgoing[0].to_node != transform_id:
        raise AssertionError("queued fan-in recovery queue did not feed the normalization transform")

    with context.database.connection() as conn:
        source_rows = tuple(
            conn.execute(
                select(
                    run_sources_table.c.source_name,
                    rows_table.c.source_row_index,
                    rows_table.c.ingest_sequence,
                    rows_table.c.source_data_hash,
                    rows_table.c.row_id,
                    rows_table.c.source_node_id,
                    tokens_table.c.token_id,
                )
                .select_from(
                    rows_table.join(
                        run_sources_table,
                        and_(
                            run_sources_table.c.run_id == rows_table.c.run_id,
                            run_sources_table.c.source_node_id == rows_table.c.source_node_id,
                        ),
                    ).join(
                        tokens_table,
                        and_(
                            tokens_table.c.run_id == rows_table.c.run_id,
                            tokens_table.c.row_id == rows_table.c.row_id,
                        ),
                    )
                )
                .where(rows_table.c.run_id == context.run_id)
                .order_by(rows_table.c.ingest_sequence)
            ).mappings()
        )
        states = tuple(
            conn.execute(
                select(
                    node_states_table.c.token_id,
                    node_states_table.c.node_id,
                    node_states_table.c.step_index,
                    node_states_table.c.attempt,
                    node_states_table.c.status,
                    node_states_table.c.resume_checkpoint_id,
                )
                .where(node_states_table.c.run_id == context.run_id)
                .order_by(
                    node_states_table.c.step_index,
                    node_states_table.c.token_id,
                )
            ).mappings()
        )
        parent_count = len(conn.execute(select(token_parents_table.c.token_id).where(token_parents_table.c.run_id == context.run_id)).all())
        route_count = len(
            conn.execute(select(routing_events_table.c.event_id).where(routing_events_table.c.run_id == context.run_id)).all()
        )
        outcome_count = len(
            conn.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.run_id == context.run_id)).all()
        )

    observed_source_rows = tuple(
        (
            str(row["source_name"]),
            int(row["source_row_index"]),
            int(row["ingest_sequence"]),
            str(row["source_data_hash"]),
        )
        for row in source_rows
    )
    if observed_source_rows != _EXPECTED_SOURCE_ROWS:
        raise AssertionError(f"queued fan-in recovery changed exact source attribution or order: {observed_source_rows!r}")
    row_ids_in_order = tuple(str(row["row_id"]) for row in source_rows)
    token_ids_in_order = tuple(str(row["token_id"]) for row in source_rows)
    source_node_by_token = {str(row["token_id"]): str(row["source_node_id"]) for row in source_rows}
    if len(set(row_ids_in_order)) != 6 or len(set(token_ids_in_order)) != 6:
        raise AssertionError("queued fan-in recovery remapped rows or tokens")
    if tuple(sorted(token_ids_in_order)) != context.token_ids:
        raise AssertionError("queued fan-in recovery context omitted a durable token")

    source_states = tuple(state for state in states if str(state["node_id"]) == source_node_by_token[str(state["token_id"])])
    transform_states = tuple(state for state in states if str(state["node_id"]) == transform_id)
    sink_states = tuple(state for state in states if str(state["node_id"]) == sink_id)
    if len(states) != 18 or tuple(
        (len(group), {int(state["step_index"]) for state in group}) for group in (source_states, transform_states, sink_states)
    ) != ((6, {0}), (6, {2}), (6, {3})):
        raise AssertionError("queued fan-in recovery did not persist the exact source/transform/sink states")
    if any(
        state["status"] != "completed" or state["attempt"] != 0 or state["resume_checkpoint_id"] is not None
        for state in (*source_states, *transform_states)
    ):
        raise AssertionError("queued fan-in recovery did not complete all source and normalization work before reopen")
    if any(state["status"] != "open" or state["attempt"] != 0 or state["resume_checkpoint_id"] is not None for state in sink_states):
        raise AssertionError("queued fan-in recovery crossed the sink publication boundary before reopen")
    if parent_count != 0 or route_count != 0 or outcome_count != 0:
        raise AssertionError("queued fan-in recovery persisted unexpected lineage, routing, or terminal outcomes")

    if (
        context.checkpoint_topology_hash != context.built.graph_evidence.topology_hash
        or not context.checkpoint_id
        or context.checkpoint_sequence != 0
    ):
        raise AssertionError("queued fan-in recovery retained the wrong checkpoint")
    expected_work_members = set(zip(token_ids_in_order, row_ids_in_order, strict=True))
    observed_work_members = {(item.token_id, item.row_id) for item in context.work}
    if (
        len(context.work) != 6
        or any(
            item.node_id != queue_id
            or item.attempt != 1
            or item.status != "pending_sink"
            or item.pending_sink_name != "output"
            or item.pending_outcome != "success"
            or item.pending_path != "default_flow"
            or item.pending_error_hash is not None
            or item.pending_error_message is not None
            or item.row_payload_state != "live"
            or item.row_payload_anchor_sha256 is not None
            for item in context.work
        )
        or observed_work_members != expected_work_members
    ):
        raise AssertionError("queued fan-in recovery did not retain the exact six queue-derived pending sink work items")
    if context.effects != (context.interrupted_effect,):
        raise AssertionError("queued fan-in recovery retained more than the one interrupted effect")
    if (
        context.interrupted_effect.state != "in_flight"
        or context.interrupted_effect.member_token_ids != token_ids_in_order
        or context.interrupted_effect.member_row_ids != row_ids_in_order
    ):
        raise AssertionError("queued fan-in recovery changed ordered six-member effect identity")
    if context.rendered.output_paths["output"].exists():
        raise AssertionError("queued fan-in recovery published output before reopen")


def _assert_exact_resumed_fan_in(evidence: ScenarioRunEvidence) -> None:
    if evidence.runtime.sink_outputs != (SinkOutputProjection(sink_name="output", rows=_EXPECTED_OUTPUT_ROWS),):
        raise AssertionError("queued fan-in recovery changed canonical output order")
    projection = evidence.runtime.durable_projection
    if projection is None:
        raise AssertionError("queued fan-in recovery lacks its durable projection")
    observed_source_rows = tuple(
        (
            row.source_name,
            row.source_row_index,
            row.ingest_sequence,
            row.source_data_hash,
        )
        for row in projection.rows
    )
    if observed_source_rows != _EXPECTED_SOURCE_ROWS:
        raise AssertionError("queued fan-in recovery changed final source attribution or order")
    if tuple(token.key for token in projection.tokens) != (
        "orders:0#0",
        "orders:1#0",
        "orders:2#0",
        "refunds:0#0",
        "refunds:1#0",
        "refunds:2#0",
    ):
        raise AssertionError("queued fan-in recovery changed canonical token identity")

    transform_states = tuple(state for state in projection.node_states if state.node_key.startswith("transform:normalize_rows@"))
    if (
        len(transform_states) != 6
        or {state.token_key for state in transform_states} != {token.key for token in projection.tokens}
        or {state.step_index for state in transform_states} != {2}
        or {state.status for state in transform_states} != {"completed"}
    ):
        raise AssertionError("queued fan-in recovery lost final normalization evidence")
    audit_keys = {record.key for record in projection.audit_records}
    if not any(key.startswith("node|queue:inbound@") for key in audit_keys):
        raise AssertionError("queued fan-in recovery lost its durable queue node")
    if sum("|continue|move|queue:inbound@" in key for key in audit_keys) != 2:
        raise AssertionError("queued fan-in recovery lost one source-to-queue edge")
    if sum(key.startswith("edge|queue:inbound@") and "|continue|move|transform:normalize_rows@" in key for key in audit_keys) != 1:
        raise AssertionError("queued fan-in recovery lost its queue-to-transform edge")
    if len(projection.terminal_dispositions) != 6 or any(
        disposition.outcome != "success" or disposition.path != "default_flow" or disposition.sink_name != "output"
        for disposition in projection.terminal_dispositions
    ):
        raise AssertionError(
            f"queued fan-in recovery did not terminalize every row at the declared sink: {projection.terminal_dispositions!r}"
        )


def run_queued_fan_in_recovery_case(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
) -> ScenarioRunEvidence:
    """Prove queued fan-in recovery through the shared sink-boundary runner."""

    evidence = corpus_harness.run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=_assert_exact_interrupted_fan_in,
    )
    _assert_exact_resumed_fan_in(evidence)
    return evidence
