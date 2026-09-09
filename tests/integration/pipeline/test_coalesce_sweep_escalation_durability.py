# tests/integration/pipeline/test_coalesce_sweep_escalation_durability.py
"""WS3 Task 6 fix round 1 (Ruling 39 / C2) — a timeout/EOF sweep's escalated
group loss becomes durable in the SAME transaction as its own barrier
terminalization, over a REAL CoalesceExecutor and REAL LandscapeDB (no
mocks).

Before the fix, `_terminalize_swept_coalesce_failure` staged the escalated
`GroupLossSpec` into `RowProcessor._pending_group_losses` with no committing
consumer anywhere in `orchestrator/`: a mid-run sweep's orphaned spec would
poison a LATER, unrelated claim's `take_claim_group_losses` guard (that
claim's own lineage cannot carry the stale spec's frame), and an EOF-flush
sweep's spec would simply never reach the durable `group_losses` table at
all — a silent audit-completeness hole in exactly the record WS5/6 resume
and Task 8 escalation are meant to read.

This drives `handle_coalesce_timeouts` directly against a nested pair of
REAL `CoalesceExecutor` registrations (`merge_inner` closes an inner FORK
group whose release is one of `merge_outer`'s declared branches) so the
settlement channel's remaining-path walk genuinely resolves to a bound
OUTER frame — the exact shape the sweep path has to escalate.

Both tests also pin completeness at the REAL `token_outcomes` DB layer
(I2 / Ruling 40): N consumed tokens produce N durable (FAILURE, UNROUTED)
rows, not just N calls into a mock's captured arguments — the mock-level
completeness tests in `test_outcomes.py` stay (they catch a dropped token
in the sweep-to-port handoff), this is the layer beneath them that catches
a dropped WRITE.

The second test in this file (`test_three_branch_timeout_dedupes_escalation_to_one_outer_loss`)
is the fix-round-1 Ruling 38 / C1 reproduction: a require_all inner group
whose LAST branch times out with TWO siblings already arrived and held
hands `_fail_pending` two consumed tokens that share one enclosing frame.
It is deliberately driven through `check_timeouts`/`handle_coalesce_timeouts`
rather than a live branch-loss notification through the full Orchestrator:
an initial attempt at a full YAML/Orchestrator reproduction surfaced a
SEPARATE, related dedup gap in the live intake path (see the fix-round-1
report's concerns section) that this unit-level construction avoids by
construction — accepting both siblings directly and firing the timeout in
one controlled call, with no barrier-intake adoption race in the mix.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import BranchName, CoalesceName, NodeID
from elspeth.core.config import CoalesceSettings
from elspeth.core.landscape.schema import group_losses_table, token_outcomes_table
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.orchestrator.outcomes import handle_coalesce_timeouts
from elspeth.engine.orchestrator.types import ExecutionCounters
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_row
from tests.fixtures.factories import make_context
from tests.unit.engine.test_processor import _TEST_LEADER_WORKER_ID, _make_factory, _make_processor, _persist_blocked_scheduler_work

RUN_ID = "test-run"
MERGE_INNER = CoalesceName("merge_inner")
MERGE_OUTER = CoalesceName("merge_outer")
INNER_NODE = NodeID("coalesce::merge_inner")
OUTER_NODE = NodeID("coalesce::merge_outer")


def _durable_failed_token_outcomes(db: Any, *, run_id: str) -> dict[str, tuple[str, str]]:
    """I2 / Ruling 40: read the REAL `token_outcomes` rows for a run — the
    layer beneath a mock's captured call arguments, and the direction the
    project's own hints call out as having no automatic detection otherwise.
    Returns {token_id: (outcome, path)} for every completed row."""
    with db.connection() as conn:
        rows = conn.execute(
            select(token_outcomes_table.c.token_id, token_outcomes_table.c.outcome, token_outcomes_table.c.path).where(
                token_outcomes_table.c.run_id == run_id,
                token_outcomes_table.c.completed == 1,
            )
        ).all()
    return {row.token_id: (row.outcome, row.path) for row in rows}


def _mint_group_member_token(factory: Any, *, row_id: str, token_id: str, group_id: str, member_key: str) -> None:
    """Durably mint the token a real `fork_token` call would have created
    for this frame (Ruling 42, `resolve_group_member_token`'s lookup target):
    crafted-token tests must build real lineage, not just a real consumed
    sibling — the 2026-08-22 convention ("the fix is always the fixture,
    never the pop") applies identically here to the new group-member-token
    read. Without this, the escalated siblings' own (deeper) lineage rows
    are the ONLY rows carrying this frame, and `resolve_group_member_token`
    correctly finds none whose OWN path terminates there."""
    try:
        factory.data_flow.resolve_row_ingest_sequence(row_id)
    except AuditIntegrityError:
        factory.data_flow.create_row(
            run_id=RUN_ID,
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            row_id=row_id,
            data={},
        )
    factory.data_flow.create_token(
        row_id,
        token_id=token_id,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=group_id, member_key=member_key),),
    )


def test_sweep_timeout_escalation_loss_is_durable_and_drains_pending() -> None:
    """A nested inner coalesce's timeout failure escalates to the outer
    group; the escalated loss lands in the durable `group_losses` table
    (real repository read) via the SAME transaction as the sweep's own
    barrier terminalization, and nothing is left staged afterward."""
    clock = MockClock(start=1_750_000_000.0)
    db, factory = _make_factory(run_id=RUN_ID)
    token_manager = TokenManager(factory.data_flow, step_resolver=lambda node_id: 2)
    executor = CoalesceExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=token_manager,
        run_id=RUN_ID,
        step_resolver=lambda node_id: 2,
        clock=clock,
        data_flow=factory.data_flow,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_coalesce(
        CoalesceSettings(
            name="merge_inner",
            branches=["inner_a1", "inner_a2"],
            policy="require_all",
            merge="union",
            on_success="merge_outer",
            timeout_seconds=10.0,
        ),
        INNER_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )
    executor.register_coalesce(
        CoalesceSettings(
            name="merge_outer",
            branches=["outer_a", "outer_b"],
            policy="require_all",
            merge="union",
            on_success="out",
            timeout_seconds=3600.0,
        ),
        OUTER_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )

    processor = _make_processor(
        factory,
        coalesce_executor=executor,
        coalesce_node_ids={MERGE_INNER: INNER_NODE, MERGE_OUTER: OUTER_NODE},
        branch_to_coalesce={
            BranchName("inner_a1"): MERGE_INNER,
            BranchName("inner_a2"): MERGE_INNER,
            BranchName("outer_a"): MERGE_OUTER,
            BranchName("outer_b"): MERGE_OUTER,
        },
        node_step_map={INNER_NODE: 2, OUTER_NODE: 3},
        coalesce_on_success_map={MERGE_INNER: "merge_outer", MERGE_OUTER: "out"},
        sink_names=frozenset({"out"}),
        clock=clock,
        scheduler=factory.scheduler,
        scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
    )

    # inner_a1 arrives and holds (require_all needs inner_a2 too); inner_a2
    # never arrives, so merge_inner's timeout fails the group. inner_a1's
    # lineage carries the OUTER fork frame (outer_a) beneath its own inner
    # frame — the shape a real fork-in-fork run produces. The outer_a-only
    # token (what a real fork_token call mints for the fork the inner group
    # opened FROM) must exist durably too — see _mint_group_member_token.
    _mint_group_member_token(factory, row_id="row-1", token_id="tok-outer-a", group_id="fg-outer-row1", member_key="outer_a")
    token = TokenInfo(
        row_id="row-1",
        token_id="tok-inner-a1",
        row_data=make_row({"field": 1}),
        lineage_path=(
            LineageFrame(kind=FrameKind.FORK, group_id="fg-outer-row1", member_key="outer_a"),
            LineageFrame(kind=FrameKind.FORK, group_id="fg-inner-row1", member_key="inner_a1"),
        ),
    )
    _persist_blocked_scheduler_work(factory, processor, token, node_id=INNER_NODE, barrier_key="merge_inner", coalesce_name="merge_inner")
    outcome = executor.accept(token, "merge_inner")
    assert outcome.held is True

    clock.advance(20.0)

    counters = ExecutionCounters()
    pending_tokens: dict[str, list[Any]] = {"out": []}
    ctx = make_context(landscape=factory.plugin_audit_writer())

    handle_coalesce_timeouts(
        coalesce_executor=executor,
        coalesce_node_map={MERGE_INNER: INNER_NODE, MERGE_OUTER: OUTER_NODE},
        processor=processor,
        ctx=ctx,
        counters=counters,
        pending_tokens=pending_tokens,
    )

    # The durable group-loss row exists for the ESCALATED outer member —
    # this is the record that never existed before the fix.
    with db.connection() as conn:
        rows = [dict(row) for row in conn.execute(select(group_losses_table)).mappings()]
    assert len(rows) == 1, f"expected exactly one durable group-loss row, got {rows!r}"
    assert rows[0]["closer_name"] == "merge_outer"
    assert rows[0]["group_id"] == "fg-outer-row1"
    assert rows[0]["member_key"] == "outer_a"
    # Ruling 42: token_id is the LOST MEMBER's own token, not the reporting
    # inner sibling's.
    assert rows[0]["token_id"] == "tok-outer-a"
    assert rows[0]["adopted_epoch"] is None  # not yet replayed into any leader's memory

    # I2 / Ruling 40: the ONE consumed inner token has a durable FAILURE/
    # UNROUTED token_outcomes row — real DB read, not a mock's captured args.
    recorded = _durable_failed_token_outcomes(db, run_id=RUN_ID)
    assert recorded == {"tok-inner-a1": ("failure", "unrouted")}

    # Nothing left staged after the sweep — the orphan that used to poison
    # a later claim's take_claim_group_losses guard is gone. Emptiness here
    # is the direct proof: take_claim_group_losses's own first line is
    # `if not self._pending_group_losses: return ()`, so any later claim's
    # guard is trivially unaffected once this list is empty.
    assert processor._pending_group_losses == []


def test_three_branch_timeout_dedupes_escalation_to_one_outer_loss() -> None:
    """WS3 Task 6 fix round 1 (Ruling 38 / C1) reproduction: a require_all
    inner group with THREE branches, two already arrived and held, times
    out — `_fail_pending` consumes BOTH held siblings in ONE call, and both
    pop to the SAME enclosing (group_id, member_key). Before the fix, the
    escalation walk ran once per consumed token and the second pop's stage
    attempt tripped `_stage_group_loss`'s duplicate guard:
    `OrchestrationInvariantError("...at most one loss per bound frame per
    claim. Processor bug.")`. This must complete and stage exactly one
    escalated outer loss instead.
    """
    clock = MockClock(start=1_750_000_000.0)
    db, factory = _make_factory(run_id=RUN_ID)
    token_manager = TokenManager(factory.data_flow, step_resolver=lambda node_id: 2)
    executor = CoalesceExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=token_manager,
        run_id=RUN_ID,
        step_resolver=lambda node_id: 2,
        clock=clock,
        data_flow=factory.data_flow,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_coalesce(
        CoalesceSettings(
            name="merge_inner",
            branches=["inner_a1", "inner_a2", "inner_a3"],
            policy="require_all",
            merge="union",
            on_success="merge_outer",
            timeout_seconds=10.0,
        ),
        INNER_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )
    executor.register_coalesce(
        CoalesceSettings(
            name="merge_outer",
            branches=["outer_a", "outer_b"],
            policy="require_all",
            merge="union",
            on_success="out",
            timeout_seconds=3600.0,
        ),
        OUTER_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )

    processor = _make_processor(
        factory,
        coalesce_executor=executor,
        coalesce_node_ids={MERGE_INNER: INNER_NODE, MERGE_OUTER: OUTER_NODE},
        branch_to_coalesce={
            BranchName("inner_a1"): MERGE_INNER,
            BranchName("inner_a2"): MERGE_INNER,
            BranchName("inner_a3"): MERGE_INNER,
            BranchName("outer_a"): MERGE_OUTER,
            BranchName("outer_b"): MERGE_OUTER,
        },
        node_step_map={INNER_NODE: 2, OUTER_NODE: 3},
        coalesce_on_success_map={MERGE_INNER: "merge_outer", MERGE_OUTER: "out"},
        sink_names=frozenset({"out"}),
        clock=clock,
        scheduler=factory.scheduler,
        scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
    )

    # inner_a1 and inner_a2 arrive and hold; inner_a3 never arrives. Both
    # held siblings share the SAME outer frame (fg-outer-row1, outer_a).
    # The outer_a-only token must exist durably too — see
    # _mint_group_member_token.
    _mint_group_member_token(factory, row_id="row-1", token_id="tok-outer-a", group_id="fg-outer-row1", member_key="outer_a")

    def _sibling(member_key: str, token_id: str) -> TokenInfo:
        return TokenInfo(
            row_id="row-1",
            token_id=token_id,
            row_data=make_row({"field": 1}),
            lineage_path=(
                LineageFrame(kind=FrameKind.FORK, group_id="fg-outer-row1", member_key="outer_a"),
                LineageFrame(kind=FrameKind.FORK, group_id="fg-inner-row1", member_key=member_key),
            ),
        )

    inner_a1 = _sibling("inner_a1", "tok-inner-a1")
    inner_a2 = _sibling("inner_a2", "tok-inner-a2")
    for member_token, member_key in ((inner_a1, "inner_a1"), (inner_a2, "inner_a2")):
        _persist_blocked_scheduler_work(
            factory, processor, member_token, node_id=INNER_NODE, barrier_key="merge_inner", coalesce_name="merge_inner"
        )
        outcome = executor.accept(member_token, "merge_inner")
        assert outcome.held is True, f"{member_key} should be holding, not {outcome}"

    clock.advance(20.0)

    counters = ExecutionCounters()
    pending_tokens: dict[str, list[Any]] = {"out": []}
    ctx = make_context(landscape=factory.plugin_audit_writer())

    # Must NOT raise — this is the C1 regression this test pins.
    handle_coalesce_timeouts(
        coalesce_executor=executor,
        coalesce_node_map={MERGE_INNER: INNER_NODE, MERGE_OUTER: OUTER_NODE},
        processor=processor,
        ctx=ctx,
        counters=counters,
        pending_tokens=pending_tokens,
    )

    with db.connection() as conn:
        rows = [dict(row) for row in conn.execute(select(group_losses_table)).mappings()]
    outer_losses = [row for row in rows if row["closer_name"] == "merge_outer" and row["member_key"] == "outer_a"]
    assert len(outer_losses) == 1, f"expected exactly one escalated outer_a loss (deduplicated), got {outer_losses!r}"
    # Ruling 42: the deduplicated loss's identity is the LOST MEMBER's own
    # token, not either reporting inner sibling's.
    assert outer_losses[0]["token_id"] == "tok-outer-a"
    assert processor._pending_group_losses == []

    # I2 / Ruling 40: BOTH consumed siblings have their own durable FAILURE/
    # UNROUTED token_outcomes row — N consumed tokens, N real DB writes.
    recorded = _durable_failed_token_outcomes(db, run_id=RUN_ID)
    assert recorded == {
        "tok-inner-a1": ("failure", "unrouted"),
        "tok-inner-a2": ("failure", "unrouted"),
    }
