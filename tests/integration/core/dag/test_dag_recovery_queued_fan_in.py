"""Production-path recovery evidence for the queued fan-in DAG scenario."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.engine.orchestrator import Orchestrator
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.recovery_queued_fan_in import (
    run_queued_fan_in_recovery_case,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    HarnessCaseSpec,
    ScenarioSpec,
    SinkOutputProjection,
)

_EXPECTED_OUTPUT_ROWS = (
    '{"id":1,"value":10}',
    '{"id":2,"value":20}',
    '{"id":3,"value":30}',
    '{"id":101,"value":-5}',
    '{"id":102,"value":-10}',
    '{"id":103,"value":-15}',
)


def _queued_fan_in_recovery_declaration() -> tuple[ScenarioSpec, HarnessCaseSpec]:
    manifest = load_manifest()
    scenario, runtime_case = next(
        (scenario, case)
        for scenario, case in iter_harness_cases(manifest)
        if (scenario.id, case.id) == ("multi-source-queue-fan-in", "queued-fan-in")
    )
    recovery_case = HarnessCaseSpec.model_validate(
        {
            **runtime_case.model_dump(mode="json"),
            "id": "queued-fan-in-reopen-resume",
            "workflow": "recovery",
            "recovery_kind": "sink_boundary",
            "recovery_fault": {
                "kind": "sink_effect",
                "seam": "before_effect",
                "sink_name": "output",
                "occurrence": 1,
            },
            "expected": {
                "kind": "summary",
                "status": "completed",
                "output_rows": 6,
                "required_audit_record_types": [
                    "artifact",
                    "operation",
                    "row",
                    "run",
                    "scheduler_event",
                    "sink_effect",
                    "sink_effect_member",
                    "token",
                    "token_outcome",
                ],
            },
        }
    )
    return scenario, recovery_case


def test_queued_fan_in_sink_boundary_recovery_reopens_and_resumes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _queued_fan_in_recovery_declaration()
    assert case.fixture == "multi-source-queue-fan-in/queued-fan-in.yaml"
    assert dict(case.input_fixtures) == {
        "orders": "multi-source-queue-fan-in/orders.csv",
        "refunds": "multi-source-queue-fan-in/refunds.csv",
    }

    production_run = inspect.unwrap(Orchestrator.run)
    production_resume = inspect.unwrap(Orchestrator.resume)
    run_calls: list[str] = []
    resume_calls: list[str] = []

    def record_run(self: Orchestrator, *args: Any, **kwargs: Any) -> Any:
        run_calls.append("run")
        return production_run(self, *args, **kwargs)

    def record_resume(self: Orchestrator, *args: Any, **kwargs: Any) -> Any:
        resume_calls.append("resume")
        return production_resume(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "run", record_run)
    monkeypatch.setattr(Orchestrator, "resume", record_resume)
    install_corpus_plugin_manager(monkeypatch)

    foundation_calls: list[tuple[str, str]] = []
    adversarial_guards: list[str] = []
    production_foundation = corpus_harness.run_sink_boundary_recovery_case

    def record_foundation(
        invoked_scenario: ScenarioSpec,
        invoked_case: HarnessCaseSpec,
        invoked_tmp_path: Path,
        **kwargs: Any,
    ) -> Any:
        foundation_calls.append((invoked_scenario.id, invoked_case.id))
        before_reopen_verifier = kwargs["before_reopen_verifier"]

        def verify_adversarial_work_and_checkpoint_guards(
            context: corpus_harness.SinkBoundaryInterruptedContext,
        ) -> None:
            first_work, second_work, *remaining_work = context.work
            mutations = (
                ("queue-node", {"node_id": "not-the-queue"}),
                ("attempt", {"attempt": 0}),
                ("outcome", {"pending_outcome": "failure"}),
                ("path", {"pending_path": "routed_error"}),
                ("error", {"pending_error_hash": "f" * 64}),
                ("row", {"row_id": second_work.row_id}),
                ("token", {"token_id": second_work.token_id}),
            )
            for label, update in mutations:
                mutated_work = (
                    first_work.model_copy(update=update),
                    second_work,
                    *remaining_work,
                )
                with pytest.raises(AssertionError):
                    before_reopen_verifier(
                        replace(context, work=mutated_work),
                    )
                adversarial_guards.append(label)
            with pytest.raises(AssertionError):
                before_reopen_verifier(
                    replace(
                        context,
                        checkpoint_sequence=context.checkpoint_sequence + 1,
                    )
                )
            adversarial_guards.append("checkpoint-sequence")
            before_reopen_verifier(context)

        kwargs["before_reopen_verifier"] = verify_adversarial_work_and_checkpoint_guards
        return production_foundation(
            invoked_scenario,
            invoked_case,
            invoked_tmp_path,
            **kwargs,
        )

    monkeypatch.setattr(
        corpus_harness,
        "run_sink_boundary_recovery_case",
        record_foundation,
    )

    build_identities: list[tuple[int, int, int, int, int, int]] = []
    production_build = corpus_harness.build_scenario

    def record_build(*args: Any, **kwargs: Any) -> Any:
        built = production_build(*args, **kwargs)
        build_identities.append(
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

    monkeypatch.setattr(corpus_harness, "build_scenario", record_build)

    evidence = run_queued_fan_in_recovery_case(scenario, case, tmp_path)

    assert foundation_calls == [("multi-source-queue-fan-in", "queued-fan-in-reopen-resume")]
    assert adversarial_guards == [
        "queue-node",
        "attempt",
        "outcome",
        "path",
        "error",
        "row",
        "token",
        "checkpoint-sequence",
    ]
    assert run_calls == ["run"]
    assert resume_calls == ["resume"]
    assert len(build_identities) == 2
    assert len(set(build_identities)) == 2

    assert evidence.schema_version == 2
    assert (evidence.scenario_id, evidence.case_id) == (
        "multi-source-queue-fan-in",
        "queued-fan-in-reopen-resume",
    )
    assert evidence.runtime.status == "completed"
    assert evidence.runtime.rows_processed == 6
    assert evidence.runtime.rows_succeeded == 6
    assert evidence.runtime.rows_failed == 0
    assert evidence.runtime.sink_outputs == (SinkOutputProjection(sink_name="output", rows=_EXPECTED_OUTPUT_ROWS),)
    assert evidence.audit.source_operation_count == 2
    assert evidence.recovery.database_reopened is True
    assert evidence.recovery.can_resume is True
    assert evidence.recovery.source_replayed is False
    assert evidence.recovery.checkpoint_removed is True

    proof = evidence.recovery.sink_boundary
    assert proof is not None
    assert proof.fault.model_dump(mode="json") == {
        "kind": "sink_effect",
        "seam": "before_effect",
        "sink_name": "output",
        "occurrence": 1,
    }
    assert proof.source_names_exhausted_before == ("orders", "refunds")
    assert proof.checkpoint_topology_hash == proof.fresh_topology_hash
    assert len(proof.token_ids_before) == 6
    assert proof.token_ids_after == proof.token_ids_before
    assert len(proof.work_before) == 6
    assert {item.status for item in proof.work_before} == {"pending_sink"}
    assert {item.row_payload_state for item in proof.work_before} == {"live"}
    assert {item.status for item in proof.work_after} == {"terminal"}
    assert {item.row_payload_state for item in proof.work_after} == {"purged"}
    assert proof.effect_count_before == 1
    assert proof.effect_member_count_before == 6
    assert proof.artifact_count_before == proof.publication_count_before == 0
    assert proof.effect_count_after == 1
    assert proof.artifact_count_after == proof.publication_count_after == 1
    assert proof.resume_marker_count == 1
    assert proof.resume_marker_event_type == "leader_acquire"
    assert proof.resume_marker_entry_point == "resume"
    assert proof.durable_identity_reused is True
    assert proof.durable_export_parity is True
