"""Lineage expansion and collection preserve complete retry evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import delete, select, update

from elspeth.contracts import AggregationParentDisposition, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    batches_table,
    coalesce_effect_members_table,
    coalesce_effects_table,
    group_records_table,
    node_states_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.core.landscape.test_coalesce_effects import _materialize
from tests.unit.core.landscape.test_coalesce_effects import _setup as _coalesce_setup
from tests.unit.core.landscape.test_token_recording import _MINIMAL_CONTRACT, _add_batch_member, _make_batch, _make_row, _setup


def _lineage_snapshot(db: LandscapeDB) -> tuple[tuple[tuple[object, ...], ...], ...]:
    tables = (
        batches_table,
        tokens_table,
        token_parents_table,
        token_lineage_frames_table,
        group_records_table,
        token_outcomes_table,
        coalesce_effects_table,
        coalesce_effect_members_table,
        node_states_table,
    )
    with db.read_only_connection() as conn:
        return tuple(tuple(sorted((tuple(row) for row in conn.execute(select(table))), key=repr)) for table in tables)


@pytest.mark.parametrize("corruption", ["duplicate", "omitted-parent", "cross-run", "illegal-path", "missing-sibling", "reordered"])
def test_batch_expansion_requires_exact_ordered_parent_dispositions(corruption: str) -> None:
    db, factory = _setup()
    try:
        row, parent = _make_row(factory)
        _, sibling = _make_row(factory, row_index=1)
        batch_id = _make_batch(factory)
        _add_batch_member(factory, batch_id, parent.token_id, 0)
        _add_batch_member(factory, batch_id, sibling.token_id, 1)
        dispositions = tuple(
            AggregationParentDisposition(
                parent_ref=TokenRef(token_id=token.token_id, run_id="run-1"),
                outcome=TerminalOutcome.TRANSIENT,
                path=TerminalPath.BATCH_CONSUMED,
            )
            for token in (parent, sibling)
        )
        if corruption == "duplicate":
            dispositions = (dispositions[0], dispositions[0])
        elif corruption == "omitted-parent":
            dispositions = dispositions[1:]
        elif corruption == "cross-run":
            dispositions = (dispositions[0], replace(dispositions[1], parent_ref=TokenRef(token_id=sibling.token_id, run_id="foreign-run")))
        elif corruption == "illegal-path":
            dispositions = (dispositions[0], replace(dispositions[1], outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED))
        elif corruption == "missing-sibling":
            dispositions = dispositions[:1]
        elif corruption == "reordered":
            dispositions = tuple(reversed(dispositions))
        else:
            raise AssertionError(corruption)
        before = _lineage_snapshot(db)
        with pytest.raises(AuditIntegrityError):
            factory.data_flow.expand_token(
                parent_ref=TokenRef(token_id=parent.token_id, run_id="run-1"),
                row_id=row.row_id,
                child_payloads=[{"item": 1}],
                output_contract=_MINIMAL_CONTRACT,
                parent_path=TerminalPath.BATCH_CONSUMED,
                parent_batch_id=batch_id,
                aggregation_parent_dispositions=dispositions,
                member_token=leader_coordination_token(factory, "run-1").membership,
            )
        assert _lineage_snapshot(db) == before
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["batch-on-normal", "missing-batch", "illegal-parent-path", "dispositions-on-normal"])
def test_expansion_rejects_incompatible_parent_mode_before_mint(corruption: str) -> None:
    db, factory = _setup()
    try:
        row, parent = _make_row(factory)
        ref = TokenRef(token_id=parent.token_id, run_id="run-1")
        path = TerminalPath.EXPAND_PARENT
        batch = None
        dispositions = ()
        if corruption == "batch-on-normal":
            batch = "batch-1"
        elif corruption == "missing-batch":
            path = TerminalPath.BATCH_CONSUMED
        elif corruption == "illegal-parent-path":
            path = TerminalPath.COALESCED
        elif corruption == "dispositions-on-normal":
            dispositions = (
                AggregationParentDisposition(parent_ref=ref, outcome=TerminalOutcome.TRANSIENT, path=TerminalPath.BATCH_CONSUMED),
            )
        else:
            raise AssertionError(corruption)
        before = _lineage_snapshot(db)
        with pytest.raises(ValueError):
            factory.data_flow.expand_token(
                parent_ref=ref,
                row_id=row.row_id,
                child_payloads=[{"item": 1}],
                output_contract=_MINIMAL_CONTRACT,
                parent_path=path,
                parent_batch_id=batch,
                aggregation_parent_dispositions=dispositions,
                member_token=leader_coordination_token(factory, "run-1").membership,
            )
        assert _lineage_snapshot(db) == before
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["empty", "duplicate", "contract-count", "wrong-group", "foreign-ref", "missing-lineage"])
def test_collection_rejects_invalid_roster_without_partial_release(corruption: str) -> None:
    db, factory = _setup()
    try:
        row, parent = _make_row(factory)
        children, group_id = factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=parent.token_id, run_id="run-1"),
            row_id=row.row_id,
            child_payloads=[{"item": 0}, {"item": 1}],
            output_contract=_MINIMAL_CONTRACT,
            member_token=leader_coordination_token(factory, "run-1").membership,
        )
        refs = [TokenRef(token_id=child.token_id, run_id="run-1") for child in children]
        contracts = [_MINIMAL_CONTRACT]
        if corruption == "empty":
            refs = []
        elif corruption == "duplicate":
            refs = [refs[0], refs[0]]
        elif corruption == "contract-count":
            contracts = []
        elif corruption == "wrong-group":
            group_id = "unknown-group"
        elif corruption == "foreign-ref":
            refs[1] = TokenRef(token_id=children[1].token_id, run_id="foreign-run")
        elif corruption == "missing-lineage":
            with db.engine.begin() as conn:
                conn.execute(delete(token_lineage_frames_table).where(token_lineage_frames_table.c.token_id == children[1].token_id))
        else:
            raise AssertionError(corruption)
        before = _lineage_snapshot(db)
        with pytest.raises((AuditIntegrityError, ValueError)):
            factory.data_flow.collect_tokens(
                member_refs=refs,
                group_id=group_id,
                collector_node_id="collector-1",
                output_payloads=[{"combined": True}],
                output_contracts=contracts,
                coordination_token=leader_coordination_token(factory, "run-1"),
            )
        assert _lineage_snapshot(db) == before
    finally:
        db.close()


@pytest.mark.parametrize(
    "corruption",
    ["empty-parents", "duplicate-parents", "state-count", "empty-state", "duplicate-state", "step", "effect-hash", "parent-link"],
)
def test_coalesce_replay_refuses_divergent_receipt_atomically(corruption: str) -> None:
    setup, row, refs, completions = _coalesce_setup()
    try:
        merged = _materialize(setup, row, refs, completions)
        states = [item.state_id for item in completions]
        requested_refs = list(refs)
        step = 4
        if corruption == "empty-parents":
            requested_refs, states = [], []
        elif corruption == "duplicate-parents":
            requested_refs = [refs[0], refs[0]]
        elif corruption == "state-count":
            states = states[:1]
        elif corruption == "empty-state":
            states[0] = ""
        elif corruption == "duplicate-state":
            states[1] = states[0]
        elif corruption == "step":
            step = 5
        elif corruption == "effect-hash":
            with setup.db.engine.begin() as conn:
                conn.execute(update(coalesce_effects_table).values(effect_hash="0" * 64))
        elif corruption == "parent-link":
            with setup.db.engine.begin() as conn:
                conn.execute(delete(token_parents_table).where(token_parents_table.c.token_id == merged.token_id))
        else:
            raise AssertionError(corruption)
        before = _lineage_snapshot(setup.db)
        with pytest.raises(AuditIntegrityError):
            setup.data_flow.coalesce_tokens(
                parent_refs=requested_refs,
                row_id=row.row_id,
                merged_payload={"merged": True},
                merged_contract=_MINIMAL_CONTRACT,
                coalesce_node_id="coalesce-0",
                parent_state_ids=states,
                step_in_pipeline=step,
                coordination_token=leader_coordination_token(setup.factory, setup.run_id),
            )
        assert _lineage_snapshot(setup.db) == before
    finally:
        setup.db.close()


@pytest.mark.parametrize(
    "corruption", ["missing-join", "empty-completions", "duplicate-completions", "reordered-completions", "missing-state"]
)
def test_coalesce_finalization_validates_complete_parent_witness(corruption: str) -> None:
    setup, row, refs, completions = _coalesce_setup()
    try:
        merged = _materialize(setup, row, refs, completions)
        if corruption == "missing-join":
            merged = replace(merged, join_group_id=None)
        elif corruption == "empty-completions":
            completions = ()
        elif corruption == "duplicate-completions":
            completions = (completions[0], completions[0])
        elif corruption == "reordered-completions":
            completions = tuple(reversed(completions))
        elif corruption == "missing-state":
            completions = (replace(completions[0], state_id="absent-state"), completions[1])
        else:
            raise AssertionError(corruption)
        before = _lineage_snapshot(setup.db)
        with pytest.raises(AuditIntegrityError):
            setup.data_flow.finalize_coalesce_effect(
                merged=merged, parent_completions=completions, coordination_token=leader_coordination_token(setup.factory, setup.run_id)
            )
        assert _lineage_snapshot(setup.db) == before
    finally:
        setup.db.close()


@pytest.mark.parametrize("field", ["token_data_ref", "step_in_pipeline", "row_id"])
def test_coalesce_replay_refuses_divergent_result_identity(field: str) -> None:
    setup, row, refs, completions = _coalesce_setup()
    try:
        merged = _materialize(setup, row, refs, completions)
        assert merged.token_data_ref != "0" * 64
        assert _materialize(setup, row, refs, completions).token_data_ref == merged.token_data_ref
        other_row, _ = setup.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(setup.factory, setup.run_id),
            source_node_id=setup.source_node_id,
            row_index=1,
            source_row_index=1,
            ingest_sequence=1,
            data={"other": True},
        )
        value = {"token_data_ref": "0" * 64, "step_in_pipeline": 99, "row_id": other_row.row_id}[field]
        with setup.db.engine.begin() as conn:
            conn.execute(update(tokens_table).where(tokens_table.c.token_id == merged.token_id).values(**{field: value}))
        before = _lineage_snapshot(setup.db)
        with pytest.raises(AuditIntegrityError, match="result token"):
            _materialize(setup, row, refs, completions)
        assert _lineage_snapshot(setup.db) == before
    finally:
        setup.db.close()


def test_coalesce_retry_does_not_store_or_remint_after_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    setup, row, refs, completions = _coalesce_setup()
    try:
        first = _materialize(setup, row, refs, completions)
        before = _lineage_snapshot(setup.db)
        assert setup.factory.payload_store is not None

        def refuse_store(payload: bytes) -> str:
            raise AssertionError("committed coalesce retry must not write payload storage")

        monkeypatch.setattr(setup.factory.payload_store, "store", refuse_store)
        replayed = _materialize(setup, row, refs, completions)
        assert (replayed.token_id, replayed.join_group_id, replayed.token_data_ref, replayed.row_id, replayed.step_in_pipeline) == (
            first.token_id,
            first.join_group_id,
            first.token_data_ref,
            first.row_id,
            first.step_in_pipeline,
        )
        with pytest.raises(AuditIntegrityError, match="merged payload/contract"):
            setup.data_flow.coalesce_tokens(
                parent_refs=list(refs),
                row_id=row.row_id,
                merged_payload={"merged": False},
                merged_contract=_MINIMAL_CONTRACT,
                coalesce_node_id="coalesce-0",
                parent_state_ids=[item.state_id for item in completions],
                step_in_pipeline=4,
                coordination_token=leader_coordination_token(setup.factory, setup.run_id),
            )
        assert _lineage_snapshot(setup.db) == before
    finally:
        setup.db.close()
