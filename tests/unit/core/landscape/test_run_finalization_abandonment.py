# tests/unit/core/landscape/test_run_finalization_abandonment.py
"""ADR-038: complete_run's abandonment sweep (elspeth-b4254f9a01).

A FAILED/INTERRUPTED finalize on a NON-RESUMABLE run must record
``(NULL, ABANDONED)`` for every token no completed outcome describes — inside
the same terminal transaction as the stamp — and must record NOTHING when the
run is resumable (sources complete + checkpoint exists), where undecided
tokens honestly stay pending for a future resume.

The end-to-end shape (real Orchestrator crash, accounting derivation, refused
resume) lives in
``tests/integration/audit/test_contract_violation_token_outcomes.py``; these
tests pin the repository seam arm by arm.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from elspeth.contracts import NodeType, RunStatus
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.checkpoint import CheckpointDraft
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.sink_effects import SinkEffectMemberCandidate
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.landscape.execution.sink_effect_identity import resolve_sink_effect_members
from elspeth.core.landscape.schema import RunSourceLifecycleState, operations_table, runs_table, token_outcomes_table
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_request

_TOPOLOGY_HASH = "a" * 64
_LEADER_WORKER_ID = "worker:run-adr038:test"


def _leader_token(setup: RecorderSetup) -> CoordinationToken:
    """The epoch-1 token ``begin_run`` minted for the fixture's leader seat.

    The sweep runs only on the FENCED finalize arm (ADR-038 review finding:
    token-less finalizes like the web orphan reaper are unfenced and must
    under-abandon), so every sweep test finalizes with this token.
    """
    return CoordinationToken(run_id=setup.run_id, worker_id=_LEADER_WORKER_ID, leader_epoch=1)


def _setup_run_with_tokens(
    *,
    lifecycle_state: RunSourceLifecycleState,
    with_checkpoint: bool,
    token_count: int = 2,
) -> tuple[RecorderSetup, list[str]]:
    """Run + registered source + ``token_count`` undecided tokens."""
    setup = make_recorder_with_run(run_id="run-adr038", leader_worker_id=_LEADER_WORKER_ID)
    setup.factory.run_lifecycle.record_run_source(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        source_name="primary",
        plugin_name="source",
        config_hash="c" * 64,
        lifecycle_state=lifecycle_state,
    )
    if with_checkpoint:
        CheckpointManager(setup.db).create_checkpoint(
            draft=CheckpointDraft(run_id=setup.run_id, sequence_number=0, upstream_topology_hash=_TOPOLOGY_HASH)
        )
    token_ids: list[str] = []
    for index in range(token_count):
        _row, token = setup.factory.data_flow.create_row_with_token(
            setup.run_id,
            setup.source_node_id,
            index,
            {"value": index},
            source_row_index=index,
            ingest_sequence=index,
        )
        token_ids.append(token.token_id)
    return setup, token_ids


def _abandoned_rows(setup: RecorderSetup) -> list:
    with setup.db.connection() as conn:
        return conn.execute(
            select(token_outcomes_table)
            .where(token_outcomes_table.c.run_id == setup.run_id)
            .where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
            .order_by(token_outcomes_table.c.token_id)
        ).fetchall()


def _reserve_effect_operation(setup: RecorderSetup, *, token_id: str, suffix: str) -> str:
    """Reserve a real effect so its automatically-opened operation is authentic."""
    sink_node_id = register_test_node(
        setup.factory.data_flow,
        setup.run_id,
        f"sink-{suffix}",
        node_type=NodeType.SINK,
        plugin_name="sink",
    )
    setup.factory.execution.begin_node_state(
        token_id=token_id,
        node_id=sink_node_id,
        run_id=setup.run_id,
        step_index=0,
        input_data={"effect": suffix},
    )
    members = resolve_sink_effect_members(
        setup.factory,
        (SinkEffectMemberCandidate(token_id=token_id, row={"effect": suffix}),),
    )
    effect = setup.factory.execution.sink_effects.reserve(_pipeline_request(setup.run_id, sink_node_id, members)).new_effect
    assert effect is not None
    operations = setup.factory.execution.get_operations_for_run(setup.run_id)
    operation = next(item for item in operations if item.sink_effect_id == effect.effect_id)
    return operation.operation_id


class TestAbandonmentSweepFires:
    """Non-resumable death ⇒ every undecided token gets one ABANDONED record."""

    def test_incomplete_source_arm_abandons_undecided_tokens(self) -> None:
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        rows = _abandoned_rows(setup)
        assert [row.token_id for row in rows] == sorted(token_ids)
        for row in rows:
            assert row.outcome is None
            assert row.completed == 0
            context = json.loads(row.context_json)
            assert context["abandoned_by"] == "run_finalization"
            assert context["run_status"] == RunStatus.FAILED.value
            assert context["non_resumable_arms"] == ["incomplete_sources"]
            assert context["incomplete_sources"] == {"primary": "loading"}

        # The Tier-1 read path (TokenOutcomeLoader) must load the record —
        # not just the raw row — through the ADR-019/038 cross-checks.
        loaded = setup.factory.data_flow.get_token_outcome(token_ids[0])
        assert loaded is not None
        assert loaded.outcome is None
        assert loaded.path is TerminalPath.ABANDONED
        assert loaded.completed is False

    def test_no_checkpoint_arm_abandons_despite_complete_sources(self) -> None:
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.EXHAUSTED, with_checkpoint=False)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        rows = _abandoned_rows(setup)
        assert len(rows) == len(token_ids)
        context = json.loads(rows[0].context_json)
        assert context["non_resumable_arms"] == ["no_checkpoint"]
        assert context["incomplete_sources"] == {}

    def test_no_source_records_arm(self) -> None:
        setup = make_recorder_with_run(run_id="run-adr038", leader_worker_id=_LEADER_WORKER_ID)
        _row, token = setup.factory.data_flow.create_row_with_token(
            setup.run_id, setup.source_node_id, 0, {"value": 0}, source_row_index=0, ingest_sequence=0
        )

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        rows = _abandoned_rows(setup)
        assert [row.token_id for row in rows] == [token.token_id]
        context = json.loads(rows[0].context_json)
        assert context["non_resumable_arms"] == ["no_checkpoint", "no_source_records"]

    def test_interrupted_finalize_sweeps_like_failed(self) -> None:
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.INTERRUPTED, token=_leader_token(setup))

        rows = _abandoned_rows(setup)
        assert len(rows) == len(token_ids)
        assert json.loads(rows[0].context_json)["run_status"] == RunStatus.INTERRUPTED.value

    def test_decided_tokens_are_never_swept(self) -> None:
        """A token with a completed outcome is decided — abandonment would contradict it."""
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=token_ids[0], run_id=setup.run_id),
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
            error_hash="e" * 64,
        )

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        rows = _abandoned_rows(setup)
        assert [row.token_id for row in rows] == [token_ids[1]]

    def test_refinalize_after_resume_transition_is_idempotent(self) -> None:
        """The documented resume path (terminal → RUNNING → re-complete) must
        not duplicate abandonment records: the sweep's WHERE excludes tokens
        already bearing one."""
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)
        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))
        assert len(_abandoned_rows(setup)) == len(token_ids)

        setup.factory.run_lifecycle.update_run_status(setup.run_id, RunStatus.RUNNING)
        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        assert len(_abandoned_rows(setup)) == len(token_ids)

    def test_non_resumable_finalize_fails_open_effect_operation_without_tokens_to_abandon(self) -> None:
        """The operation sweep is independent of the token-outcome sweep."""
        setup, token_ids = _setup_run_with_tokens(
            lifecycle_state=RunSourceLifecycleState.LOADING,
            with_checkpoint=True,
            token_count=1,
        )
        setup.factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=token_ids[0], run_id=setup.run_id),
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
            error_hash="e" * 64,
        )
        operation_id = _reserve_effect_operation(setup, token_id=token_ids[0], suffix="open")

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        assert _abandoned_rows(setup) == []
        operation = setup.factory.execution.get_operation(operation_id)
        assert operation is not None
        assert operation.status == "failed"
        assert operation.completed_at is not None
        assert operation.duration_ms is not None and operation.duration_ms >= 0.0
        assert operation.error_message == "run finalized as non-resumable before sink effect completed"

    def test_operation_sweep_changes_only_open_effect_linked_operations(self) -> None:
        setup, token_ids = _setup_run_with_tokens(
            lifecycle_state=RunSourceLifecycleState.LOADING,
            with_checkpoint=True,
            token_count=4,
        )
        open_id = _reserve_effect_operation(setup, token_id=token_ids[0], suffix="open")
        completed_id = _reserve_effect_operation(setup, token_id=token_ids[1], suffix="completed")
        failed_id = _reserve_effect_operation(setup, token_id=token_ids[2], suffix="failed")
        pending_id = _reserve_effect_operation(setup, token_id=token_ids[3], suffix="pending")
        effectless_id = setup.factory.execution.begin_operation(
            setup.run_id,
            setup.source_node_id,
            "source_load",
        ).operation_id
        setup.factory.execution.complete_operation(completed_id, "completed", duration_ms=1.0)
        setup.factory.execution.complete_operation(failed_id, "failed", duration_ms=2.0, error="bounded prior failure")
        pending_at = datetime.now(UTC)
        with setup.db.write_connection() as conn:
            conn.execute(
                update(operations_table)
                .where(operations_table.c.operation_id == pending_id)
                .values(status="pending", completed_at=pending_at, duration_ms=3.0)
            )

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.INTERRUPTED, token=_leader_token(setup))

        by_id = {item.operation_id: item for item in setup.factory.execution.get_operations_for_run(setup.run_id)}
        assert by_id[open_id].status == "failed"
        assert by_id[completed_id].status == "completed"
        assert by_id[completed_id].error_message is None
        assert by_id[failed_id].status == "failed"
        assert by_id[failed_id].error_message == "bounded prior failure"
        assert by_id[pending_id].status == "pending"
        assert by_id[pending_id].completed_at is not None
        assert by_id[effectless_id].status == "open"

    def test_operation_sweep_failure_rolls_back_terminal_stamp_and_abandonment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, token_ids = _setup_run_with_tokens(
            lifecycle_state=RunSourceLifecycleState.LOADING,
            with_checkpoint=True,
            token_count=1,
        )
        operation_id = _reserve_effect_operation(setup, token_id=token_ids[0], suffix="rollback")

        def fail_sweep(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected operation sweep failure")

        monkeypatch.setattr(setup.factory.run_lifecycle._operations_repo, "fail_open_effect_operations_for_run", fail_sweep)

        with pytest.raises(RuntimeError, match="injected operation sweep failure"):
            setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        with setup.db.read_only_connection() as conn:
            run_row = conn.execute(select(runs_table).where(runs_table.c.run_id == setup.run_id)).one()
        assert run_row.status == RunStatus.RUNNING.value
        assert run_row.completed_at is None
        assert _abandoned_rows(setup) == []
        operation = setup.factory.execution.get_operation(operation_id)
        assert operation is not None and operation.status == "open"

    def test_abandoned_token_refuses_a_late_terminal_decision(self) -> None:
        setup, token_ids = _setup_run_with_tokens(
            lifecycle_state=RunSourceLifecycleState.LOADING,
            with_checkpoint=True,
            token_count=1,
        )
        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        with pytest.raises(AuditIntegrityError, match="decided-plus-abandoned history is forbidden"):
            setup.factory.data_flow.record_token_outcome(
                ref=TokenRef(token_id=token_ids[0], run_id=setup.run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
                error_hash="e" * 64,
            )

        rows = _abandoned_rows(setup)
        assert len(rows) == 1
        assert rows[0].token_id == token_ids[0]


class TestAbandonmentSweepStaysSilent:
    """Resumable or successful deaths must not write abandonment records."""

    def test_resumable_run_keeps_tokens_pending(self) -> None:
        """Sources complete + checkpoint exists ⇒ a resume may yet decide the
        tokens; abandoning them would be a false record."""
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.EXHAUSTED, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        assert _abandoned_rows(setup) == []

    def test_resumable_run_keeps_effect_operation_open(self) -> None:
        setup, token_ids = _setup_run_with_tokens(
            lifecycle_state=RunSourceLifecycleState.EXHAUSTED,
            with_checkpoint=True,
            token_count=1,
        )
        operation_id = _reserve_effect_operation(setup, token_id=token_ids[0], suffix="resumable")

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        operation = setup.factory.execution.get_operation(operation_id)
        assert operation is not None and operation.status == "open"

    def test_loaded_lifecycle_also_counts_complete(self) -> None:
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADED, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED, token=_leader_token(setup))

        assert _abandoned_rows(setup) == []

    def test_tokenless_finalize_never_sweeps(self) -> None:
        """A token-less finalize runs UNFENCED (plain write_connection) —
        e.g. the web orphan reaper stamping INTERRUPTED over a RUNNING-stale
        run. An unfenced sweep could abandon tokens of a run a live leader
        is still driving, so these paths deliberately under-abandon."""
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.INTERRUPTED)

        assert _abandoned_rows(setup) == []

    def test_success_finalize_never_sweeps(self) -> None:
        """A COMPLETED stamp with undecided tokens is a closure violation for
        accounting to surface — sweeping it under ABANDONED would hide it."""
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=False)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.COMPLETED, token=_leader_token(setup))

        assert _abandoned_rows(setup) == []
