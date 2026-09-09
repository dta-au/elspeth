"""Claim-context frame guard (spec §6.2): a staged loss is legal iff the
claimed token's own lineage_path contains a frame matching
(group_id, member_key) — self-authenticating because frames are minted by
openers, never asserted by failing code.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import GroupLossSpec
from tests.unit.engine.test_processor import _make_claimed_work_item, _make_factory, _make_processor

FORK_FRAME = LineageFrame(kind=FrameKind.FORK, group_id="fg_1", member_key="path_a")
OUTER_EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_1", member_key="tok_m1")


def _loss(group_id="fg_1", member_key="path_a", token_id="tok_x"):
    return GroupLossSpec(
        closer_name="merge_paths",
        group_id=group_id,
        member_key=member_key,
        token_id=token_id,
        reason="quarantined",
    )


@pytest.fixture
def drain():
    _db, factory = _make_factory()
    processor = _make_processor(factory)
    return processor._scheduler_drain


@pytest.fixture
def claimed_item():
    def _factory(*, lineage_path=(), token_id="tok-claim"):
        return _make_claimed_work_item(token_id=token_id, lineage_path=lineage_path)

    return _factory


def test_guard_accepts_loss_whose_frame_the_claimed_path_carries(drain, claimed_item):
    claimed = claimed_item(lineage_path=(OUTER_EXPAND, FORK_FRAME))
    drain._pending_group_losses.append(_loss(token_id=claimed.token_id))
    assert drain.take_claim_group_losses(claimed) == (_loss(token_id=claimed.token_id),)
    assert drain._pending_group_losses == []


def test_guard_rejects_group_id_match_with_wrong_member_key(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.append(_loss(member_key="path_b"))
    with pytest.raises(OrchestrationInvariantError, match="lineage path"):
        drain.take_claim_group_losses(claimed)


def test_guard_rejects_member_key_match_with_wrong_group_id(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.append(_loss(group_id="fg_OTHER"))
    with pytest.raises(OrchestrationInvariantError, match="lineage path"):
        drain.take_claim_group_losses(claimed)


def test_guard_rejects_two_losses_for_one_frame(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.extend([_loss(), _loss(token_id="tok_y")])
    with pytest.raises(OrchestrationInvariantError, match="at most one loss per bound frame"):
        drain.take_claim_group_losses(claimed)


def test_guard_allows_empty_staging(drain, claimed_item):
    assert drain.take_claim_group_losses(claimed_item(lineage_path=())) == ()
