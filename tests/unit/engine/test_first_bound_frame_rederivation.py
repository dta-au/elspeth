"""EXPAND-group binding re-derivation (META-9.1, integration C3.5).

The settle seam's frame resolver (`RowProcessor._first_bound_frame`) used to
treat any EXPAND frame the in-memory registry did not know as UNBOUND — and
the registry only ever learns a group in the process that minted it. So a
member lost on a non-opener worker, or after a resume, staged nothing and
stranded its group. These tests build the mint in ONE processor (registry A)
and settle in ANOTHER over the same durable store (registry B, fresh) — the
takeover shape — against the real Landscape reads.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import NodeID
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.testing import make_contract, make_token_info
from tests.unit.engine.test_processor import _make_factory, _make_processor, _persist_token_for_scheduler

OPENER = NodeID("explode-node")
OTHER_OPENER = NodeID("other-explode-node")
COLLECTOR = NodeID("collector-stitch")
OTHER_COLLECTOR = NodeID("collector-other")
PLAIN = NodeID("plain-explode")


def _registry(*, closer_node: NodeID = COLLECTOR) -> GroupBindingRegistry:
    return GroupBindingRegistry(
        bindings=(
            GroupBinding(
                kind=FrameKind.EXPAND,
                opener_node_id=OPENER,
                opener_name="explode",
                closer_node_id=closer_node,
                closer_name="stitch",
                closer_kind=CloserKind.COLLECTOR,
                policy="require_all",
                member_roster=(),
            ),
            GroupBinding(
                kind=FrameKind.EXPAND,
                opener_node_id=OTHER_OPENER,
                opener_name="other_explode",
                closer_node_id=OTHER_COLLECTOR,
                closer_name="other_stitch",
                closer_kind=CloserKind.COLLECTOR,
                policy="require_all",
                member_roster=(),
            ),
        )
    )


_STEPS = {NodeID("source-0"): 0, OPENER: 1, OTHER_OPENER: 2, COLLECTOR: 3, PLAIN: 4, OTHER_COLLECTOR: 5}


def _mint(factory: Any, *, opener_node: NodeID, registry: GroupBindingRegistry | None) -> tuple[list[TokenInfo], str]:
    """Run the opener at ``opener_node`` in a minting processor: a completed
    node_state for the parent at that node, then expand_token (durable
    group_records + children frames). ``registry`` None models an UNDECLARED
    opener (the minting process registers nothing)."""
    minting = _make_processor(factory, node_step_map=_STEPS, group_bindings=registry)
    parent = make_token_info(row_id="row-1", token_id=f"parent-{opener_node}")
    _persist_token_for_scheduler(factory, parent)
    factory.execution.record_completed_node_state(
        token_id=parent.token_id,
        node_id=str(opener_node),
        run_id="test-run",
        step_index=_STEPS[opener_node],
        input_data={"value": 1},
        output_data={"value": 1},
        duration_ms=1.0,
    )
    children, group_id = minting._token_manager.expand_token(
        parent_token=parent,
        expanded_rows=[{"value": 1}, {"value": 2}],
        output_contract=make_contract(),
        node_id=opener_node,
        run_id="test-run",
    )
    return children, group_id


def test_fresh_registry_rederives_a_declared_openers_binding_from_durable_state() -> None:
    _db, factory = _make_factory()
    children, group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())
    assert settling._group_bindings.binding_for(children[0].lineage_path[-1]) is None

    resolved = settling._first_bound_frame(children[0])

    assert resolved is not None
    frame, binding = resolved
    assert (frame.kind, frame.group_id, binding.closer_name, binding.opener_node_id) == (FrameKind.EXPAND, group_id, "stitch", OPENER)
    # Registered on the settling registry: the sibling's frame is now an
    # in-memory hit, no second durable read.
    with patch.object(settling._barrier_restore_reads, "get_group_record", side_effect=AssertionError("re-read")):
        assert settling._first_bound_frame(children[1]) == (children[1].lineage_path[-1], binding)


def test_undeclared_expansion_stays_inert_and_is_remembered() -> None:
    _db, factory = _make_factory()
    children, _group_id = _mint(factory, opener_node=PLAIN, registry=None)
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())

    assert settling._first_bound_frame(children[0]) is None
    with patch.object(settling._barrier_restore_reads, "get_group_record", side_effect=AssertionError("re-read")):
        assert settling._first_bound_frame(children[1]) is None


def test_no_declared_openers_means_no_durable_read_at_all() -> None:
    _db, factory = _make_factory()
    children, _group_id = _mint(factory, opener_node=OPENER, registry=None)
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=GroupBindingRegistry(bindings=()))
    with patch.object(settling._barrier_restore_reads, "get_group_record", side_effect=AssertionError("re-read")):
        assert settling._first_bound_frame(children[0]) is None


def _complete_member_at(factory: Any, token_id: str, node_id: NodeID) -> None:
    """A REAL completed node_state for a group member at ``node_id`` — the
    durable write `resolve_group_collector_node`'s join actually reads
    (META-35: the earlier version of these cases patched the resolver,
    which is exactly what hid the any-node false positive)."""
    factory.execution.record_completed_node_state(
        token_id=token_id,
        node_id=str(node_id),
        run_id="test-run",
        step_index=_STEPS[node_id],
        input_data={"value": 1},
        output_data={"value": 1},
        duration_ms=1.0,
    )


def test_intermediate_transform_completion_is_not_collector_evidence() -> None:
    """META-35 (elspeth-421d9004bb): a member completing an ORDINARY node
    between opener and collector — the realistic shape — is any-node
    evidence, not collector evidence. The re-derivation must bind, not
    raise a node-id mismatch."""
    _db, factory = _make_factory()
    children, group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    _complete_member_at(factory, children[0].token_id, PLAIN)
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())

    resolved = settling._first_bound_frame(children[0])

    assert resolved is not None
    assert (resolved[0].group_id, resolved[1].closer_name) == (group_id, "stitch")


def test_cross_check_is_membership_when_config_closer_is_one_of_the_scoped_nodes() -> None:
    # Membership, not equality: completions at BOTH configured collector
    # nodes (the nested shape's durable footprint) — config's closer is one
    # of them, so the re-derivation binds.
    _db, factory = _make_factory()
    children, _group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    _complete_member_at(factory, children[0].token_id, COLLECTOR)
    _complete_member_at(factory, children[1].token_id, OTHER_COLLECTOR)
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())

    resolved = settling._first_bound_frame(children[0])

    assert resolved is not None and resolved[1].closer_name == "stitch"


def test_completion_at_a_different_configured_collector_fails_closed() -> None:
    # The check the scoping must NOT lose: durable completion at another
    # CONFIGURED COLLECTOR node, config's closer not among the scoped
    # evidence — durable is authoritative.
    _db, factory = _make_factory()
    children, _group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    _complete_member_at(factory, children[0].token_id, OTHER_COLLECTOR)
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())

    with pytest.raises(AuditIntegrityError, match="durable is authoritative"):
        settling._first_bound_frame(children[0])


def test_frame_for_a_group_never_minted_fails_closed() -> None:
    _db, factory = _make_factory()
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())
    ghost = make_token_info(
        row_id="row-1", token_id="ghost", lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="never", member_key="ghost"),)
    )
    with pytest.raises(AuditIntegrityError, match="group_records has no such group"):
        settling._first_bound_frame(ghost)


def test_two_declared_opener_nodes_holding_the_opener_token_fail_closed() -> None:
    """Deleting this raise takes candidates[0] and stages the loss against
    the WRONG closer, silently (C3.5 review M-1)."""
    _db, factory = _make_factory()
    children, _group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    parent_id = f"parent-{OPENER}"
    factory.execution.record_completed_node_state(
        token_id=parent_id,
        node_id=str(OTHER_OPENER),
        run_id="test-run",
        step_index=_STEPS[OTHER_OPENER],
        input_data={"value": 1},
        output_data={"value": 1},
        duration_ms=1.0,
    )
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())
    with pytest.raises(AuditIntegrityError, match="holds node_states at 2 declared opener nodes"):
        settling._first_bound_frame(children[0])


def test_group_record_of_another_kind_fails_closed() -> None:
    from types import SimpleNamespace

    _db, factory = _make_factory()
    children, group_id = _mint(factory, opener_node=OPENER, registry=_registry())
    settling = _make_processor(factory, node_step_map=_STEPS, group_bindings=_registry())
    fork_record = SimpleNamespace(run_id="test-run", group_id=group_id, kind="fork", opener_token_id="x", member_count=2)
    with (
        patch.object(settling._barrier_restore_reads, "get_group_record", return_value=fork_record),
        pytest.raises(AuditIntegrityError, match="lineage/roster disagreement"),
    ):
        settling._first_bound_frame(children[0])


def test_processor_without_a_restore_read_model_fails_closed_instead_of_stranding() -> None:
    """The ExecutionRepository fallback has neither read; deleting this
    raise silently reinstates the stranding bug for fallback-wired
    processors."""
    from elspeth.engine.processor import DAGTraversalContext, RowProcessor
    from elspeth.engine.spans import SpanFactory
    from tests.fixtures.landscape import make_recorder_with_run

    setup = make_recorder_with_run(run_id="run-fallback", source_node_id="source-0")
    processor = RowProcessor(
        execution=setup.execution,
        data_flow=setup.data_flow,
        span_factory=SpanFactory(),
        run_id=setup.run_id,
        source_node_id=NodeID("source-0"),
        source_on_success="default",
        traversal=DAGTraversalContext(node_step_map={NodeID("source-0"): 0}, node_to_plugin={}, node_to_next={}, coalesce_node_map={}),
        scheduler=setup.factory.scheduler,
        group_bindings=_registry(),
    )
    token = make_token_info(
        row_id="row-1", token_id="orphan", lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g", member_key="m"),)
    )
    with pytest.raises(OrchestrationInvariantError, match="no barrier restore read model"):
        processor._first_bound_frame(token)
