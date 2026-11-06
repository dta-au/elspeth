"""Helper controls supplementing the normal settings/run regression."""

import pytest

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import BranchName, CoalesceName, CollectorName, RowUnionName
from elspeth.testing import make_contract, make_token_info
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.engine.test_first_bound_frame_rederivation import _STEPS, OPENER, _mint, _registry
from tests.unit.engine.test_processor import _make_factory, _make_processor
from tests.unit.engine.test_processor_collector_wiring import TestNestedReleaseCursor as _NestedCursorFixture
from tests.unit.engine.test_processor_collector_wiring import TestReleaseCursorWalk as _ReleaseFixture

_EXPAND = _NestedCursorFixture.OUTER_EXPAND
_COALESCE = LineageFrame(kind=FrameKind.FORK, group_id="fork-coalesce", member_key="path_a")
_UNION = LineageFrame(kind=FrameKind.FORK, group_id="fork-union", member_key="path_u")
_UNBOUND = LineageFrame(kind=FrameKind.FORK, group_id="fork-unbound", member_key="consumer_path")


@pytest.mark.parametrize(
    ("lineage", "expected"),
    [
        ((_COALESCE, _EXPAND), (None, None, CollectorName("outer_stitch"))),
        ((_UNION, _EXPAND), (None, None, CollectorName("outer_stitch"))),
        ((_UNBOUND, _EXPAND), (None, None, CollectorName("outer_stitch"))),
        ((_EXPAND, _COALESCE), (CoalesceName("merge"), None, None)),
        ((_EXPAND, _UNION), (None, RowUnionName("variants"), None)),
        ((_EXPAND, _UNBOUND), (None, None, CollectorName("outer_stitch"))),
        ((_COALESCE, _EXPAND, _UNBOUND), (None, None, CollectorName("outer_stitch"))),
        ((_COALESCE, _UNBOUND), (CoalesceName("merge"), None, None)),
        ((_UNBOUND,), (None, None, None)),
        ((), (CoalesceName("completed"), None, None)),
        ((_COALESCE,), (CoalesceName("merge"), None, None)),
        ((_EXPAND,), (None, None, CollectorName("outer_stitch"))),
    ],
    ids=[
        "nearer-collector-than-coalesce",
        "nearer-collector-than-union",
        "collector-inside-unbound-fork",
        "nearer-coalesce-than-collector",
        "nearer-union-than-collector",
        "unbound-fork-inside-collector",
        "unbound-fork-inside-collector-inside-coalesce",
        "unbound-fork-inside-coalesce",
        "unbound-fork-with-no-barrier",
        "flat-terminal-completed-coalesce",
        "outer-coalesce",
        "outer-collector",
    ],
)
def test_merged_cursor_selects_nearest_bound_frame(
    lineage: tuple[LineageFrame, ...],
    expected: tuple[CoalesceName | None, RowUnionName | None, CollectorName | None],
) -> None:
    db, factory = _make_factory()
    try:
        processor = _NestedCursorFixture()._processor(factory)
        token = make_token_info(data={}, lineage_path=lineage)
        assert processor._merged_continuation_cursor(token, CoalesceName("completed")) == expected
    finally:
        db.close()


@pytest.mark.parametrize("record_opener_state", [True, False], ids=["cold-durable-scope", "missing-required-opener-state"])
def test_merged_cursor_rederives_or_refuses_nearer_scope_before_outer_fork(record_opener_state: bool) -> None:
    """Synthetic outer FORK selection control over a genuinely minted cold EXPAND."""
    db, factory = _make_factory()
    try:
        children, group_id = _mint(factory, opener_node=OPENER, registry=_registry(), record_opener_state=record_opener_state)
        processor = _make_processor(
            factory,
            node_step_map=_STEPS,
            group_bindings=_registry(),
            branch_to_coalesce={BranchName("path_a"): CoalesceName("outer_merge")},
        )
        child = children[0]
        token = make_token_info(data={}, token_id=child.token_id, row_id=child.row_id, lineage_path=(_COALESCE, *child.lineage_path))
        assert processor._group_bindings.binding_for(child.lineage_path[-1]) is None
        if record_opener_state:
            assert processor._merged_continuation_cursor(token, CoalesceName("completed")) == (None, None, CollectorName("stitch"))
            assert processor._group_bindings.binding_for(child.lineage_path[-1]) is not None
        else:
            with pytest.raises(AuditIntegrityError, match="holds no node_state there"):
                processor._merged_continuation_cursor(token, CoalesceName("completed"))
            assert group_id not in processor._inert_expand_groups
    finally:
        db.close()


def test_merged_cursor_refuses_missing_nearer_group_instead_of_using_outer_fork() -> None:
    db, factory = _make_factory()
    try:
        processor = _NestedCursorFixture()._processor(factory)
        missing = LineageFrame(kind=FrameKind.EXPAND, group_id="not-minted", member_key="not-minted-member")
        token = make_token_info(data={}, lineage_path=(_COALESCE, missing))
        with pytest.raises(AuditIntegrityError, match="group_records has no such group"):
            processor._merged_continuation_cursor(token, CoalesceName("completed"))
    finally:
        db.close()


@pytest.mark.parametrize("outer_scope", [True, False], ids=["outer-scope-past-release-frame", "flat-release-retains-terminal-fallback"])
def test_merged_cursor_walks_past_real_inert_collector_release_frame(outer_scope: bool) -> None:
    db, factory = _make_factory()
    try:
        fixture = _ReleaseFixture()
        registry = fixture._registry()
        processor = _make_processor(factory, group_bindings=registry)
        row_id, root_id = fixture._seed_root(factory)
        if outer_scope:
            members, group_id = factory.data_flow.expand_token(
                member_token=leader_coordination_token(factory, "test-run").membership,
                parent_ref=TokenRef(token_id=root_id, run_id="test-run"),
                row_id=row_id,
                child_payloads=[{"page": 1}],
                output_contract=make_contract(),
            )
            registry.register_expand_group(group_id, opener_name="outer_explode")
            root_id = members[0].token_id
        release = fixture._collect_release(factory, scope_parent_id=root_id, row_id=row_id)
        assert release.lineage_path[-1].kind is FrameKind.EXPAND
        expected = (None, None, CollectorName("outer_stitch")) if outer_scope else (CoalesceName("completed"), None, None)
        assert processor._merged_continuation_cursor(release, CoalesceName("completed")) == expected
        assert release.lineage_path[-1].group_id in processor._inert_expand_groups
    finally:
        db.close()
