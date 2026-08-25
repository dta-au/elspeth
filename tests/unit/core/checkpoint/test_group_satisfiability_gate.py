"""Fail-closed group-satisfiability resume gate (spec §8, ADR-038 amendment, ADR-042 D4).

Every minted member of every bound group must be lost (group_losses row,
adopted or not), live (a frame-bearing token without a completed terminal),
or arrived (a journal row at the group's closer). A member with none of the
three can never settle, so the roster can never close: refuse resume naming
closer, group, and member. Unbound groups are inert provenance and never
refuse (ADR-020 batch posture, made structural).
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.checkpoint.recovery import (
    GroupBindingView,
    check_group_satisfiability_resumable,
)
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import group_records_table, token_work_items_table
from tests.fixtures.group_lineage import (
    COALESCE_NODE_ID,
    EXPAND_GROUP,
    FORK_GROUP,
    NOW,
    OPENER_NODE_ID,
    RUN_ID,
    make_landscape_db,
    payload_json,
    seed_expand_group,
    seed_fork_member,
    seed_loss,
    seed_run,
    terminalize,
)

_FORK_BINDINGS = GroupBindingView(
    fork_branch_closers={"path_a": "merger", "path_b": "merger"},
    fork_branch_rosters={
        "path_a": ("path_a", "path_b"),
        "path_b": ("path_a", "path_b"),
    },
    scope_opener_closers={},
)
_UNBOUND = GroupBindingView(fork_branch_closers={}, fork_branch_rosters={}, scope_opener_closers={})
_SCOPE_BINDINGS = GroupBindingView(
    fork_branch_closers={},
    fork_branch_rosters={},
    scope_opener_closers={OPENER_NODE_ID: "page_stitcher"},
)


def _enqueue_journal_row(db: LandscapeDB, *, token_id: str, node_id: str) -> None:
    TokenSchedulerRepository(db.engine).enqueue_ready(
        run_id=RUN_ID,
        token_id=token_id,
        row_id="row-1",
        node_id=node_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=payload_json(),
        available_at=NOW,
    )


def _seed_arrival(db: LandscapeDB, *, token_id: str) -> None:
    """Journal evidence that the member's token reached the coalesce closer."""
    _enqueue_journal_row(db, token_id=token_id, node_id=COALESCE_NODE_ID)
    with db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.token_id == token_id)
            .values(coalesce_name="merger", barrier_key="merger")
        )


class TestForkGroups:
    def test_both_members_live_is_satisfiable(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        seed_fork_member(db, token_id="tok-b", member_key="path_b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert gate.check.can_resume
        assert gate.unsatisfiable_members == ()

    def test_terminal_member_without_loss_or_arrival_refuses_naming_the_member(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        seed_fork_member(db, token_id="tok-b", member_key="path_b")
        terminalize(db, "tok-b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert not gate.check.can_resume
        assert len(gate.unsatisfiable_members) == 1
        member = gate.unsatisfiable_members[0]
        assert member.closer_name == "merger"
        assert member.group_id == FORK_GROUP
        assert member.member_key == "path_b"
        assert member.kind is FrameKind.FORK
        # Exact-reason refusal (spec §9 row 5: "prefer exact-reason refusal").
        assert gate.check.reason is not None
        for needle in ("merger", FORK_GROUP, "path_b", "group_losses"):
            assert needle in gate.check.reason

    def test_group_losses_row_settles_the_member_adopted_or_not(self) -> None:
        for adopted_epoch in (None, 7):
            db = make_landscape_db()
            seed_run(db)
            seed_fork_member(db, token_id="tok-a", member_key="path_a")
            seed_fork_member(db, token_id="tok-b", member_key="path_b")
            terminalize(db, "tok-b")
            seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=adopted_epoch)
            gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
            assert gate.check.can_resume, f"adopted_epoch={adopted_epoch} must not filter the loss read"

    def test_arrived_terminal_member_is_settled(self) -> None:
        """A consumed member of a closed group: terminal, no loss, journal row at the closer."""
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        seed_fork_member(db, token_id="tok-b", member_key="path_b")
        _seed_arrival(db, token_id="tok-a")
        _seed_arrival(db, token_id="tok-b")
        terminalize(db, "tok-a")
        terminalize(db, "tok-b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert gate.check.can_resume

    def test_unbound_fork_group_never_refuses(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        terminalize(db, "tok-a")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _UNBOUND)
        assert gate.check.can_resume

    def test_declared_roster_member_never_minted_refuses(self) -> None:
        """Fail-closed: a declared branch with no frames rows can never settle either."""
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        terminalize(db, "tok-a")
        _seed_arrival(db, token_id="tok-a")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert not gate.check.can_resume
        assert [m.member_key for m in gate.unsatisfiable_members] == ["path_b"]

    def test_mixed_binding_within_one_group_is_integrity_error(self) -> None:
        """Ruling 23 whole-roster: one group half-bound is corrupt config/audit state."""
        db = make_landscape_db()
        seed_run(db)
        seed_fork_member(db, token_id="tok-a", member_key="path_a")
        seed_fork_member(db, token_id="tok-b", member_key="path_b")
        half_bound = GroupBindingView(
            fork_branch_closers={"path_a": "merger"},
            fork_branch_rosters={"path_a": ("path_a", "path_b")},
            scope_opener_closers={},
        )
        with pytest.raises(AuditIntegrityError, match="whole-roster"):
            check_group_satisfiability_resumable(db, RUN_ID, half_bound)


class TestExpandGroups:
    def test_bound_expand_member_terminal_unsettled_refuses(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        members = seed_expand_group(db, member_count=2)
        terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert not gate.check.can_resume
        member = gate.unsatisfiable_members[0]
        assert member.kind is FrameKind.EXPAND
        assert member.closer_name == "page_stitcher"
        assert member.member_key == members[0]

    def test_unbound_expand_group_is_inert(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        members = seed_expand_group(db, member_count=2)
        terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _UNBOUND)
        assert gate.check.can_resume

    def test_arrived_collector_member_is_settled_via_collector_name(self) -> None:
        """A flushed collector member: terminal, no loss, journal row whose
        collector_name (NOT bare barrier_key — WS4 formats collector
        barrier_key as 'collector:<name>:<group-id>') names the closer.

        barrier_key is deliberately NULL so this test discriminates the
        collector_name disjunct ALONE — with the compound key present the
        barrier_key disjunct rescues the row and a deleted collector_name
        disjunct survives green (measured: mutant MB4's first round)."""
        db = make_landscape_db()
        seed_run(db)
        members = seed_expand_group(db, member_count=1)
        _enqueue_journal_row(db, token_id=members[0], node_id=OPENER_NODE_ID)
        with db.engine.begin() as conn:
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.token_id == members[0])
                .values(collector_name="page_stitcher", barrier_key=None)
            )
        terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert gate.check.can_resume, f"collector_name disjunct missing: {gate.check.reason}"

    def test_arrived_collector_member_settles_via_compound_barrier_key_alone(self) -> None:
        """The compound-key disjunct in its own right: a row whose cursor
        column is somehow unset but whose barrier_key carries the WS4
        compound address still counts as arrived — the gate calls
        collector_barrier_key rather than re-deriving the format."""
        db = make_landscape_db()
        seed_run(db)
        members = seed_expand_group(db, member_count=1)
        _enqueue_journal_row(db, token_id=members[0], node_id=OPENER_NODE_ID)
        with db.engine.begin() as conn:
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.token_id == members[0])
                .values(collector_name=None, barrier_key=collector_barrier_key("page_stitcher", EXPAND_GROUP))
            )
        terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert gate.check.can_resume, f"compound barrier_key disjunct missing: {gate.check.reason}"

    def test_empty_expand_group_is_vacuously_satisfiable(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        seed_expand_group(db, member_count=0)
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert gate.check.can_resume

    def test_member_count_mismatch_is_integrity_error(self) -> None:
        db = make_landscape_db()
        seed_run(db)
        seed_expand_group(db, member_count=2)
        with db.engine.begin() as conn:
            conn.execute(update(group_records_table).where(group_records_table.c.group_id == EXPAND_GROUP).values(member_count=3))
        with pytest.raises(AuditIntegrityError, match="member_count"):
            check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
