"""Independent-root recovery evidence through the public production path."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.engine.orchestrator import Orchestrator
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_independent_roots import (
    run_independent_roots_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioSpec,
    SinkOutputProjection,
)


def _independent_roots_recovery_case() -> tuple[ScenarioSpec, HarnessCaseSpec]:
    manifest = load_manifest()
    return next(
        (scenario, case)
        for scenario, case in iter_harness_cases(manifest)
        if (scenario.id, case.id)
        == (
            "multiple-independent-sources",
            "independent-roots-reopen-resume",
        )
    )


def test_independent_roots_reopen_and_resume_without_replay_or_reminting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _independent_roots_recovery_case()
    monkeypatch.setattr(Orchestrator, "run", inspect.unwrap(Orchestrator.run))
    monkeypatch.setattr(Orchestrator, "resume", inspect.unwrap(Orchestrator.resume))
    install_corpus_plugin_manager(monkeypatch)

    built_identity_tuples: list[tuple[int, int, int, int, int, int]] = []
    production_build = corpus_harness.build_scenario

    def record_fresh_build(*args: Any, **kwargs: Any) -> Any:
        built = production_build(*args, **kwargs)
        built_identity_tuples.append(
            (
                id(built),
                id(built.rendered),
                id(built.rendered.settings),
                id(built.bundle),
                id(built.graph),
                id(built.config),
            )
        )
        return built

    monkeypatch.setattr(corpus_harness, "build_scenario", record_fresh_build)

    evidence = run_independent_roots_recovery_case(scenario, case, tmp_path)

    assert evidence.completed_stages == (
        "config",
        "build",
        "runtime",
        "audit",
        "recovery",
    )
    assert len(built_identity_tuples) == 2
    assert len(set(built_identity_tuples)) == 2
    assert evidence.runtime.sink_outputs == (
        SinkOutputProjection(
            sink_name="output",
            rows=(
                '{"id":1,"value":10}',
                '{"id":2,"value":20}',
                '{"id":3,"value":30}',
                '{"id":101,"value":-5}',
                '{"id":102,"value":-10}',
                '{"id":103,"value":-15}',
            ),
        ),
    )
    assert evidence.audit.source_operation_count == 2

    projection = evidence.runtime.durable_projection
    assert projection is not None
    assert tuple(
        (
            row.key,
            row.source_name,
            row.source_row_index,
            row.ingest_sequence,
        )
        for row in projection.rows
    ) == (
        ("orders:0", "orders", 0, 0),
        ("orders:1", "orders", 1, 1),
        ("orders:2", "orders", 2, 2),
        ("refunds:0", "refunds", 0, 3),
        ("refunds:1", "refunds", 1, 4),
        ("refunds:2", "refunds", 2, 5),
    )
    assert len(projection.tokens) == 6
    assert all(not token.parents for token in projection.tokens)
    assert len(projection.terminal_dispositions) == 6
    assert {
        (
            disposition.outcome,
            disposition.path,
            disposition.sink_name,
        )
        for disposition in projection.terminal_dispositions
    } == {("success", "default_flow", "output")}
    assert len(projection.scheduler_work) == 6
    assert {work.final_status for work in projection.scheduler_work} == {"terminal"}

    proof = evidence.recovery.sink_boundary
    assert proof is not None
    assert proof.fault.model_dump(mode="json") == {
        "kind": "sink_effect",
        "seam": "before_effect",
        "sink_name": "output",
        "occurrence": 1,
    }
    assert proof.source_names_exhausted_before == ("orders", "refunds")
    assert proof.token_ids_after == proof.token_ids_before
    assert len(proof.token_ids_before) == 6
    assert tuple(item.work_item_id for item in proof.work_after) == tuple(item.work_item_id for item in proof.work_before)
    assert {item.status for item in proof.work_before} == {"pending_sink"}
    assert {item.row_payload_state for item in proof.work_before} == {"live"}
    assert {item.status for item in proof.work_after} == {"terminal"}
    assert {item.row_payload_state for item in proof.work_after} == {"purged"}
    assert proof.effect_count_before == proof.effect_count_after == 1
    assert proof.effect_member_count_before == 6
    assert proof.effects_before[0].effect_id == proof.effects_after[0].effect_id
    assert proof.effects_before[0].artifact_id == proof.effects_after[0].artifact_id
    assert proof.effects_before[0].member_token_ids == proof.effects_after[0].member_token_ids
    assert proof.artifact_count_before == proof.publication_count_before == 0
    assert proof.artifact_count_after == proof.publication_count_after == 1
    assert proof.resume_marker_count == 1
    assert proof.resume_marker_entry_point == "resume"
    assert evidence.recovery.checkpoint_removed is True
    assert proof.durable_identity_reused is True
    assert proof.durable_export_parity is True

    output_rows = tuple(json.loads(line) for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines())
    assert output_rows == (
        {"id": 1, "value": 10},
        {"id": 2, "value": 20},
        {"id": 3, "value": 30},
        {"id": 101, "value": -5},
        {"id": 102, "value": -10},
        {"id": 103, "value": -15},
    )
