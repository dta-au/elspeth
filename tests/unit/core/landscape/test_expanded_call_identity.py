"""Call parents bind to durable expansion positions, including corrupt evidence."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.schema import calls_table, token_parents_table
from tests.fixtures.landscape import claim_test_work_item, leader_coordination_token, register_test_node
from tests.unit.core.landscape.test_call_mode_persistence import _two_runs


@pytest.mark.parametrize("corruption", [None, "duplicate_member", "missing_parent", "cycle", "cross_row"])
@pytest.mark.parametrize("kind", ["expand", "fork"])
def test_expanded_call_identity_is_order_independent_and_rejects_corruption(corruption: str | None, kind: str) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    children_by_run = {}
    states_by_run = {}
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    for run_id in ("source", "current"):
        authority = leader_coordination_token(factory, run_id)
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        row, token = factory.data_flow.create_row_with_token(
            "source-node", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=authority
        )
        if kind == "expand":
            children, _group = factory.data_flow.expand_token(
                TokenRef(token_id=token.token_id, run_id=run_id),
                row.row_id,
                [{"value": 1}, {"value": 1}],
                output_contract=contract,
                member_token=authority.membership,
            )
        else:
            children, _group = factory.data_flow.fork_token(
                TokenRef(token_id=token.token_id, run_id=run_id),
                row.row_id,
                ["left", "right"],
                member_token=authority.membership,
                work_item=claim_test_work_item(factory, member_token=authority.membership, token_id=token.token_id, node_id=None),
            )
        children_by_run[run_id] = children
        states_by_run[run_id] = [
            factory.execution.begin_node_state(child.token_id, "transform", 2, {"value": 1}, member_token=authority.membership)
            for child in children
        ]
    # Insert in reverse member order; neither timestamps nor call IDs encode
    # the correct mapping. Each parent-local occurrence is index zero.
    with factory._db.write_connection() as conn:
        for index in (1, 0):
            conn.execute(
                calls_table.insert().values(
                    call_id=f"recorded-{1 - index}",
                    state_id=states_by_run["source"][index].state_id,
                    operation_id=None,
                    call_index=0,
                    call_type=CallType.HTTP.value,
                    status=CallStatus.SUCCESS.value,
                    request_hash=stable_hash({"url": "https://example.test/a"}),
                    response_hash=stable_hash({"member": index}),
                    created_at=datetime.now(UTC),
                )
            )
        second = children_by_run["source"][1]
        if corruption == "duplicate_member":
            conn.execute(update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(ordinal=0))
        elif corruption == "missing_parent":
            conn.execute(delete(token_parents_table).where(token_parents_table.c.token_id == second.token_id))
        elif corruption == "cycle":
            conn.execute(
                update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(parent_token_id=second.token_id)
            )
    if corruption == "cross_row":
        authority = leader_coordination_token(factory, "source")
        _other_row, other = factory.data_flow.create_row_with_token(
            "source-node", 1, {"value": 1}, source_row_index=1, ingest_sequence=1, coordination_token=authority
        )
        with factory._db.write_connection() as conn:
            conn.execute(
                update(token_parents_table).where(token_parents_table.c.token_id == second.token_id).values(parent_token_id=other.token_id)
            )

    def lookup(index: int):
        return factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=stable_hash({"url": "https://example.test/a"}),
            current_state_id=states_by_run["current"][index].state_id,
            current_operation_id=None,
            current_call_index=0,
        )

    if corruption is not None:
        with pytest.raises(AuditIntegrityError, match=r"ambiguous|lineage"):
            lookup(0)
    else:
        assert lookup(1).call_id == "recorded-0"
        assert lookup(0).call_id == "recorded-1"
