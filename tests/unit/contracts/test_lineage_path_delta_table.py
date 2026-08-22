"""tests/unit/contracts/test_lineage_path_delta_table.py

The spec §4.1a delta table, case by case. Each test names the topology row and
states BOTH truths: what today's destructive tri-field said (in-memory /
durable-row) and what the preservative accessor says. Rulings 26/27 are final:
the accessor value is the contract; the "today" values are recorded in comments
as the adjudicated delta, not asserted.
"""

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import (
    LineageFrame,
    path_branch_name,
    path_expand_group_id,
    path_fork_group_id,
)

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")
OUTER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="left")


def _accessors(path: tuple[LineageFrame, ...]) -> tuple[str | None, str | None, str | None]:
    # exactly the three helpers the flip exposes as TokenInfo properties (WS1a Task 1)
    return (path_branch_name(path), path_fork_group_id(path), path_expand_group_id(path))


def test_row_plain_and_single_frame_topologies_match_both_truths() -> None:
    # §4.1a row 1: plain / fork-only child / expand-only child — identical to both truths.
    assert _accessors(()) == (None, None, None)
    assert _accessors((FORK,)) == ("path_a", "fg-1", None)
    assert _accessors((EXPAND,)) == (None, None, "eg-1")


def test_row_expand_child_inside_a_fork_branch_adopts_the_in_memory_truth() -> None:
    # §4.1a row 2: today in-memory said branch_name="path_a" (inherited) while the
    # durable tokens row said None (data_flow/tokens.py:1374-1385 wrote neither);
    # fork_group_id was None in BOTH truths (expand_token dropped it). The accessor
    # adopts the in-memory branch AND regains the fork group.
    assert _accessors((FORK, EXPAND)) == ("path_a", "fg-1", "eg-1")


def test_row_fork_child_inside_an_expand_regains_the_outer_group() -> None:
    # §4.1a row 3: today expand_group_id was None (fork_token dropped it).
    assert _accessors((EXPAND, FORK)) == ("path_a", "fg-1", "eg-1")


def test_row_merged_token_under_outer_frames_sees_outer_membership() -> None:
    # §4.1a row 4: post-coalesce merged token. Strict pop removed the inner FORK
    # frame; the outer frames remain visible (today all None). Required for
    # whole-roster settlement at the outer closer.
    assert _accessors((OUTER_FORK,)) == ("left", "fg-outer", None)
    assert _accessors((OUTER_FORK, EXPAND)) == ("left", "fg-outer", "eg-1")


def test_row_merged_token_top_level_is_all_none() -> None:
    # §4.1a row 5: identical to today.
    assert _accessors(()) == (None, None, None)


def test_row_row_union_released_token_has_no_branch_identity() -> None:
    # §4.1a row 6 / ruling 27: the release PORTs each of the N tokens through a
    # strict pop, so branch_name/fork_group_id become None (today deliberately
    # retained, processor.py:3043-3048 — a WS1 delta; downstream reads audit rows).
    released_path = (FORK,)[:-1]
    assert released_path == ()
    assert _accessors(released_path) == (None, None, None)
