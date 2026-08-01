"""S7 terminal-publication recovery evidence through the public resume path."""

from __future__ import annotations

import inspect
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from elspeth.engine.orchestrator import Orchestrator
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_s7_terminal_publication import (
    run_s7_terminal_publication_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import HarnessCaseSpec, ScenarioSpec


def _s7_recovery_case() -> tuple[ScenarioSpec, HarnessCaseSpec]:
    return next(
        (scenario, case)
        for scenario, case in iter_harness_cases(load_manifest())
        if (scenario.id, case.id)
        == (
            "sequential-nested-fork-coalesce",
            "reopen-terminal-publication",
        )
    )


def test_s7_reopens_after_both_coalesces_and_publishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _s7_recovery_case()
    monkeypatch.setattr(Orchestrator, "run", inspect.unwrap(Orchestrator.run))
    monkeypatch.setattr(Orchestrator, "resume", inspect.unwrap(Orchestrator.resume))
    install_corpus_plugin_manager(monkeypatch)

    built_object_tuples: list[tuple[object, object, object, object, object, object]] = []
    production_build = corpus_harness.build_scenario

    def record_fresh_build(*args: Any, **kwargs: Any) -> Any:
        built = production_build(*args, **kwargs)
        built_object_tuples.append(
            (
                built,
                built.rendered,
                built.rendered.settings,
                built.bundle,
                built.graph,
                built.config,
            )
        )
        return built

    monkeypatch.setattr(corpus_harness, "build_scenario", record_fresh_build)

    recovery = run_s7_terminal_publication_recovery_case(scenario, case, tmp_path)
    evidence = recovery.evidence
    interrupted = recovery.interrupted
    resumed = recovery.resumed

    assert len(built_object_tuples) == 2
    assert all(
        initial_object is not resumed_object
        for initial_object, resumed_object in zip(
            built_object_tuples[0],
            built_object_tuples[1],
            strict=True,
        )
    )
    assert (evidence.graph.node_count, evidence.graph.edge_count) == (6, 9)
    assert interrupted.source_names_exhausted == ("primary",)
    assert interrupted.checkpoint_sequence == 0
    assert interrupted.checkpoint_topology_hash == evidence.graph.topology_hash
    assert (len(interrupted.row_ids), len(interrupted.token_rows), len(interrupted.parent_links)) == (3, 21, 24)
    assert interrupted.coalesce_node_names == ("merge_a", "merge_b")
    assert len(interrupted.coalesce_states) == 12
    assert Counter(state.coalesce_name for state in interrupted.coalesce_states) == {
        "merge_a": 6,
        "merge_b": 6,
    }
    assert {state.status for state in interrupted.coalesce_states} == {"completed"}
    assert interrupted.work_status_counts == (("pending_sink", 3), ("terminal", 18))
    assert interrupted.effect_state == "in_flight"
    proof = evidence.recovery.sink_boundary
    assert proof is not None
    assert set(interrupted.effect_member_token_ids) == {item.token_id for item in proof.work_before if item.status == "pending_sink"}
    assert interrupted.commit_attempt.action == "commit"
    assert interrupted.commit_attempt.state == "intent"
    assert interrupted.commit_attempt.effect_id == interrupted.effect_id
    assert interrupted.output_absent is True

    assert resumed.row_ids == interrupted.row_ids
    assert resumed.token_rows == interrupted.token_rows
    assert resumed.parent_links == interrupted.parent_links
    assert resumed.coalesce_states == interrupted.coalesce_states
    assert evidence.runtime.status == "completed"
    assert (evidence.runtime.rows_processed, evidence.runtime.rows_succeeded, evidence.runtime.rows_failed) == (3, 3, 0)
    assert evidence.audit.source_operation_count == 1
    durable_projection = evidence.runtime.durable_projection
    assert durable_projection is not None
    assert len(durable_projection.tokens) == 21
    assert len(durable_projection.terminal_dispositions) == 21
    assert {item.final_status for item in durable_projection.scheduler_work} == {"terminal"}
    assert evidence.recovery.checkpoint_removed is True

    assert proof.token_ids_after == proof.token_ids_before
    assert len(proof.token_ids_before) == 21
    assert len(proof.work_before) == len(proof.work_after) == 21
    assert {item.status for item in proof.work_after} == {"terminal"}
    assert proof.effect_count_before == proof.effect_count_after == 1
    assert proof.effect_member_count_before == 3
    assert proof.artifact_count_before == proof.publication_count_before == 0
    assert proof.artifact_count_after == proof.publication_count_after == 1
    assert proof.effects_after[0].effect_id == proof.effects_before[0].effect_id
    assert proof.effects_after[0].member_token_ids == proof.effects_before[0].member_token_ids
    assert proof.durable_identity_reused is True
    assert proof.durable_export_parity is True
    assert tuple(attempt.action for attempt in recovery.resumed_commit_attempts) == (
        "commit",
        "commit",
    )
    assert tuple(attempt.state for attempt in recovery.resumed_commit_attempts) == (
        "response_lost",
        "returned",
    )
    assert recovery.resumed_commit_attempts[0].attempt_id == interrupted.commit_attempt.attempt_id
    assert recovery.resumed_commit_attempts[0].effect_id == recovery.resumed_commit_attempts[1].effect_id == interrupted.effect_id
    assert len({attempt.attempt_id for attempt in recovery.resumed_commit_attempts}) == 2

    assert [json.loads(line) for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {
            "branch_b1": {
                "branch_a1": {"id": 1, "value": 10},
                "branch_a2": {"id": 1, "value": 10},
            },
            "branch_b2": {
                "branch_a1": {"id": 1, "value": 10},
                "branch_a2": {"id": 1, "value": 10},
            },
        },
        {
            "branch_b1": {
                "branch_a1": {"id": 2, "value": 20},
                "branch_a2": {"id": 2, "value": 20},
            },
            "branch_b2": {
                "branch_a1": {"id": 2, "value": 20},
                "branch_a2": {"id": 2, "value": 20},
            },
        },
        {
            "branch_b1": {
                "branch_a1": {"id": 3, "value": 30},
                "branch_a2": {"id": 3, "value": 30},
            },
            "branch_b2": {
                "branch_a1": {"id": 3, "value": 30},
                "branch_a2": {"id": 3, "value": 30},
            },
        },
    ]
    assert recovery.between_merge_recovery_proven is False
    assert recovery.recovery_promotion_ceiling == "incomplete"
