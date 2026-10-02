from __future__ import annotations

from datetime import UTC, datetime

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, TerminalPath
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.database import begin_write
from elspeth.core.landscape.scheduler_repository import record_group_loss
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

# Minimal contract for tests that only care about token/group lifecycle, not
# contract content (mirrors test_token_recording.py's _MINIMAL_CONTRACT).
_MINIMAL_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


def test_barrier_restore_read_model_reports_duplicate_live_buffered_acceptances() -> None:
    setup = make_recorder_with_run(run_id="run-restore-read")
    agg_node_id = register_test_node(
        setup.factory.data_flow,
        setup.run_id,
        "agg-node-1",
        node_type=NodeType.TRANSFORM,
        plugin_name="batch_stats",
    )
    batch = setup.factory.execution.create_batch(agg_node_id, coordination_token=setup.coordination_token)
    row, token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id, 0, {"id": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    ref = TokenRef(token_id=token.token_id, run_id=setup.run_id)
    work_item = setup.factory.scheduler.enqueue_ready_claimed(
        member_token=setup.coordination_token.membership,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=agg_node_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json='{"id":1}',
        lease_owner=setup.coordination_token.worker_id,
        lease_seconds=60,
    )

    setup.factory.data_flow.record_token_outcome(
        ref, None, TerminalPath.BUFFERED, batch_id=batch.batch_id, member_token=setup.coordination_token.membership, work_item=work_item
    )
    setup.factory.data_flow.record_token_outcome(
        ref, None, TerminalPath.BUFFERED, batch_id=batch.batch_id, member_token=setup.coordination_token.membership, work_item=work_item
    )

    duplicate_acceptances = setup.factory.barrier_restore.find_duplicate_live_buffered_acceptances(setup.run_id)

    assert duplicate_acceptances == [(token.token_id, 2)]


def test_barrier_restore_read_model_reports_max_node_state_attempts() -> None:
    setup = make_recorder_with_run(run_id="run-restore-attempts")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "sink-node")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id, 0, {"id": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    token = setup.factory.data_flow.create_token(row_id=row.row_id, token_id="token-attempt", coordination_token=setup.coordination_token)
    setup.factory.execution.begin_node_state(
        token.token_id, node_id, 1, {"id": 1}, attempt=0, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.begin_node_state(
        token.token_id, node_id, 2, {"id": 1}, attempt=3, member_token=setup.coordination_token.membership
    )

    assert setup.factory.barrier_restore.get_max_node_state_attempts(setup.run_id, [token.token_id]) == {token.token_id: 3}
    assert setup.factory.barrier_restore.get_max_node_state_attempts(
        setup.run_id,
        [token.token_id],
        step_index=1,
    ) == {token.token_id: 0}


def test_barrier_restore_read_model_reports_open_coalesce_hold_state_ids() -> None:
    setup = make_recorder_with_run(run_id="run-restore-open-holds")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "coalesce-node")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id, 0, {"id": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    token = setup.factory.data_flow.create_token(row_id=row.row_id, token_id="token-held", coordination_token=setup.coordination_token)
    setup.factory.execution.begin_node_state(
        token.token_id, node_id, 1, {"id": 1}, state_id="state-low", attempt=0, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.begin_node_state(
        token.token_id, node_id, 1, {"id": 1}, state_id="state-high", attempt=2, member_token=setup.coordination_token.membership
    )

    assert setup.factory.barrier_restore.get_open_node_state_ids(
        setup.run_id,
        node_ids=[node_id],
        token_ids=[token.token_id],
    ) == {token.token_id: "state-high"}


def test_barrier_restore_read_model_finds_one_durable_group_loss_by_closer() -> None:
    setup = make_recorder_with_run(run_id="run-restore-branch-loss")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        row_id="row-lost",
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=setup.coordination_token,
    )
    setup.factory.data_flow.create_token(row_id=row.row_id, token_id="token-lost", coordination_token=setup.coordination_token)
    with begin_write(setup.db.engine) as conn:
        record_group_loss(
            conn,
            run_id=setup.run_id,
            spec=GroupLossSpec(
                closer_name="variant_union",
                group_id="eg_1",
                member_key="treatment",
                token_id="token-lost",
                reason="error_routed",
            ),
            recorded_by="worker-1",
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )

    reads = setup.factory.barrier_restore
    # WS4 Task 12 re-key: has_group_loss (retired has_branch_loss_for_group)
    # queries the unified group_losses ledger directly by group_id — no
    # tokens-table join needed, since the ledger row carries group_id itself
    # (spec §6.2 unification).
    assert reads.has_group_loss(
        run_id=setup.run_id,
        closer_name="variant_union",
        group_id="eg_1",
    )
    assert not reads.has_group_loss(
        run_id=setup.run_id,
        closer_name="variant_union",
        group_id="other-group",
    )
    assert not reads.has_group_loss(
        run_id=setup.run_id,
        closer_name="other-union",
        group_id="eg_1",
    )
    assert not reads.has_group_loss(
        run_id="other-run",
        closer_name="variant_union",
        group_id="eg_1",
    )
    # row_id_for_token is unrelated to the loss ledger — a separate, still
    # row-scoped accessor (e.g. for scope_row_id derivation) — untouched by
    # the re-key.
    assert reads.row_id_for_token(run_id=setup.run_id, token_id="token-lost") == "row-lost"
    assert reads.row_id_for_token(run_id=setup.run_id, token_id="no-such-token") is None


def test_group_member_reads_roster_from_frames_and_group_record() -> None:
    # Opener token expands into 2 members (ordinals 0, 1). WS1a's production
    # expand write mints group_records(member_count=2) and one
    # token_lineage_frames row per member (kind='expand', member_key=child
    # token_id) -- frames and group records are seeded through the real
    # writer, never raw INSERTs into the new tables.
    setup = make_recorder_with_run(run_id="run-restore-group-roster")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-roster",
        coordination_token=setup.coordination_token,
    )
    opener = setup.factory.data_flow.create_token(row_id=row.row_id, token_id="token-opener", coordination_token=setup.coordination_token)
    children, expand_group_id = setup.factory.data_flow.expand_token(
        parent_ref=TokenRef(token_id=opener.token_id, run_id=setup.run_id),
        row_id=row.row_id,
        child_payloads=[{"item": 0}, {"item": 1}],
        output_contract=_MINIMAL_CONTRACT,
        member_token=setup.coordination_token.membership,
    )
    reads = setup.factory.barrier_restore

    record = reads.get_group_record(run_id=setup.run_id, group_id=expand_group_id)
    assert record is not None
    assert record.kind == "expand"
    assert record.member_count == 2
    assert record.opener_token_id == opener.token_id

    keys = reads.get_group_member_keys(run_id=setup.run_id, group_id=expand_group_id)
    assert keys == frozenset(child.token_id for child in children)

    ordinals = reads.get_group_member_ordinals(run_id=setup.run_id, opener_token_id=opener.token_id)
    assert ordinals == {children[0].token_id: 0, children[1].token_id: 1}


def test_get_group_record_returns_none_for_unknown_group() -> None:
    setup = make_recorder_with_run(run_id="run-restore-group-unknown")
    assert setup.factory.barrier_restore.get_group_record(run_id=setup.run_id, group_id="no-such") is None


def test_has_completed_group_for_node_discriminates_sibling_groups_on_one_row() -> None:
    # THE collision this workstream exists for: two sibling fork groups share
    # row_id at one coalesce node; completing one must not mark the other.
    # create_token's lineage_path is the crafted-token seam (docstring:
    # "crafted-token seam for tests and recovery tooling") -- used here only
    # to stack a chosen FORK frame directly, not to bypass any strict-pop
    # invariant (no coalesce/collect release is exercised in this test).
    setup = make_recorder_with_run(run_id="run-restore-group-sibling")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "merge_x")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    g1_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-g1-left",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-1", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-g2-left",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-2", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    state = setup.factory.execution.begin_node_state(
        g1_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state.state_id, NodeStateStatus.COMPLETED, output_data={"id": 1}, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    reads = setup.factory.barrier_restore
    assert reads.has_completed_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-1") is True
    assert reads.has_completed_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-2") is False
    assert reads.has_released_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-1") is True
    assert reads.has_released_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-2") is False


def test_get_completed_group_ids_for_nodes_pairs() -> None:
    setup = make_recorder_with_run(run_id="run-restore-group-pairs")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "merge_x")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    g1_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-g1-left",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-1", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    state = setup.factory.execution.begin_node_state(
        g1_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state.state_id, NodeStateStatus.COMPLETED, output_data={"id": 1}, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    pairs = setup.factory.barrier_restore.get_completed_group_ids_for_nodes(setup.run_id, frozenset({node_id}))
    assert pairs == {(node_id, "g-fork-1")}


def test_get_released_group_ids_for_nodes_pairs_and_discriminates_from_failed() -> None:
    # WS4 Task 12 / F-1 (elspeth-14660ce1c0): the group-keyed released sweep
    # the row_union crash-window holdless-reconcile needs. Both tokens share row_id -- the sibling-fork-group
    # collision this workstream exists for -- and only the COMPLETED one may
    # appear; a mutant deleting the ``status == COMPLETED`` predicate would
    # let the FAILED sibling's group through too (completed_at is set on a
    # FAILED state as well).
    from elspeth.contracts.errors import ExecutionError

    setup = make_recorder_with_run(run_id="run-restore-group-released-pairs")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "row_union_x")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    released_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-released",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-released", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    released_state = setup.factory.execution.begin_node_state(
        released_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        released_state.state_id,
        NodeStateStatus.COMPLETED,
        output_data={"id": 1},
        duration_ms=1.0,
        member_token=setup.coordination_token.membership,
    )

    failed_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-failed",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-failed", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    failed_state = setup.factory.execution.begin_node_state(
        failed_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    error = ExecutionError(exception="boom", exception_type="ValueError")
    setup.factory.execution.complete_node_state(
        failed_state.state_id, NodeStateStatus.FAILED, error=error, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    pairs = setup.factory.barrier_restore.get_released_group_ids_for_nodes(setup.run_id, frozenset({node_id}))
    assert pairs == {(node_id, "g-fork-released")}
    assert setup.factory.barrier_restore.get_completed_group_ids_for_nodes("other-run", frozenset({node_id})) == set()


def test_resolve_group_collector_node_agrees_with_the_real_closer() -> None:
    # META-22 happy path: the durable walk (resolve_group_collector_node)
    # must name the SAME node a real close completed at — empty before
    # completion (no durable evidence yet), a singleton set after.
    setup = make_recorder_with_run(run_id="run-restore-resolve-node-happy")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "stitch")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    member = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-member",
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g-expand-1", member_key="m-1"),),
        coordination_token=setup.coordination_token,
    )
    assert setup.factory.barrier_restore.resolve_group_collector_node(run_id=setup.run_id, group_id="g-expand-1") == frozenset()

    state = setup.factory.execution.begin_node_state(
        member.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state.state_id, NodeStateStatus.COMPLETED, output_data={"id": 1}, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    assert setup.factory.barrier_restore.resolve_group_collector_node(run_id=setup.run_id, group_id="g-expand-1") == frozenset({node_id})


def test_resolve_group_collector_node_returns_every_node_for_a_nested_group() -> None:
    # META-22 "forced apart" case, corrected after review: the mechanism a
    # genuine durable-vs-config divergence would trip is NOT "raise on two
    # distinct nodes" — that shape is the ordinary, HEALTHY signature of a
    # nested EXPAND scope (I-6's depth-agnostic join means an outer group's
    # frame is carried by every descendant, so a token completing under an
    # INNER collector legitimately shows the OUTER group_id as completed
    # there too, alongside the outer group's own closer node). Pin that
    # directly: two distinct completion nodes for the SAME group_id must
    # come back as a two-element SET, not raise.
    setup = make_recorder_with_run(run_id="run-restore-resolve-node-forced-apart")
    node_a = register_test_node(setup.factory.data_flow, setup.run_id, "stitch-a")
    node_b = register_test_node(setup.factory.data_flow, setup.run_id, "stitch-b")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    member_a = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-member-a",
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g-expand-split", member_key="m-a"),),
        coordination_token=setup.coordination_token,
    )
    member_b = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-member-b",
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g-expand-split", member_key="m-b"),),
        coordination_token=setup.coordination_token,
    )
    state_a = setup.factory.execution.begin_node_state(
        member_a.token_id, node_a, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state_a.state_id,
        NodeStateStatus.COMPLETED,
        output_data={"id": 1},
        duration_ms=1.0,
        member_token=setup.coordination_token.membership,
    )
    state_b = setup.factory.execution.begin_node_state(
        member_b.token_id, node_b, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state_b.state_id,
        NodeStateStatus.COMPLETED,
        output_data={"id": 1},
        duration_ms=1.0,
        member_token=setup.coordination_token.membership,
    )

    assert setup.factory.barrier_restore.resolve_group_collector_node(run_id=setup.run_id, group_id="g-expand-split") == frozenset(
        {node_a, node_b}
    )


def test_group_completion_joins_match_any_frame_depth_not_only_innermost() -> None:
    """I-6: the three group_id-keyed joins (has_completed_group_for_node,
    has_released_group_for_node, get_completed_group_ids_for_nodes) match a
    frame at ANY depth on the token's lineage path, not only its innermost
    frame -- this is intentional (an enclosing group's membership includes
    every descendant token, at any depth) but was previously undocumented
    and untested. Pin it: a token whose path carries TWO group_ids (an
    outer FORK wrapping an inner EXPAND) reports BOTH as completed/released
    once the token completes at the node, and get_completed_group_ids_for_nodes
    emits one pair PER depth."""
    setup = make_recorder_with_run(run_id="run-restore-group-nested-depth")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "merge_x")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    nested_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-nested",
        lineage_path=(
            LineageFrame(kind=FrameKind.FORK, group_id="g-outer-fork", member_key="left"),
            LineageFrame(kind=FrameKind.EXPAND, group_id="g-inner-expand", member_key="token-nested"),
        ),
        coordination_token=setup.coordination_token,
    )
    state = setup.factory.execution.begin_node_state(
        nested_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    setup.factory.execution.complete_node_state(
        state.state_id, NodeStateStatus.COMPLETED, output_data={"id": 1}, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    reads = setup.factory.barrier_restore
    assert reads.has_completed_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-outer-fork") is True
    assert reads.has_completed_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-inner-expand") is True
    assert reads.has_released_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-outer-fork") is True
    assert reads.has_released_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-inner-expand") is True

    pairs = reads.get_completed_group_ids_for_nodes(setup.run_id, frozenset({node_id}))
    assert pairs == {(node_id, "g-outer-fork"), (node_id, "g-inner-expand")}


def test_has_released_group_for_node_discriminates_completed_from_failed() -> None:
    """M-2: the only prior test for has_released_group_for_node used a
    COMPLETED state throughout, so a mutant deleting the ``status ==
    COMPLETED`` predicate would survive (has_completed_group_for_node's
    weaker completed_at-only check gives the same answer). Pin the
    discrimination with a FAILED state, which sets completed_at but is not
    COMPLETED."""
    from elspeth.contracts.errors import ExecutionError

    setup = make_recorder_with_run(run_id="run-restore-group-failed-vs-completed")
    node_id = register_test_node(setup.factory.data_flow, setup.run_id, "merge_x")
    row, _initial_token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        row_id="row-1",
        coordination_token=setup.coordination_token,
    )
    failed_token = setup.factory.data_flow.create_token(
        row_id=row.row_id,
        token_id="token-failed",
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="g-fork-failed", member_key="left"),),
        coordination_token=setup.coordination_token,
    )
    state = setup.factory.execution.begin_node_state(
        failed_token.token_id, node_id, 1, {"id": 1}, member_token=setup.coordination_token.membership
    )
    error = ExecutionError(exception="boom", exception_type="ValueError")
    setup.factory.execution.complete_node_state(
        state.state_id, NodeStateStatus.FAILED, error=error, duration_ms=1.0, member_token=setup.coordination_token.membership
    )

    reads = setup.factory.barrier_restore
    # completed_at IS set on a FAILED state -- the weaker check reports True.
    assert reads.has_completed_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-failed") is True
    # status != COMPLETED -- the release check must NOT.
    assert reads.has_released_group_for_node(run_id=setup.run_id, node_id=node_id, group_id="g-fork-failed") is False
