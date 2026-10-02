"""Real database authority for shared audit fixtures."""

import pytest

from tests.fixtures.landscape import claim_test_work_item, leader_member_token, make_recorder_with_run, register_test_node


def test_claim_helper_reuses_exact_claim_without_renewing_and_separates_nodes() -> None:
    setup = make_recorder_with_run()
    register_test_node(setup.data_flow, setup.run_id, "transform_a")
    register_test_node(setup.data_flow, setup.run_id, "transform_b")
    _, token = setup.data_flow.create_row_with_token(
        coordination_token=setup.coordination_token,
        source_node_id=setup.source_node_id,
        row_index=0,
        data={"value": 1},
        source_row_index=0,
        ingest_sequence=0,
    )
    member = leader_member_token(setup.factory, setup.run_id)
    first = claim_test_work_item(setup.factory, member_token=member, token_id=token.token_id, node_id="transform_a")
    repeated = claim_test_work_item(setup.factory, member_token=member, token_id=token.token_id, node_id="transform_a")
    second = claim_test_work_item(setup.factory, member_token=member, token_id=token.token_id, node_id="transform_b", step_index=1)
    assert repeated == first
    assert second.work_item_id != first.work_item_id
    with pytest.raises(AssertionError, match="different step index"):
        claim_test_work_item(setup.factory, member_token=member, token_id=token.token_id, node_id="transform_a", step_index=2)
    other = make_recorder_with_run()
    with pytest.raises(AssertionError, match="does not belong to run"):
        claim_test_work_item(
            setup.factory,
            member_token=leader_member_token(other.factory, other.run_id),
            token_id=token.token_id,
            node_id="transform_a",
        )
