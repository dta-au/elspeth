"""WS1b Phase B flip: TokenWorkItem retires branch_name/fork_group_id/
expand_group_id (join_group_id stays — decision D1) and the journal codec
reconstructs TokenInfo from lineage_path alone."""

import dataclasses
from datetime import UTC, datetime

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")


def test_token_work_item_lineage_fields_retired_and_path_added() -> None:
    names = {f.name for f in dataclasses.fields(TokenWorkItem)}
    assert {"branch_name", "fork_group_id", "expand_group_id"} & names == set()
    assert "join_group_id" in names  # ruling 20 / decision D1
    assert "lineage_path" in names
    assert {"coalesce_node_id", "coalesce_name", "row_union_name", "barrier_key", "barrier_adopted_epoch"} <= names


def test_token_from_journal_item_reconstructs_the_path_purely() -> None:
    from elspeth.core.landscape.scheduler.payload_codec import serialize_row_payload, token_from_journal_item
    from elspeth.testing import make_row

    now = datetime.now(UTC)
    item = TokenWorkItem(
        work_item_id="w1",
        run_id="run1",
        token_id="t1",
        row_id="r1",
        node_id="n1",
        step_index=1,
        ingest_sequence=0,
        row_payload_json=serialize_row_payload(make_row({"a": 1})),
        status=TokenWorkStatus.READY,
        attempt=1,
        available_at=now,
        created_at=now,
        updated_at=now,
        lineage_path=(FORK,),
    )
    token = token_from_journal_item(item, attempt_offset=0, resume_checkpoint_id=None)
    assert token.lineage_path == (FORK,)
    assert token.branch_name == "path_a"
