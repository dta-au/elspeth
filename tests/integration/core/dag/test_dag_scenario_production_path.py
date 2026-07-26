"""Production-path integration evidence for maintained DAG scenarios."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.core.dag import GraphValidationError
from elspeth.core.landscape import LandscapeDB, LandscapeExporter
from elspeth.core.landscape.schema import node_states_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus import loader as corpus_loader
from tests.fixtures.dag_scenario_corpus.harness import build_scenario, compute_fixture_sha256, render_settings, run_scenario_case
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest, resolve_fixture_path
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.schema import (
    BuildExpectation,
    HarnessCaseSpec,
    RunExpectation,
    ScenarioRunEvidence,
    ScenarioSpec,
    SummaryRunExpectation,
)

MANIFEST = load_manifest()
RUN_CASES = [
    pytest.param(scenario, case, id=f"{scenario.id}:{case.id}") for scenario, case in iter_harness_cases(MANIFEST) if case.workflow == "run"
]
RECOVERY_CASES = [
    pytest.param(scenario, case, id=f"{scenario.id}:{case.id}")
    for scenario, case in iter_harness_cases(MANIFEST)
    if case.workflow == "recovery"
]
BUILD_CASES = [
    pytest.param("fork-coalesce-policies", "require-all-nested", id="fork-coalesce-policies:require-all-nested"),
]

B1_RUNTIME_CASES = (
    ("linear", "happy-path"),
    ("multiple-independent-sources", "independent-roots"),
    ("multi-source-queue-fan-in", "queued-fan-in"),
    ("conditional-routing", "two-way-gate"),
    ("conditional-routing", "error-route-and-discard"),
)

B2_PARTIAL_TERMINAL_FAILURE_CASE = (
    "fork-multiple-terminals-partial-failure",
    "one-terminal-fails",
)


def _declared_case(scenario_id: str, case_id: str) -> tuple[ScenarioSpec, HarnessCaseSpec]:
    return next((scenario, case) for scenario, case in iter_harness_cases(MANIFEST) if (scenario.id, case.id) == (scenario_id, case_id))


@pytest.mark.parametrize(("scenario_id", "case_id"), B1_RUNTIME_CASES)
def test_b1_runtime_table_declares_exact_run_oracle(
    scenario_id: str,
    case_id: str,
) -> None:
    _scenario, case = _declared_case(scenario_id, case_id)

    assert case.workflow == "run"
    assert isinstance(case.expected, RunExpectation)


@pytest.mark.parametrize(("scenario_id", "case_id"), B1_RUNTIME_CASES)
def test_b1_runtime_table_executes_exact_production_oracle(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)
    _assert_declared_run_evidence(scenario, case, evidence)


def test_b1_multiple_independent_sources_preserves_exact_source_identity_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("multiple-independent-sources", "independent-roots")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)
    projection = evidence.runtime.durable_projection
    assert projection is not None

    assert tuple(row.source_name for row in projection.rows) == (
        "orders",
        "orders",
        "orders",
        "refunds",
        "refunds",
        "refunds",
    )
    assert tuple(row.ingest_sequence for row in projection.rows) == tuple(range(6))
    assert evidence.runtime.sink_outputs[0].rows == (
        '{"id":1,"value":10}',
        '{"id":2,"value":20}',
        '{"id":3,"value":30}',
        '{"id":101,"value":-5}',
        '{"id":102,"value":-10}',
        '{"id":103,"value":-15}',
    )
    assert evidence.audit.source_operation_count == 2


def test_b1_multi_source_queue_fan_in_proves_queue_traversal_and_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("multi-source-queue-fan-in", "queued-fan-in")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    projection = evidence.runtime.durable_projection
    assert projection is not None
    assert tuple(row.source_name for row in projection.rows) == (
        "orders",
        "orders",
        "orders",
        "refunds",
        "refunds",
        "refunds",
    )
    assert tuple(row.ingest_sequence for row in projection.rows) == tuple(range(6))
    assert evidence.runtime.sink_outputs[0].rows == (
        '{"id":1,"value":10}',
        '{"id":2,"value":20}',
        '{"id":3,"value":30}',
        '{"id":101,"value":-5}',
        '{"id":102,"value":-10}',
        '{"id":103,"value":-15}',
    )
    transform_states = tuple(state for state in projection.node_states if state.node_key.startswith("transform:normalize_rows@"))
    assert len(transform_states) == 6
    assert {state.token_key for state in transform_states} == {token.key for token in projection.tokens}
    assert {state.step_index for state in transform_states} == {2}
    audit_keys = {record.key for record in projection.audit_records}
    assert any(key.startswith("node|queue:inbound@") for key in audit_keys)
    assert sum("|continue|move|queue:inbound@" in key for key in audit_keys) == 2
    assert sum(key.startswith("edge|queue:inbound@") and "|continue|move|transform:normalize_rows@" in key for key in audit_keys) == 1
    assert evidence.audit.source_operation_count == 2


def test_b1_conditional_routing_proves_exact_artifacts_routes_and_dispositions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("conditional-routing", "two-way-gate")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)
    projection = evidence.runtime.durable_projection
    assert projection is not None

    assert [(output.sink_name, output.rows) for output in evidence.runtime.sink_outputs] == [
        ("accepted", ('{"id":2,"value":20}', '{"id":3,"value":30}')),
        ("rejected", ('{"id":1,"value":10}',)),
    ]
    assert [(route.token_key, route.label, route.mode) for route in projection.routes] == [
        ("primary:0#0", "false", "move"),
        ("primary:1#0", "true", "move"),
        ("primary:2#0", "true", "move"),
    ]
    assert [
        (disposition.token_key, disposition.outcome, disposition.path, disposition.sink_name)
        for disposition in projection.terminal_dispositions
    ] == [
        ("primary:0#0", "success", "gate_routed", "rejected"),
        ("primary:1#0", "success", "gate_routed", "accepted"),
        ("primary:2#0", "success", "gate_routed", "accepted"),
    ]


def test_b1_conditional_routing_proves_error_route_discard_and_audit_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_cases = {(scenario.id, case.id) for scenario, case in iter_harness_cases(MANIFEST)}
    assert ("conditional-routing", "error-route-and-discard") in registered_cases
    scenario, case = _declared_case("conditional-routing", "error-route-and-discard")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_run_evidence(scenario, case, evidence)
    projection = evidence.runtime.durable_projection
    assert projection is not None
    assert evidence.audit.portable_projection == projection
    assert [(output.sink_name, output.rows) for output in evidence.runtime.sink_outputs] == [
        ("errors", ('{"id":2,"value":20}', '{"id":3,"value":30}')),
    ]
    assert [(route.token_key, route.label, route.mode) for route in projection.routes] == [
        ("primary:1#0", "true", "move"),
        ("primary:1#0", "__error_fail_selected__", "divert"),
        ("primary:2#0", "true", "move"),
        ("primary:2#0", "__error_fail_selected__", "divert"),
    ]
    assert [
        (disposition.token_key, disposition.outcome, disposition.path, disposition.sink_name)
        for disposition in projection.terminal_dispositions
    ] == [
        ("primary:0#0", "success", "gate_discarded", None),
        ("primary:1#0", "failure", "on_error_routed", "errors"),
        ("primary:2#0", "failure", "on_error_routed", "errors"),
    ]


def test_b2_partial_terminal_failure_executes_exact_production_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(*B2_PARTIAL_TERMINAL_FAILURE_CASE)
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_run_evidence(scenario, case, evidence)
    projection = evidence.runtime.durable_projection
    assert projection is not None
    assert evidence.runtime.status == "completed_with_failures"
    assert [(output.sink_name, output.rows) for output in evidence.runtime.sink_outputs] == [
        (
            "survivor",
            (
                '{"id":1,"value":10}',
                '{"id":2,"value":20}',
                '{"id":3,"value":30}',
            ),
        ),
    ]
    assert [
        (disposition.token_key, disposition.outcome, disposition.path, disposition.sink_name)
        for disposition in projection.terminal_dispositions
    ] == [
        ("primary:0#0", "transient", "fork_parent", None),
        ("primary:0#1", "failure", "sink_discarded", "__discard__"),
        ("primary:0#2", "success", "default_flow", "survivor"),
        ("primary:1#0", "transient", "fork_parent", None),
        ("primary:1#1", "failure", "sink_discarded", "__discard__"),
        ("primary:1#2", "success", "default_flow", "survivor"),
        ("primary:2#0", "transient", "fork_parent", None),
        ("primary:2#1", "failure", "sink_discarded", "__discard__"),
        ("primary:2#2", "success", "default_flow", "survivor"),
    ]
    assert [token.key for token in projection.tokens] == [
        "primary:0#0",
        "primary:0#1",
        "primary:0#2",
        "primary:1#0",
        "primary:1#1",
        "primary:1#2",
        "primary:2#0",
        "primary:2#1",
        "primary:2#2",
    ]
    assert [token.parents for token in projection.tokens] == [
        (),
        ("primary:0#0",),
        ("primary:0#0",),
        (),
        ("primary:1#0",),
        ("primary:1#0",),
        (),
        ("primary:2#0",),
        ("primary:2#0",),
    ]
    assert tuple(work.token_key for work in projection.scheduler_work) == tuple(token.key for token in projection.tokens)
    assert all(work.final_status == "terminal" for work in projection.scheduler_work)
    assert not (tmp_path / "failing.jsonl").exists()
    assert evidence.audit.portable_projection == projection


def _copy_conditional_routing_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[HarnessCaseSpec, Path]:
    _scenario, case = _declared_case("conditional-routing", "two-way-gate")
    fixture_root = tmp_path / "fixtures"
    copied_yaml = fixture_root / case.fixture
    copied_input = fixture_root / case.input_fixtures["primary"]
    copied_yaml.parent.mkdir(parents=True)
    copied_yaml.write_bytes(resolve_fixture_path(case.fixture).read_bytes())
    copied_input.write_bytes(resolve_fixture_path(case.input_fixtures["primary"]).read_bytes())
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    return case, copied_yaml


def test_b1_conditional_routing_rejects_missing_boolean_gate_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, copied_yaml = _copy_conditional_routing_fixture(tmp_path, monkeypatch)
    copied_yaml.write_text(
        copied_yaml.read_text(encoding="utf-8").replace(
            'routes: {"true": accepted, "false": rejected}',
            'routes: {"true": accepted}',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"Missing required labels.*false"):
        render_settings(case, tmp_path / "runtime")


def test_b1_conditional_routing_rejects_invalid_gate_destination_during_production_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, copied_yaml = _copy_conditional_routing_fixture(tmp_path, monkeypatch)
    copied_yaml.write_text(
        copied_yaml.read_text(encoding="utf-8").replace(
            'routes: {"true": accepted, "false": rejected}',
            'routes: {"true": accepted, "false": missing_sink}',
        ),
        encoding="utf-8",
    )
    rendered = render_settings(case, tmp_path / "runtime")
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(GraphValidationError, match=r"missing_sink"):
        build_scenario(rendered)


def _assert_declared_run_evidence(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    evidence: ScenarioRunEvidence,
) -> None:
    assert isinstance(case.expected, RunExpectation)
    expected_fixture_hash = compute_fixture_sha256(case)

    assert evidence.schema_version == 1
    assert (evidence.scenario_id, evidence.case_id) == (scenario.id, case.id)
    assert evidence.fixture_sha256 == expected_fixture_hash
    assert evidence.completed_stages == ("config", "build", "runtime", "audit")

    assert evidence.config.loaded is True
    assert len(evidence.config.settings_sha256) == 64

    assert evidence.graph.accepted is True
    assert evidence.graph.node_count is not None and evidence.graph.node_count > 0
    assert evidence.graph.edge_count is not None and evidence.graph.edge_count > 0
    assert evidence.graph.node_type_counts is not None
    assert sum(item.count for item in evidence.graph.node_type_counts) == evidence.graph.node_count
    assert evidence.graph.edge_labels is not None
    assert evidence.graph.edge_labels == tuple(sorted(evidence.graph.edge_labels))
    assert len(evidence.graph.edge_labels) == evidence.graph.edge_count
    assert evidence.graph.topology_hash is not None
    assert len(evidence.graph.topology_hash) == 64

    assert evidence.runtime.attempted is True
    assert evidence.runtime.status == case.expected.status
    assert evidence.runtime.output_rows == sum(len(output.rows) for output in case.expected.sink_outputs)
    assert evidence.runtime.rows_processed == case.expected.rows_processed
    assert evidence.runtime.rows_succeeded == case.expected.rows_succeeded
    assert evidence.runtime.rows_failed == case.expected.rows_failed
    assert evidence.runtime.sink_outputs == case.expected.sink_outputs
    assert evidence.runtime.durable_projection == case.expected.projection

    assert evidence.audit.attempted is True
    assert evidence.audit.total_records > 0
    assert evidence.audit.total_records == sum(record.count for record in evidence.audit.record_counts)
    record_types = tuple(record.record_type for record in evidence.audit.record_counts)
    assert record_types == tuple(sorted(record_types))
    assert evidence.audit.record_counts == case.expected.audit_record_counts
    assert evidence.audit.source_operation_count == case.expected.source_operation_count
    assert evidence.audit.portable_projection == case.expected.projection

    assert evidence.recovery.model_dump() == {
        "attempted": False,
        "database_reopened": False,
        "checkpoint_id": None,
        "checkpoint_sequence": None,
        "can_resume": False,
        "source_replayed": False,
        "checkpoint_removed": False,
    }


def _assert_declared_recovery_evidence(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    evidence: ScenarioRunEvidence,
) -> None:
    assert isinstance(case.expected, SummaryRunExpectation)
    expected_fixture_hash = compute_fixture_sha256(case)

    assert evidence.schema_version == 1
    assert (evidence.scenario_id, evidence.case_id) == (scenario.id, case.id)
    assert evidence.fixture_sha256 == expected_fixture_hash
    assert evidence.completed_stages == ("config", "build", "runtime", "audit", "recovery")

    assert evidence.config.loaded is True
    assert len(evidence.config.settings_sha256) == 64
    assert evidence.graph.accepted is True
    assert evidence.graph.node_count is not None and evidence.graph.node_count > 0
    assert evidence.graph.edge_count is not None and evidence.graph.edge_count > 0
    assert evidence.graph.node_type_counts is not None
    assert sum(item.count for item in evidence.graph.node_type_counts) == evidence.graph.node_count
    assert evidence.graph.edge_labels is not None
    assert evidence.graph.edge_labels == tuple(sorted(evidence.graph.edge_labels))
    assert len(evidence.graph.edge_labels) == evidence.graph.edge_count
    assert evidence.graph.topology_hash is not None and len(evidence.graph.topology_hash) == 64

    assert evidence.runtime.attempted is True
    assert evidence.runtime.status == case.expected.status
    assert evidence.runtime.output_rows == case.expected.output_rows

    assert evidence.audit.attempted is True
    assert evidence.audit.total_records > 0
    assert evidence.audit.total_records == sum(record.count for record in evidence.audit.record_counts)
    record_types = tuple(record.record_type for record in evidence.audit.record_counts)
    assert record_types == tuple(sorted(record_types))
    assert set(case.expected.required_audit_record_types) <= set(record_types)

    assert evidence.recovery.attempted is True
    assert evidence.recovery.database_reopened is True
    assert evidence.recovery.checkpoint_id is not None
    assert evidence.recovery.checkpoint_sequence is not None
    assert evidence.recovery.can_resume is True
    assert evidence.recovery.source_replayed is False
    assert evidence.recovery.checkpoint_removed is True


def _assert_declared_build_evidence(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    evidence: ScenarioRunEvidence,
) -> None:
    expected_fixture_hash = compute_fixture_sha256(case)
    expected = case.expected
    assert isinstance(expected, BuildExpectation)

    assert evidence.schema_version == 1
    assert (evidence.scenario_id, evidence.case_id) == (scenario.id, case.id)
    assert evidence.fixture_sha256 == expected_fixture_hash
    assert evidence.completed_stages == ("config", "build")

    assert evidence.config.loaded is True
    assert len(evidence.config.settings_sha256) == 64
    assert evidence.graph.accepted is True
    assert evidence.graph.node_count == expected.node_count
    assert evidence.graph.edge_count == expected.edge_count
    assert evidence.graph.node_type_counts == expected.node_type_counts
    assert evidence.graph.edge_labels == expected.edge_labels
    assert evidence.graph.topology_hash is not None and len(evidence.graph.topology_hash) == 64

    assert evidence.runtime.model_dump() == {
        "attempted": False,
        "run_id": None,
        "status": None,
        "rows_processed": 0,
        "rows_succeeded": 0,
        "rows_failed": 0,
        "output_rows": 0,
    }
    assert evidence.audit.model_dump() == {
        "attempted": False,
        "total_records": 0,
        "record_counts": (),
        "source_operation_count": 0,
    }
    assert evidence.recovery.model_dump() == {
        "attempted": False,
        "database_reopened": False,
        "checkpoint_id": None,
        "checkpoint_sequence": None,
        "can_resume": False,
        "source_replayed": False,
        "checkpoint_removed": False,
    }


def test_build_workflow_has_dedicated_dispatcher() -> None:
    assert callable(corpus_harness._build_case)


@pytest.mark.parametrize(("scenario_id", "case_id"), BUILD_CASES)
def test_declared_build_case_uses_complete_production_build_path(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)

    def forbid_runtime_or_audit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build-only corpus workflow crossed the runtime/audit boundary")

    monkeypatch.setattr(corpus_harness, "Orchestrator", forbid_runtime_or_audit)
    monkeypatch.setattr(corpus_harness, "LandscapeDB", forbid_runtime_or_audit)
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_build_evidence(scenario, case, evidence)
    assert not (tmp_path / "output.jsonl").exists()
    assert not (tmp_path / "fault-triggered.marker").exists()


def test_run_case_owns_production_preflight_without_pytest_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    production_run = inspect.unwrap(Orchestrator.run)

    def run_without_autouse_defaults(self: Orchestrator, *args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("openrouter_catalog_sha256")
        assert kwargs.get("openrouter_catalog_source") in {"bundled", "live"}
        return production_run(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "run", run_without_autouse_defaults)
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    assert evidence.runtime.status == "completed"


def test_exact_runtime_projection_rejects_corrupted_portable_sink_effect_artifact_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    export_run = LandscapeExporter.export_run

    def export_run_with_corrupted_artifact_reference(
        self: LandscapeExporter,
        run_id: str,
    ) -> Any:
        for record in export_run(self, run_id):
            if record["record_type"] == "sink_effect":
                yield {**record, "artifact_id": "CORRUPTED"}
            else:
                yield record

    monkeypatch.setattr(
        LandscapeExporter,
        "export_run",
        export_run_with_corrupted_artifact_reference,
    )
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable sink_effect.*artifact"):
        run_scenario_case(scenario, case, tmp_path)


def _mutate_portable_export(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record_type: str,
    field: str,
    value: object,
) -> None:
    export_run = LandscapeExporter.export_run

    def export_run_with_corrupted_material(self: LandscapeExporter, run_id: str) -> Any:
        for record in export_run(self, run_id):
            if record["record_type"] == record_type:
                yield {**record, field: value}
            else:
                yield record

    monkeypatch.setattr(LandscapeExporter, "export_run", export_run_with_corrupted_material)


@pytest.mark.parametrize(
    "field",
    ("expected_descriptor_hash", "result_descriptor_hash", "precondition_hash"),
)
def test_exact_runtime_projection_rejects_corrupted_portable_sink_effect_hash(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    _mutate_portable_export(monkeypatch, record_type="sink_effect", field=field, value="0" * 64)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable sink_effect integrity"):
        run_scenario_case(scenario, case, tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (("descriptor_hash", "0" * 64), ("member_effect_id", "CORRUPTED")),
)
def test_exact_runtime_projection_rejects_corrupted_portable_sink_effect_member_material(
    field: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    _mutate_portable_export(monkeypatch, record_type="sink_effect_member", field=field, value=value)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable sink_effect_member integrity"):
        run_scenario_case(scenario, case, tmp_path)


@pytest.mark.parametrize("field", ("request_hash", "evidence_hash"))
def test_exact_runtime_projection_rejects_corrupted_portable_sink_effect_attempt_hash(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    _mutate_portable_export(monkeypatch, record_type="sink_effect_attempt", field=field, value="0" * 64)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable sink_effect_attempt integrity"):
        run_scenario_case(scenario, case, tmp_path)


@pytest.mark.parametrize(
    "field",
    (
        "final_hash",
        "last_chunk_seal_hash",
        "manifest_hash",
        "registry_key_hash",
        "snapshot_hash",
        "snapshot_id",
        "snapshot_seal_hash",
        "signature",
    ),
)
def test_exact_runtime_projection_rejects_corrupted_portable_manifest_material(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    value = "CORRUPTED" if field == "signature" else "0" * 64
    _mutate_portable_export(monkeypatch, record_type="manifest", field=field, value=value)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable manifest integrity"):
        run_scenario_case(scenario, case, tmp_path)


@pytest.mark.parametrize("field", ("request_hash", "response_hash"))
def test_exact_runtime_projection_rejects_corrupted_portable_sink_effect_call_hash(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    _mutate_portable_export(monkeypatch, record_type="call", field=field, value="0" * 64)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match=r"portable call integrity"):
        run_scenario_case(scenario, case, tmp_path)


def test_exact_runtime_projection_preserves_same_name_semantic_config_difference() -> None:
    first = {
        "node_id": "sink_shared_aaaaaaaaaaaa",
        "node_type": "sink",
        "config": {"operation": "first", "path": "/volatile/runtime/first"},
    }
    second = {
        "node_id": "sink_shared_bbbbbbbbbbbb",
        "node_type": "sink",
        "config": {"operation": "second", "path": "/volatile/runtime/second"},
    }
    path_only_difference = {
        "node_id": "sink_shared_cccccccccccc",
        "node_type": "sink",
        "config": {"operation": "first", "path": "/volatile/runtime/third"},
    }

    assert corpus_harness._stable_node_key(first) != corpus_harness._stable_node_key(second)
    assert corpus_harness._stable_node_key(first) == corpus_harness._stable_node_key(path_only_difference)


def test_render_settings_quotes_yaml_significant_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, case = _declared_case("linear", "happy-path")
    original_fixture = resolve_fixture_path(case.fixture)
    input_fixture = case.input_fixtures["primary"]
    original_input = resolve_fixture_path(input_fixture)
    fixture_root = tmp_path / "fixture root : # corpus"
    copied_fixture = fixture_root / case.fixture
    copied_input = fixture_root / input_fixture
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_bytes(original_fixture.read_bytes())
    copied_input.write_bytes(original_input.read_bytes())
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    runtime_root = tmp_path / "runtime root : # output"
    runtime_root.mkdir()

    rendered = render_settings(case, runtime_root)
    install_corpus_plugin_manager(monkeypatch)
    built = build_scenario(rendered)

    assert rendered.settings.sources["primary"].options["path"] == str(copied_input)
    assert rendered.settings.sinks["output"].options["path"] == str(runtime_root / "output.jsonl")
    assert rendered.output_path == runtime_root / "output.jsonl"
    assert rendered.fault_marker == runtime_root / "fault-triggered.marker"
    assert built.graph_evidence.accepted is True


def _write_plural_binding_fixture(
    fixture_root: Path,
    *,
    source_paths: tuple[str, str] = ("${input_orders}", "${input_refunds}"),
    sink_paths: tuple[str, str] = ("${output_accepted}", "${output_rejected}"),
    suffix: str = "",
) -> HarnessCaseSpec:
    scenario_root = fixture_root / "binding"
    scenario_root.mkdir(parents=True)
    (scenario_root / "orders.csv").write_text("id,value\n1,10\n", encoding="utf-8")
    (scenario_root / "refunds.csv").write_text("id,value\n2,-5\n", encoding="utf-8")
    (scenario_root / "binding.yaml").write_text(
        f"""sources:
  orders:
    plugin: csv
    on_success: accepted
    options:
      path: {source_paths[0]}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: [\"id: int\", \"value: int\"]}}
  refunds:
    plugin: csv
    on_success: rejected
    options:
      path: {source_paths[1]}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: [\"id: int\", \"value: int\"]}}
sinks:
  accepted:
    plugin: json
    on_write_failure: discard
    options:
      path: {sink_paths[0]}
      format: jsonl
      schema: {{mode: observed}}
  rejected:
    plugin: json
    on_write_failure: discard
    options:
      path: {sink_paths[1]}
      format: jsonl
      schema: {{mode: observed}}
{suffix}""",
        encoding="utf-8",
    )
    return HarnessCaseSpec.model_validate(
        {
            "id": "binding",
            "workflow": "build",
            "fixture": "binding/binding.yaml",
            "input_fixtures": {
                "orders": "binding/orders.csv",
                "refunds": "binding/refunds.csv",
            },
            "output_artifacts": {
                "accepted": "accepted.jsonl",
                "rejected": "rejected.jsonl",
            },
            "expected": {
                "node_count": 4,
                "edge_count": 2,
                "node_type_counts": (
                    {"node_type": "sink", "count": 2},
                    {"node_type": "source", "count": 2},
                ),
                "edge_labels": ("on_success", "on_success"),
            },
        }
    )


def test_plural_input_artifact_binding_renders_exact_source_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixture root : # corpus"
    case = _write_plural_binding_fixture(fixture_root)
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    runtime_root = tmp_path / "runtime root : # artifacts"
    runtime_root.mkdir()

    rendered = render_settings(case, runtime_root)

    assert {name: source.options["path"] for name, source in rendered.settings.sources.items()} == {
        "orders": str(fixture_root / "binding/orders.csv"),
        "refunds": str(fixture_root / "binding/refunds.csv"),
    }


@pytest.mark.parametrize(
    ("source_paths", "input_fixtures", "message"),
    [
        (("${input_orders}", "${input_refunds}"), {"orders": "binding/orders.csv"}, "source names must exactly match"),
        (
            ("${input_orders}", "${input_refunds}"),
            {
                "decoy": "binding/decoy.csv",
                "orders": "binding/orders.csv",
                "refunds": "binding/refunds.csv",
            },
            "source names must exactly match",
        ),
        (
            ("${input_refunds}", "${input_orders}"),
            {"orders": "binding/orders.csv", "refunds": "binding/refunds.csv"},
            "trusted input token must configure declared source",
        ),
    ],
    ids=("missing-source", "extra-source", "swapped-bindings"),
)
def test_plural_input_artifact_binding_rejects_source_map_drift(
    source_paths: tuple[str, str],
    input_fixtures: dict[str, str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    case = _write_plural_binding_fixture(fixture_root, source_paths=source_paths)
    case = HarnessCaseSpec.model_validate({**case.model_dump(mode="json"), "input_fixtures": input_fixtures})
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match=message):
        render_settings(case, tmp_path / "runtime")


def test_plural_input_artifact_binding_rejects_comment_only_decoy_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    case = _write_plural_binding_fixture(
        fixture_root,
        source_paths=(json.dumps(str(fixture_root / "binding/decoy.csv")), "${input_refunds}"),
        suffix="# declared input: ${input_orders}\n",
    )
    (fixture_root / "binding/decoy.csv").write_text("id,value\n99,999\n", encoding="utf-8")
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="trusted input token must configure declared source"):
        render_settings(case, tmp_path / "runtime")


def test_plural_input_artifact_binding_rejects_normalized_token_collision() -> None:
    values = _declared_case("multiple-independent-sources", "independent-roots")[1].model_dump(mode="json")
    values["input_fixtures"] = {
        "orders-v1": "multiple-independent-sources/orders.csv",
        "orders_v1": "multiple-independent-sources/refunds.csv",
    }
    values["output_artifacts"] = {"output": "output.jsonl"}

    with pytest.raises(ValidationError, match="normalized template token collision"):
        HarnessCaseSpec.model_validate(values)


def test_per_sink_artifact_binding_renders_exact_sink_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    case = _write_plural_binding_fixture(fixture_root)
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    rendered = render_settings(case, tmp_path / "runtime")

    assert rendered.output_paths == {
        "accepted": tmp_path / "runtime/accepted.jsonl",
        "rejected": tmp_path / "runtime/rejected.jsonl",
    }


def test_intentionally_absent_artifact_is_accepted_only_when_declared(
    tmp_path: Path,
) -> None:
    _scenario, case = _declared_case("linear", "happy-path")
    values = case.model_dump(mode="json")
    values["output_artifacts"] = {
        "output": {"filename": "output.jsonl", "presence": "absent"},
    }
    absent_case = HarnessCaseSpec.model_validate(values)
    rendered = render_settings(absent_case, tmp_path)

    assert corpus_harness._sink_outputs(rendered) == ()


def test_intentionally_absent_artifact_rejects_file_leakage(
    tmp_path: Path,
) -> None:
    _scenario, case = _declared_case("linear", "happy-path")
    values = case.model_dump(mode="json")
    values["output_artifacts"] = {
        "output": {"filename": "output.jsonl", "presence": "absent"},
    }
    absent_case = HarnessCaseSpec.model_validate(values)
    rendered = render_settings(absent_case, tmp_path)
    rendered.output_paths["output"].write_text('{"id":1,"value":10}\n', encoding="utf-8")

    with pytest.raises(AssertionError, match="produced intentionally absent artifact"):
        corpus_harness._sink_outputs(rendered)


def test_required_artifact_cannot_disappear_based_only_on_sink_plugin(
    tmp_path: Path,
) -> None:
    _scenario, case = _declared_case(*B2_PARTIAL_TERMINAL_FAILURE_CASE)
    values = case.model_dump(mode="json")
    values["output_artifacts"] = {
        "failing": {"filename": "failing.jsonl", "presence": "required"},
        "survivor": {"filename": "survivor.jsonl", "presence": "absent"},
    }
    required_case = HarnessCaseSpec.model_validate(values)
    rendered = render_settings(required_case, tmp_path)

    with pytest.raises(AssertionError, match=r"sink 'failing'.*did not produce"):
        corpus_harness._sink_outputs(rendered)


def test_per_sink_artifact_binding_rejects_existing_symlink_leaf_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    case = _write_plural_binding_fixture(fixture_root)
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    outside_artifact = tmp_path / "outside.jsonl"
    outside_artifact.write_text("outside\n", encoding="utf-8")
    (runtime_root / "accepted.jsonl").symlink_to(outside_artifact)

    with pytest.raises(ValueError, match=r"sink 'accepted'.*resolve beneath.*runtime"):
        render_settings(case, runtime_root)


@pytest.mark.parametrize(
    ("sink_paths", "output_artifacts", "message"),
    [
        (("${output_accepted}", "${output_rejected}"), {"accepted": "accepted.jsonl"}, "sink names must exactly match"),
        (
            ("${output_accepted}", "${output_rejected}"),
            {"accepted": "accepted.jsonl", "decoy": "decoy.jsonl", "rejected": "rejected.jsonl"},
            "sink names must exactly match",
        ),
        (
            ("${output_rejected}", "${output_accepted}"),
            {"accepted": "accepted.jsonl", "rejected": "rejected.jsonl"},
            "trusted output token must configure declared sink",
        ),
    ],
    ids=("missing-sink", "extra-sink", "swapped-bindings"),
)
def test_per_sink_artifact_binding_rejects_sink_map_drift(
    sink_paths: tuple[str, str],
    output_artifacts: dict[str, str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_root = tmp_path / "fixtures"
    case = _write_plural_binding_fixture(fixture_root, sink_paths=sink_paths)
    case = HarnessCaseSpec.model_validate({**case.model_dump(mode="json"), "output_artifacts": output_artifacts})
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match=message):
        render_settings(case, tmp_path / "runtime")


def test_per_sink_artifact_binding_rejects_input_output_token_collision() -> None:
    values = _declared_case("linear", "happy-path")[1].model_dump(mode="json")
    values["input_fixtures"] = {"shared": "linear/input.csv"}
    values["output_artifacts"] = {"shared": "output.jsonl"}

    with pytest.raises(ValidationError, match="input/output template token collision"):
        HarnessCaseSpec.model_validate(values)


def test_render_settings_rejects_fixture_without_declared_input_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, case = _declared_case("linear", "happy-path")
    original_fixture = resolve_fixture_path(case.fixture)
    input_fixture = case.input_fixtures["primary"]
    original_input = resolve_fixture_path(input_fixture)
    fixture_root = tmp_path / "fixtures"
    copied_fixture = fixture_root / case.fixture
    copied_input = fixture_root / input_fixture
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_text(
        original_fixture.read_text(encoding="utf-8").replace("${input_primary}", json.dumps(str(copied_input))),
        encoding="utf-8",
    )
    copied_input.write_bytes(original_input.read_bytes())
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="trusted input token must configure declared source"):
        render_settings(case, tmp_path / "runtime")


def test_render_settings_rejects_input_reference_outside_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, case = _declared_case("linear", "happy-path")
    original_fixture = resolve_fixture_path(case.fixture)
    input_fixture = case.input_fixtures["primary"]
    original_input = resolve_fixture_path(input_fixture)
    fixture_root = tmp_path / "fixtures"
    copied_fixture = fixture_root / case.fixture
    copied_input = fixture_root / input_fixture
    decoy_input = copied_fixture.parent / "decoy.csv"
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_text(
        original_fixture.read_text(encoding="utf-8").replace("${input_primary}", json.dumps(str(decoy_input)))
        + "\n# declared input: ${input_primary}\n",
        encoding="utf-8",
    )
    copied_input.write_bytes(original_input.read_bytes())
    decoy_input.write_text("id,value\n99,999\n", encoding="utf-8")
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="trusted input token must configure declared source"):
        render_settings(case, tmp_path / "runtime")


def test_generic_run_case_assertions_accept_future_case_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)
    future_scenario = scenario.model_copy(update={"id": "future-run-scenario"})
    future_case = case.model_copy(update={"id": "future-run-case"})
    future_evidence = evidence.model_copy(
        update={
            "scenario_id": future_scenario.id,
            "case_id": future_case.id,
            "graph": evidence.graph.model_copy(
                update={
                    "node_count": 7,
                    "edge_count": 6,
                    "node_type_counts": tuple(
                        item.model_copy(update={"count": 4}) if item.node_type == "transform" else item
                        for item in evidence.graph.node_type_counts or ()
                    ),
                    "edge_labels": ("continue", "continue", "continue", "continue", "on_error", "on_success"),
                }
            ),
        }
    )

    _assert_declared_run_evidence(future_scenario, future_case, future_evidence)


@pytest.mark.parametrize(("scenario", "case"), RUN_CASES)
def test_declared_run_case_uses_complete_production_path(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_run_evidence(scenario, case, evidence)


def test_linear_happy_path_has_exact_production_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    assert (scenario.id, case.id, case.fixture, dict(case.input_fixtures)) == (
        "linear",
        "happy-path",
        "linear/happy-path.yaml",
        {"primary": "linear/input.csv"},
    )

    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)
    _assert_declared_run_evidence(scenario, case, evidence)

    # The declared queue is a first-class runtime node between source and transform.
    assert evidence.graph.node_count == 4
    assert evidence.graph.edge_count == 3

    assert evidence.runtime.rows_processed == 3
    assert evidence.runtime.rows_succeeded == 3
    assert evidence.runtime.rows_failed == 0
    output_rows = [json.loads(line) for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines()]
    assert output_rows == [
        {"id": 1, "value": 10},
        {"id": 2, "value": 20},
        {"id": 3, "value": 30},
    ]

    audit_counts = {record.record_type: record.count for record in evidence.audit.record_counts}
    assert {"run", "node", "edge", "operation", "row"} <= set(audit_counts)
    assert audit_counts["run"] == 1
    assert audit_counts["node"] == 4
    assert audit_counts["edge"] == 3
    assert audit_counts["row"] == 3
    assert evidence.audit.source_operation_count == 1


def test_exact_runtime_projection_linear_matches_declared_durable_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "happy-path")
    assert isinstance(case.expected, RunExpectation)
    assert case.expected.kind == "exact"
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    assert evidence.runtime.kind == "exact"
    assert evidence.audit.kind == "exact"
    assert evidence.runtime.sink_outputs == case.expected.sink_outputs
    assert evidence.runtime.durable_projection == case.expected.projection
    assert evidence.audit.portable_projection == case.expected.projection
    assert evidence.runtime.durable_projection == evidence.audit.portable_projection
    assert evidence.audit.record_counts == case.expected.audit_record_counts
    assert evidence.audit.source_operation_count == case.expected.source_operation_count


@pytest.mark.parametrize(("scenario", "case"), RECOVERY_CASES)
def test_declared_recovery_case_reopens_and_resumes_publicly(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_recovery_evidence(scenario, case, evidence)


def test_checkpoint_reopen_resume_has_exact_restart_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    assert tuple(
        (declared_scenario.id, declared_case.id)
        for declared_scenario, declared_case in iter_harness_cases(MANIFEST)
        if declared_case.workflow == "recovery"
    ) == (("checkpoint-deterministic-resume", "reopen-resume"),)

    production_run = inspect.unwrap(Orchestrator.run)
    production_resume = inspect.unwrap(Orchestrator.resume)
    monkeypatch.setattr(Orchestrator, "run", production_run)
    monkeypatch.setattr(Orchestrator, "resume", production_resume)
    install_corpus_plugin_manager(monkeypatch)

    built_objects: list[Any] = []
    production_build = corpus_harness.build_scenario

    def record_fresh_build(*args: Any, **kwargs: Any) -> Any:
        built = production_build(*args, **kwargs)
        built_objects.append(built)
        return built

    monkeypatch.setattr(corpus_harness, "build_scenario", record_fresh_build)

    evidence = corpus_harness.run_scenario_case(scenario, case, tmp_path)
    _assert_declared_recovery_evidence(scenario, case, evidence)

    assert len(built_objects) == 2
    initial, fresh = built_objects
    assert initial is not fresh
    assert initial.rendered.settings is not fresh.rendered.settings
    assert initial.bundle is not fresh.bundle
    assert initial.graph is not fresh.graph
    assert initial.config is not fresh.config
    assert evidence.runtime.rows_processed == 3
    # Three source rows are consumed into one terminal aggregation output.
    assert evidence.runtime.rows_succeeded == 1
    assert evidence.runtime.rows_failed == 0
    assert evidence.audit.source_operation_count == 1
    assert [json.loads(line) for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"value": 60, "count": 3}
    ]
    assert (tmp_path / "fault-triggered.marker").is_file()


def test_recovery_verifier_rejects_checkpoint_marker_on_initial_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    production_run = inspect.unwrap(Orchestrator.run)
    production_resume = inspect.unwrap(Orchestrator.resume)
    monkeypatch.setattr(Orchestrator, "run", production_run)
    monkeypatch.setattr(Orchestrator, "resume", production_resume)
    install_corpus_plugin_manager(monkeypatch)
    evidence = corpus_harness.run_scenario_case(scenario, case, tmp_path)
    assert evidence.runtime.run_id is not None
    assert evidence.recovery.checkpoint_id is not None

    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=False)
    try:
        with db.engine.begin() as conn:
            resume_node_id = conn.execute(
                node_states_table.select()
                .with_only_columns(node_states_table.c.node_id)
                .where(
                    node_states_table.c.run_id == evidence.runtime.run_id,
                    node_states_table.c.attempt > 0,
                    node_states_table.c.status == "completed",
                    node_states_table.c.resume_checkpoint_id == evidence.recovery.checkpoint_id,
                )
            ).scalar_one()
            conn.execute(
                node_states_table.update().where(node_states_table.c.run_id == evidence.runtime.run_id).values(resume_checkpoint_id=None)
            )
            conn.execute(
                node_states_table.update()
                .where(
                    node_states_table.c.run_id == evidence.runtime.run_id,
                    node_states_table.c.attempt == 0,
                )
                .values(resume_checkpoint_id=evidence.recovery.checkpoint_id)
            )

        with pytest.raises(AssertionError, match="resumed node-state attempt"):
            corpus_harness._assert_terminal_recovery_state(
                db,
                run_id=evidence.runtime.run_id,
                checkpoint_id=evidence.recovery.checkpoint_id,
                resume_node_id=resume_node_id,
                payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
            )
    finally:
        db.close()
