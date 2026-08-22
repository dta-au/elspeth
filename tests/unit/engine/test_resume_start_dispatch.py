"""tests/unit/engine/test_resume_start_dispatch.py

Pins resume-start ARM SELECTION (spec §4.1a: decisions, not field values).
Uses classify_resume_start — the pure arm-selection function extracted in this
task — so the pin needs no orchestrator scaffolding.
"""

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.engine.processor import ResumeStartArm, classify_resume_start

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")
OUTER = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="left")


@pytest.mark.parametrize(
    ("path", "join_group_id", "expected"),
    [
        # today's three depth-1 shapes — arm selection identical to the tri-field dispatch
        ((EXPAND,), None, ResumeStartArm.EXPAND_CHILD),
        ((FORK,), None, ResumeStartArm.FORK_CHILD),
        ((), "jg-1", ResumeStartArm.MERGED),
        # §4.1a nested shapes — the NEW pins
        ((FORK, EXPAND), None, ResumeStartArm.EXPAND_CHILD),  # expand child inside a fork branch: expand wins (innermost)
        ((EXPAND, FORK), None, ResumeStartArm.FORK_CHILD),  # fork child inside an expand: fork wins (innermost)
        ((OUTER,), "jg-1", ResumeStartArm.MERGED),  # merged token under an outer frame: MERGED wins over the frame
        ((OUTER, EXPAND), "jg-1", ResumeStartArm.MERGED),  # merged under outer expand: still MERGED, never EXPAND_CHILD
    ],
)
def test_arm_selection(path, join_group_id, expected) -> None:
    assert classify_resume_start(lineage_path=path, join_group_id=join_group_id) is expected


def test_no_lineage_and_no_join_is_an_invariant_error() -> None:
    with pytest.raises(Exception, match="no resume-start"):
        classify_resume_start(lineage_path=(), join_group_id=None)
