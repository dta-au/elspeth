"""Conditional-route recovery evidence through the public production path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.dag_scenario_corpus.harness import compute_fixture_sha256
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_conditional_route import (
    run_conditional_route_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import SummaryRunExpectation


def test_route_reopens_and_resumes_after_first_accepted_sink_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_corpus_plugin_manager(monkeypatch)

    result = run_conditional_route_recovery_case(tmp_path)

    assert (
        result.scenario.id,
        result.case.id,
        result.case.fixture,
        dict(result.case.input_fixtures),
        {name: artifact.filename for name, artifact in result.case.output_artifacts.items()},
    ) == (
        "conditional-routing",
        "route-reopen-resume",
        "conditional-routing/route-reopen-resume.yaml",
        {"primary": "conditional-routing/input.csv"},
        {"accepted": "accepted.jsonl", "rejected": "rejected.jsonl"},
    )
    assert result.case.recovery_fault is not None
    assert result.case.recovery_fault.model_dump(mode="json") == {
        "kind": "sink_effect",
        "seam": "before_effect",
        "sink_name": "accepted",
        "occurrence": 1,
    }

    interrupted = result.interrupted
    assert interrupted.source_names_exhausted == ("primary",)
    assert interrupted.sink_declaration_order == ("accepted", "rejected")
    assert interrupted.topology_shape == (
        4,
        3,
        (("gate", 1), ("sink", 2), ("source", 1)),
        ("continue", "false", "true"),
    )
    assert interrupted.routes_by_source_row == (
        (0, "false", "move", "rejected"),
        (1, "true", "move", "accepted"),
        (2, "true", "move", "accepted"),
    )
    assert interrupted.dispositions_by_source_row == (
        (0, "success", "gate_routed", "rejected"),
        (1, "success", "gate_routed", "accepted"),
        (2, "success", "gate_routed", "accepted"),
    )
    assert interrupted.work_by_source_row == (
        (0, "pending_sink", "rejected", "live"),
        (1, "pending_sink", "accepted", "live"),
        (2, "pending_sink", "accepted", "live"),
    )
    assert interrupted.sink_effect_order == ("accepted",)
    assert interrupted.accepted_attempts == (
        ("inspect", "returned"),
        ("reconcile", "returned"),
        ("commit", "intent"),
    )
    assert interrupted.artifact_count == 0
    assert interrupted.output_files_exist == (("accepted", False), ("rejected", False))
    assert interrupted.checkpoint_id
    assert interrupted.checkpoint_sequence == 0
    assert len(interrupted.checkpoint_topology_hash) == 64

    evidence = result.evidence
    assert evidence.schema_version == 2
    assert (evidence.scenario_id, evidence.case_id) == (
        "conditional-routing",
        "route-reopen-resume",
    )
    assert evidence.fixture_sha256 == compute_fixture_sha256(result.case)
    assert evidence.completed_stages == ("config", "build", "runtime", "audit", "recovery")
    assert evidence.config.loaded is True
    assert len(evidence.config.settings_sha256) == 64
    assert evidence.runtime.status == "completed"
    assert (
        evidence.runtime.rows_processed,
        evidence.runtime.rows_succeeded,
        evidence.runtime.rows_failed,
        evidence.runtime.output_rows,
    ) == (3, 3, 0, 3)
    assert evidence.runtime.durable_projection is not None
    assert tuple((output.sink_name, output.rows) for output in evidence.runtime.sink_outputs) == (
        ("accepted", ('{"id":2,"value":20}', '{"id":3,"value":30}')),
        ("rejected", ('{"id":1,"value":10}',)),
    )
    assert evidence.audit.source_operation_count == 1
    assert evidence.audit.total_records == sum(record.count for record in evidence.audit.record_counts)
    audit_record_types = tuple(record.record_type for record in evidence.audit.record_counts)
    assert audit_record_types == tuple(sorted(audit_record_types))
    expected = result.case.expected
    assert isinstance(expected, SummaryRunExpectation)
    assert set(expected.required_audit_record_types) <= set(audit_record_types)

    proof = evidence.recovery.sink_boundary
    assert proof is not None
    assert proof.source_names_exhausted_before == ("primary",)
    assert proof.checkpoint_topology_hash == interrupted.checkpoint_topology_hash
    assert proof.checkpoint_topology_hash == proof.fresh_topology_hash
    assert proof.token_ids_before == proof.token_ids_after
    assert len(proof.token_ids_before) == 3
    assert tuple(item.work_item_id for item in proof.work_before) == tuple(item.work_item_id for item in proof.work_after)
    assert {item.status for item in proof.work_after} == {"terminal"}
    assert {item.row_payload_state for item in proof.work_after} == {"purged"}
    assert proof.effect_count_before == 1
    assert proof.effect_member_count_before == 2
    assert proof.artifact_count_before == proof.publication_count_before == 0
    assert proof.effect_count_after == proof.artifact_count_after == proof.publication_count_after == 2
    interrupted_effect = proof.effects_before[0]
    assert (
        interrupted_effect.sink_name,
        interrupted_effect.state,
        len(interrupted_effect.member_token_ids),
    ) == ("accepted", "in_flight", 2)
    resumed_effect = next(effect for effect in proof.effects_after if effect.sink_name == "accepted")
    assert (
        resumed_effect.effect_id,
        resumed_effect.artifact_id,
        resumed_effect.member_token_ids,
        resumed_effect.member_row_ids,
    ) == (
        interrupted_effect.effect_id,
        interrupted_effect.artifact_id,
        interrupted_effect.member_token_ids,
        interrupted_effect.member_row_ids,
    )
    assert {(effect.sink_name, effect.state) for effect in proof.effects_after} == {
        ("accepted", "finalized"),
        ("rejected", "finalized"),
    }
    assert proof.resume_marker_count == 1
    assert proof.resume_marker_event_type == "leader_acquire"
    assert proof.resume_marker_entry_point == "resume"
    assert proof.resume_marker_worker_id
    assert proof.resume_marker_leader_epoch >= 1
    assert proof.durable_identity_reused is True
    assert proof.durable_export_parity is True
    assert evidence.recovery.database_reopened is True
    assert (
        evidence.recovery.checkpoint_id,
        evidence.recovery.checkpoint_sequence,
    ) == (
        interrupted.checkpoint_id,
        interrupted.checkpoint_sequence,
    )
    assert evidence.recovery.can_resume is True
    assert evidence.recovery.source_replayed is False
    assert evidence.recovery.checkpoint_removed is True

    final = result.final
    assert final.routes_by_source_row == interrupted.routes_by_source_row
    assert final.dispositions_by_source_row == interrupted.dispositions_by_source_row
    assert final.sink_effect_order == ("accepted", "rejected")
    assert final.accepted_intent_reclassified_response_lost is True
    assert final.accepted_response_lost_attempt_id == interrupted.accepted_commit_attempt_id
    assert final.accepted_commit_intent_count == 0
    assert final.terminal_work_count == 3
    assert final.terminal_outcome_count == 3
    assert final.checkpoint_count == 0

    assert [json.loads(line) for line in (tmp_path / "accepted.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"id": 2, "value": 20},
        {"id": 3, "value": 30},
    ]
    assert [json.loads(line) for line in (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"id": 1, "value": 10},
    ]
