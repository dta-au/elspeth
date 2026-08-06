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

from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.checkpoint import CheckpointDraft
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.landscape.schema import RunSourceLifecycleState, token_outcomes_table
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run

_TOPOLOGY_HASH = "a" * 64


def _setup_run_with_tokens(
    *,
    lifecycle_state: RunSourceLifecycleState,
    with_checkpoint: bool,
    token_count: int = 2,
) -> tuple[RecorderSetup, list[str]]:
    """Run + registered source + ``token_count`` undecided tokens."""
    setup = make_recorder_with_run(run_id="run-adr038")
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


class TestAbandonmentSweepFires:
    """Non-resumable death ⇒ every undecided token gets one ABANDONED record."""

    def test_incomplete_source_arm_abandons_undecided_tokens(self) -> None:
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

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

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        rows = _abandoned_rows(setup)
        assert len(rows) == len(token_ids)
        context = json.loads(rows[0].context_json)
        assert context["non_resumable_arms"] == ["no_checkpoint"]
        assert context["incomplete_sources"] == {}

    def test_no_source_records_arm(self) -> None:
        setup = make_recorder_with_run(run_id="run-adr038")
        _row, token = setup.factory.data_flow.create_row_with_token(
            setup.run_id, setup.source_node_id, 0, {"value": 0}, source_row_index=0, ingest_sequence=0
        )

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        rows = _abandoned_rows(setup)
        assert [row.token_id for row in rows] == [token.token_id]
        context = json.loads(rows[0].context_json)
        assert context["non_resumable_arms"] == ["no_checkpoint", "no_source_records"]

    def test_interrupted_finalize_sweeps_like_failed(self) -> None:
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.INTERRUPTED)

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

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        rows = _abandoned_rows(setup)
        assert [row.token_id for row in rows] == [token_ids[1]]

    def test_refinalize_after_resume_transition_is_idempotent(self) -> None:
        """The documented resume path (terminal → RUNNING → re-complete) must
        not duplicate abandonment records: the sweep's WHERE excludes tokens
        already bearing one."""
        setup, token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=True)
        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)
        assert len(_abandoned_rows(setup)) == len(token_ids)

        setup.factory.run_lifecycle.update_run_status(setup.run_id, RunStatus.RUNNING)
        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        assert len(_abandoned_rows(setup)) == len(token_ids)


class TestAbandonmentSweepStaysSilent:
    """Resumable or successful deaths must not write abandonment records."""

    def test_resumable_run_keeps_tokens_pending(self) -> None:
        """Sources complete + checkpoint exists ⇒ a resume may yet decide the
        tokens; abandoning them would be a false record."""
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.EXHAUSTED, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        assert _abandoned_rows(setup) == []

    def test_loaded_lifecycle_also_counts_complete(self) -> None:
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADED, with_checkpoint=True)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.FAILED)

        assert _abandoned_rows(setup) == []

    def test_success_finalize_never_sweeps(self) -> None:
        """A COMPLETED stamp with undecided tokens is a closure violation for
        accounting to surface — sweeping it under ABANDONED would hide it."""
        setup, _token_ids = _setup_run_with_tokens(lifecycle_state=RunSourceLifecycleState.LOADING, with_checkpoint=False)

        setup.factory.run_lifecycle.finalize_run(setup.run_id, RunStatus.COMPLETED)

        assert _abandoned_rows(setup) == []
