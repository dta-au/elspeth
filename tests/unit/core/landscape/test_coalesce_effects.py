"""Durable coalesce-effect identity and replay regressions (epoch 27)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.engine import CoalesceParentCompletion
from elspeth.contracts.enums import FrameKind, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.schema import (
    coalesce_effect_members_table,
    coalesce_effects_table,
    node_states_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

_RUN_ID = "run-1"
_COALESCE_NODE_ID = "coalesce-0"
_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


def _setup():
    setup = make_recorder_with_run(run_id=_RUN_ID)
    register_test_node(
        setup.data_flow,
        setup.run_id,
        _COALESCE_NODE_ID,
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
    )
    row = setup.data_flow.create_row(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"source": True},
    )
    # Crafted siblings sharing one FORK lineage frame (the shape a real
    # fork_token would have produced), via the create_token(..., lineage_path=)
    # seam — coalesce_tokens' durable strict pop requires an innermost shared
    # FORK frame to pop (spec rulings 24/28). Never weaken the pop to
    # accommodate a fixture that models something a real fork never produces.
    parents = [
        setup.data_flow.create_token(
            row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="coalesce-effects-fork-grp", member_key=branch),),
        )
        for branch in ("a", "b")
    ]
    refs = tuple(TokenRef(token_id=token.token_id, run_id=setup.run_id) for token in parents)
    completions: list[CoalesceParentCompletion] = []
    for ordinal, ref in enumerate(refs):
        state = setup.execution.begin_node_state(
            token_id=ref.token_id,
            node_id=_COALESCE_NODE_ID,
            run_id=setup.run_id,
            step_index=4,
            input_data={"ordinal": ordinal},
        )
        completions.append(
            CoalesceParentCompletion(
                parent_ref=ref,
                state_id=state.state_id,
                duration_ms=float(ordinal + 1),
                context_after=None,
            )
        )
    return setup, row, refs, tuple(completions)


def _materialize(setup, row, refs, completions=None):
    return setup.data_flow.coalesce_tokens(
        parent_refs=list(refs),
        row_id=row.row_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        parent_state_ids=None if completions is None else [item.state_id for item in completions],
        merged_payload={"merged": True},
        merged_contract=_CONTRACT,
        step_in_pipeline=4,
    )


def _setup_sibling_group(setup, row, *, group_id: str, branches: tuple[str, str]):
    """A second (or first) fork group sharing ``row``'s row_id — the
    arch-M1 shape: sibling EXPAND members each open their own FORK group
    but converge on the same row and the same coalesce node."""
    parents = [
        setup.data_flow.create_token(
            row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=group_id, member_key=branch),),
        )
        for branch in branches
    ]
    refs = tuple(TokenRef(token_id=token.token_id, run_id=setup.run_id) for token in parents)
    completions: list[CoalesceParentCompletion] = []
    for ordinal, ref in enumerate(refs):
        state = setup.execution.begin_node_state(
            token_id=ref.token_id,
            node_id=_COALESCE_NODE_ID,
            run_id=setup.run_id,
            step_index=4,
            input_data={"ordinal": ordinal},
        )
        completions.append(
            CoalesceParentCompletion(
                parent_ref=ref,
                state_id=state.state_id,
                duration_ms=float(ordinal + 1),
                context_after=None,
            )
        )
    return refs, tuple(completions)


def test_materialization_is_idempotent_and_normalizes_parent_evidence() -> None:
    setup, row, refs, completions = _setup()

    first = _materialize(setup, row, refs, completions)
    second = _materialize(setup, row, refs, completions)

    assert second.token_id == first.token_id
    assert second.join_group_id == first.join_group_id
    with setup.db.connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
        members = conn.execute(select(coalesce_effect_members_table).order_by(coalesce_effect_members_table.c.ordinal)).mappings().all()
        outcome_count = conn.execute(select(func.count()).select_from(token_outcomes_table)).scalar_one()
        states = conn.execute(
            select(node_states_table.c.state_id, node_states_table.c.status)
            .where(node_states_table.c.state_id.in_([item.state_id for item in completions]))
            .order_by(node_states_table.c.state_id)
        ).all()

    assert effect["status"] == "materialized"
    assert effect["result_token_id"] == first.token_id
    assert effect["result_join_group_id"] == first.join_group_id
    assert [(item["parent_token_id"], item["parent_state_id"]) for item in members] == [
        (completion.parent_ref.token_id, completion.state_id) for completion in completions
    ]
    assert outcome_count == 0
    assert {state.status for state in states} == {NodeStateStatus.OPEN.value}


def test_finalization_atomically_completes_states_outcomes_and_effect() -> None:
    setup, row, refs, completions = _setup()
    merged = _materialize(setup, row, refs, completions)

    setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions)
    setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions)

    with setup.db.connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
        states = conn.execute(
            select(node_states_table.c.state_id, node_states_table.c.status).where(
                node_states_table.c.state_id.in_([item.state_id for item in completions])
            )
        ).all()
        outcomes = conn.execute(
            select(
                token_outcomes_table.c.token_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
            ).order_by(token_outcomes_table.c.token_id)
        ).all()

    assert effect["status"] == "completed"
    assert effect["completed_at"] is not None
    assert {state.status for state in states} == {NodeStateStatus.COMPLETED.value}
    # D2 flip: join_group_id is retired from token_outcomes — the (SUCCESS,
    # COALESCED) pair forbids every discriminator field now. join_group_id
    # lives only on the merged TOKEN (ruling 20), checked separately below.
    assert outcomes == sorted([(ref.token_id, TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value) for ref in refs])
    assert merged.join_group_id is not None


def test_failed_finalization_rolls_back_all_terminal_evidence_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    setup, row, refs, completions = _setup()
    merged = _materialize(setup, row, refs, completions)
    original = setup.data_flow.outcomes.record_token_outcome
    calls = 0

    def fail_after_first_outcome(*args, **kwargs):
        nonlocal calls
        outcome_id = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected coalesce finalization failure")
        return outcome_id

    monkeypatch.setattr(setup.data_flow.outcomes, "record_token_outcome", fail_after_first_outcome)

    with pytest.raises(AuditIntegrityError, match="finalization failure"):
        setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions)

    with setup.db.connection() as conn:
        effect_status = conn.execute(select(coalesce_effects_table.c.status)).scalar_one()
        state_statuses = (
            conn.execute(
                select(node_states_table.c.status).where(node_states_table.c.state_id.in_([item.state_id for item in completions]))
            )
            .scalars()
            .all()
        )
        outcome_count = conn.execute(select(func.count()).select_from(token_outcomes_table)).scalar_one()
    assert effect_status == "materialized"
    assert set(state_statuses) == {NodeStateStatus.OPEN.value}
    assert outcome_count == 0

    monkeypatch.setattr(setup.data_flow.outcomes, "record_token_outcome", original)
    setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions)

    with setup.db.connection() as conn:
        assert conn.execute(select(coalesce_effects_table.c.status)).scalar_one() == "completed"
        assert conn.execute(select(func.count()).select_from(token_outcomes_table)).scalar_one() == len(refs)


def test_same_parent_set_with_different_order_or_state_mapping_fails_closed() -> None:
    setup, row, refs, completions = _setup()
    _materialize(setup, row, refs, completions)

    with pytest.raises(AuditIntegrityError, match="ordered parent sequence"):
        _materialize(setup, row, tuple(reversed(refs)), tuple(reversed(completions)))

    forged = (
        replace(completions[0], state_id=completions[1].state_id),
        replace(completions[1], state_id=completions[0].state_id),
    )
    merged = _materialize(setup, row, refs, completions)
    with pytest.raises(AuditIntegrityError, match="parent/state membership"):
        setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=forged)


def test_effect_result_and_member_evidence_are_mechanically_constrained() -> None:
    effect_constraint_names = {constraint.name for constraint in coalesce_effects_table.constraints}
    member_constraint_names = {constraint.name for constraint in coalesce_effect_members_table.constraints}
    token_index_names = {index.name for index in tokens_table.indexes}
    state_index_names = {index.name for index in node_states_table.indexes}

    assert "ck_coalesce_effects_lifecycle" in effect_constraint_names
    assert "fk_coalesce_effects_result_identity" in effect_constraint_names
    assert "uq_coalesce_effect_members_token" in member_constraint_names
    assert "uq_coalesce_effect_members_state" in member_constraint_names
    assert "fk_coalesce_effect_members_state_token" in member_constraint_names
    assert "uq_tokens_coalesce_result_identity" in token_index_names
    assert "uq_node_states_coalesce_member_identity" in state_index_names


def test_raw_parent_evidence_cannot_duplicate_token_or_state() -> None:
    setup, row, refs, _completions = _setup()
    merged = _materialize(setup, row, refs)
    with setup.db.connection() as conn:
        effect_id = conn.execute(select(coalesce_effects_table.c.effect_id)).scalar_one()
        assert conn.execute(
            select(func.count()).select_from(token_parents_table).where(token_parents_table.c.token_id == merged.token_id)
        ).scalar_one() == len(refs)

    with pytest.raises(IntegrityError), setup.db.write_connection() as conn:
        first = (
            conn.execute(select(coalesce_effect_members_table).where(coalesce_effect_members_table.c.effect_id == effect_id))
            .mappings()
            .first()
        )
        assert first is not None
        conn.execute(
            coalesce_effect_members_table.insert().values(
                effect_id=effect_id,
                run_id=_RUN_ID,
                ordinal=99,
                parent_token_id=first["parent_token_id"],
                parent_state_id=first["parent_state_id"],
            )
        )


def test_sibling_fork_groups_sharing_row_id_commit_independent_residuals() -> None:
    """elspeth-8655045f98 (arch-M1 site #4, spec §5): sibling EXPAND members
    sharing row_id each open their own FORK group and each independently
    complete a merge at the SAME coalesce node — the shape
    _build_expand_outer_fork_inner names (an EXPAND scope whose members
    each fork->coalesce through the same 'merge' node). Before this fix,
    get_committed_coalesce_residual queried by row_id alone: with two
    completed effects at the same (node, row_id), it found 2 rows and
    raised AuditIntegrityError — a false corruption alarm on two
    legitimate, independent merges. The restore-recovery reader must
    recover BOTH residuals, separately, each with its own members."""
    setup = make_recorder_with_run(run_id=_RUN_ID)
    register_test_node(
        setup.data_flow,
        setup.run_id,
        _COALESCE_NODE_ID,
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
    )
    row = setup.data_flow.create_row(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"source": True},
    )
    a_refs, a_completions = _setup_sibling_group(setup, row, group_id="g-a", branches=("a-left", "a-right"))
    b_refs, b_completions = _setup_sibling_group(setup, row, group_id="g-b", branches=("b-left", "b-right"))

    merged_a = setup.data_flow.coalesce_tokens(
        parent_refs=list(a_refs),
        row_id=row.row_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        parent_state_ids=[item.state_id for item in a_completions],
        merged_payload={"merged": "a"},
        merged_contract=_CONTRACT,
        step_in_pipeline=4,
    )
    merged_b = setup.data_flow.coalesce_tokens(
        parent_refs=list(b_refs),
        row_id=row.row_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        parent_state_ids=[item.state_id for item in b_completions],
        merged_payload={"merged": "b"},
        merged_contract=_CONTRACT,
        step_in_pipeline=4,
    )
    setup.data_flow.finalize_coalesce_effect(merged=merged_a, parent_completions=a_completions)
    setup.data_flow.finalize_coalesce_effect(merged=merged_b, parent_completions=b_completions)

    with setup.db.connection() as conn:
        effects_by_group = {str(effect["group_id"]): effect for effect in conn.execute(select(coalesce_effects_table)).mappings().all()}
    assert set(effects_by_group) == {"g-a", "g-b"}
    # mutant #2: the persisted group_id must be the CLOSING FORK group
    # (shared_group_id, captured pre-pop at the writer), not defaulted to
    # something merely non-empty (e.g. "" or row_id).
    assert effects_by_group["g-a"]["row_id"] == row.row_id
    assert effects_by_group["g-b"]["row_id"] == row.row_id
    assert effects_by_group["g-a"]["status"] == "completed"
    assert effects_by_group["g-b"]["status"] == "completed"

    reads = setup.factory.barrier_restore
    residual_a = reads.get_committed_coalesce_residual(
        setup.run_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        coalesce_name="merge",
        group_id="g-a",
        blocked_token_ids=tuple(ref.token_id for ref in a_refs),
    )
    residual_b = reads.get_committed_coalesce_residual(
        setup.run_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        coalesce_name="merge",
        group_id="g-b",
        blocked_token_ids=tuple(ref.token_id for ref in b_refs),
    )

    # mutant #1: both residuals recover, SEPARATELY, each with its own
    # members — a test asserting only "one residual recovers" would still
    # pass under the un-re-keyed predicate (whichever row sorts first).
    assert residual_a is not None
    assert residual_b is not None
    assert residual_a.effect_id != residual_b.effect_id
    assert set(residual_a.member_token_ids) == {ref.token_id for ref in a_refs}
    assert set(residual_b.member_token_ids) == {ref.token_id for ref in b_refs}
    assert residual_a.result_token_id == merged_a.token_id
    assert residual_b.result_token_id == merged_b.token_id


def test_nested_fork_effect_records_the_closing_group_not_the_enclosing_one() -> None:
    """META-26 review M-2 (mutant #4): the persisted ``group_id`` must be the
    CLOSING fork group, captured pre-pop. The flat fixture above cannot tell
    that apart from a re-derivation over the merged token's remaining frames
    (``path_fork_group_id(merged_frames)``), because with a single FORK frame
    both spellings agree. Here every parent carries an ENCLOSING fork frame
    (``g-outer``) beneath the closing one (``g-inner``): after the closer pops
    ``g-inner`` the merged path's innermost FORK frame is ``g-outer``, so the
    re-derivation mutant persists the enclosing group and this test goes red."""
    setup = make_recorder_with_run(run_id=_RUN_ID)
    register_test_node(
        setup.data_flow,
        setup.run_id,
        _COALESCE_NODE_ID,
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
    )
    row = setup.data_flow.create_row(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"source": True},
    )
    enclosing = LineageFrame(kind=FrameKind.FORK, group_id="g-outer", member_key="outer-left")
    parents = [
        setup.data_flow.create_token(
            row.row_id,
            lineage_path=(enclosing, LineageFrame(kind=FrameKind.FORK, group_id="g-inner", member_key=branch)),
        )
        for branch in ("inner-a", "inner-b")
    ]
    refs = tuple(TokenRef(token_id=token.token_id, run_id=setup.run_id) for token in parents)
    completions = []
    for ordinal, ref in enumerate(refs):
        state = setup.execution.begin_node_state(
            token_id=ref.token_id,
            node_id=_COALESCE_NODE_ID,
            run_id=setup.run_id,
            step_index=4,
            input_data={"ordinal": ordinal},
        )
        completions.append(CoalesceParentCompletion(parent_ref=ref, state_id=state.state_id, duration_ms=1.0, context_after=None))

    merged = setup.data_flow.coalesce_tokens(
        parent_refs=list(refs),
        row_id=row.row_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        parent_state_ids=[item.state_id for item in completions],
        merged_payload={"merged": "nested"},
        merged_contract=_CONTRACT,
        step_in_pipeline=4,
    )
    setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=tuple(completions))

    # The closer popped exactly its own frame: the merged token now sits
    # directly inside the enclosing fork group.
    assert merged.lineage_path == (enclosing,)
    with setup.db.connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
    assert effect["group_id"] == "g-inner"
    assert effect["group_id"] != "g-outer"

    reads = setup.factory.barrier_restore
    residual = reads.get_committed_coalesce_residual(
        setup.run_id,
        coalesce_node_id=_COALESCE_NODE_ID,
        coalesce_name="merge",
        group_id="g-inner",
        blocked_token_ids=tuple(ref.token_id for ref in refs),
    )
    assert residual is not None
    assert residual.result_token_id == merged.token_id
    assert (
        reads.get_committed_coalesce_residual(
            setup.run_id,
            coalesce_node_id=_COALESCE_NODE_ID,
            coalesce_name="merge",
            group_id="g-outer",
            blocked_token_ids=tuple(ref.token_id for ref in refs),
        )
        is None
    )
