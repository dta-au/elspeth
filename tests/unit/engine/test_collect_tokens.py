# tests/unit/engine/test_collect_tokens.py
"""TokenManager.collect_tokens strict-pop release mint (WS4, spec §4.2)."""

from typing import Any

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_field
from tests.fixtures.landscape import make_recorder_with_run
from tests.unit.engine.conftest import make_test_step_resolver as _make_step_resolver


def _make_observed_contract(*field_names: str) -> SchemaContract:
    fields = tuple(
        make_field(name=name, original_name=f"'{name}'", python_type=object, required=False, source="inferred") for name in field_names
    )
    return SchemaContract(mode="OBSERVED", fields=fields, locked=True)


def _make_pipeline_row(data: dict[str, Any]) -> PipelineRow:
    return PipelineRow(data, _make_observed_contract(*data.keys()))


def _make_manager_context() -> tuple[TokenManager, Any, str, str]:
    setup = make_recorder_with_run()
    manager = TokenManager(setup.factory.data_flow, step_resolver=_make_step_resolver())
    return manager, setup.factory, setup.run_id, setup.source_node_id


class TestCollectTokensPathAlgebra:
    def test_release_pops_the_collector_frame_and_opens_a_fresh_expand_group(self) -> None:
        from elspeth.contracts import SourceRow

        manager, _factory, run_id, source_node_id = _make_manager_context()
        initial = manager.create_initial_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=0,
            source_row=SourceRow.valid({"original": "data"}, contract=_make_observed_contract("original"), source_row_index=0),
            source_row_index=0,
            ingest_sequence=0,
        )
        members, group_id = manager.expand_token(
            parent_token=initial,
            expanded_rows=[{"item": 0}, {"item": 1}],
            output_contract=_make_observed_contract("item"),
            node_id=NodeID("expand_node"),
            run_id=run_id,
        )

        released = manager.collect_tokens(
            members=members,
            output_rows=[_make_pipeline_row({"combined": True})],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )

        assert len(released) == 1
        child = released[0]
        # The collector's own frame is POPPED; the emission is a fresh EXPAND group.
        assert child.lineage_path[-1].kind is FrameKind.EXPAND
        assert child.lineage_path[-1].group_id != group_id
        assert child.lineage_path[:-1] == members[0].lineage_path[:-1]
        assert child.row_id == members[0].row_id
        assert child.row_data.to_dict() == {"combined": True}

    def test_all_members_must_carry_the_closers_innermost_frame(self) -> None:
        from elspeth.contracts import SourceRow

        manager, _factory, run_id, source_node_id = _make_manager_context()
        initial = manager.create_initial_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=0,
            source_row=SourceRow.valid({}, contract=_make_observed_contract(), source_row_index=0),
            source_row_index=0,
            ingest_sequence=0,
        )
        forked, _fork_group_id = manager.fork_token(
            parent_token=initial,
            branches=["a", "b"],
            node_id=NodeID("gate_node"),
            run_id=run_id,
        )
        branch_a, branch_b = forked
        members_a, group_a = manager.expand_token(
            parent_token=branch_a,
            expanded_rows=[{"x": 1}],
            output_contract=_make_observed_contract("x"),
            node_id=NodeID("expand_node"),
            run_id=run_id,
        )
        members_b, _group_b = manager.expand_token(
            parent_token=branch_b,
            expanded_rows=[{"x": 2}],
            output_contract=_make_observed_contract("x"),
            node_id=NodeID("expand_node"),
            run_id=run_id,
        )

        # Simulate a member whose innermost frame is NOT the closer's group —
        # statically impossible under §7 rule 5, so it must crash loudly.
        with pytest.raises(OrchestrationInvariantError):
            manager.collect_tokens(
                members=[members_a[0], members_b[0]],
                output_rows=[_make_pipeline_row({"combined": True})],
                node_id=NodeID("collector_node"),
                run_id=run_id,
                group_id=group_a,
            )

    def test_empty_output_mints_nothing_engine_side_but_mints_a_durable_empty_release(self) -> None:
        """Ruling 1 (fix-round): the engine-visible return stays () for M=0,
        but the durable half must NOT be skipped — an empty release still
        needs the same idempotent group_records footprint a non-empty one
        gets (spec §4.3/§5), the exact rev-2 bug class group_records exists
        to kill."""
        from elspeth.contracts import SourceRow

        manager, factory, run_id, source_node_id = _make_manager_context()
        initial = manager.create_initial_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=0,
            source_row=SourceRow.valid({"original": "data"}, contract=_make_observed_contract("original"), source_row_index=0),
            source_row_index=0,
            ingest_sequence=0,
        )
        members, group_id = manager.expand_token(
            parent_token=initial,
            expanded_rows=[{"item": 0}, {"item": 1}],
            output_contract=_make_observed_contract("item"),
            node_id=NodeID("expand_node"),
            run_id=run_id,
        )

        released = manager.collect_tokens(
            members=members,
            output_rows=[],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )
        assert released == ()

        records = factory.data_flow.get_group_records_for_run(run_id)
        empty_releases = [r for r in records if r["opener_token_id"] == members[0].token_id and r["group_id"] != group_id]
        assert len(empty_releases) == 1
        assert empty_releases[0]["member_count"] == 0

    def test_requires_at_least_one_member(self) -> None:
        manager, _factory, run_id, _source_node_id = _make_manager_context()
        with pytest.raises(OrchestrationInvariantError):
            manager.collect_tokens(
                members=[],
                output_rows=[],
                node_id=NodeID("collector_node"),
                run_id=run_id,
                group_id="g-exp-1",
            )


class TestReleaseFactMeta38:
    """META-38: the WRITTEN release fact. collect_tokens records
    ``group_records.closes_group_id`` = the group it closed on BOTH release
    insert arms (M>0 and the M=0 empty release); real fork/expand openers
    leave it NULL. ``is_release_group`` reads that fact and nothing else."""

    def _expand(self, manager: TokenManager, run_id: str, source_node_id: str) -> tuple[list[Any], str]:
        from elspeth.contracts import SourceRow

        initial = manager.create_initial_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=0,
            source_row=SourceRow.valid({"original": "data"}, contract=_make_observed_contract("original"), source_row_index=0),
            source_row_index=0,
            ingest_sequence=0,
        )
        return manager.expand_token(
            parent_token=initial,
            expanded_rows=[{"item": 0}, {"item": 1}],
            output_contract=_make_observed_contract("item"),
            node_id=NodeID("expand_node"),
            run_id=run_id,
        )

    def test_release_row_writes_closes_group_id_and_openers_leave_it_null(self) -> None:
        manager, factory, run_id, source_node_id = _make_manager_context()
        members, group_id = self._expand(manager, run_id, source_node_id)
        [child] = manager.collect_tokens(
            members=members,
            output_rows=[_make_pipeline_row({"combined": True})],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )
        release_group_id = child.lineage_path[-1].group_id

        rows = {str(r["group_id"]): r for r in factory.data_flow.get_group_records_for_run(run_id)}
        assert rows[release_group_id]["closes_group_id"] == group_id
        assert rows[group_id]["closes_group_id"] is None

    def test_empty_release_writes_the_fact_too(self) -> None:
        manager, factory, run_id, source_node_id = _make_manager_context()
        members, group_id = self._expand(manager, run_id, source_node_id)
        assert (
            manager.collect_tokens(members=members, output_rows=[], node_id=NodeID("collector_node"), run_id=run_id, group_id=group_id)
            == ()
        )
        rows = [r for r in factory.data_flow.get_group_records_for_run(run_id) if r["closes_group_id"] == group_id]
        assert [int(r["member_count"]) for r in rows] == [0]

    def test_is_release_group_reads_the_durable_fact_and_fails_closed_on_a_missing_row(self) -> None:
        from elspeth.contracts.errors import AuditIntegrityError

        manager, factory, run_id, source_node_id = _make_manager_context()
        members, group_id = self._expand(manager, run_id, source_node_id)
        [child] = manager.collect_tokens(
            members=members,
            output_rows=[_make_pipeline_row({"combined": True})],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )
        release_group_id = child.lineage_path[-1].group_id

        assert factory.data_flow.is_release_group(run_id=run_id, group_id=release_group_id) is True
        assert factory.data_flow.is_release_group(run_id=run_id, group_id=group_id) is False
        with pytest.raises(AuditIntegrityError, match="never minted"):
            factory.data_flow.is_release_group(run_id=run_id, group_id="never-minted")

    def test_token_manager_memo_is_populated_only_by_the_durable_read(self) -> None:
        """The minting leader learns the fact the SAME way a follower or a
        resumed process does — through the durable read, never from the
        CommittedCollect it just received; one read per (run, group); a
        fail-closed raise is not memoised."""
        from unittest.mock import patch

        from elspeth.contracts.errors import AuditIntegrityError

        manager, factory, run_id, source_node_id = _make_manager_context()
        members, group_id = self._expand(manager, run_id, source_node_id)
        [child] = manager.collect_tokens(
            members=members,
            output_rows=[_make_pipeline_row({"combined": True})],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )
        release_group_id = child.lineage_path[-1].group_id
        assert manager._release_group_memo == {}, "the mint must not seed the memo"

        with patch.object(factory.data_flow, "is_release_group", wraps=factory.data_flow.is_release_group) as durable_read:
            assert manager.is_release_group(run_id, release_group_id) is True
            assert manager.is_release_group(run_id, release_group_id) is True
            assert manager.is_release_group(run_id, group_id) is False
            assert durable_read.call_count == 2
            with pytest.raises(AuditIntegrityError):
                manager.is_release_group(run_id, "never-minted")
            with pytest.raises(AuditIntegrityError):
                manager.is_release_group(run_id, "never-minted")
            assert durable_read.call_count == 4
        assert manager._release_group_memo == {(run_id, release_group_id): True, (run_id, group_id): False}
