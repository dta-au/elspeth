"""Exercise collision diagnostics through the durable coalesce cleanup consumer."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.enums import FrameKind, NodeStateStatus, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import CoalesceCollisionError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import CoalesceSettings
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_token_info
from tests.fixtures.landscape import RecorderSetup, leader_token_for, make_factory, make_landscape_db, register_test_node


def make_diagnostic_recorder(*, run_id: str | None = None) -> RecorderSetup:
    """Create a diagnostic audit run with explicit production bootstrap and catalog identity."""
    prepare_for_run()
    db = make_landscape_db()
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    source_node_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE)
    return RecorderSetup(
        db=db,
        factory=factory,
        run_id=run.run_id,
        source_node_id=source_node_id,
        coordination_token=leader_token_for(db, run.run_id),
    )


def exercise_coalesce_metadata() -> None:
    """Require both failed siblings to retain changing branch provenance without values."""
    for branches in (("left", "right"), ("north", "south")):
        setup = make_diagnostic_recorder()
        try:
            node_id = NodeID(register_test_node(setup.data_flow, setup.run_id, "merge", node_type=NodeType.COALESCE))
            executor = CoalesceExecutor(
                execution=setup.execution,
                span_factory=SpanFactory(),
                token_manager=TokenManager(setup.data_flow, step_resolver=lambda node: 1),
                run_id=setup.run_id,
                step_resolver=lambda node: 1,
                data_flow=setup.data_flow,
                barrier_restore_reads=setup.factory.barrier_restore,
            )
            executor.register_coalesce(
                CoalesceSettings(
                    name="merge",
                    branches=list(branches),
                    policy="require_all",
                    merge="union",
                    union_collision_policy="fail",
                    on_success="out",
                ),
                node_id,
                output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
            )
            row, _ = setup.data_flow.create_row_with_token(
                source_node_id=setup.source_node_id,
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                data={},
                coordination_token=setup.coordination_token,
            )
            values = ("private-value-left-92641", "private-value-right-38270")
            tokens = []
            for branch, value in zip(branches, values, strict=True):
                lineage = (LineageFrame(kind=FrameKind.FORK, group_id="collision-group", member_key=branch),)
                token = setup.data_flow.create_token(row.row_id, coordination_token=setup.coordination_token, lineage_path=lineage)
                tokens.append(
                    make_token_info(
                        row_id=row.row_id,
                        token_id=token.token_id,
                        data={"shared": value},
                        lineage_path=lineage,
                    )
                )
            assert executor.accept(tokens[0], "merge", coordination_token=setup.coordination_token).held
            with pytest.raises(CoalesceCollisionError):
                executor.accept(tokens[1], "merge", coordination_token=setup.coordination_token)

            token_ids = [token.token_id for token in tokens]
            states = setup.query.get_node_states_for_tokens(setup.run_id, token_ids)
            assert len(states) == 2
            assert {state.token_id for state in states} == set(token_ids)
            for state in states:
                assert state.node_id == node_id
                assert state.status is NodeStateStatus.FAILED
                assert state.context_after_json is not None
                persisted = json.loads(state.context_after_json)
                assert persisted["union_field_origins"] == {"shared": branches[1]}
                assert persisted["union_field_collisions"] == {"shared": list(branches)}
                assert "union_field_collision_values" not in persisted
                assert all(value not in state.context_after_json for value in values)
                assert state.error_json is not None
                assert all(value not in state.error_json for value in values)
            outcomes = setup.query.get_token_outcomes_for_tokens(setup.run_id, token_ids)
            assert len(outcomes) == 2
            assert {(outcome.token_id, outcome.outcome, outcome.path) for outcome in outcomes} == {
                (token_id, TerminalOutcome.FAILURE, TerminalPath.UNROUTED) for token_id in token_ids
            }
        finally:
            setup.db.close()
