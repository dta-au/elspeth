"""Production-path integration evidence for maintained DAG scenarios."""

from __future__ import annotations

import hashlib
import inspect
import json
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.dag import GraphValidationError
from elspeth.core.landscape import LandscapeDB, LandscapeExporter
from elspeth.core.landscape.data_flow import tokens as data_flow_tokens
from elspeth.core.landscape.scheduler import barrier as scheduler_barrier
from elspeth.core.landscape.scheduler import queue as scheduler_queue
from elspeth.core.landscape.scheduler import work_items as scheduler_work_items
from elspeth.core.landscape.schema import (
    node_states_table,
    routing_events_table,
    rows_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine import processor as engine_processor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator import run_lifecycle as orchestrator_run_lifecycle
from tests.fixtures.dag_scenario_corpus import harness as corpus_harness
from tests.fixtures.dag_scenario_corpus import loader as corpus_loader
from tests.fixtures.dag_scenario_corpus.harness import (
    build_scenario,
    compute_fixture_sha256,
    render_settings,
    run_scenario_case,
    semantic_runtime_projection,
    semantic_runtime_projection_counts,
    semantic_runtime_projection_sha256,
)
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest, resolve_fixture_path
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.schema import (
    BuildExpectation,
    HarnessCaseSpec,
    RunExpectation,
    ScenarioRunEvidence,
    ScenarioSpec,
    SemanticRunExpectation,
    SinkOutputProjection,
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

B3_RUNTIME_CASES = (
    ("aggregation-immutable-batch", "eof-immutable-membership"),
    ("row-expansion-parent-child-recovery", "json-explode-parent-child"),
    ("retry-quarantine-discard-routed-errors", "retry-then-success"),
    ("retry-quarantine-discard-routed-errors", "source-quarantine-routed"),
    ("retry-quarantine-discard-routed-errors", "transform-discard"),
    ("retry-quarantine-discard-routed-errors", "transform-error-route"),
    ("sink-write-pending-redrive", "write-once"),
)

B3_RECOVERY_CASES = (
    ("aggregation-immutable-batch", "resume-after-eof-flush-fault"),
    ("row-expansion-parent-child-recovery", "resume-after-child-enqueue"),
    ("sink-write-pending-redrive", "pending-redrive-reopen"),
)

B2_COALESCE_POSITIVE_CASES = tuple(
    ("fork-coalesce-policies", case_id)
    for case_id in (
        "require-all-union",
        "require-all-nested",
        "require-all-select",
        "first-union",
        "first-nested",
        "first-select",
        "quorum-union-lost-c",
        "quorum-nested-lost-c",
        "quorum-select-lost-c",
        "best-effort-union-lost-c",
        "best-effort-nested-lost-c",
        "best-effort-select-lost-c",
    )
)

B2_COALESCE_FAILURE_AND_COLLISION_CASES = tuple(
    ("fork-coalesce-policies", case_id)
    for case_id in (
        "require-all-lost-c",
        "quorum-impossible-lost-c",
        "best-effort-all-lost",
        "first-all-lost",
        "union-collision-last-wins",
        "union-collision-first-wins",
        "union-collision-fail",
    )
)
B2_COALESCE_ALL_CASES = B2_COALESCE_POSITIVE_CASES + B2_COALESCE_FAILURE_AND_COLLISION_CASES
B2_COMPOSED_COALESCE_CASES = (
    ("sequential-nested-fork-coalesce", "two-sequential-require-all"),
    ("parallel-coalesces", "two-parallel-require-all"),
)


def _deterministic_source_id_factory() -> Any:
    sequence = count()

    def generate() -> str:
        return hashlib.sha256(f"b2-78-source:{next(sequence)}".encode()).hexdigest()[:32]

    return generate


def _deterministic_token_id_factory() -> Any:
    sequence = count()

    def generate() -> str:
        return hashlib.sha256(f"b2-78-token:{next(sequence)}".encode()).hexdigest()[:32]

    return generate


def _ordered_work_item_id(*, reverse: bool) -> Any:
    def generate(run_id: str, token_id: str, node_id: str | None, attempt: int) -> str:
        node_key = "<terminal>" if node_id is None else node_id
        digest = hashlib.sha256(f"{run_id}:{token_id}:{node_key}:{attempt}".encode()).hexdigest()
        if not reverse:
            return digest
        return "".join(format(15 - int(character, 16), "x") for character in digest)

    return generate


def _install_repeat_run_identity_order(monkeypatch: pytest.MonkeyPatch, *, reverse: bool) -> None:
    monkeypatch.setattr(orchestrator_run_lifecycle, "generate_id", lambda: "1" * 32)
    monkeypatch.setattr(engine_processor, "generate_id", _deterministic_source_id_factory())
    monkeypatch.setattr(data_flow_tokens, "generate_id", _deterministic_token_id_factory())
    work_item_id = _ordered_work_item_id(reverse=reverse)
    monkeypatch.setattr(scheduler_queue, "make_work_item_id", work_item_id)
    monkeypatch.setattr(scheduler_barrier, "make_work_item_id", work_item_id)
    monkeypatch.setattr(scheduler_work_items, "work_item_id", work_item_id)


def _archive_repeat_run_files(case: HarnessCaseSpec, runtime_root: Path) -> Path:
    first_db = runtime_root / "first-audit.db"
    (runtime_root / "audit.db").replace(first_db)
    payloads = runtime_root / "payloads"
    if payloads.exists():
        payloads.replace(runtime_root / "first-payloads")
    for artifact in case.output_artifacts.values():
        output_path = runtime_root / artifact.filename
        if output_path.exists():
            output_path.replace(runtime_root / f"first-{artifact.filename}")
    return first_db


def _portable_records(database_path: Path, run_id: str) -> list[dict[str, Any]]:
    db = LandscapeDB(f"sqlite:///{database_path}")
    try:
        return list(LandscapeExporter(db).export_run(run_id))
    finally:
        db.close()


def _sink_member_material(projection: Any, field: str) -> tuple[tuple[str, object], ...]:
    return tuple(
        (record.key, json.loads(record.material)[field])
        for record in projection.audit_records
        if record.record_type == "sink_effect_member"
    )


def _audit_without_lineage_hash(projection: Any) -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    normalized = []
    for record in projection.audit_records:
        material = json.loads(record.material)
        if record.record_type == "sink_effect_member":
            material["lineage_hash"] = "$ORDERED_LINEAGE_IDENTITY"
        normalized.append((record.key, record.record_type, json.dumps(material, sort_keys=True, separators=(",", ":")), record.references))
    return tuple(normalized)


def _ordered_parent_sequences(projection: Any) -> tuple[tuple[str, tuple[tuple[int, str], ...]], ...]:
    return tuple(
        (
            token.key,
            tuple((parent.ordinal, parent.parent_key) for parent in token.parents),
        )
        for token in projection.tokens
    )


def _coalesce_contexts(projection: Any) -> tuple[tuple[str, str], ...]:
    return tuple((state.key, state.context_after) for state in projection.node_states if state.context_after is not None)


def _initial_raw_identities(records: list[dict[str, Any]]) -> tuple[tuple[object, ...], tuple[object, ...]]:
    child_token_ids = {str(record["token_id"]) for record in records if record.get("record_type") == "token_parent"}
    rows = tuple(
        sorted(
            (
                record.get("source_name"),
                record.get("source_row_index"),
                record.get("row_id"),
            )
            for record in records
            if record.get("record_type") == "row"
        )
    )
    root_tokens = tuple(
        sorted(
            (record.get("row_id"), record.get("token_id"))
            for record in records
            if record.get("record_type") == "token" and str(record.get("token_id")) not in child_token_ids
        )
    )
    return rows, root_tokens


def _assert_require_all_parent_order_drift(
    first: tuple[tuple[str, tuple[tuple[int, str], ...]], ...],
    second: tuple[tuple[str, tuple[tuple[int, str], ...]], ...],
) -> None:
    differences = []
    for (first_key, first_parents), (second_key, second_parents) in zip(first, second, strict=True):
        assert first_key == second_key
        if first_parents == second_parents:
            continue
        assert len(first_parents) == len(second_parents) == 2
        assert tuple(ordinal for ordinal, _parent in first_parents) == tuple(ordinal for ordinal, _parent in second_parents)
        assert {parent for _ordinal, parent in first_parents} == {parent for _ordinal, parent in second_parents}
        differences.append(first_key)
    assert differences


def _artifact_content_facts(records: list[dict[str, Any]]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                record.get("artifact_type"),
                record.get("content_hash"),
                record.get("size_bytes"),
                record.get("path_or_uri"),
            )
            for record in records
            if record.get("record_type") == "artifact"
        )
    )


def _node_output_schema_shape(node: Any) -> tuple[str, tuple[tuple[object, ...], ...]]:
    schema = node.output_schema_config
    assert schema is not None
    return (
        schema.mode,
        tuple((field.name, field.field_type, field.required, field.nullable) for field in (schema.fields or ())),
    )


def _output_schema_shape(graph: Any, plugin_name: str) -> tuple[str, tuple[tuple[object, ...], ...]]:
    return _node_output_schema_shape(next(node for node in graph.get_nodes() if node.plugin_name == plugin_name))


def _run_repeat_identity_pair(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reverse_second: bool = True,
) -> tuple[ScenarioRunEvidence, ScenarioRunEvidence, list[dict[str, Any]], list[dict[str, Any]]]:
    install_corpus_plugin_manager(monkeypatch)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    _install_repeat_run_identity_order(monkeypatch, reverse=False)
    first = run_scenario_case(scenario, case, runtime_root)
    first_db = _archive_repeat_run_files(case, runtime_root)

    _install_repeat_run_identity_order(monkeypatch, reverse=reverse_second)
    second = run_scenario_case(scenario, case, runtime_root)
    first_run_id = first.runtime.run_id
    second_run_id = second.runtime.run_id
    assert first_run_id is not None and second_run_id is not None
    return (
        first,
        second,
        _portable_records(first_db, first_run_id),
        _portable_records(runtime_root / "audit.db", second_run_id),
    )


def _assert_repeat_run_boundary(
    case: HarnessCaseSpec,
    first: ScenarioRunEvidence,
    second: ScenarioRunEvidence,
    first_records: list[dict[str, Any]],
    second_records: list[dict[str, Any]],
) -> bool:
    assert isinstance(case.expected, SemanticRunExpectation)
    first_projection = first.runtime.durable_projection
    second_projection = second.runtime.durable_projection
    assert first_projection is not None and second_projection is not None

    assert first.graph == second.graph
    assert first.config == second.config
    expected_runtime = (
        case.expected.status,
        case.expected.rows_processed,
        case.expected.rows_succeeded,
        case.expected.rows_failed,
        case.expected.sink_outputs,
    )
    assert (
        first.runtime.status,
        first.runtime.rows_processed,
        first.runtime.rows_succeeded,
        first.runtime.rows_failed,
        first.runtime.sink_outputs,
    ) == expected_runtime
    assert (
        second.runtime.status,
        second.runtime.rows_processed,
        second.runtime.rows_succeeded,
        second.runtime.rows_failed,
        second.runtime.sink_outputs,
    ) == expected_runtime
    assert (
        first.runtime.status,
        first.runtime.rows_processed,
        first.runtime.rows_succeeded,
        first.runtime.rows_failed,
        first.runtime.sink_outputs,
    ) == (
        second.runtime.status,
        second.runtime.rows_processed,
        second.runtime.rows_succeeded,
        second.runtime.rows_failed,
        second.runtime.sink_outputs,
    )
    assert first.audit.record_counts == second.audit.record_counts == case.expected.audit_record_counts
    assert first.audit.source_operation_count == second.audit.source_operation_count == 1
    assert first.audit.portable_projection == first_projection
    assert second.audit.portable_projection == second_projection

    first_semantic = semantic_runtime_projection(first_projection)
    second_semantic = semantic_runtime_projection(second_projection)
    assert first_semantic == second_semantic
    assert semantic_runtime_projection_sha256(first_semantic) == case.expected.projection_sha256
    assert semantic_runtime_projection_counts(first_semantic) == case.expected.projection_counts
    assert _audit_without_lineage_hash(first_projection) == _audit_without_lineage_hash(second_projection)
    assert _sink_member_material(first_projection, "payload_hash") == _sink_member_material(second_projection, "payload_hash")
    assert _artifact_content_facts(first_records) == _artifact_content_facts(second_records)
    assert _initial_raw_identities(first_records) == _initial_raw_identities(second_records)

    first_parents = _ordered_parent_sequences(first_projection)
    second_parents = _ordered_parent_sequences(second_projection)
    first_lineage = _sink_member_material(first_projection, "lineage_hash")
    second_lineage = _sink_member_material(second_projection, "lineage_hash")
    first_effects = tuple(sorted(str(record["effect_id"]) for record in first_records if record.get("record_type") == "sink_effect"))
    second_effects = tuple(sorted(str(record["effect_id"]) for record in second_records if record.get("record_type") == "sink_effect"))
    first_artifacts = tuple(sorted(str(record["artifact_id"]) for record in first_records if record.get("record_type") == "artifact"))
    second_artifacts = tuple(sorted(str(record["artifact_id"]) for record in second_records if record.get("record_type") == "artifact"))
    if first_parents == second_parents:
        assert first_lineage == second_lineage
        assert first_effects == second_effects
        assert first_artifacts == second_artifacts
        return True

    _assert_require_all_parent_order_drift(first_parents, second_parents)
    assert first_lineage != second_lineage
    assert first_effects != second_effects
    assert first_artifacts != second_artifacts
    assert _coalesce_contexts(first_projection) != _coalesce_contexts(second_projection)
    assert first_projection != second_projection
    return False


def _declared_case(scenario_id: str, case_id: str) -> tuple[ScenarioSpec, HarnessCaseSpec]:
    return next((scenario, case) for scenario, case in iter_harness_cases(MANIFEST) if (scenario.id, case.id) == (scenario_id, case_id))


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COMPOSED_COALESCE_CASES)
def test_b2_composed_coalesces_register_semantic_runtime_oracles(
    scenario_id: str,
    case_id: str,
) -> None:
    _scenario, case = _declared_case(scenario_id, case_id)

    assert case.workflow == "run"
    assert isinstance(case.expected, SemanticRunExpectation)


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COMPOSED_COALESCE_CASES)
def test_b2_composed_coalesces_execute_exact_semantic_production_oracles(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    assert isinstance(case.expected, SemanticRunExpectation)
    install_corpus_plugin_manager(monkeypatch)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    evidence = run_scenario_case(scenario, case, runtime_root)
    _assert_declared_run_evidence(scenario, case, evidence)
    projection = evidence.runtime.durable_projection
    assert projection is not None
    fresh = build_scenario(render_settings(case, runtime_root))

    assert evidence.graph.topology_hash == fresh.graph_evidence.topology_hash
    assert (len(projection.rows), len(projection.tokens), sum(len(token.parents) for token in projection.tokens)) == (3, 21, 24)
    assert (len(projection.node_states), len(projection.routes), len(projection.terminal_dispositions)) == (24, 12, 21)
    assert len(projection.scheduler_work) == 21
    assert evidence.audit.source_operation_count == 1
    assert evidence.audit.portable_projection == projection

    expected_graph = {
        "sequential-nested-fork-coalesce": (6, 9, {"coalesce": 2, "gate": 2, "sink": 1, "source": 1}),
        "parallel-coalesces": (6, 8, {"coalesce": 2, "gate": 1, "sink": 2, "source": 1}),
    }[scenario_id]
    assert (evidence.graph.node_count, evidence.graph.edge_count) == expected_graph[:2]
    assert evidence.graph.node_type_counts is not None
    assert {item.node_type: item.count for item in evidence.graph.node_type_counts} == expected_graph[2]

    if scenario_id == "sequential-nested-fork-coalesce":
        merge_a_schema = (
            "flexible",
            (
                ("branch_a1", "any", True, False),
                ("branch_a2", "any", True, False),
            ),
        )
        assert _output_schema_shape(fresh.graph, "coalesce:merge_a") == merge_a_schema
        assert _output_schema_shape(fresh.graph, "config_gate:second_fork") == merge_a_schema
        assert _output_schema_shape(fresh.graph, "coalesce:merge_b") == (
            "flexible",
            (
                ("branch_b1", "any", True, False),
                ("branch_b2", "any", True, False),
            ),
        )
        assert _output_schema_shape(fresh.graph, "json") == ("observed", ())
        assert (evidence.runtime.rows_processed, evidence.runtime.rows_succeeded, evidence.runtime.rows_failed) == (3, 3, 0)
        assert tuple(output.sink_name for output in evidence.runtime.sink_outputs) == ("output",)
        for index, row in enumerate(evidence.runtime.sink_outputs[0].rows, start=1):
            assert json.loads(row) == {
                "branch_b1": {
                    "branch_a1": {"id": index, "value": index * 10},
                    "branch_a2": {"id": index, "value": index * 10},
                },
                "branch_b2": {
                    "branch_a1": {"id": index, "value": index * 10},
                    "branch_a2": {"id": index, "value": index * 10},
                },
            }
    else:
        assert _output_schema_shape(fresh.graph, "coalesce:merge_left") == (
            "flexible",
            (
                ("left_a", "any", True, False),
                ("left_b", "any", True, False),
            ),
        )
        assert _output_schema_shape(fresh.graph, "coalesce:merge_right") == (
            "flexible",
            (
                ("right_a", "any", True, False),
                ("right_b", "any", True, False),
            ),
        )
        assert tuple(_node_output_schema_shape(node) for node in fresh.graph.get_nodes() if node.node_type.value == "sink") == (
            ("observed", ()),
            ("observed", ()),
        )
        assert (evidence.runtime.rows_processed, evidence.runtime.rows_succeeded, evidence.runtime.rows_failed) == (3, 6, 0)
        assert tuple(output.sink_name for output in evidence.runtime.sink_outputs) == ("left", "right")
        assert all(len(output.rows) == 3 for output in evidence.runtime.sink_outputs)


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COMPOSED_COALESCE_CASES)
def test_b2_composed_coalesces_repeat_run_semantic_boundary(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    pair = _run_repeat_identity_pair(scenario, case, tmp_path, monkeypatch)

    _assert_repeat_run_boundary(case, *pair)


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COMPOSED_COALESCE_CASES)
def test_b2_composed_coalesces_identical_arrival_order_has_identical_raw_identity(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    pair = _run_repeat_identity_pair(
        scenario,
        case,
        tmp_path,
        monkeypatch,
        reverse_second=False,
    )

    assert _assert_repeat_run_boundary(case, *pair)
    first, second, _first_records, _second_records = pair
    first_projection = first.runtime.durable_projection
    second_projection = second.runtime.durable_projection
    assert first_projection is not None and second_projection is not None
    assert _coalesce_contexts(first_projection) == _coalesce_contexts(second_projection)
    assert first_projection == second_projection


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COMPOSED_COALESCE_CASES)
def test_b2_composed_coalesces_raw_identity_converges_across_equivalent_runs(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    pair = _run_repeat_identity_pair(scenario, case, tmp_path, monkeypatch)
    assert _assert_repeat_run_boundary(case, *pair)
    first, second, _first_records, _second_records = pair
    first_projection = first.runtime.durable_projection
    second_projection = second.runtime.durable_projection
    assert first_projection is not None and second_projection is not None
    assert _coalesce_contexts(first_projection) != _coalesce_contexts(second_projection)
    assert first_projection != second_projection


def _copy_composed_coalesce_fixture(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HarnessCaseSpec, Path]:
    _scenario, case = _declared_case(scenario_id, case_id)
    fixture_root = tmp_path / "fixtures"
    copied_yaml = fixture_root / case.fixture
    copied_yaml.parent.mkdir(parents=True)
    copied_yaml.write_bytes(resolve_fixture_path(case.fixture).read_bytes())
    for relative_input in case.input_fixtures.values():
        copied_input = fixture_root / relative_input
        copied_input.parent.mkdir(parents=True, exist_ok=True)
        copied_input.write_bytes(resolve_fixture_path(relative_input).read_bytes())
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    return case, copied_yaml


def test_b2_sequential_nested_rejects_incompatible_second_merge_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, copied_yaml = _copy_composed_coalesce_fixture(
        "sequential-nested-fork-coalesce",
        "two-sequential-require-all",
        tmp_path,
        monkeypatch,
    )
    copied_yaml.write_text(
        copied_yaml.read_text(encoding="utf-8")
        .replace('routes: {"true": fork, "false": output}', 'routes: {"true": fork, "false": discard}')
        .replace(
            "      schema: {mode: observed}",
            '      schema: {mode: fixed, fields: ["id: int", "value: int"]}',
        ),
        encoding="utf-8",
    )
    rendered = render_settings(case, tmp_path / "runtime")
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(
        GraphValidationError,
        match=r"Sink 'json' requires fields \['id', 'value'\].*upstream 'coalesce_merge_b_.*' does not guarantee them",
    ):
        build_scenario(rendered)


def test_b2_parallel_coalesces_reject_cross_claimed_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, copied_yaml = _copy_composed_coalesce_fixture(
        "parallel-coalesces",
        "two-parallel-require-all",
        tmp_path,
        monkeypatch,
    )
    copied_yaml.write_text(
        copied_yaml.read_text(encoding="utf-8").replace(
            "branches: {right_a: right_a, right_b: right_b}",
            "branches: {left_a: right_a, right_b: right_b}",
        ),
        encoding="utf-8",
    )
    rendered = render_settings(case, tmp_path / "runtime")
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(GraphValidationError) as error:
        build_scenario(rendered)
    assert str(error.value) == (
        "Duplicate branch name 'left_a' found in coalesce settings.\n"
        "Branch 'left_a' is already mapped to coalesce 'merge_left', but coalesce 'merge_right' also declares it.\n"
        "Each fork branch can only merge at one coalesce point."
    )


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COALESCE_POSITIVE_CASES)
def test_b2_coalesce_positive_matrix_declares_exact_run_oracle(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
) -> None:
    _scenario, case = _declared_case(scenario_id, case_id)

    assert case.workflow == "run"
    assert isinstance(case.expected, SemanticRunExpectation)
    assert resolve_fixture_path(case.input_fixtures["primary"]).read_bytes() == b"id,value\n1,10\n"

    rendered = render_settings(case, tmp_path)
    policy = next(
        value
        for prefix, value in (
            ("require-all-", "require_all"),
            ("first-", "first"),
            ("quorum-", "quorum"),
            ("best-effort-", "best_effort"),
        )
        if case_id.startswith(prefix)
    )
    merge = next(value for value in ("union", "nested", "select") if f"-{value}" in case_id)
    loses_path_c = case_id.endswith("-lost-c")
    gate = rendered.settings.gates[0]
    coalesce = rendered.settings.coalesce[0]

    assert gate.fork_to == ["path_a", "path_c", "path_b"]
    assert tuple(coalesce.branches) == ("path_a", "path_b", "path_c")
    assert (coalesce.policy, coalesce.merge) == (policy, merge)
    assert coalesce.quorum_count == (2 if policy == "quorum" else None)
    assert coalesce.timeout_seconds == (60 if policy == "best_effort" else None)
    assert coalesce.select_branch == ("path_a" if merge == "select" else None)
    transforms_by_input = {transform.input: transform for transform in rendered.settings.transforms}
    assert tuple(transforms_by_input) == ("path_a", "path_c", "path_b")
    if loses_path_c:
        assert transforms_by_input["path_c"].plugin == "dag_corpus_branch_loss"
        assert "operations" not in transforms_by_input["path_c"].options
    else:
        assert transforms_by_input["path_c"].plugin == "value_transform"
        assert transforms_by_input["path_c"].options["operations"][0]["expression"] == "'c'"

    expected = case.expected
    expected_status = "completed" if policy == "require_all" else "completed_with_failures"
    expected_failures = 0 if policy == "require_all" else 2 if policy == "first" else 1
    assert (expected.status, expected.rows_processed, expected.rows_succeeded, expected.rows_failed) == (
        expected_status,
        1,
        1,
        expected_failures,
    )
    assert len(expected.sink_outputs) == 1
    assert len(expected.sink_outputs[0].rows) == 1
    output_row = json.loads(expected.sink_outputs[0].rows[0])
    arrived = ("path_a", "path_c", "path_b") if policy == "require_all" else ("path_a",) if policy == "first" else ("path_a", "path_b")
    if merge == "nested":
        assert tuple(output_row) == tuple(sorted(arrived))
    else:
        expected_marker = "c" if policy == "require_all" and merge == "union" else "b" if loses_path_c and merge == "union" else "a"
        assert output_row["branch_marker"] == expected_marker

    expected_parent_links = 6 if policy == "require_all" else 4 if policy == "first" else 5
    assert expected.projection_counts.transform_errors == (1 if loses_path_c else 0)
    assert expected.projection_counts.model_dump() == {
        "rows": 1,
        "tokens": 5,
        "parent_links": expected_parent_links,
        "node_states": 8 if loses_path_c else 9,
        "routes": 3,
        "terminal_dispositions": 5,
        "scheduler_work": 5,
        **({"transform_errors": 1} if loses_path_c else {}),
    }


@pytest.mark.parametrize(("scenario_id", "case_id"), B2_COALESCE_FAILURE_AND_COLLISION_CASES)
def test_b2_coalesce_failure_and_collision_cases_declare_exact_run_oracles(
    scenario_id: str,
    case_id: str,
) -> None:
    _scenario, case = _declared_case(scenario_id, case_id)

    assert case.workflow == "run"
    if case_id == "union-collision-fail":
        assert isinstance(case.expected, RunExpectation)
        assert case.expected.status == "failed"
        assert case.expected.expected_error is not None
        assert case.expected.expected_error.exception_type == "CoalesceCollisionError"
    else:
        assert isinstance(case.expected, SemanticRunExpectation)


def test_b2_coalesce_full_matrix_declares_exact_contracts(tmp_path: Path) -> None:
    declared = tuple((scenario.id, case.id) for scenario, case in iter_harness_cases(MANIFEST) if scenario.id == "fork-coalesce-policies")
    assert declared == B2_COALESCE_ALL_CASES

    for scenario_id, case_id in declared:
        _scenario, case = _declared_case(scenario_id, case_id)
        assert isinstance(case.expected, (RunExpectation, SemanticRunExpectation))
        assert resolve_fixture_path(case.input_fixtures["primary"]).read_bytes() == b"id,value\n1,10\n"
        case_root = tmp_path / case_id
        case_root.mkdir()
        rendered = render_settings(case, case_root)
        gate = rendered.settings.gates[0]
        coalesce = rendered.settings.coalesce[0]

        assert gate.fork_to == ["path_a", "path_c", "path_b"]
        assert tuple(coalesce.branches) == ("path_a", "path_b", "path_c")

        if case_id.endswith("-lost-c"):
            transforms_by_input = {transform.input: transform for transform in rendered.settings.transforms}
            assert transforms_by_input["path_c"].plugin == "dag_corpus_branch_loss"
            assert "operations" not in transforms_by_input["path_c"].options

        if (scenario_id, case_id) in B2_COALESCE_POSITIVE_CASES:
            assert isinstance(case.expected, SemanticRunExpectation)
            assert case.expected.rows_processed == 1
            assert case.expected.rows_succeeded == 1
        elif case_id in {
            "require-all-lost-c",
            "quorum-impossible-lost-c",
            "best-effort-all-lost",
            "first-all-lost",
        }:
            assert (case.expected.status, case.expected.rows_processed, case.expected.rows_succeeded, case.expected.rows_failed) == (
                "completed_with_failures",
                1,
                0,
                3,
            )
            assert case.expected.sink_outputs == ()
            assert case.output_artifacts["output"].presence == "absent"
            assert isinstance(case.expected, SemanticRunExpectation)
            if case_id.endswith("all-lost"):
                assert {transform.plugin for transform in rendered.settings.transforms} == {"dag_corpus_always_error"}
            else:
                transforms_by_input = {transform.input: transform for transform in rendered.settings.transforms}
                assert transforms_by_input["path_c"].plugin == "dag_corpus_branch_loss"
        else:
            collision_policy = {
                "union-collision-last-wins": "last_wins",
                "union-collision-first-wins": "first_wins",
                "union-collision-fail": "fail",
            }[case_id]
            assert (coalesce.policy, coalesce.merge, coalesce.union_collision_policy) == (
                "require_all",
                "union",
                collision_policy,
            )
            if collision_policy == "fail":
                assert isinstance(case.expected, RunExpectation)
                projection = case.expected.projection
                assert case.expected.status == "failed"
                assert case.expected.expected_error is not None
                assert case.expected.expected_error.exception_type == "CoalesceCollisionError"
                assert case.expected.sink_outputs == ()
                assert all(work.final_status == "blocked" for work in projection.scheduler_work[1:])
            else:
                assert isinstance(case.expected, SemanticRunExpectation)
                assert (case.expected.status, case.expected.rows_succeeded, case.expected.rows_failed) == ("completed", 1, 0)
                output = json.loads(case.expected.sink_outputs[0].rows[0])
                expected_branch = "c" if collision_policy == "last_wins" else "a"
                assert output["branch_marker"] == expected_branch


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
    assert [tuple((parent.ordinal, parent.parent_key) for parent in token.parents) for token in projection.tokens] == [
        (),
        ((0, "primary:0#0"),),
        ((1, "primary:0#0"),),
        (),
        ((0, "primary:1#0"),),
        ((1, "primary:1#0"),),
        (),
        ((0, "primary:2#0"),),
        ((1, "primary:2#0"),),
    ]
    assert tuple(work.token_key for work in projection.scheduler_work) == tuple(token.key for token in projection.tokens)
    assert all(work.final_status == "terminal" for work in projection.scheduler_work)
    assert not (tmp_path / "failing.jsonl").exists()
    assert evidence.audit.portable_projection == projection


def test_b2_terminal_leaf_resume_reconciles_finalized_pending_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public resume must drain sink debt even when every leaf is terminal."""
    _scenario, case = _declared_case(*B2_PARTIAL_TERMINAL_FAILURE_CASE)
    install_corpus_plugin_manager(monkeypatch)
    db_url = f"sqlite:///{tmp_path / 'audit.db'}"
    payload_root = tmp_path / "payloads"

    initial_rendered = render_settings(case, tmp_path)
    initial_built = build_scenario(initial_rendered)
    survivor_sink_id = str(initial_built.graph.get_sink_id_map()["survivor"])
    checkpoint_config = corpus_harness.RuntimeCheckpointConfig.from_settings(initial_rendered.settings.checkpoint)
    initial_store = FilesystemPayloadStore(payload_root)
    initial_db = LandscapeDB(db_url)
    initial_checkpoints = corpus_harness.CheckpointManager(initial_db)
    injected: list[corpus_harness.SinkEffectExecutionSeam] = []

    def fail_after_survivor_finalize(
        coordinator: corpus_harness.SinkEffectCoordinator,
        seam: corpus_harness.SinkEffectExecutionSeam,
    ) -> None:
        if seam is not corpus_harness.SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE or injected:
            return
        active_runs = coordinator._factory.run_lifecycle.list_runs()
        survivor_finalized = any(
            effect.sink_node_id == survivor_sink_id
            for run in active_runs
            for effect in coordinator._factory.execution.sink_effects.get_effects_for_run(run.run_id)
        )
        if survivor_finalized:
            injected.append(seam)
            raise corpus_harness.SinkEffectInjectedFault(seam)

    try:
        catalog_sha256, catalog_source = corpus_harness.read_openrouter_catalog_snapshot_id()
        with monkeypatch.context() as fault_patch:
            fault_patch.setattr(corpus_harness.SinkEffectCoordinator, "_fault", fail_after_survivor_finalize)
            with pytest.raises(
                corpus_harness.SinkEffectInjectedFault,
                match=corpus_harness.SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
            ):
                Orchestrator(
                    initial_db,
                    checkpoint_manager=initial_checkpoints,
                    checkpoint_config=checkpoint_config,
                ).run(
                    initial_built.config,
                    graph=initial_built.graph,
                    settings=initial_rendered.settings,
                    payload_store=initial_store,
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                )

        assert injected == [corpus_harness.SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE]
        repositories = corpus_harness.RecorderFactory(initial_db, payload_store=initial_store)
        runs = repositories.run_lifecycle.list_runs()
        assert len(runs) == 1
        run_id = runs[0].run_id
        checkpoint = initial_checkpoints.get_latest_checkpoint(run_id)
        assert checkpoint is not None

        with initial_db.connection() as conn:
            outcomes = tuple(
                conn.execute(
                    select(corpus_harness.token_outcomes_table).where(
                        corpus_harness.token_outcomes_table.c.run_id == run_id,
                    )
                ).mappings()
            )
            work_rows = tuple(
                conn.execute(
                    select(corpus_harness.token_work_items_table).where(
                        corpus_harness.token_work_items_table.c.run_id == run_id,
                    )
                ).mappings()
            )
        assert outcomes
        assert all(outcome["completed"] for outcome in outcomes)
        assert sum(work["status"] == "pending_sink" for work in work_rows) == 3

        effects_before = tuple(
            sorted(
                effect.effect_id
                for effect in repositories.execution.sink_effects.get_effects_for_run(run_id)
                if effect.sink_node_id == survivor_sink_id
            )
        )
        artifacts_before = tuple(
            sorted(
                artifact.artifact_id
                for artifact in repositories.execution.get_artifacts(run_id)
                if artifact.sink_node_id == survivor_sink_id
            )
        )
        survivor_output_before = initial_rendered.output_paths["survivor"].read_bytes()
        assert len(effects_before) == len(artifacts_before) == 1
    finally:
        initial_db.close()

    reopened_store = FilesystemPayloadStore(payload_root)
    reopened_db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        reopened_rendered = render_settings(case, tmp_path)
        reopened_built = build_scenario(
            reopened_rendered,
            purpose=corpus_harness.SinkEffectExecutionPurpose.RESUME,
        )
        reopened_checkpoints = corpus_harness.CheckpointManager(reopened_db)
        recovery = corpus_harness.RecoveryManager(reopened_db, reopened_checkpoints)
        resume_point = recovery.get_resume_point(run_id, reopened_built.graph)
        assert resume_point is not None

        result = Orchestrator(
            reopened_db,
            checkpoint_manager=reopened_checkpoints,
            checkpoint_config=checkpoint_config,
        ).resume(
            resume_point,
            reopened_built.config,
            reopened_built.graph,
            payload_store=reopened_store,
            settings=reopened_rendered.settings,
        )

        assert result.to_dict()["status"] == "completed_with_failures"
        final_repositories = corpus_harness.RecorderFactory(reopened_db, payload_store=reopened_store)
        effects_after = tuple(
            sorted(
                effect.effect_id
                for effect in final_repositories.execution.sink_effects.get_effects_for_run(run_id)
                if effect.sink_node_id == survivor_sink_id
            )
        )
        artifacts_after = tuple(
            sorted(
                artifact.artifact_id
                for artifact in final_repositories.execution.get_artifacts(run_id)
                if artifact.sink_node_id == survivor_sink_id
            )
        )
        assert effects_after == effects_before
        assert artifacts_after == artifacts_before
        survivor_effects = tuple(
            effect
            for effect in final_repositories.execution.sink_effects.get_effects_for_run(run_id)
            if effect.sink_node_id == survivor_sink_id
        )
        assert sum(effect.publication_performed is True for effect in survivor_effects) == 1
        assert reopened_rendered.output_paths["survivor"].read_bytes() == survivor_output_before
        with reopened_db.connection() as conn:
            final_work_statuses = tuple(
                conn.execute(
                    select(corpus_harness.token_work_items_table.c.status).where(
                        corpus_harness.token_work_items_table.c.run_id == run_id,
                    )
                ).scalars()
            )
        assert set(final_work_statuses) == {"terminal"}
        assert reopened_checkpoints.get_latest_checkpoint(run_id) is None
        durable_projection, final_audit = corpus_harness._exact_recovery_views(
            reopened_db,
            run_id=run_id,
            payload_store=reopened_store,
        )
        assert final_audit.portable_projection == durable_projection
    finally:
        reopened_db.close()


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
    assert isinstance(case.expected, (RunExpectation, SemanticRunExpectation))
    expected_fixture_hash = compute_fixture_sha256(case)

    assert evidence.schema_version == 2
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
    if isinstance(case.expected, RunExpectation):
        assert evidence.runtime.durable_projection == case.expected.projection
        assert evidence.runtime.observed_error == case.expected.expected_error
    else:
        assert evidence.runtime.durable_projection is not None
        semantic_projection = semantic_runtime_projection(evidence.runtime.durable_projection)
        assert semantic_runtime_projection_sha256(semantic_projection) == case.expected.projection_sha256
        assert semantic_runtime_projection_counts(semantic_projection) == case.expected.projection_counts
        assert evidence.runtime.observed_error is None

    assert evidence.audit.attempted is True
    assert evidence.audit.total_records > 0
    assert evidence.audit.total_records == sum(record.count for record in evidence.audit.record_counts)
    record_types = tuple(record.record_type for record in evidence.audit.record_counts)
    assert record_types == tuple(sorted(record_types))
    assert evidence.audit.record_counts == case.expected.audit_record_counts
    assert evidence.audit.source_operation_count == case.expected.source_operation_count
    if isinstance(case.expected, SemanticRunExpectation):
        assert evidence.audit.kind == "exact"
        assert evidence.audit.portable_projection == evidence.runtime.durable_projection
        assert evidence.audit.portable_export_unavailable is None
    elif case.expected.expected_error is None:
        assert evidence.audit.kind == "exact"
        assert evidence.audit.portable_projection == case.expected.projection
        assert evidence.audit.portable_export_unavailable is None
    else:
        assert evidence.audit.kind == "unavailable_by_policy"
        assert evidence.audit.portable_projection is None
        assert evidence.audit.portable_export_unavailable is not None
        assert evidence.audit.portable_export_unavailable.model_dump() == {
            "run_status": "failed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
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


def _assert_declared_recovery_evidence(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    evidence: ScenarioRunEvidence,
) -> None:
    assert isinstance(case.expected, SummaryRunExpectation)
    expected_fixture_hash = compute_fixture_sha256(case)

    assert evidence.schema_version == 2
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

    assert evidence.schema_version == 2
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


def test_b3_stateful_runtime_cases_pin_exact_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        tuple((scenario.id, case.id) for scenario, case in iter_harness_cases(MANIFEST) if (scenario.id, case.id) in B3_RUNTIME_CASES)
        == B3_RUNTIME_CASES
    )
    install_corpus_plugin_manager(monkeypatch)
    observed: dict[tuple[str, str], ScenarioRunEvidence] = {}
    for scenario_id, case_id in B3_RUNTIME_CASES:
        scenario, case = _declared_case(scenario_id, case_id)
        assert isinstance(case.expected, RunExpectation)
        case_root = tmp_path / case_id
        case_root.mkdir()
        evidence = run_scenario_case(scenario, case, case_root)
        _assert_declared_run_evidence(scenario, case, evidence)
        assert evidence.audit.portable_projection == evidence.runtime.durable_projection
        observed[(scenario_id, case_id)] = evidence

    aggregation = observed[("aggregation-immutable-batch", "eof-immutable-membership")]
    aggregation_projection = aggregation.runtime.durable_projection
    assert aggregation_projection is not None
    assert aggregation.runtime.sink_outputs[0].rows == ('{"count":3,"value":60}',)
    assert [
        (batch.status, batch.trigger_type, batch.trigger_reason, tuple((member.ordinal, member.token_key) for member in batch.members))
        for batch in aggregation_projection.batches
    ] == [
        (
            "completed",
            "end_of_source",
            None,
            ((0, "primary:0#0"), (1, "primary:1#0"), (2, "primary:2#0")),
        )
    ]
    assert [(outcome.token_key, outcome.path, outcome.ordinal) for outcome in aggregation_projection.intermediate_outcomes] == [
        ("primary:0#0", "buffered", 0),
        ("primary:1#0", "buffered", 0),
        ("primary:2#0", "buffered", 0),
    ]

    expansion = observed[("row-expansion-parent-child-recovery", "json-explode-parent-child")]
    expansion_projection = expansion.runtime.durable_projection
    assert expansion_projection is not None
    assert (len(expansion_projection.rows), len(expansion_projection.tokens), expansion.runtime.rows_succeeded) == (3, 9, 6)
    assert [
        (item.parent_token_key, item.expected_child_count, tuple((child.ordinal, child.token_key) for child in item.children))
        for item in expansion_projection.expansions
    ] == [
        ("primary:0#0", 2, ((0, "primary:0#1"), (1, "primary:0#2"))),
        ("primary:1#0", 1, ((0, "primary:1#1"),)),
        ("primary:2#0", 3, ((0, "primary:2#1"), (1, "primary:2#2"), (2, "primary:2#3"))),
    ]

    retry = observed[("retry-quarantine-discard-routed-errors", "retry-then-success")]
    retry_projection = retry.runtime.durable_projection
    assert retry_projection is not None
    retry_states = [state for state in retry_projection.node_states if state.node_key.startswith("transform:retry_once@")]
    assert [(state.attempt, state.status, state.error) for state in retry_states] == [
        (0, "failed", '{"exception":"injected DAG corpus retryable failure","type":"ConnectionError"}'),
        (1, "completed", None),
    ]

    quarantine = observed[("retry-quarantine-discard-routed-errors", "source-quarantine-routed")]
    quarantine_projection = quarantine.runtime.durable_projection
    assert quarantine_projection is not None
    assert [(route.label, route.mode) for route in quarantine_projection.routes] == [("__quarantine__", "divert")]
    assert [
        (error.row_hash, error.row_data, error.schema_mode, error.destination) for error in quarantine_projection.validation_errors
    ] == [("5a3fdc1573df66d8628620d1457e81eedea5b6fb5ad7aeabda743bc219ba1cc0", None, "fixed", "quarantine")]

    discard = observed[("retry-quarantine-discard-routed-errors", "transform-discard")]
    discard_projection = discard.runtime.durable_projection
    assert discard_projection is not None
    assert discard_projection.routes == ()
    assert [(error.destination, error.error_details) for error in discard_projection.transform_errors] == [
        ("discard", '{"error":"injected DAG corpus routed error","reason":"invalid_input"}')
    ]
    assert [(item.outcome, item.path, item.sink_name) for item in discard_projection.terminal_dispositions] == [
        ("failure", "quarantined_at_source", None)
    ]

    error_route = observed[("retry-quarantine-discard-routed-errors", "transform-error-route")]
    error_route_projection = error_route.runtime.durable_projection
    assert error_route_projection is not None
    assert [(route.label, route.mode) for route in error_route_projection.routes] == [
        ("true", "move"),
        ("__error_routed_error__", "divert"),
    ]
    assert [(item.outcome, item.path, item.sink_name) for item in error_route_projection.terminal_dispositions] == [
        ("failure", "on_error_routed", "error_output"),
        ("success", "gate_discarded", None),
    ]

    write_once = observed[("sink-write-pending-redrive", "write-once")]
    write_once_projection = write_once.runtime.durable_projection
    assert write_once_projection is not None
    effect_records = [record for record in write_once_projection.audit_records if record.record_type == "sink_effect"]
    attempt_records = [record for record in write_once_projection.audit_records if record.record_type == "sink_effect_attempt"]
    assert len(effect_records) == 1
    assert json.loads(effect_records[0].material)["publication_performed"] is True
    assert [json.loads(record.material)["action"] for record in attempt_records] == ["inspect", "reconcile", "commit"]


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


def test_linear_sink_boundary_recovery_reopens_and_resumes_without_reminting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("linear", "reopen-after-source")
    production_run = inspect.unwrap(Orchestrator.run)
    production_resume = inspect.unwrap(Orchestrator.resume)
    monkeypatch.setattr(Orchestrator, "run", production_run)
    monkeypatch.setattr(Orchestrator, "resume", production_resume)
    install_corpus_plugin_manager(monkeypatch)
    coordinator_fault_hooks: list[object | None] = []
    production_coordinator_init = corpus_harness.SinkEffectCoordinator.__init__

    def record_coordinator_fault_hook(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        coordinator_fault_hooks.append(kwargs.get("fault_hook"))
        production_coordinator_init(self, *args, **kwargs)

    monkeypatch.setattr(
        corpus_harness.SinkEffectCoordinator,
        "__init__",
        record_coordinator_fault_hook,
    )
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
    observed_terminal_statuses: list[RunStatus] = []
    assert_all_tokens_and_work_terminal = corpus_harness._assert_all_tokens_and_work_terminal

    def assert_declared_terminal_status(
        *args: Any,
        expected_run_status: RunStatus,
        **kwargs: Any,
    ) -> None:
        observed_terminal_statuses.append(expected_run_status)
        assert_all_tokens_and_work_terminal(
            *args,
            expected_run_status=expected_run_status,
            **kwargs,
        )

    monkeypatch.setattr(
        corpus_harness,
        "_assert_all_tokens_and_work_terminal",
        assert_declared_terminal_status,
    )

    interrupted_facts: list[dict[str, object]] = []

    def verify_interrupted_linear_state(context: corpus_harness.SinkBoundaryInterruptedContext) -> None:
        assert context.scenario is scenario
        assert context.case is case
        assert context.rendered is context.built.rendered
        assert (
            id(context.built),
            id(context.rendered),
            id(context.rendered.settings),
            id(context.built.bundle),
            id(context.built.graph),
            id(context.built.config),
        ) == built_identity_tuples[0]
        assert context.source_names_exhausted == ("primary",)
        assert context.token_ids == tuple(sorted(context.interrupted_effect.member_token_ids))
        with context.database.connection() as conn:
            row_ids = tuple(
                str(row_id)
                for row_id in conn.execute(
                    select(rows_table.c.row_id).where(rows_table.c.run_id == context.run_id).order_by(rows_table.c.row_id)
                ).scalars()
            )
            token_lineage = tuple(
                conn.execute(
                    select(
                        tokens_table.c.token_id,
                        tokens_table.c.row_id,
                        tokens_table.c.fork_group_id,
                        tokens_table.c.join_group_id,
                        tokens_table.c.expand_group_id,
                        tokens_table.c.branch_name,
                    )
                    .where(tokens_table.c.run_id == context.run_id)
                    .order_by(tokens_table.c.token_id)
                ).mappings()
            )
            parent_links = tuple(
                conn.execute(
                    select(token_parents_table.c.token_id, token_parents_table.c.parent_token_id)
                    .where(token_parents_table.c.run_id == context.run_id)
                    .order_by(token_parents_table.c.token_id, token_parents_table.c.ordinal)
                ).mappings()
            )
            routes = tuple(
                conn.execute(
                    select(routing_events_table.c.event_id)
                    .where(routing_events_table.c.run_id == context.run_id)
                    .order_by(routing_events_table.c.event_id)
                ).scalars()
            )
            outcomes = tuple(
                conn.execute(
                    select(token_outcomes_table.c.outcome_id)
                    .where(token_outcomes_table.c.run_id == context.run_id)
                    .order_by(token_outcomes_table.c.outcome_id)
                ).scalars()
            )
            states = tuple(
                conn.execute(
                    select(
                        node_states_table.c.token_id,
                        node_states_table.c.node_id,
                        node_states_table.c.status,
                        node_states_table.c.attempt,
                        node_states_table.c.resume_checkpoint_id,
                    )
                    .where(node_states_table.c.run_id == context.run_id)
                    .order_by(node_states_table.c.node_id, node_states_table.c.token_id)
                ).mappings()
            )
        assert len(row_ids) == 3
        assert tuple(str(token["token_id"]) for token in token_lineage) == context.token_ids
        assert {str(token["row_id"]) for token in token_lineage} == set(row_ids)
        assert all(
            token[key] is None for token in token_lineage for key in ("fork_group_id", "join_group_id", "expand_group_id", "branch_name")
        )
        assert parent_links == ()
        assert routes == ()
        assert outcomes == ()
        assert len(states) == 9
        assert sum(state["status"] == "completed" and state["attempt"] == 0 for state in states) == 6
        open_states = tuple(state for state in states if state["status"] == "open")
        assert len(open_states) == 3
        assert {str(state["token_id"]) for state in open_states} == set(context.token_ids)
        assert {str(state["node_id"]) for state in open_states} == {context.interrupted_effect.sink_node_id}
        assert all(state["resume_checkpoint_id"] is None for state in states)
        interrupted_facts.append(
            {
                "row_ids": row_ids,
                "token_ids": context.token_ids,
                "parent_link_count": len(parent_links),
                "route_count": len(routes),
                "outcome_count": len(outcomes),
                "completed_node_state_count": sum(state["status"] == "completed" for state in states),
            }
        )

    evidence = corpus_harness.run_sink_boundary_recovery_case(
        scenario,
        case,
        tmp_path,
        before_reopen_verifier=verify_interrupted_linear_state,
    )

    _assert_declared_recovery_evidence(scenario, case, evidence)
    assert len(interrupted_facts) == 1
    assert len(coordinator_fault_hooks) == 2
    assert callable(coordinator_fault_hooks[0])
    assert coordinator_fault_hooks[1] is None
    assert observed_terminal_statuses == [RunStatus.COMPLETED]
    assert interrupted_facts[0]["completed_node_state_count"] == 6
    assert len(built_identity_tuples) == 2
    assert len(set(built_identity_tuples)) == 2
    assert evidence.runtime.sink_outputs == (
        SinkOutputProjection(
            sink_name="output",
            rows=('{"id":1,"value":10}', '{"id":2,"value":20}', '{"id":3,"value":30}'),
        ),
    )
    assert evidence.audit.source_operation_count == 1
    proof = evidence.recovery.sink_boundary
    assert proof is not None
    assert proof.fault.model_dump(mode="json") == {
        "kind": "sink_effect",
        "seam": "before_effect",
        "sink_name": "output",
        "occurrence": 1,
    }
    assert proof.fault_count == 1
    assert proof.initial_run_status == "failed"
    assert proof.source_names_exhausted_before == ("primary",)
    assert proof.checkpoint_topology_hash == proof.fresh_topology_hash
    assert len(proof.token_ids_before) == 3
    assert proof.token_ids_before == proof.token_ids_after
    assert len(proof.work_before) == 3
    assert {item.status for item in proof.work_before} == {"pending_sink"}
    assert {item.row_payload_state for item in proof.work_before} == {"live"}
    assert {item.status for item in proof.work_after} == {"terminal"}
    assert {item.row_payload_state for item in proof.work_after} == {"purged"}
    assert all(item.row_payload_anchor_sha256 == hashlib.sha256(item.token_id.encode()).hexdigest() for item in proof.work_after)
    assert proof.effect_count_before == 1
    assert proof.effect_member_count_before == 3
    assert proof.artifact_count_before == proof.publication_count_before == 0
    assert proof.effect_count_after == proof.artifact_count_after == proof.publication_count_after == 1
    assert proof.resume_marker_count == 1
    assert proof.resume_marker_event_type == "leader_acquire"
    assert proof.resume_marker_entry_point == "resume"
    assert proof.resume_marker_worker_id
    assert proof.resume_marker_leader_epoch >= 1
    assert proof.durable_identity_reused is True
    assert proof.durable_export_parity is True
    assert proof.provisional_until_deferred_platform_rebase is True
    assert [json.loads(line) for line in (tmp_path / "output.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"id": 1, "value": 10},
        {"id": 2, "value": 20},
        {"id": 3, "value": 30},
    ]


def test_recovery_durable_oracle_rejects_shared_serializer_record_family_omission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("retry-quarantine-discard-routed-errors", "source-quarantine-routed")
    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)
    run_id = evidence.runtime.run_id
    assert run_id is not None

    original_iter_records = LandscapeExporter._iter_records

    def omit_validation_errors(self: LandscapeExporter, target_run_id: str) -> Any:
        yield from (record for record in original_iter_records(self, target_run_id) if record["record_type"] != "validation_error")

    monkeypatch.setattr(LandscapeExporter, "_iter_records", omit_validation_errors)
    reopened_store = FilesystemPayloadStore(tmp_path / "payloads")
    reopened_db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}", create_tables=False)
    try:
        with pytest.raises(
            AssertionError,
            match=r"portable validation_error integrity: record identities differ from durable data",
        ):
            corpus_harness._exact_recovery_views(
                reopened_db,
                run_id=run_id,
                payload_store=reopened_store,
            )
    finally:
        reopened_db.close()


def test_b3_recovery_cases_are_registered_as_closed_recovery_workflows() -> None:
    observed = tuple(
        (scenario.id, case.id, case.recovery_kind)
        for scenario, case in iter_harness_cases(MANIFEST)
        if (scenario.id, case.id) in B3_RECOVERY_CASES
    )

    assert observed == (
        ("aggregation-immutable-batch", "resume-after-eof-flush-fault", "eof_aggregation"),
        (
            "row-expansion-parent-child-recovery",
            "resume-after-child-enqueue",
            "expansion_child_enqueue",
        ),
        ("sink-write-pending-redrive", "pending-redrive-reopen", "pending_sink_redrive"),
    )
    expansion_reference = next(
        reference
        for reference in MANIFEST.evidence
        if reference.id == "harness-row-expansion-parent-child-recovery-resume-after-child-enqueue"
    )
    assert "observed-schema" in expansion_reference.claim
    assert "P1 elspeth-0b0eaa63df" in expansion_reference.claim


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


@pytest.mark.parametrize(("scenario_id", "case_id"), B3_RECOVERY_CASES)
def test_b3_recovery_rebuilds_fresh_settings_plugins_graph_and_config(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case(scenario_id, case_id)
    monkeypatch.setattr(Orchestrator, "run", inspect.unwrap(Orchestrator.run))
    monkeypatch.setattr(Orchestrator, "resume", inspect.unwrap(Orchestrator.resume))
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
    expected_build_count = 3 if case.recovery_kind == "pending_sink_redrive" else 2
    assert len(built_objects) == expected_build_count
    assert len({id(built) for built in built_objects}) == expected_build_count
    assert len({id(built.rendered) for built in built_objects}) == expected_build_count
    assert len({id(built.rendered.settings) for built in built_objects}) == expected_build_count
    assert len({id(built.bundle) for built in built_objects}) == expected_build_count
    assert len({id(built.graph) for built in built_objects}) == expected_build_count
    assert len({id(built.config) for built in built_objects}) == expected_build_count


def test_checkpoint_reopen_resume_has_exact_restart_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    assert tuple(
        (declared_scenario.id, declared_case.id)
        for declared_scenario, declared_case in iter_harness_cases(MANIFEST)
        if declared_case.workflow == "recovery"
    ) == (
        ("linear", "reopen-after-source"),
        ("parallel-coalesces", "resume-after-left-finalize"),
        ("aggregation-immutable-batch", "resume-after-eof-flush-fault"),
        ("row-expansion-parent-child-recovery", "resume-after-child-enqueue"),
        ("sink-write-pending-redrive", "pending-redrive-reopen"),
        ("checkpoint-deterministic-resume", "reopen-resume"),
    )

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

    assert len(built_objects) == 4
    assert len({id(built) for built in built_objects}) == 4
    assert len({id(built.rendered) for built in built_objects}) == 4
    assert len({id(built.rendered.settings) for built in built_objects}) == 4
    assert len({id(built.bundle) for built in built_objects}) == 4
    assert len({id(built.graph) for built in built_objects}) == 4
    assert len({id(built.config) for built in built_objects}) == 4
    assert evidence.runtime.rows_processed == 3
    # Three source rows are consumed into one terminal aggregation output.
    assert evidence.runtime.rows_succeeded == 1
    assert evidence.runtime.rows_failed == 0
    assert evidence.audit.source_operation_count == 1
    assert [json.loads(line) for line in (tmp_path / "artifacts/output.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"value": 60, "count": 3}
    ]
    assert (tmp_path / "fault-triggered.marker").is_file()
    proof = evidence.recovery.terminal_resume_idempotence
    assert proof is not None
    assert proof.control_terminal_projection == proof.resumed_terminal_projection
    assert proof.fresh_object_lifetimes == 4
    assert proof.second_resume_error_type == "NonResumableRunError"
    assert proof.second_resume_error_run_id == evidence.runtime.run_id
    assert proof.second_resume_error_reason == ("Run is terminal (status 'completed'); successful terminal runs are immutable")
    assert proof.database_sha256_before == proof.database_sha256_after
    assert proof.durable_records_sha256_before == proof.durable_records_sha256_after
    assert proof.portable_export_sha256_before == proof.portable_export_sha256_after
    assert proof.output_tree_sha256_before == proof.output_tree_sha256_after
    assert proof.artifact_digests_before == proof.artifact_digests_after
    assert evidence.runtime.durable_projection is not None
    rendered = corpus_harness.render_settings(case, tmp_path)
    assert proof.resumed_full_projection_sha256 == corpus_harness.stable_run_projection_sha256(
        evidence.runtime.durable_projection,
        runtime_root=tmp_path,
        settings=rendered.settings,
    )


def test_checkpoint_terminal_refusal_rejects_non_exact_error_subclass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DerivedNonResumableRunError(NonResumableRunError):
        pass

    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    production_resume = inspect.unwrap(Orchestrator.resume)
    resume_calls = 0

    def subclass_on_second_resume(self: Orchestrator, resume_point: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal resume_calls
        resume_calls += 1
        if resume_calls == 1:
            return production_resume(self, resume_point, *args, **kwargs)
        raise DerivedNonResumableRunError(
            resume_point.checkpoint.run_id,
            "Run is terminal (status 'completed'); successful terminal runs are immutable",
        )

    monkeypatch.setattr(Orchestrator, "run", inspect.unwrap(Orchestrator.run))
    monkeypatch.setattr(Orchestrator, "resume", subclass_on_second_resume)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(AssertionError, match="exact NonResumableRunError"):
        corpus_harness.run_scenario_case(scenario, case, tmp_path)


def test_checkpoint_terminal_refusal_detects_undeclared_output_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    production_resume = inspect.unwrap(Orchestrator.resume)
    resume_calls = 0

    def add_sidecar_on_second_resume(self: Orchestrator, resume_point: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal resume_calls
        resume_calls += 1
        if resume_calls == 2:
            output_root = tmp_path / "artifacts"
            (output_root / "unexpected-sidecar.tmp").write_bytes(b"unexpected")
        return production_resume(self, resume_point, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "run", inspect.unwrap(Orchestrator.run))
    monkeypatch.setattr(Orchestrator, "resume", add_sidecar_on_second_resume)
    install_corpus_plugin_manager(monkeypatch)

    with pytest.raises(ValidationError, match="output tree"):
        corpus_harness.run_scenario_case(scenario, case, tmp_path)


def test_checkpoint_full_history_pin_rejects_observed_semantic_settings_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    install_corpus_plugin_manager(monkeypatch)
    evidence = corpus_harness.run_scenario_case(scenario, case, tmp_path)
    projection = evidence.runtime.durable_projection
    assert projection is not None
    mutated_records = []
    for record in projection.audit_records:
        if record.record_type != "run":
            mutated_records.append(record)
            continue
        material = json.loads(record.material)
        material["semantic_settings_sha256"] = "f" * 64
        mutated_records.append(record.model_copy(update={"material": json.dumps(material, sort_keys=True, separators=(",", ":"))}))
    mutated_projection = projection.model_copy(update={"audit_records": tuple(mutated_records)})
    rendered = corpus_harness.render_settings(case, tmp_path)

    with pytest.raises(AssertionError, match="observed semantic settings hash"):
        corpus_harness.stable_run_projection_sha256(
            mutated_projection,
            runtime_root=tmp_path,
            settings=rendered.settings,
        )


def test_checkpoint_full_history_pin_rejects_consistent_node_identity_suffix_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("checkpoint-deterministic-resume", "reopen-resume")
    install_corpus_plugin_manager(monkeypatch)
    evidence = corpus_harness.run_scenario_case(scenario, case, tmp_path)
    projection = evidence.runtime.durable_projection
    assert projection is not None
    aggregation_record = next(
        record for record in projection.audit_records if record.record_type == "node" and "node|aggregation:eof_sum@" in record.key
    )
    original_node_key = aggregation_record.key.removeprefix("node|")
    mutated_node_key = f"{original_node_key.rsplit('@', maxsplit=1)[0]}@ffffffffffff"

    def replace_node_key(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_node_key(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_node_key(item) for item in value]
        if isinstance(value, str):
            return value.replace(original_node_key, mutated_node_key)
        return value

    mutated_projection = type(projection).model_validate(replace_node_key(projection.model_dump(mode="json")))
    rendered = corpus_harness.render_settings(case, tmp_path)

    with pytest.raises(AssertionError, match="observed node identity suffix"):
        corpus_harness.stable_run_projection_sha256(
            mutated_projection,
            runtime_root=tmp_path,
            settings=rendered.settings,
        )


def test_eof_aggregation_recovery_preserves_failed_batch_and_member_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("aggregation-immutable-batch", "resume-after-eof-flush-fault")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    recovery = evidence.recovery.aggregation_eof
    assert recovery is not None
    assert recovery.original_batch_id_before == recovery.original_batch_id_after
    assert recovery.recovery_batch_id_after != recovery.original_batch_id_after
    assert recovery.member_token_ids_before == recovery.member_token_ids_after
    assert tuple((batch.attempt, batch.status) for batch in recovery.final_batches) == (
        (0, "failed"),
        (1, "completed"),
    )
    assert tuple(tuple(member.token_key for member in batch.members) for batch in recovery.final_batches) == (
        ("primary:0#0", "primary:1#0", "primary:2#0"),
        ("primary:0#0", "primary:1#0", "primary:2#0"),
    )
    assert evidence.runtime.rows_processed == 3
    assert evidence.runtime.rows_succeeded == 1
    assert evidence.audit.source_operation_count == 1


def test_expansion_recovery_preserves_parent_child_group_and_scheduler_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("row-expansion-parent-child-recovery", "resume-after-child-enqueue")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    recovery = evidence.recovery.expansion_child_enqueue
    assert recovery is not None
    assert recovery.parent_token_ids_before == recovery.parent_token_ids_after
    assert recovery.child_token_ids_before == recovery.child_token_ids_after
    assert recovery.expand_group_ids_before == recovery.expand_group_ids_after
    assert recovery.scheduler_work_ids_before == recovery.scheduler_work_ids_after
    assert recovery.parent_scheduler_work_ids_before == recovery.parent_scheduler_work_ids_after
    assert recovery.child_scheduler_work_ids_before == recovery.child_scheduler_work_ids_after
    assert len(recovery.parent_scheduler_work_ids_before) == 3
    assert len(recovery.child_scheduler_work_ids_before) == 6
    assert set(recovery.parent_scheduler_work_ids_before).isdisjoint(recovery.child_scheduler_work_ids_before)
    assert tuple(len(expansion.children) for expansion in recovery.final_expansions) == (2, 1, 3)
    assert recovery.pending_children_before == 6
    assert evidence.runtime.rows_processed == 3
    assert evidence.runtime.rows_succeeded == 6
    assert evidence.runtime.output_rows == 6
    assert evidence.audit.source_operation_count == 1


def test_expansion_recovery_reconstructs_fixed_any_schema_without_reminting_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh public resume accepts the exact persisted form of fixed ``items:any``."""
    scenario, case = _declared_case("row-expansion-parent-child-recovery", "resume-after-child-enqueue")
    fixture_root = tmp_path / "fixed-any-fixtures"
    copied_yaml = fixture_root / case.fixture
    copied_yaml.parent.mkdir(parents=True)
    original_yaml = resolve_fixture_path(case.fixture).read_text(encoding="utf-8")
    observed_source_schema = "schema: {mode: observed}"
    fixed_any_source_schema = 'schema: {mode: fixed, fields: ["order_id: int", "items: any"]}'
    assert original_yaml.count(observed_source_schema) == 3
    copied_yaml.write_text(
        original_yaml.replace(observed_source_schema, fixed_any_source_schema, 1),
        encoding="utf-8",
    )
    for relative_input in case.input_fixtures.values():
        copied_input = fixture_root / relative_input
        copied_input.parent.mkdir(parents=True, exist_ok=True)
        copied_input.write_bytes(resolve_fixture_path(relative_input).read_bytes())
    monkeypatch.setattr(corpus_loader, "FIXTURE_ROOT", fixture_root)
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    recovery = evidence.recovery.expansion_child_enqueue
    assert recovery is not None
    assert recovery.parent_token_ids_before == recovery.parent_token_ids_after
    assert recovery.child_token_ids_before == recovery.child_token_ids_after
    assert recovery.expand_group_ids_before == recovery.expand_group_ids_after
    assert recovery.scheduler_work_ids_before == recovery.scheduler_work_ids_after
    assert len(recovery.parent_token_ids_after) == 3
    assert len(recovery.child_token_ids_after) == 6
    assert len(recovery.scheduler_work_ids_after) == 9
    assert recovery.pending_children_before == 6
    assert recovery.durable_export_parity is True
    assert evidence.runtime.rows_processed == 3
    assert evidence.runtime.rows_succeeded == 6
    assert evidence.runtime.output_rows == 6
    assert evidence.audit.source_operation_count == 1


def test_pending_sink_redrive_recovery_preserves_complete_bundle_and_exactly_once_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("sink-write-pending-redrive", "pending-redrive-reopen")
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    recovery = evidence.recovery.pending_sink_redrive
    assert recovery is not None
    assert recovery.work_item_id_before == recovery.work_item_id_claimed == recovery.work_item_id_after
    assert recovery.token_id_before == recovery.token_id_claimed == recovery.token_id_after
    assert recovery.row_id_before == recovery.row_id_claimed == recovery.row_id_after
    assert recovery.row_payload_hash_before == recovery.row_payload_hash_claimed == recovery.row_payload_hash_after
    assert recovery.scheduler_attempt_before == recovery.scheduler_attempt_claimed == recovery.scheduler_attempt_after == 1
    assert recovery.pending_sink_name_before == recovery.pending_sink_name_claimed == recovery.pending_sink_name_after == "output"
    assert recovery.pending_outcome_before == recovery.pending_outcome_claimed == recovery.pending_outcome_after == "success"
    assert recovery.pending_path_before == recovery.pending_path_claimed == recovery.pending_path_after == "default_flow"
    assert recovery.pending_error_hash_before is recovery.pending_error_hash_after is None
    assert recovery.pending_error_message_before is recovery.pending_error_message_after is None
    assert recovery.expired_lease_recovery_events == 1
    assert recovery.sink_effects_after == 1
    assert recovery.artifacts_after == 1
    assert recovery.publications_after == 1
    assert evidence.runtime.rows_processed == 1
    assert evidence.runtime.rows_succeeded == 1
    assert evidence.runtime.rows_failed == 0
    assert evidence.audit.source_operation_count == 1


def test_parallel_coalesces_recovery_reuses_finalized_first_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, case = _declared_case("parallel-coalesces", "resume-after-left-finalize")
    assert case.workflow == "recovery"
    assert case.recovery_kind == "parallel_sink_finalization"
    install_corpus_plugin_manager(monkeypatch)

    evidence = run_scenario_case(scenario, case, tmp_path)

    _assert_declared_recovery_evidence(scenario, case, evidence)
    assert evidence.graph.node_count == 6
    assert evidence.graph.edge_count == 8
    assert (evidence.runtime.rows_processed, evidence.runtime.rows_succeeded, evidence.runtime.rows_failed) == (3, 6, 0)
    assert tuple((output.sink_name, len(output.rows)) for output in evidence.runtime.sink_outputs) == (
        ("left", 3),
        ("right", 3),
    )
    recovery = evidence.recovery.sink_finalization
    assert recovery is not None
    assert recovery.model_dump() == {
        "fault_seam": "after_finalize_before_response",
        "fault_count": 1,
        "first_sink": "left",
        "second_sink": "right",
        "source_exhausted_before": True,
        "completed_coalesces_before": 2,
        "first_sink_rows_before": 3,
        "first_effect_id_before": recovery.first_effect_id_after,
        "first_effect_id_after": recovery.first_effect_id_after,
        "first_artifact_id_before": recovery.first_artifact_id_after,
        "first_artifact_id_after": recovery.first_artifact_id_after,
        "first_attempt_ids_before": recovery.first_attempt_ids_after,
        "first_attempt_ids_after": recovery.first_attempt_ids_after,
        "first_effect_unchanged": True,
        "first_artifact_unchanged": True,
        "first_attempts_unchanged": True,
        "first_sink_republished": False,
        "second_effect_absent_before": True,
        "second_artifact_absent_before": True,
        "second_attempt_count_before": 0,
        "second_effect_id_after": recovery.second_effect_id_after,
        "second_artifact_id_after": recovery.second_artifact_id_after,
        "second_attempt_ids_after": recovery.second_attempt_ids_after,
        "final_output_rows": 6,
        "durable_export_parity": True,
        "held_barrier_proven": False,
    }
    assert len(recovery.first_effect_id_after) == 64
    assert len(recovery.first_artifact_id_after) == 64
    assert len(recovery.second_effect_id_after) == 64
    assert len(recovery.second_artifact_id_after) == 64
    assert len(recovery.first_attempt_ids_after) == 3
    assert len(recovery.second_attempt_ids_after) == 3
    assert recovery.first_effect_id_after != recovery.second_effect_id_after
    assert recovery.first_artifact_id_after != recovery.second_artifact_id_after


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
