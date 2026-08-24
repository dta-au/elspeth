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

    def test_empty_output_mints_nothing(self) -> None:
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
            output_rows=[],
            node_id=NodeID("collector_node"),
            run_id=run_id,
            group_id=group_id,
        )
        assert released == ()

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
