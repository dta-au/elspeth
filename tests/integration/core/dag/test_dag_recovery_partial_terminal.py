"""Fresh-object recovery evidence for the partial-terminal DAG scenario."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts import RunStatus
from tests.fixtures.dag_scenario_corpus.loader import load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_partial_terminal import (
    declared_partial_terminal_recovery_case,
    run_partial_terminal_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import SinkOutputProjection


@pytest.mark.timeout(20)
def test_partial_terminal_sink_boundary_reopens_and_resumes_exact_survivor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = declared_partial_terminal_recovery_case(load_manifest())
    install_corpus_plugin_manager(monkeypatch)

    result = run_partial_terminal_recovery_case(scenario, case, tmp_path)

    assert result.evidence.runtime.status == RunStatus.COMPLETED_WITH_FAILURES.value
    assert result.evidence.runtime.sink_outputs == (
        SinkOutputProjection(
            sink_name="survivor",
            rows=(
                '{"id":1,"value":10}',
                '{"id":2,"value":20}',
                '{"id":3,"value":30}',
            ),
        ),
    )
    assert result.interrupted.checkpoint_sequence == 3
    assert len(result.interrupted.token_lineage) == 9
    assert len(result.interrupted.work) == 9
    assert result.interrupted.terminal_outcome_count == 6
    assert result.interrupted.failing_effect.state == "finalized"
    assert result.interrupted.survivor_effect.state == "in_flight"
    assert result.interrupted.survivor_commit_intent_count == 1
    failing_lineage = tuple(item for item in result.interrupted.token_lineage if item.branch_name == "failing")
    survivor_lineage = tuple(item for item in result.interrupted.token_lineage if item.branch_name == "survivor")
    assert set(result.interrupted.failing_effect.member_token_ids) == {item.token_id for item in failing_lineage}
    assert set(result.interrupted.failing_effect.member_row_ids) == {item.row_id for item in failing_lineage}
    assert set(result.interrupted.survivor_effect.member_token_ids) == {item.token_id for item in survivor_lineage}
    assert set(result.interrupted.survivor_effect.member_row_ids) == {item.row_id for item in survivor_lineage}
    assert result.interrupted.survivor_lease_expires_at <= result.first_recovery_attempt_started_at
    assert result.interrupted_commit_state_after == "response_lost"
    assert result.resume_window_seconds < 15
    assert result.final_token_outcome_count == 9
    assert result.final_work_count == 9
    assert result.evidence.audit.source_operation_count == 1
