"""Exact public recovery evidence for the S6 coalesced pending-sink branch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_s6_pending_sink import (
    S6_PENDING_SINK_CASE_ID,
    build_s6_pending_sink_case,
    run_s6_pending_sink_recovery,
)


def test_s6_require_all_nested_reopens_at_pending_sink_without_replaying_coalesce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = build_s6_pending_sink_case()
    assert (scenario.id, case.id) == (
        "fork-coalesce-policies",
        S6_PENDING_SINK_CASE_ID,
    )
    install_corpus_plugin_manager(monkeypatch)

    proof = run_s6_pending_sink_recovery(scenario, case, tmp_path)
    recovery = proof.evidence.recovery
    sink_boundary = recovery.sink_boundary
    assert sink_boundary is not None

    assert proof.checkpoint_sequence_before == 0
    assert proof.checkpoint_topology_hash_before == proof.evidence.graph.topology_hash
    assert len(proof.row_ids_before) == 1
    assert len(proof.token_ids_before) == 5
    assert len(proof.parent_links_before) == 6
    assert len(proof.completed_coalesce_state_ids_before) == 3
    assert proof.merged_token_id_before in sink_boundary.token_ids_before
    assert proof.output_absent_before is True
    assert proof.effect_generation_before == 2
    assert proof.attempts_before == (
        (1, "inspect", "returned"),
        (2, "reconcile", "returned"),
        (2, "commit", "intent"),
    )

    assert recovery.database_reopened is True
    assert recovery.can_resume is True
    assert recovery.source_replayed is False
    assert recovery.checkpoint_removed is True
    assert sink_boundary.checkpoint_topology_hash == sink_boundary.fresh_topology_hash
    assert sink_boundary.token_ids_before == proof.token_ids_before
    assert sink_boundary.token_ids_after == proof.token_ids_before
    assert len(sink_boundary.work_before) == 5
    assert [(work.token_id, work.status, work.row_payload_state) for work in sink_boundary.work_before].count(
        (proof.merged_token_id_before, "pending_sink", "live")
    ) == 1
    assert sum(work.status == "terminal" for work in sink_boundary.work_before) == 4
    assert all(work.status == "terminal" for work in sink_boundary.work_after)
    assert tuple(work.work_item_id for work in sink_boundary.work_after) == tuple(work.work_item_id for work in sink_boundary.work_before)
    assert sink_boundary.effects_before[0].effect_id == proof.effect_id_before
    assert sink_boundary.effects_after[0].effect_id == proof.effect_id_before
    assert sink_boundary.effects_before[0].artifact_id == proof.artifact_id_before
    assert sink_boundary.effects_after[0].artifact_id == proof.artifact_id_before
    assert sink_boundary.publication_count_before == 0
    assert sink_boundary.publication_count_after == 1
    assert sink_boundary.durable_identity_reused is True
    assert sink_boundary.durable_export_parity is True

    assert proof.evidence.runtime.sink_outputs[0].rows == (
        '{"path_a":{"branch_marker":"a","id":1,"value":10},'
        '"path_b":{"branch_marker":"b","id":1,"value":10},'
        '"path_c":{"branch_marker":"c","id":1,"value":10}}',
    )
    assert proof.evidence.audit.source_operation_count == 1
    durable = proof.evidence.runtime.durable_projection
    assert durable is not None
    assert durable == proof.evidence.audit.portable_projection
    assert len(durable.rows) == 1
    assert len(durable.tokens) == 5
    assert sum(len(token.parents) for token in durable.tokens) == 6
    assert len(durable.terminal_dispositions) == 5
    assert len(durable.scheduler_work) == 5
    assert {work.final_status for work in durable.scheduler_work} == {"terminal"}
    assert len(durable.node_states) == 9
    assert {state.attempt for state in durable.node_states} == {0}

    effect_records = tuple(json.loads(record.material) for record in durable.audit_records if record.record_type == "sink_effect")
    attempt_records = tuple(json.loads(record.material) for record in durable.audit_records if record.record_type == "sink_effect_attempt")
    assert len(effect_records) == 1
    assert (
        effect_records[0]["generation"],
        effect_records[0]["publication_evidence_kind"],
        effect_records[0]["publication_performed"],
        effect_records[0]["reconcile_kind"],
        effect_records[0]["state"],
    ) == (
        3,
        "returned",
        True,
        None,
        "finalized",
    )
    assert tuple((record["generation"], record["action"], record["state"]) for record in attempt_records) == (
        (1, "inspect", "returned"),
        (2, "reconcile", "returned"),
        (2, "commit", "response_lost"),
        (3, "reconcile", "returned"),
        (3, "commit", "returned"),
    )
