"""Spec §6.3: intake-only escalation. One loss against the ENCLOSING bound
frame, staged in the adoption transaction, authenticated against the durable
roster authority; verdicts wait for settlement; best_effort enclosing
closers still receive the loss.

Harness built directly on real Landscape audit tables (real fork_token/
create_token/record_token_outcome writes via `_make_factory`'s in-memory DB)
rather than the full Orchestrator: escalation reads durable group_records /
token_lineage_frames / token_outcomes state. The five parking/staging tests
never need a functioning CoalesceExecutor.notify_branch_lost — no
run_barrier_intake pass in them touches a NEWLY staged escalated loss on a
SECOND pass (see `_UnusedCoalesceExecutor`'s docstring). The final
composition test (`test_staged_escalation_replays_and_settles_the_enclosing_
closer_on_next_pass`) is the deliberate exception: it wires a REAL
CoalesceExecutor for the enclosing closer and drives two passes, proving the
staged row is actually replayed and the enclosing closer genuinely settles —
not just that the row's fields are correct in isolation (review round 1,
finding 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, TerminalOutcome, TerminalPath
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.config import CoalesceSettings
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from tests.fixtures.factories import make_context
from tests.unit.engine.test_processor import _TEST_LEADER_WORKER_ID, _make_factory, _make_processor


class _UnusedCoalesceExecutor:
    """Non-None coalesce-executor stand-in, needed only to satisfy
    `BarrierIntakeCoordinator.run_intake_pass`'s early-return guard (`not
    aggregation_settings and coalesce_executor is None and
    row_union_executor is None`). Four of this file's five tests never drive
    a SECOND intake pass over a newly-staged escalated loss (that would be
    `_replay_group_losses` calling into the executor), so both methods raise
    if actually invoked there — a silent no-op double would hide the day
    this stops being true. `notify_branch_lost` always returns None (no
    consequence yet) rather than raising: the one test that DOES redrive a
    second pass (`test_escalation_staging_is_idempotent_across_redrive`)
    needs replay to drain the already-staged loss without crashing, but is
    testing `record_group_loss`'s natural-key idempotence, not this
    executor's own group-failure semantics (covered elsewhere, e.g.
    test_coalesce_sweep_escalation_durability.py). `has_recorded_branch_loss`
    always returns False so replay genuinely calls `notify_branch_lost`
    every pass — idempotency is proven by the natural key, never by this
    double silently skipping."""

    def has_recorded_branch_loss(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def notify_branch_lost(self, *args: Any, **kwargs: Any) -> Any:
        return None


@dataclass
class _NestedIntakeHarness:
    coordinator: Any  # RowProcessor — brief names the attribute `coordinator`
    factory: Any
    run_id: str
    ctx: Any
    outer_group_id: str | None = None
    outer_member_key: str | None = None
    outer_closer_name: str | None = None
    outer_member_token_id: str | None = None
    inner_group_id: str | None = None
    inner_closer_name: str | None = None
    inner_member_tokens: dict[str, str] = field(default_factory=dict)
    solo_group_id: str | None = None
    solo_closer_name: str | None = None
    solo_member_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def inner_opener_token_id(self) -> str:
        assert self.outer_member_token_id is not None
        return self.outer_member_token_id

    def group_losses(self) -> list[Any]:
        return self.factory.scheduler.list_group_losses(run_id=self.run_id)

    def _terminalize(self, token_id: str) -> None:
        self.factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=token_id, run_id=self.run_id),
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
            error_hash=compute_error_hash("quarantined"),
        )

    def fail_inner_group_and_settle_all_members(self) -> None:
        self.coordinator._barrier_intake.note_group_failed(
            closer_name=self.inner_closer_name, group_id=self.inner_group_id, reason="quarantined"
        )
        for token_id in self.inner_member_tokens.values():
            self._terminalize(token_id)

    def fail_inner_group_leaving_one_member_live(self) -> None:
        self.coordinator._barrier_intake.note_group_failed(
            closer_name=self.inner_closer_name, group_id=self.inner_group_id, reason="quarantined"
        )
        first_key = next(iter(self.inner_member_tokens))
        self._terminalize(self.inner_member_tokens[first_key])

    def settle_remaining_member(self) -> None:
        for token_id in self.inner_member_tokens.values():
            outcome = self.factory.data_flow.get_token_outcome(token_id)
            if outcome is None or not outcome.completed:
                self._terminalize(token_id)

    def reark_inner_failure(self) -> None:
        """Re-park the SAME (closer_name, group_id) note — simulates a
        takeover re-deriving an identical FAIL verdict from durable state
        (note_group_failed's own docstring)."""
        self.coordinator._barrier_intake.note_group_failed(
            closer_name=self.inner_closer_name, group_id=self.inner_group_id, reason="quarantined"
        )

    def fail_group_and_settle_all_members(self) -> None:
        self.coordinator._barrier_intake.note_group_failed(
            closer_name=self.solo_closer_name, group_id=self.solo_group_id, reason="quarantined"
        )
        for token_id in self.solo_member_tokens.values():
            self._terminalize(token_id)

    def outer_group_failed(self) -> bool:
        assert self.outer_member_token_id is not None
        outcome = self.factory.data_flow.get_token_outcome(self.outer_member_token_id)
        return outcome is not None and outcome.completed and outcome.outcome is TerminalOutcome.FAILURE


@pytest.fixture
def nested_intake_harness():
    def _build(*, inner_policy: str = "require_all", outer_policy: str = "require_all", nesting: bool = True):
        run_id = "test-run"
        _db, factory = _make_factory(run_id=run_id)
        row = factory.data_flow.create_row(run_id, "source-0", 0, {"value": 1}, source_row_index=0, ingest_sequence=0)
        root = factory.data_flow.create_token(row.row_id)

        h = _NestedIntakeHarness(coordinator=None, factory=factory, run_id=run_id, ctx=make_context(run_id=run_id))

        if nesting:
            outer_children, outer_group_id = factory.data_flow.fork_token(
                TokenRef(token_id=root.token_id, run_id=run_id), row.row_id, ["outer_a"]
            )
            (outer_child,) = outer_children
            inner_children, inner_group_id = factory.data_flow.fork_token(
                TokenRef(token_id=outer_child.token_id, run_id=run_id),
                row.row_id,
                ["inner_1", "inner_2"],
                parent_lineage_path=outer_child.lineage_path,
            )
            outer_binding = GroupBinding(
                kind=FrameKind.FORK,
                opener_node_id=NodeID("outer-opener"),
                opener_name="outer-opener",
                closer_node_id=NodeID("coalesce::outer_closer"),
                closer_name="outer_closer",
                closer_kind=CloserKind.COALESCE,
                policy=outer_policy,
                on_group_failure=None,
                member_roster=("outer_a",),
            )
            inner_binding = GroupBinding(
                kind=FrameKind.FORK,
                opener_node_id=NodeID("inner-opener"),
                opener_name="inner-opener",
                closer_node_id=NodeID("coalesce::inner_closer"),
                closer_name="inner_closer",
                closer_kind=CloserKind.COALESCE,
                policy=inner_policy,
                on_group_failure=None,
                member_roster=("inner_1", "inner_2"),
            )
            registry = GroupBindingRegistry(bindings=(outer_binding, inner_binding))
            coalesce_node_ids = {
                CoalesceName("outer_closer"): NodeID("coalesce::outer_closer"),
                CoalesceName("inner_closer"): NodeID("coalesce::inner_closer"),
            }
            h.outer_group_id = outer_group_id
            h.outer_member_key = "outer_a"
            h.outer_closer_name = "outer_closer"
            h.outer_member_token_id = outer_child.token_id
            h.inner_group_id = inner_group_id
            h.inner_closer_name = "inner_closer"
            h.inner_member_tokens = {child.lineage_path[-1].member_key: child.token_id for child in inner_children}
        else:
            solo_children, solo_group_id = factory.data_flow.fork_token(
                TokenRef(token_id=root.token_id, run_id=run_id), row.row_id, ["solo_1", "solo_2"]
            )
            solo_binding = GroupBinding(
                kind=FrameKind.FORK,
                opener_node_id=NodeID("solo-opener"),
                opener_name="solo-opener",
                closer_node_id=NodeID("coalesce::solo_closer"),
                closer_name="solo_closer",
                closer_kind=CloserKind.COALESCE,
                policy=inner_policy,
                on_group_failure=None,
                member_roster=("solo_1", "solo_2"),
            )
            registry = GroupBindingRegistry(bindings=(solo_binding,))
            coalesce_node_ids = {CoalesceName("solo_closer"): NodeID("coalesce::solo_closer")}
            h.solo_group_id = solo_group_id
            h.solo_closer_name = "solo_closer"
            h.solo_member_tokens = {child.lineage_path[-1].member_key: child.token_id for child in solo_children}

        proc = _make_processor(
            factory,
            run_id=run_id,
            group_bindings=registry,
            coalesce_executor=_UnusedCoalesceExecutor(),
            coalesce_node_ids=coalesce_node_ids,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        h.coordinator = proc
        return h

    return _build


def test_fail_verdict_with_settled_roster_stages_enclosing_loss(nested_intake_harness):
    """Inner require_all coalesce fails; all inner members settled durably.
    ONE group_losses row appears against the OUTER frame — (outer closer,
    outer group, outer member) — with reason 'group_failed' and the inner
    group's opener token as token_id. Kills the
    escalation-against-failing-frame mutant (asserting the OUTER group_id,
    not the failed group's)."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    ledger = h.group_losses()
    escalated = [loss for loss in ledger if loss.reason == "group_failed"]
    assert len(escalated) == 1
    (loss,) = escalated
    assert loss.group_id == h.outer_group_id  # enclosing, NOT fg_inner
    assert loss.member_key == h.outer_member_key
    assert loss.closer_name == h.outer_closer_name
    assert loss.token_id == h.inner_opener_token_id


def test_fail_verdict_with_unsettled_roster_defers(nested_intake_harness):
    """A member still live (no terminal, no loss): the verdict parks; no
    escalated row this pass. It stages on a later pass once the member
    settles."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_leaving_one_member_live()
    h.coordinator.run_barrier_intake(h.ctx)
    assert [loss for loss in h.group_losses() if loss.reason == "group_failed"] == []
    h.settle_remaining_member()
    h.coordinator.run_barrier_intake(h.ctx)
    assert len([loss for loss in h.group_losses() if loss.reason == "group_failed"]) == 1


def test_outermost_fail_verdict_discards_note(nested_intake_harness):
    """No enclosing bound frame: today's behaviour verbatim (ruling 19) —
    nothing escalated, members already terminalized FAILURE/UNROUTED."""
    h = nested_intake_harness(nesting=False)
    h.fail_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    assert [loss for loss in h.group_losses() if loss.reason == "group_failed"] == []


def test_best_effort_enclosing_closer_still_receives_the_loss(nested_intake_harness):
    """Settlement propagation is policy-independent (§6.4): the loss is
    staged and notified; the enclosing best_effort closer absorbs it (its
    own group does NOT fail)."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="best_effort")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    assert len([loss for loss in h.group_losses() if loss.reason == "group_failed"]) == 1
    assert h.outer_group_failed() is False


def test_escalation_staging_is_idempotent_across_redrive(nested_intake_harness):
    """Escalated rows are materialized derivations, idempotent on the natural
    key, re-derivable at takeover (§6.3 item 3).

    `_stage_pending_escalations` deletes a note once it stages — a bare
    second `run_barrier_intake()` call with no re-park has nothing parked to
    redrive and would pin nothing (it would pass identically against a
    regressed implementation that staged a SECOND, distinct row). Re-park
    the identical note between passes to actually exercise
    `record_group_loss`'s natural-key idempotent tolerance."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    h.reark_inner_failure()  # simulate takeover re-deriving the identical FAIL verdict
    h.coordinator.run_barrier_intake(h.ctx)  # re-derive
    assert len([loss for loss in h.group_losses() if loss.reason == "group_failed"]) == 1


def test_staged_escalation_replays_and_settles_the_enclosing_closer_on_next_pass():
    """Composition test (review round 1, finding 3): the generic replay
    mechanism and the staged row's fields are each pinned separately
    elsewhere in this file — this proves the COMPOSITION. A real (non-
    double) CoalesceExecutor is wired for the enclosing ("outer_closer")
    closer; the inner group fails and settles exactly as in the other
    tests, but this time the SECOND `run_barrier_intake()` pass's replay
    step must actually call the real executor's `notify_branch_lost` and
    observe it settle the outer closer's row — proven via the same public
    `has_recorded_branch_loss` dedup check `_replay_group_losses` itself
    uses before calling `notify_branch_lost`."""
    run_id = "test-run"
    _db, factory = _make_factory(run_id=run_id)
    row = factory.data_flow.create_row(run_id, "source-0", 0, {"value": 1}, source_row_index=0, ingest_sequence=0)
    root = factory.data_flow.create_token(row.row_id)

    outer_children, outer_group_id = factory.data_flow.fork_token(TokenRef(token_id=root.token_id, run_id=run_id), row.row_id, ["outer_a"])
    (outer_child,) = outer_children
    inner_children, inner_group_id = factory.data_flow.fork_token(
        TokenRef(token_id=outer_child.token_id, run_id=run_id),
        row.row_id,
        ["inner_1", "inner_2"],
        parent_lineage_path=outer_child.lineage_path,
    )

    outer_binding = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("outer-opener"),
        opener_name="outer-opener",
        closer_node_id=NodeID("coalesce::outer_closer"),
        closer_name="outer_closer",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("outer_a",),
    )
    inner_binding = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("inner-opener"),
        opener_name="inner-opener",
        closer_node_id=NodeID("coalesce::inner_closer"),
        closer_name="inner_closer",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("inner_1", "inner_2"),
    )
    registry = GroupBindingRegistry(bindings=(outer_binding, inner_binding))
    coalesce_node_ids = {
        CoalesceName("outer_closer"): NodeID("coalesce::outer_closer"),
        CoalesceName("inner_closer"): NodeID("coalesce::inner_closer"),
    }

    # A REAL CoalesceExecutor for "outer_closer" — the enclosing closer this
    # test proves gets genuinely notified/settled. CoalesceSettings.branches
    # requires >=2 entries (pydantic min_length); "outer_b" is never
    # exercised — the GroupBinding's own member_roster (the settlement
    # walk's roster authority) is the SEPARATE, single-member authority that
    # actually governs this test's escalation.
    token_manager = TokenManager(factory.data_flow, step_resolver=lambda node_id: 2)
    outer_executor = CoalesceExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=token_manager,
        run_id=run_id,
        step_resolver=lambda node_id: 2,
        clock=MockClock(start=100.0),
        data_flow=factory.data_flow,
        barrier_restore_reads=factory.barrier_restore,
    )
    outer_executor.register_coalesce(
        CoalesceSettings(
            name="outer_closer",
            branches={"outer_a": "outer_a", "outer_b": "outer_b"},
            policy="require_all",
            merge="union",
            on_success="default",
        ),
        NodeID("coalesce::outer_closer"),
    )

    proc = _make_processor(
        factory,
        run_id=run_id,
        group_bindings=registry,
        coalesce_executor=outer_executor,
        coalesce_node_ids=coalesce_node_ids,
        scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
    )
    ctx = make_context(run_id=run_id)

    # Fail the inner group and settle all its members (identical to the
    # single-pass tests above).
    proc._barrier_intake.note_group_failed(closer_name="inner_closer", group_id=inner_group_id, reason="quarantined")
    for child in inner_children:
        factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=child.token_id, run_id=run_id),
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
            error_hash=compute_error_hash("quarantined"),
        )

    # Pass 1: stages the escalated loss against "outer_closer" durably.
    proc.run_barrier_intake(ctx)
    escalated = [loss for loss in factory.scheduler.list_group_losses(run_id=run_id) if loss.reason == "group_failed"]
    assert len(escalated) == 1
    assert escalated[0].closer_name == "outer_closer"
    # Not yet replayed — staging (pass 1) is a durable write only, never an
    # in-memory notify (§6.3 item 1).
    assert escalated[0].adopted_epoch is None

    # Pass 2: replay drains the pass-1 escalated row and calls the REAL
    # executor's notify_branch_lost — require_all with one lost branch
    # fails the group immediately, and the executor settles (completes)
    # this row.
    proc.run_barrier_intake(ctx)

    (replayed,) = [loss for loss in factory.scheduler.list_group_losses(run_id=run_id) if loss.reason == "group_failed"]
    assert replayed.adopted_epoch is not None, "the staged row must be marked adopted by pass 2's replay"

    # The genuinely settled proof: notify_branch_lost's own "already
    # completed" dedup (the same check _replay_group_losses relies on for
    # idempotent re-drives) now no-ops a repeat call — the real executor
    # considers this row's coalesce resolved, not merely "not yet told".
    # Keyed on outer_group_id (WS4 Task 8 re-key), not row.row_id — the
    # completed-keys/Landscape dedup check would miss under the wrong key,
    # as it did before this fix (the group settled under outer_group_id,
    # a lookup by row_id would find nothing and re-run the loss).
    assert outer_executor.notify_branch_lost("outer_closer", outer_group_id, "outer_a", "group_failed") is None
