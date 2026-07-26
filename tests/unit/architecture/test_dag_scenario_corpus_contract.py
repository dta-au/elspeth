from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import cast, get_args
from urllib.parse import unquote, urlsplit

import pytest
import tests.fixtures.dag_scenario_corpus.loader as loader_module
import yaml
from markdown_it import MarkdownIt
from pydantic import ValidationError
from tests.fixtures.dag_scenario_corpus.harness import compute_fixture_sha256, render_settings
from tests.fixtures.dag_scenario_corpus.loader import (
    DEFAULT_MANIFEST_PATH,
    REPOSITORY_ROOT,
    iter_harness_cases,
    load_manifest,
    resolve_fixture_path,
)
from tests.fixtures.dag_scenario_corpus.plugins import (
    CorpusFailOnceEOFBatchTransform,
    CorpusInputSchema,
    CorpusOutputSchema,
    install_corpus_plugin_manager,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    EXPECTED_DIMENSIONS,
    EXPECTED_SCENARIOS,
    AuditEvidence,
    AuditRecordCount,
    BuildExpectation,
    CellStatus,
    ConfigEvidence,
    Dimension,
    EvidenceCell,
    EvidenceKind,
    EvidenceReference,
    GraphEvidence,
    GraphNodeTypeCount,
    HarnessCaseSpec,
    RecoveryEvidence,
    RunExpectation,
    RuntimeEvidence,
    ScenarioManifest,
    ScenarioRunEvidence,
    ScenarioSpec,
    Stage,
    SummaryRunExpectation,
    Workflow,
)

from elspeth.contracts import Determinism, PipelineRow, PluginSchema
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sinks_for_runtime,
)
from elspeth.plugins.infrastructure import manager as manager_module
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config

EXPECTED_DIMENSION_VALUES = (
    "config",
    "build",
    "contracts",
    "runtime",
    "audit",
    "recovery",
    "concurrency",
    "freeform",
    "guided",
    "round_trip",
    "scale",
)

EXPECTED_SCENARIO_VALUES = (
    ("linear", "Linear source → transform → sink"),
    ("multiple-independent-sources", "Multiple independent sources"),
    ("multi-source-queue-fan-in", "Multi-source queue fan-in"),
    ("conditional-routing", "Conditional routing, including missing and error destinations"),
    ("fork-multiple-terminals-partial-failure", "Fork to multiple terminals with partial failure"),
    ("fork-coalesce-policies", "Fork and coalesce across every completion policy and merge strategy"),
    ("sequential-nested-fork-coalesce", "Sequential or nested forks and coalesces"),
    ("parallel-coalesces", "Parallel coalesces"),
    ("aggregation-immutable-batch", "Aggregation, batch closure, and immutable membership"),
    ("row-expansion-parent-child-recovery", "Row expansion with parent/child identity and recovery"),
    ("row-union-interleave", "Row union or interleave, whether supported or consistently rejected"),
    ("retry-quarantine-discard-routed-errors", "Retry, quarantine, discard, and routed error handling"),
    ("sink-write-pending-redrive", "Sink write and pending-sink redrive"),
    ("checkpoint-deterministic-resume", "Checkpoint and deterministic resume"),
    (
        "multi-worker-lease-reclaim-late-completion",
        "Multi-worker execution, lease expiry, reclaim, and late completion",
    ),
)

EXPECTED_STATUS_MATRIX = {
    "linear": ("pass", "pass", "pass", "partial", "partial", "partial", "unknown", "pass", "partial", "partial", "partial"),
    "multiple-independent-sources": (
        "pass",
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "unknown",
        "pass",
        "fail",
        "partial",
        "unknown",
    ),
    "multi-source-queue-fan-in": (
        "pass",
        "pass",
        "pass",
        "partial",
        "partial",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "partial",
        "unknown",
    ),
    "conditional-routing": (
        "pass",
        "pass",
        "pass",
        "partial",
        "partial",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "partial",
        "unknown",
    ),
    "fork-multiple-terminals-partial-failure": (
        "pass",
        "pass",
        "pass",
        "partial",
        "partial",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "fork-coalesce-policies": (
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "partial",
        "pass",
        "fail",
        "partial",
        "unknown",
    ),
    "sequential-nested-fork-coalesce": (
        "pass",
        "pass",
        "partial",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "parallel-coalesces": (
        "pass",
        "partial",
        "partial",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "aggregation-immutable-batch": (
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "row-expansion-parent-child-recovery": (
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "partial",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "row-union-interleave": (
        "fail",
        "fail",
        "fail",
        "fail",
        "not_applicable",
        "not_applicable",
        "not_applicable",
        "fail",
        "fail",
        "not_applicable",
        "not_applicable",
    ),
    "retry-quarantine-discard-routed-errors": (
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "partial",
        "unknown",
    ),
    "sink-write-pending-redrive": (
        "pass",
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "pass",
        "partial",
        "partial",
        "unknown",
    ),
    "checkpoint-deterministic-resume": (
        "pass",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "unknown",
        "not_applicable",
        "not_applicable",
        "not_applicable",
        "unknown",
    ),
    "multi-worker-lease-reclaim-late-completion": (
        "not_applicable",
        "pass",
        "partial",
        "partial",
        "partial",
        "partial",
        "partial",
        "not_applicable",
        "not_applicable",
        "not_applicable",
        "unknown",
    ),
}

EXPECTED_ASSESSMENT_LOCATORS = {
    "core-builder-schema-plural-sources": (
        "tests/unit/core/dag/test_builder_validation.py",
        "tests/unit/core/dag/test_graph_validation.py",
        "tests/unit/core/test_dag_schema_propagation.py",
        "tests/unit/core/test_multi_source_foundation.py::test_plural_sources_are_canonical_and_stable_named",
        "tests/unit/core/test_multi_source_foundation.py::test_legacy_singular_source_yaml_is_rejected",
        "tests/unit/core/test_multi_source_foundation.py::test_settings_round_trip_plural_only",
        "tests/unit/core/test_multi_source_foundation.py::test_explicit_named_sources_keep_source_name_in_identity_and_audit_config",
        "tests/unit/core/test_multi_source_foundation.py::test_plugin_bundle_instantiates_named_sources_via_production_path",
        "tests/unit/core/test_multi_source_foundation.py::test_from_plugin_instances_builds_declared_queue_fan_in_via_production_path",
        "tests/unit/core/test_multi_source_foundation.py::test_pipeline_config_assembly_preserves_named_sources",
        "tests/unit/core/test_multi_source_foundation.py::test_graph_allows_multiple_source_roots_when_reachable",
        "tests/unit/core/test_multi_source_foundation.py::test_graph_rejects_fan_in_without_queue",
    ),
    "yaml-importer-generator": (
        "tests/unit/web/composer/test_yaml_importer.py",
        "tests/unit/web/composer/test_yaml_generator.py",
    ),
    "composer-runtime-agreement": (
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_reject_missing_required_field",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_reject_aggregation_nested_required_input_fields_without_upstream_guarantee",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_reject_direct_fork_to_sink_required_field_mismatch",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_accept_pass_through_downstream_of_coalesce",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_reject_mixed_coalesce_branch_schemas",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeAgreement::test_both_accept_aggregation_with_input_fields_and_required_fields",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeGateRouteParityAgreement",
        "tests/integration/pipeline/test_composer_runtime_agreement.py::TestComposerRuntimeQueueAgreement",
    ),
    "cardinality-identity": (
        "tests/unit/engine/test_batch_token_identity.py",
        "tests/unit/core/landscape/repository_integration/test_recorder_tokens.py::TestAtomicTokenOperations::test_expand_token_records_parent_expanded_outcome",
        "tests/unit/core/landscape/repository_integration/test_recorder_tokens.py::TestAtomicTokenOperations::test_expand_token_stores_expected_count_contract",
        "tests/unit/engine/test_processor.py::TestTransformModeOutcomeOrdering::test_cardinality_mismatch_does_not_record_parent_terminal_outcome",
        "tests/unit/engine/test_processor.py::TestTransformModeOutcomeOrdering::test_expand_token_failure_does_not_record_parent_terminal_outcome",
        "tests/unit/engine/test_processor.py::TestProcessRowMultiRowOutput",
        "tests/property/audit/test_fork_join_balance.py::TestForkRecoveryInvariant::test_expand_token_persists_per_child_payload",
        "tests/integration/core/test_batch_membership_contention.py",
        "tests/unit/core/landscape/repository_integration/test_recorder_tokens.py::TestAtomicTokenOperations::test_expand_token_records_batch_parent_outcome_atomically",
        "tests/testcontainer/core/test_token_outcome_atomicity_postgres.py::test_postgres_batch_expansion_claims_batch_once_under_contention",
        "tests/unit/core/landscape/test_token_recording.py::TestExpandToken::test_batch_expansion_claim_is_scoped_to_batch_not_selected_parent",
    ),
    "runtime-disposition-drains": (
        "tests/unit/engine/test_scheduler_drain_characterization.py::test_sink_bound_result_parks_pending_sink_with_fenced_owner_and_tags_result",
        "tests/unit/engine/test_scheduler_drain_characterization.py::test_claimed_token_failure_marks_failed_with_fence",
        "tests/unit/engine/test_scheduler_drain_characterization.py::test_non_sink_terminal_marks_terminal_and_unregistered_build_is_unfenced",
        "tests/unit/engine/test_processor.py::TestDurableSchedulerResumeDrain::test_aggregation_buffering_leaves_scheduler_work_blocked",
    ),
    "focused-crash-restart": (
        "tests/unit/core/landscape/test_scheduler_lease_recovery_races.py",
        "tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py::test_complete_barrier_crash_atomicity",
        "tests/integration/pipeline/test_aggregation_recovery.py::TestFlushOutputJournalDurability::test_timeout_flush_output_is_journal_durable_before_sink_write",
        "tests/integration/pipeline/test_aggregation_recovery.py::TestFailedFlushReconcile::test_failed_flush_crash_between_terminal_write_and_release_resumes",
        "tests/integration/pipeline/test_sink_effect_recovery.py::test_fresh_pipeline_executor_reuses_interrupted_open_state_and_publishes_once",
        "tests/integration/pipeline/test_sink_effect_recovery.py::test_redrive_after_crash_before_reservation_recovers",
        "tests/unit/engine/test_processor.py::TestDurableSchedulerResumeDrain::test_pending_sink_resume_repairs_already_outcomed_row_without_reemitting_sink",
        "tests/unit/engine/test_processor.py::TestDurableSchedulerResumeDrain::test_recovers_expired_lease_then_drains_without_source_replay",
        "tests/e2e/recovery/test_concurrent_resume.py::TestMidClaimCrashResume::test_ts02_source_completion_gap_reconciles_once_before_plugin_execution",
    ),
    "direct-contention-fencing": (
        "tests/integration/engine/test_two_process_scheduler_contention.py",
        "tests/integration/engine/test_multi_source_chaos.py::test_lease_expiry_mid_transform_peer_reclaim_bumps_attempt_and_fences_stale_owner",
        "tests/e2e/recovery/test_suspended_winner_fences.py",
        "tests/unit/engine/test_scheduler_drain_characterization.py::test_immediate_enqueue_routes_registered_worker_to_strict_and_unregistered_to_explicit_legacy",
        "tests/unit/engine/test_scheduler_drain_characterization.py::test_immediate_enqueue_routing_ast_and_legacy_production_references_are_pinned",
    ),
}

EXPECTED_ASSESSMENT_EVIDENCE = tuple(
    (
        evidence_group if index == 1 else f"{evidence_group}-{index:02}",
        locator,
    )
    for evidence_group, locators in EXPECTED_ASSESSMENT_LOCATORS.items()
    for index, locator in enumerate(locators, start=1)
)
EXPECTED_EVIDENCE_REGISTRY_SHA256 = "409034bb798cd578ce8e4cd741b1d77fc6858a042dc5f1f917f50b3272355d70"
EXPECTED_CASE_REGISTRY_SHA256 = "f27758e8a8f29879e01e288b531d5b1f373215c9e0065a6589a53c6a6eecd28a"
EXPECTED_CASE_FIXTURE_SHA256 = {
    "linear:happy-path": "12adb2d878a143756243fb56138b50b1e86ab21c6b3f439c2c79dd037ddf96e4",
    "multiple-independent-sources:independent-roots": "10b5d812e415dddd67d088fc771da3d4623d75fc3d2e4041562a4e4ae02741c0",
    "multi-source-queue-fan-in:queued-fan-in": "ccff919ce91062633679fcbe577194b4ce3c852a90c1f8f97622ac371b377c4e",
    "conditional-routing:two-way-gate": "e8b931a998d752ca7a461abb7b41edeb3f3251542d4349ebc66e9f450c316720",
    "fork-coalesce-policies:require-all-nested": "55b934bdb55c478ec552209008eda10bc6b211b6a17d8844acbf81609772acd2",
    "checkpoint-deterministic-resume:reopen-resume": "ce62216ce20210600f1a9c20e362aaf299c7538e6c4d3bd0e97627563dc813e6",
}

EXPECTED_HARNESS_EVIDENCE = (
    (
        "harness-linear-happy-path",
        "linear:happy-path",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-checkpoint-deterministic-resume-reopen-resume",
        "checkpoint-deterministic-resume:reopen-resume",
        ("config", "build", "runtime", "audit", "recovery"),
    ),
    (
        "harness-multiple-independent-sources-independent-roots",
        "multiple-independent-sources:independent-roots",
        ("config", "build"),
    ),
    (
        "harness-multi-source-queue-fan-in-queued-fan-in",
        "multi-source-queue-fan-in:queued-fan-in",
        ("config", "build"),
    ),
    (
        "harness-conditional-routing-two-way-gate",
        "conditional-routing:two-way-gate",
        ("config", "build"),
    ),
    (
        "harness-fork-coalesce-policies-require-all-nested",
        "fork-coalesce-policies:require-all-nested",
        ("config", "build"),
    ),
)

EXPECTED_INPUT_CSV = b"id,value\n1,10\n2,20\n3,30\n"
EXPECTED_ORDERS_CSV = b"id,value\n1,10\n2,20\n3,30\n"
EXPECTED_REFUNDS_CSV = b"id,value\n101,-5\n102,-10\n103,-15\n"

DAG_HUB_PATH = REPOSITORY_ROOT / "docs/architecture/dag/README.md"
CORPUS_README_PATH = REPOSITORY_ROOT / "docs/architecture/dag/scenario-corpus/README.md"
CURRENT_ASSESSMENT_ROOT = REPOSITORY_ROOT / "docs/architecture/dag/assessments/2026-07-18-0319"
CURRENT_ASSESSMENT_DOCUMENTS = tuple(sorted(CURRENT_ASSESSMENT_ROOT.rglob("*.md")))
ACTIVE_CORPUS_ISSUE = "elspeth-ef29ef6ba4"

EXPECTED_HAPPY_PATH_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: inbound
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
queues: {inbound: {}}
transforms:
  - name: pass_rows
    plugin: passthrough
    input: inbound
    on_success: output
    on_error: discard
    options: {schema: {mode: observed}}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_REOPEN_RESUME_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: batch_in
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
aggregations:
  - name: eof_sum
    plugin: dag_corpus_fail_once_eof_batch
    input: batch_in
    on_success: output
    on_error: discard
    trigger: {count: 100}
    output_mode: transform
    options:
      schema: {mode: observed}
      fault_marker_path: ${fault_marker}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_INDEPENDENT_ROOTS_YAML = b"""sources:
  orders:
    plugin: csv
    on_success: output
    options:
      path: ${input_orders}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
  refunds:
    plugin: csv
    on_success: output
    options:
      path: ${input_refunds}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_QUEUED_FAN_IN_YAML = b"""sources:
  orders:
    plugin: csv
    on_success: inbound
    options:
      path: ${input_orders}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
  refunds:
    plugin: csv
    on_success: inbound
    options:
      path: ${input_refunds}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
queues: {inbound: {description: deterministic multi-source fan-in}}
transforms:
  - name: normalize_rows
    plugin: passthrough
    input: inbound
    on_success: output
    on_error: discard
    options: {schema: {mode: observed}}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_TWO_WAY_GATE_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: routing
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
gates:
  - name: route_by_value
    input: routing
    condition: "row['value'] >= 20"
    routes: {"true": accepted, "false": rejected}
sinks:
  accepted:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_accepted}
      format: jsonl
      schema: {mode: observed}
  rejected:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_rejected}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_REQUIRE_ALL_NESTED_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
gates:
  - name: fork_gate
    input: fork_input
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [path_a, path_b]
coalesce:
  - name: merge_paths
    branches: [path_a, path_b]
    policy: require_all
    merge: nested
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
"""


def _markdown_link_targets(path: Path) -> tuple[str, ...]:
    targets: list[str] = []
    # Textual is a runtime dependency and guarantees markdown-it-py. Parsing
    # CommonMark avoids silently missing reference links, images, or titles.
    for token in MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8")):
        for child in token.children or ():
            attribute = "href" if child.type == "link_open" else "src" if child.type == "image" else None
            if attribute is not None:
                target = child.attrGet(attribute)
                if not isinstance(target, str):
                    raise AssertionError(f"CommonMark {child.type} token lacks a string {attribute}: {target!r}")
                targets.append(target)
    return tuple(targets)


def _repository_relative_link_targets(path: Path) -> tuple[str, ...]:
    relative_targets: list[str] = []
    for target in _markdown_link_targets(path):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
            continue
        relative_targets.append(unquote(parsed.path))
    return tuple(relative_targets)


def _missing_repository_relative_link_targets(path: Path) -> tuple[str, ...]:
    repository_root = REPOSITORY_ROOT.resolve()
    missing_targets: list[str] = []
    for target in _repository_relative_link_targets(path):
        resolved_target = (path.parent / target).resolve()
        if not resolved_target.is_relative_to(repository_root) or not resolved_target.exists():
            missing_targets.append(target)
    return tuple(missing_targets)


@pytest.mark.parametrize(
    ("markdown", "expected_targets", "expected_relative_targets"),
    [
        ("[inline](relative.md#section)", ("relative.md#section",), ("relative.md",)),
        ("![image](images/diagram.png?raw=1)", ("images/diagram.png?raw=1",), ("images/diagram.png",)),
        ("[reference][ref]\n\n[ref]: reference.md 'title'", ("reference.md",), ("reference.md",)),
        ('[space](<dir/file name.md> "title")', ("dir/file%20name.md",), ("dir/file name.md",)),
        ("[external](https://example.test/docs)", ("https://example.test/docs",), ()),
        ("[anchor](#status-vocabulary)", ("#status-vocabulary",), ()),
        ("[root absolute](/docs/index.md)", ("/docs/index.md",), ()),
        ("[malformed](<unterminated.md)", (), ()),
    ],
    ids=("inline", "image-query", "reference", "angle-space-title", "external", "fragment", "absolute", "malformed"),
)
def test_markdown_link_target_parser_covers_supported_commonmark_forms(
    tmp_path: Path,
    markdown: str,
    expected_targets: tuple[str, ...],
    expected_relative_targets: tuple[str, ...],
) -> None:
    document = tmp_path / "document.md"
    document.write_text(markdown, encoding="utf-8")

    assert _markdown_link_targets(document) == expected_targets
    assert _repository_relative_link_targets(document) == expected_relative_targets


def test_existing_repository_relative_link_target_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repository"
    document = repository_root / "docs/document.md"
    document.parent.mkdir(parents=True)
    (document.parent / "present.md").touch()
    document.write_text("[present](present.md)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPOSITORY_ROOT", repository_root)

    assert _missing_repository_relative_link_targets(document) == ()


def test_missing_repository_relative_link_target_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repository"
    document = repository_root / "docs/document.md"
    document.parent.mkdir(parents=True)
    document.write_text("[missing][target]\n\n[target]: missing.md\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPOSITORY_ROOT", repository_root)

    assert _missing_repository_relative_link_targets(document) == ("missing.md",)


def test_parent_traversal_to_existing_repository_target_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repository"
    document = repository_root / "docs/nested/document.md"
    document.parent.mkdir(parents=True)
    (repository_root / "docs/present.md").touch()
    document.write_text("[present](../present.md)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPOSITORY_ROOT", repository_root)

    assert _missing_repository_relative_link_targets(document) == ()


def test_parent_traversal_to_existing_target_outside_repository_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repository"
    document = repository_root / "docs/document.md"
    document.parent.mkdir(parents=True)
    (tmp_path / "outside.md").touch()
    document.write_text("[outside](../../outside.md)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPOSITORY_ROOT", repository_root)

    assert _missing_repository_relative_link_targets(document) == ("../../outside.md",)


def test_dag_hub_links_the_live_scenario_corpus() -> None:
    assert "scenario-corpus/README.md" in _markdown_link_targets(DAG_HUB_PATH)


def test_scenario_corpus_readme_links_manifest_criteria_and_active_issue() -> None:
    targets = _markdown_link_targets(CORPUS_README_PATH)
    content = CORPUS_README_PATH.read_text(encoding="utf-8")

    assert "v1/manifest.yaml" in targets
    assert "../completeness-criteria.md" in targets
    assert ACTIVE_CORPUS_ISSUE in content
    assert f"filigree show {ACTIVE_CORPUS_ISSUE} --json" in content


@pytest.mark.parametrize("document", [DAG_HUB_PATH, CORPUS_README_PATH, *CURRENT_ASSESSMENT_DOCUMENTS])
def test_dag_corpus_document_repository_relative_links_resolve(document: Path) -> None:
    assert _missing_repository_relative_link_targets(document) == ()


def _reference(*, kind: EvidenceKind = "harness") -> EvidenceReference:
    return EvidenceReference(
        id="evidence-1",
        kind=kind,
        locator="tests/path.py::test_case",
        claim="Exercises the production path",
        stages=("runtime",),
    )


def _expectation() -> SummaryRunExpectation:
    return SummaryRunExpectation(
        kind="summary",
        status="completed",
        output_rows=1,
        required_audit_record_types=("run_started",),
    )


def _case() -> HarnessCaseSpec:
    return HarnessCaseSpec(
        id="happy-path",
        workflow="run",
        fixture="linear.yaml",
        input_fixtures={"primary": "linear.jsonl"},
        output_artifacts={"output": "output.jsonl"},
        expected=_expectation(),
    )


def _plural_binding_case_values() -> dict[str, object]:
    return {
        "id": "plural-bindings",
        "workflow": "build",
        "fixture": "multiple-independent-sources/independent-roots.yaml",
        "input_fixtures": {
            "orders": "multiple-independent-sources/orders.csv",
            "refunds": "multiple-independent-sources/refunds.csv",
        },
        "output_artifacts": {"output": "output.jsonl"},
        "expected": {
            "node_count": 3,
            "edge_count": 2,
            "node_type_counts": (
                {"node_type": "sink", "count": 1},
                {"node_type": "source", "count": 2},
            ),
            "edge_labels": ("on_success", "on_success"),
        },
    }


def test_plural_input_artifact_binding_is_sorted_immutable_and_exact() -> None:
    case = HarnessCaseSpec.model_validate(_plural_binding_case_values())

    assert tuple(case.input_fixtures.items()) == (
        ("orders", "multiple-independent-sources/orders.csv"),
        ("refunds", "multiple-independent-sources/refunds.csv"),
    )
    assert case.model_dump(mode="json")["input_fixtures"] == {
        "orders": "multiple-independent-sources/orders.csv",
        "refunds": "multiple-independent-sources/refunds.csv",
    }
    with pytest.raises(TypeError):
        case.input_fixtures["orders"] = "multiple-independent-sources/decoy.csv"  # type: ignore[index]


@pytest.mark.parametrize(
    ("input_fixtures", "message"),
    [
        ({}, "input_fixtures must not be empty"),
        (
            {
                "refunds": "multiple-independent-sources/refunds.csv",
                "orders": "multiple-independent-sources/orders.csv",
            },
            "input_fixtures must be sorted",
        ),
        (
            {
                "orders": "multiple-independent-sources/input.csv",
                "refunds": "multiple-independent-sources/input.csv",
            },
            "input_fixtures must use distinct fixture paths",
        ),
    ],
    ids=("empty", "unsorted", "duplicate-path"),
)
def test_plural_input_artifact_binding_rejects_inexact_mapping(
    input_fixtures: dict[str, str],
    message: str,
) -> None:
    values = _plural_binding_case_values()
    values["input_fixtures"] = input_fixtures

    with pytest.raises(ValidationError, match=message):
        HarnessCaseSpec.model_validate(values)


def test_per_sink_artifact_binding_is_sorted_immutable_and_exact() -> None:
    values = _plural_binding_case_values()
    values["output_artifacts"] = {"accepted": "accepted.jsonl", "rejected": "rejected.jsonl"}
    case = HarnessCaseSpec.model_validate(values)

    assert tuple(case.output_artifacts.items()) == (
        ("accepted", "accepted.jsonl"),
        ("rejected", "rejected.jsonl"),
    )
    assert case.model_dump(mode="json")["output_artifacts"] == {
        "accepted": "accepted.jsonl",
        "rejected": "rejected.jsonl",
    }
    with pytest.raises(TypeError):
        case.output_artifacts["accepted"] = "decoy.jsonl"  # type: ignore[index]


@pytest.mark.parametrize(
    ("output_artifacts", "message"),
    [
        ({}, "output_artifacts must not be empty"),
        (
            {"rejected": "rejected.jsonl", "accepted": "accepted.jsonl"},
            "output_artifacts must be sorted",
        ),
        (
            {"accepted": "shared.jsonl", "rejected": "shared.jsonl"},
            "output_artifacts must use unique filenames",
        ),
        ({"output": "../output.jsonl"}, "safe relative leaf filename"),
        ({"output": "/tmp/output.jsonl"}, "safe relative leaf filename"),
        ({"output": "nested/output.jsonl"}, "safe relative leaf filename"),
    ],
    ids=("empty", "unsorted", "duplicate-filename", "traversal", "absolute", "nested"),
)
def test_per_sink_artifact_binding_rejects_inexact_mapping(
    output_artifacts: dict[str, str],
    message: str,
) -> None:
    values = _plural_binding_case_values()
    values["output_artifacts"] = output_artifacts

    with pytest.raises(ValidationError, match=message):
        HarnessCaseSpec.model_validate(values)


def _scenario(cell: EvidenceCell) -> ScenarioSpec:
    return ScenarioSpec(
        id="linear",
        ordinal=1,
        title="Linear source → transform → sink",
        cases=(_case(),),
        dimensions={"config": cell},
    )


def _manifest(*cells: EvidenceCell) -> ScenarioManifest:
    return ScenarioManifest(
        schema_version=1,
        criteria_ref="docs/reference/dag-completeness.md",
        evidence=(_reference(),),
        scenarios=tuple(_scenario(cell) for cell in cells),
    )


def _valid_runtime() -> RuntimeEvidence:
    return RuntimeEvidence(
        attempted=True,
        run_id="run-1",
        status="completed",
        rows_processed=1,
        rows_succeeded=1,
        rows_failed=0,
        output_rows=1,
    )


def _valid_audit() -> AuditEvidence:
    return AuditEvidence(
        attempted=True,
        total_records=1,
        record_counts=(AuditRecordCount(record_type="run_started", count=1),),
        source_operation_count=1,
    )


def _valid_recovery() -> RecoveryEvidence:
    return RecoveryEvidence(
        attempted=True,
        database_reopened=True,
        checkpoint_id="checkpoint-1",
        checkpoint_sequence=1,
        can_resume=True,
        source_replayed=False,
        checkpoint_removed=True,
    )


def _exact_runtime_projection_values() -> dict[str, object]:
    return {
        "rows": (
            {
                "key": "primary:0",
                "source_name": "primary",
                "source_row_index": 0,
                "ingest_sequence": 0,
                "source_data_hash": "a" * 64,
            },
        ),
        "tokens": ({"key": "primary:0#0", "row_key": "primary:0", "parents": ()},),
        "node_states": (
            {
                "key": "primary:0#0|source:primary|0|0",
                "token_key": "primary:0#0",
                "node_key": "source:primary",
                "step_index": 0,
                "attempt": 0,
                "status": "completed",
            },
        ),
        "routes": (),
        "terminal_dispositions": (
            {
                "key": "primary:0#0",
                "token_key": "primary:0#0",
                "outcome": "success",
                "path": "default_flow",
                "sink_name": "output",
            },
        ),
        "scheduler_work": (
            {
                "key": "primary:0#0|queue:inbound|0",
                "token_key": "primary:0#0",
                "node_key": "queue:inbound",
                "transitions": ("enqueue:ready", "mark_terminal:terminal"),
                "final_status": "terminal",
            },
        ),
        "audit_records": (
            {
                "key": "run|run",
                "record_type": "run",
                "material": '{"status":"completed"}',
                "references": (),
            },
        ),
    }


def _exact_run_expectation_values() -> dict[str, object]:
    return {
        "kind": "exact",
        "status": "completed",
        "sink_outputs": ({"sink_name": "output", "rows": ('{"id":1,"value":10}',)},),
        "rows_processed": 1,
        "rows_succeeded": 1,
        "rows_failed": 0,
        "projection": _exact_runtime_projection_values(),
        "audit_record_counts": ({"record_type": "run", "count": 1},),
        "source_operation_count": 1,
    }


def test_exact_runtime_projection_expectation_is_discriminated_immutable_and_canonical() -> None:
    expectation = RunExpectation.model_validate(_exact_run_expectation_values())

    assert expectation.kind == "exact"
    assert expectation.sink_outputs[0].rows == ('{"id":1,"value":10}',)
    assert expectation.projection.tokens[0].parents == ()
    with pytest.raises(ValidationError, match="frozen"):
        expectation.__setattr__("rows_processed", 2)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "tokens",
            (
                {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
                {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
            ),
            "tokens must contain unique sorted keys",
        ),
        (
            "terminal_dispositions",
            (),
            "terminal dispositions must exactly cover tokens",
        ),
        (
            "rows",
            (
                {
                    "key": "primary:1",
                    "source_name": "primary",
                    "source_row_index": 1,
                    "ingest_sequence": 1,
                    "source_data_hash": "b" * 64,
                },
                {
                    "key": "primary:0",
                    "source_name": "primary",
                    "source_row_index": 0,
                    "ingest_sequence": 0,
                    "source_data_hash": "a" * 64,
                },
            ),
            "rows must contain unique sorted keys",
        ),
    ],
    ids=("duplicate-token", "missing-disposition", "unsorted-rows"),
)
def test_exact_runtime_projection_rejects_inexact_durable_shape(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _exact_run_expectation_values()
    projection = cast(dict[str, object], values["projection"])
    projection[field] = value

    with pytest.raises(ValidationError, match=message):
        RunExpectation.model_validate(values)


def test_exact_runtime_projection_rejects_count_and_output_mismatches() -> None:
    values = _exact_run_expectation_values()
    values["rows_processed"] = 2

    with pytest.raises(ValidationError, match="rows_processed must equal projected row count"):
        RunExpectation.model_validate(values)


def test_expected_dimension_and_scenario_constants_are_exact_and_ordered() -> None:
    assert EXPECTED_DIMENSIONS == EXPECTED_DIMENSION_VALUES
    assert EXPECTED_SCENARIOS == EXPECTED_SCENARIO_VALUES


def test_closed_vocabularies_are_exact() -> None:
    assert get_args(CellStatus) == ("pass", "partial", "fail", "unknown", "not_applicable")
    assert get_args(Dimension) == EXPECTED_DIMENSION_VALUES
    assert get_args(EvidenceKind) == ("harness", "pytest", "document", "decision")
    assert get_args(Stage) == ("config", "build", "runtime", "audit", "recovery")
    assert get_args(Workflow) == ("run", "recovery", "build")


def test_build_expectation_is_dedicated_immutable_and_exact() -> None:
    expectation = BuildExpectation(
        node_count=3,
        edge_count=2,
        node_type_counts=(
            GraphNodeTypeCount(node_type="sink", count=1),
            GraphNodeTypeCount(node_type="source", count=2),
        ),
        edge_labels=("on_success", "on_success"),
    )

    assert expectation.model_dump(mode="json") == {
        "node_count": 3,
        "edge_count": 2,
        "node_type_counts": [
            {"node_type": "sink", "count": 1},
            {"node_type": "source", "count": 2},
        ],
        "edge_labels": ["on_success", "on_success"],
    }
    with pytest.raises(ValidationError, match="frozen"):
        expectation.__setattr__("node_count", 4)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "node_type_counts",
            ({"node_type": "source", "count": 2}, {"node_type": "sink", "count": 1}),
            "sorted order",
        ),
        (
            "node_type_counts",
            ({"node_type": "sink", "count": 1}, {"node_type": "source", "count": 1}),
            "sum exactly",
        ),
        ("edge_labels", ("on_success", "continue"), "sorted"),
        ("edge_labels", ("on_success",), "exactly edge_count"),
    ],
    ids=("node-types-order", "node-types-sum", "labels-order", "labels-count"),
)
def test_build_expectation_rejects_inexact_graph_shape(field: str, value: object, message: str) -> None:
    values: dict[str, object] = {
        "node_count": 3,
        "edge_count": 2,
        "node_type_counts": (
            {"node_type": "sink", "count": 1},
            {"node_type": "source", "count": 2},
        ),
        "edge_labels": ("on_success", "on_success"),
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        BuildExpectation.model_validate(values)


@pytest.mark.parametrize("model", ["build-expectation", "accepted-evidence"])
def test_accepted_graph_contracts_reject_zero_shape(model: str) -> None:
    values: dict[str, object] = {
        "node_count": 0,
        "edge_count": 0,
        "node_type_counts": (),
        "edge_labels": (),
    }
    if model == "accepted-evidence":
        values.update(accepted=True, topology_hash="topology-sha")

    contract = GraphEvidence if model == "accepted-evidence" else BuildExpectation
    with pytest.raises(ValidationError, match="positive node_count and edge_count"):
        contract.model_validate(values)


@pytest.mark.parametrize("model", ["build-expectation", "accepted-evidence"])
@pytest.mark.parametrize(
    "node_type_counts",
    [
        ({"node_type": "sink", "count": 1},),
        ({"node_type": "source", "count": 1},),
    ],
    ids=("missing-source", "missing-sink"),
)
def test_accepted_graph_contracts_require_source_and_sink(
    model: str,
    node_type_counts: tuple[dict[str, object], ...],
) -> None:
    values: dict[str, object] = {
        "node_count": 1,
        "edge_count": 1,
        "node_type_counts": node_type_counts,
        "edge_labels": ("on_success",),
    }
    if model == "accepted-evidence":
        values.update(accepted=True, topology_hash="topology-sha")

    contract = GraphEvidence if model == "accepted-evidence" else BuildExpectation
    with pytest.raises(ValidationError, match="at least one source and one sink"):
        contract.model_validate(values)


@pytest.mark.parametrize(
    ("workflow", "expected", "expected_kind"),
    [
        ("build", _expectation(), "BuildExpectation"),
        (
            "run",
            {
                "node_count": 3,
                "edge_count": 2,
                "node_type_counts": [
                    {"node_type": "sink", "count": 1},
                    {"node_type": "source", "count": 2},
                ],
                "edge_labels": ["on_success", "on_success"],
            },
            "a run expectation",
        ),
    ],
)
def test_harness_case_rejects_workflow_expectation_kind_mismatch(
    workflow: str,
    expected: object,
    expected_kind: str,
) -> None:
    values = {
        "id": "kind-mismatch",
        "workflow": workflow,
        "fixture": "linear/happy-path.yaml",
        "input_fixtures": {"primary": "linear/input.csv"},
        "output_artifacts": {"output": "output.jsonl"},
        "expected": expected,
    }

    with pytest.raises(ValidationError, match=rf"{workflow} workflow requires {expected_kind}"):
        HarnessCaseSpec.model_validate(values)


def test_non_empty_strings_are_strict_stripped_and_non_empty() -> None:
    reference = _reference()
    assert (
        EvidenceReference(
            id="  evidence-1  ",
            kind="harness",
            locator="  tests/path.py::test_case  ",
            claim="  claim  ",
        ).id
        == "evidence-1"
    )
    assert reference.executable is True

    with pytest.raises(ValidationError):
        EvidenceReference(id=" ", kind="harness", locator="test", claim="claim")
    with pytest.raises(ValidationError):
        EvidenceReference.model_validate({"id": 1, "kind": "harness", "locator": "test", "claim": "claim"})


@pytest.mark.parametrize("kind", ["harness", "pytest"])
def test_executable_evidence_kinds_are_executable(kind: EvidenceKind) -> None:
    assert _reference(kind=kind).executable is True


@pytest.mark.parametrize("kind", ["document", "decision"])
def test_non_executable_evidence_kinds_are_not_executable(kind: EvidenceKind) -> None:
    assert _reference(kind=kind).executable is False


def test_pass_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="pass"):
        EvidenceCell(status="pass")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", "gap remains"),
        ("owner_issue", "elspeth-0123456789"),
        ("exit_gate", "close the gap"),
    ],
)
def test_pass_with_gap_metadata_is_rejected(field: str, value: str) -> None:
    values: dict[str, object] = {"status": "pass", "evidence": ("evidence-1",), field: value}
    with pytest.raises(ValidationError, match="pass"):
        EvidenceCell.model_validate(values)


@pytest.mark.parametrize("status", ["partial", "fail", "unknown"])
@pytest.mark.parametrize("missing", ["reason", "owner_issue", "exit_gate"])
def test_gap_status_without_owned_exit_gate_is_rejected(status: CellStatus, missing: str) -> None:
    values: dict[str, object] = {
        "status": status,
        "reason": "coverage gap",
        "owner_issue": "elspeth-0123456789",
        "exit_gate": "focused regression passes",
    }
    del values[missing]

    with pytest.raises(ValidationError, match=r"reason.*owner_issue.*exit_gate"):
        EvidenceCell.model_validate(values)


def test_owned_gap_status_is_accepted() -> None:
    cell = EvidenceCell(
        status="partial",
        reason="coverage gap",
        owner_issue="elspeth-0123456789",
        exit_gate="focused regression passes",
    )
    assert cell.status == "partial"


def test_invalid_issue_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="owner_issue"):
        EvidenceCell(
            status="fail",
            reason="gap",
            owner_issue="ELSPETH-123",
            exit_gate="fix lands",
        )


def test_not_applicable_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not_applicable"):
        EvidenceCell(status="not_applicable")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence", ("evidence-1",)),
        ("owner_issue", "elspeth-0123456789"),
        ("exit_gate", "not relevant"),
    ],
)
def test_not_applicable_with_evidence_or_ownership_is_rejected(field: str, value: object) -> None:
    values: dict[str, object] = {"status": "not_applicable", "reason": "not part of this scenario", field: value}
    with pytest.raises(ValidationError, match="not_applicable"):
        EvidenceCell.model_validate(values)


def test_not_applicable_with_reason_is_accepted() -> None:
    cell = EvidenceCell(status="not_applicable", reason="not part of this scenario")
    assert cell.reason == "not part of this scenario"


def test_unknown_fields_are_rejected_and_models_are_frozen() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ConfigEvidence.model_validate({"loaded": True, "settings_sha256": "abc", "unexpected": "field"})

    evidence = ConfigEvidence(loaded=True, settings_sha256="abc")
    # Exercise Pydantic's runtime freeze guard without a statically invalid assignment.
    with pytest.raises(ValidationError, match="frozen"):
        evidence.__setattr__("loaded", False)


def test_scenario_dimensions_reject_post_validation_mutation() -> None:
    scenario = _scenario(EvidenceCell(status="pass", evidence=("evidence-1",)))

    with pytest.raises(TypeError):
        # Deliberately attempt mutation through the public read-only Mapping.
        scenario.dimensions["runtime"] = EvidenceCell(status="pass", evidence=("evidence-1",))  # type: ignore[index]


def test_scenario_dimensions_preserve_mapping_input_access_iteration_and_serialization() -> None:
    scenario = ScenarioSpec.model_validate(
        {
            "id": "linear",
            "ordinal": 1,
            "title": "Linear source → transform → sink",
            "dimensions": {
                "config": {
                    "status": "pass",
                    "evidence": ["evidence-1"],
                }
            },
        }
    )

    assert scenario.dimensions["config"].status == "pass"
    assert [(dimension, cell.status) for dimension, cell in scenario.dimensions.items()] == [("config", "pass")]
    assert scenario.model_dump(mode="json", exclude_none=True)["dimensions"] == {"config": {"status": "pass", "evidence": ["evidence-1"]}}
    assert ScenarioSpec.model_validate_json(scenario.model_dump_json()).dimensions["config"].status == "pass"


def test_strict_scalar_types_reject_coercion() -> None:
    with pytest.raises(ValidationError):
        ConfigEvidence.model_validate({"loaded": 1, "settings_sha256": "abc"})
    with pytest.raises(ValidationError):
        RunExpectation.model_validate({"status": "completed", "output_rows": "1", "required_audit_record_types": ()})
    with pytest.raises(ValidationError):
        AuditRecordCount(record_type="run_started", count=True)


@pytest.mark.parametrize(
    "missing",
    ["node_count", "edge_count", "node_type_counts", "edge_labels", "topology_hash"],
)
def test_accepted_graph_requires_all_graph_facts(missing: str) -> None:
    values: dict[str, object] = {
        "accepted": True,
        "node_count": 3,
        "edge_count": 2,
        "node_type_counts": (
            {"node_type": "sink", "count": 1},
            {"node_type": "source", "count": 2},
        ),
        "edge_labels": ("on_success", "on_success"),
        "topology_hash": "topology-sha",
    }
    del values[missing]
    with pytest.raises(ValidationError, match="accepted"):
        GraphEvidence.model_validate(values)


@pytest.mark.parametrize("field", ["rejection_type", "rejection_message"])
def test_accepted_graph_forbids_rejection_facts(field: str) -> None:
    values: dict[str, object] = {
        "accepted": True,
        "node_count": 3,
        "edge_count": 2,
        "node_type_counts": (
            {"node_type": "sink", "count": 1},
            {"node_type": "source", "count": 2},
        ),
        "edge_labels": ("on_success", "on_success"),
        "topology_hash": "topology-sha",
        field: "rejected",
    }
    with pytest.raises(ValidationError, match="accepted"):
        GraphEvidence.model_validate(values)


@pytest.mark.parametrize("missing", ["rejection_type", "rejection_message"])
def test_rejected_graph_requires_both_rejection_facts(missing: str) -> None:
    values: dict[str, object] = {
        "accepted": False,
        "rejection_type": "ValueError",
        "rejection_message": "unsupported topology",
    }
    del values[missing]
    with pytest.raises(ValidationError, match="rejected"):
        GraphEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_count", 3),
        ("edge_count", 2),
        ("node_type_counts", ({"node_type": "source", "count": 3},)),
        ("edge_labels", ("continue", "continue")),
        ("topology_hash", "topology-sha"),
    ],
)
def test_rejected_graph_forbids_graph_facts(field: str, value: object) -> None:
    values: dict[str, object] = {
        "accepted": False,
        "rejection_type": "ValueError",
        "rejection_message": "unsupported topology",
        field: value,
    }
    with pytest.raises(ValidationError, match="rejected"):
        GraphEvidence.model_validate(values)


def test_accepted_and_rejected_graph_shapes_are_accepted() -> None:
    accepted = GraphEvidence(
        accepted=True,
        node_count=3,
        edge_count=2,
        node_type_counts=(
            {"node_type": "sink", "count": 1},
            {"node_type": "source", "count": 2},
        ),
        edge_labels=("on_success", "on_success"),
        topology_hash="topology-sha",
    )
    rejected = GraphEvidence(accepted=False, rejection_type="ValueError", rejection_message="unsupported topology")
    assert accepted.node_count == 3
    assert accepted.node_type_counts is not None
    assert accepted.node_type_counts[1].node_type == "source"
    assert rejected.rejection_type == "ValueError"


@pytest.mark.parametrize("missing", ["run_id", "status"])
def test_attempted_runtime_requires_run_identity_and_status(missing: str) -> None:
    values: dict[str, object] = {"attempted": True, "run_id": "run-1", "status": "completed"}
    del values[missing]
    with pytest.raises(ValidationError, match="attempted"):
        RuntimeEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run-1"),
        ("status", "completed"),
        ("rows_processed", 1),
        ("rows_succeeded", 1),
        ("rows_failed", 1),
        ("output_rows", 1),
    ],
)
def test_unattempted_runtime_forbids_identity_status_and_nonzero_counters(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="unattempted"):
        RuntimeEvidence.model_validate({"attempted": False, field: value})


def test_attempted_and_unattempted_runtime_shapes_are_accepted() -> None:
    assert _valid_runtime().rows_processed == 1
    assert RuntimeEvidence(attempted=False).rows_processed == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_records", 1),
        ("record_counts", ({"record_type": "run_started", "count": 1},)),
        ("source_operation_count", 1),
    ],
)
def test_unattempted_audit_forbids_records(field: str, value: object) -> None:
    values: dict[str, object] = {
        "attempted": False,
        "total_records": 0,
        "record_counts": (),
        "source_operation_count": 0,
        field: value,
    }
    with pytest.raises(ValidationError, match="unattempted"):
        AuditEvidence.model_validate(values)


def test_attempted_and_unattempted_audit_shapes_are_accepted() -> None:
    assert _valid_audit().record_counts[0].record_type == "run_started"
    unattempted = AuditEvidence(attempted=False, total_records=0, record_counts=(), source_operation_count=0)
    assert unattempted.total_records == 0


@pytest.mark.parametrize("missing", ["checkpoint_id", "checkpoint_sequence"])
def test_attempted_recovery_requires_checkpoint_identity(missing: str) -> None:
    values: dict[str, object] = {
        "attempted": True,
        "database_reopened": True,
        "checkpoint_id": "checkpoint-1",
        "checkpoint_sequence": 1,
        "can_resume": True,
        "source_replayed": False,
        "checkpoint_removed": True,
    }
    del values[missing]
    with pytest.raises(ValidationError, match="attempted"):
        RecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkpoint_id", "checkpoint-1"),
        ("checkpoint_sequence", 1),
        ("database_reopened", True),
        ("can_resume", True),
        ("source_replayed", True),
        ("checkpoint_removed", True),
    ],
)
def test_unattempted_recovery_forbids_checkpoint_identity_and_true_results(field: str, value: object) -> None:
    values: dict[str, object] = {
        "attempted": False,
        "database_reopened": False,
        "can_resume": False,
        "source_replayed": False,
        "checkpoint_removed": False,
        field: value,
    }
    with pytest.raises(ValidationError, match="unattempted"):
        RecoveryEvidence.model_validate(values)


def test_attempted_and_unattempted_recovery_shapes_are_accepted() -> None:
    assert _valid_recovery().checkpoint_id == "checkpoint-1"
    unattempted = RecoveryEvidence(
        attempted=False,
        database_reopened=False,
        can_resume=False,
        source_replayed=False,
        checkpoint_removed=False,
    )
    assert unattempted.checkpoint_id is None


def test_scenario_run_evidence_accepts_the_complete_observed_shape() -> None:
    evidence = ScenarioRunEvidence(
        schema_version=1,
        scenario_id="linear",
        case_id="happy-path",
        fixture_sha256="fixture-sha",
        config=ConfigEvidence(loaded=True, settings_sha256="settings-sha"),
        graph=GraphEvidence(
            accepted=True,
            node_count=3,
            edge_count=2,
            node_type_counts=(
                {"node_type": "sink", "count": 1},
                {"node_type": "source", "count": 2},
            ),
            edge_labels=("on_success", "on_success"),
            topology_hash="topology-sha",
        ),
        runtime=_valid_runtime(),
        audit=_valid_audit(),
        recovery=_valid_recovery(),
        completed_stages=("config", "build", "runtime", "audit", "recovery"),
    )
    assert evidence.scenario_id == "linear"
    assert evidence.completed_stages[-1] == "recovery"


def test_manifest_verdict_is_complete_only_for_pass_or_not_applicable_cells() -> None:
    passing = EvidenceCell(status="pass", evidence=("evidence-1",))
    not_applicable = EvidenceCell(status="not_applicable", reason="not part of this scenario")
    assert _manifest(passing, not_applicable).verdict == "complete"

    for status in ("partial", "fail", "unknown"):
        gap = EvidenceCell(
            status=status,
            reason="coverage gap",
            owner_issue="elspeth-0123456789",
            exit_gate="focused regression passes",
        )
        assert _manifest(gap).verdict == "not_complete"


def valid_manifest_dict() -> dict[str, object]:
    loaded = yaml.safe_load(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def write_manifest(tmp_path: Path, raw: object) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _raw_scenarios(raw: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], raw["scenarios"])


def _raw_dimensions(scenario: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], scenario["dimensions"])


def _raw_evidence(raw: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], raw["evidence"])


def _case_dict(case_id: str = "happy-path") -> dict[str, object]:
    return {
        "id": case_id,
        "workflow": "run",
        "fixture": "linear/happy-path.yaml",
        "input_fixtures": {"primary": "linear/input.csv"},
        "output_artifacts": {"output": "output.jsonl"},
        "expected": {
            "kind": "summary",
            "status": "completed",
            "output_rows": 3,
            "required_audit_record_types": ["run"],
        },
    }


def _add_harness_evidence(raw: dict[str, object], locator: str) -> str:
    evidence_id = f"harness-{locator.replace(':', '-')}"
    _raw_evidence(raw).append(
        {
            "id": evidence_id,
            "kind": "harness",
            "locator": locator,
            "claim": "Exercises a registered DAG scenario case",
            "stages": ["config", "build", "runtime", "audit"],
        }
    )
    return evidence_id


def _remove_harness_evidence(raw: dict[str, object], locator: str) -> None:
    removed_ids = {
        cast(str, evidence["id"])
        for evidence in _raw_evidence(raw)
        if evidence.get("kind") == "harness" and evidence.get("locator") == locator
    }
    raw["evidence"] = [evidence for evidence in _raw_evidence(raw) if evidence.get("id") not in removed_ids]
    for scenario in _raw_scenarios(raw):
        for cell in _raw_dimensions(scenario).values():
            cell["evidence"] = [evidence_id for evidence_id in cast(list[str], cell.get("evidence", [])) if evidence_id not in removed_ids]


def _register_linear_case(raw: dict[str, object], case: dict[str, object]) -> None:
    _remove_harness_evidence(raw, "linear:happy-path")
    scenario = _raw_scenarios(raw)[0]
    scenario["cases"] = [case]
    evidence_id = _add_harness_evidence(raw, f"linear:{case['id']}")
    runtime_cell = deepcopy(_raw_dimensions(scenario)["runtime"])
    runtime_cell["evidence"] = [*cast(list[str], runtime_cell.get("evidence", [])), evidence_id]
    _raw_dimensions(scenario)["runtime"] = runtime_cell


def test_manifest_has_exact_inventory_status_matrix_and_registered_cases() -> None:
    manifest = load_manifest()

    assert manifest.schema_version == 1
    assert manifest.criteria_ref == "docs/architecture/dag/completeness-criteria.md"
    assert tuple((scenario.id, scenario.title) for scenario in manifest.scenarios) == EXPECTED_SCENARIOS
    assert tuple(scenario.ordinal for scenario in manifest.scenarios) == tuple(range(1, 16))
    assert tuple(scenario.id for scenario in manifest.scenarios) == tuple(EXPECTED_STATUS_MATRIX)
    for scenario in manifest.scenarios:
        assert tuple(scenario.dimensions) == EXPECTED_DIMENSIONS
        assert tuple(cell.status for cell in scenario.dimensions.values()) == EXPECTED_STATUS_MATRIX[scenario.id]
    assert tuple((scenario.id, case.id) for scenario, case in iter_harness_cases(manifest)) == (
        ("linear", "happy-path"),
        ("multiple-independent-sources", "independent-roots"),
        ("multi-source-queue-fan-in", "queued-fan-in"),
        ("conditional-routing", "two-way-gate"),
        ("fork-coalesce-policies", "require-all-nested"),
        ("checkpoint-deterministic-resume", "reopen-resume"),
    )
    assert manifest.verdict == "not_complete"


def test_manifest_pins_every_exact_current_assessment_evidence_record() -> None:
    manifest = load_manifest()

    assessment_evidence = tuple((reference.id, reference.locator) for reference in manifest.evidence if reference.kind == "pytest")
    harness_evidence = tuple(
        (reference.id, reference.locator, reference.stages) for reference in manifest.evidence if reference.kind == "harness"
    )
    assert assessment_evidence == EXPECTED_ASSESSMENT_EVIDENCE
    assert harness_evidence == EXPECTED_HARNESS_EVIDENCE
    assert len(manifest.evidence) == 57
    assert len(assessment_evidence) == 51
    assert len(harness_evidence) == 6
    assert len({reference.id for reference in manifest.evidence}) == 57
    assert len({reference.locator for reference in manifest.evidence}) == 57
    normalized_registry = json.dumps(
        [reference.model_dump(mode="json") for reference in manifest.evidence],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(normalized_registry).hexdigest() == EXPECTED_EVIDENCE_REGISTRY_SHA256


def test_registered_cases_and_harness_references_have_exact_atomic_parity() -> None:
    manifest = load_manifest()
    cases = tuple((scenario.id, case.model_dump(mode="json")) for scenario, case in iter_harness_cases(manifest))
    assert tuple((scenario_id, case["id"]) for scenario_id, case in cases) == (
        ("linear", "happy-path"),
        ("multiple-independent-sources", "independent-roots"),
        ("multi-source-queue-fan-in", "queued-fan-in"),
        ("conditional-routing", "two-way-gate"),
        ("fork-coalesce-policies", "require-all-nested"),
        ("checkpoint-deterministic-resume", "reopen-resume"),
    )
    normalized_cases = json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(normalized_cases).hexdigest() == EXPECTED_CASE_REGISTRY_SHA256
    linear_case = cases[0][1]
    assert linear_case["input_fixtures"] == {"primary": "linear/input.csv"}
    assert linear_case["output_artifacts"] == {"output": "output.jsonl"}
    assert linear_case["expected"]["kind"] == "exact"
    assert cases[-1][1]["expected"]["kind"] == "summary"
    assert all("input_fixture" not in case for _scenario_id, case in cases)

    referenced_cells = {
        reference.id: tuple(
            (scenario.id, dimension)
            for scenario in manifest.scenarios
            for dimension, cell in scenario.dimensions.items()
            if reference.id in cell.evidence
        )
        for reference in manifest.evidence
        if reference.kind == "harness"
    }
    assert referenced_cells == {
        "harness-linear-happy-path": (("linear", "runtime"), ("linear", "audit")),
        "harness-multiple-independent-sources-independent-roots": (
            ("multiple-independent-sources", "config"),
            ("multiple-independent-sources", "build"),
        ),
        "harness-multi-source-queue-fan-in-queued-fan-in": (
            ("multi-source-queue-fan-in", "config"),
            ("multi-source-queue-fan-in", "build"),
        ),
        "harness-conditional-routing-two-way-gate": (
            ("conditional-routing", "config"),
            ("conditional-routing", "build"),
        ),
        "harness-fork-coalesce-policies-require-all-nested": (
            ("fork-coalesce-policies", "config"),
            ("fork-coalesce-policies", "build"),
        ),
        "harness-checkpoint-deterministic-resume-reopen-resume": (
            ("checkpoint-deterministic-resume", "runtime"),
            ("checkpoint-deterministic-resume", "audit"),
            ("checkpoint-deterministic-resume", "recovery"),
        ),
    }


def _corpus_rows() -> list[PipelineRow]:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            FieldContract("id", "id", int, False, "inferred"),
            FieldContract("value", "value", int, False, "inferred"),
        ),
        locked=True,
    )
    return [
        PipelineRow({"id": 1, "value": 10}, contract),
        PipelineRow({"id": 2, "value": 20}, contract),
        PipelineRow({"id": 3, "value": 30}, contract),
    ]


def test_corpus_plugin_manager_exposes_builtins_and_custom_through_public_instantiation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = install_corpus_plugin_manager(monkeypatch)
    input_path = tmp_path / "input.csv"
    input_path.write_bytes(EXPECTED_INPUT_CSV)

    csv_source = manager.create_source(
        "csv",
        {
            "path": str(input_path),
            "on_validation_failure": "discard",
            "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
        },
    )
    passthrough = manager.create_transform("passthrough", {"schema": {"mode": "observed"}})
    json_sink = manager.create_sink(
        "json",
        {"path": str(tmp_path / "output.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
    )
    custom = manager.create_transform(
        "dag_corpus_fail_once_eof_batch",
        {"fault_marker_path": str(tmp_path / "fault.marker")},
    )

    assert (csv_source.name, passthrough.name, json_sink.name, custom.name) == (
        "csv",
        "passthrough",
        "json",
        "dag_corpus_fail_once_eof_batch",
    )
    registered_transform: object = manager.get_transform_by_name("dag_corpus_fail_once_eof_batch")
    assert registered_transform is CorpusFailOnceEOFBatchTransform
    assert manager_module.get_shared_plugin_manager() is manager


def test_corpus_transform_declares_exact_schema_and_runtime_contract() -> None:
    assert issubclass(CorpusInputSchema, PluginSchema)
    assert CorpusInputSchema.model_fields["id"].annotation is int
    assert CorpusInputSchema.model_fields["value"].annotation is int
    assert issubclass(CorpusOutputSchema, PluginSchema)
    assert CorpusOutputSchema.model_fields["value"].annotation is int
    assert CorpusOutputSchema.model_fields["count"].annotation is int
    assert CorpusFailOnceEOFBatchTransform.name == "dag_corpus_fail_once_eof_batch"
    assert CorpusFailOnceEOFBatchTransform.determinism is Determinism.DETERMINISTIC
    assert CorpusFailOnceEOFBatchTransform.input_schema is CorpusInputSchema
    assert CorpusFailOnceEOFBatchTransform.output_schema is CorpusOutputSchema
    assert CorpusFailOnceEOFBatchTransform.is_batch_aware is True
    assert CorpusFailOnceEOFBatchTransform.on_error == "discard"


def test_corpus_transform_scalar_call_buffers_the_same_row(tmp_path: Path) -> None:
    transform = CorpusFailOnceEOFBatchTransform({"fault_marker_path": str(tmp_path / "fault.marker")})
    row = _corpus_rows()[0]

    result = transform.process(row, object())

    assert result.row is row
    assert result.success_reason == {"action": "buffer"}
    assert not (tmp_path / "fault.marker").exists()


def test_corpus_transform_first_atomic_batch_crashes_then_fresh_instance_succeeds(tmp_path: Path) -> None:
    marker = tmp_path / "nested" / "fault.marker"
    rows = _corpus_rows()
    crashing = CorpusFailOnceEOFBatchTransform({"fault_marker_path": str(marker)})

    with pytest.raises(RuntimeError, match=r"^injected DAG corpus EOF flush crash$"):
        crashing.process(rows, object())

    assert marker.read_bytes() == b""
    fresh = CorpusFailOnceEOFBatchTransform({"fault_marker_path": str(marker)})
    result = fresh.process(rows, object())
    expected_contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            FieldContract("value", "value", int, False, "inferred"),
            FieldContract("count", "count", int, False, "inferred"),
        ),
        locked=True,
    )

    assert result.success_reason == {"action": "batch_sum"}
    assert result.row is not None
    assert result.row.to_dict() == {"value": 60, "count": 3}
    assert result.row.contract == expected_contract


@pytest.mark.parametrize("marker", [None, "", 0, False])
def test_corpus_transform_rejects_invalid_fault_marker_config(marker: object) -> None:
    with pytest.raises(ValueError, match=r"^fault_marker_path must be a non-empty string$"):
        CorpusFailOnceEOFBatchTransform({"fault_marker_path": marker})


def test_registered_fixture_bytes_and_production_config_loading_are_exact(tmp_path: Path) -> None:
    fixture_bytes = {
        "linear/happy-path.yaml": EXPECTED_HAPPY_PATH_YAML,
        "linear/input.csv": EXPECTED_INPUT_CSV,
        "multiple-independent-sources/independent-roots.yaml": EXPECTED_INDEPENDENT_ROOTS_YAML,
        "multiple-independent-sources/orders.csv": EXPECTED_ORDERS_CSV,
        "multiple-independent-sources/refunds.csv": EXPECTED_REFUNDS_CSV,
        "multi-source-queue-fan-in/queued-fan-in.yaml": EXPECTED_QUEUED_FAN_IN_YAML,
        "multi-source-queue-fan-in/orders.csv": EXPECTED_ORDERS_CSV,
        "multi-source-queue-fan-in/refunds.csv": EXPECTED_REFUNDS_CSV,
        "conditional-routing/two-way-gate.yaml": EXPECTED_TWO_WAY_GATE_YAML,
        "conditional-routing/input.csv": EXPECTED_INPUT_CSV,
        "fork-coalesce-policies/require-all-nested.yaml": EXPECTED_REQUIRE_ALL_NESTED_YAML,
        "fork-coalesce-policies/input.csv": EXPECTED_INPUT_CSV,
        "checkpoint-deterministic-resume/reopen-resume.yaml": EXPECTED_REOPEN_RESUME_YAML,
        "checkpoint-deterministic-resume/input.csv": EXPECTED_INPUT_CSV,
    }
    for relative_path, expected in fixture_bytes.items():
        assert resolve_fixture_path(relative_path).read_bytes() == expected

    manifest = load_manifest()
    assert {
        f"{scenario.id}:{case.id}": compute_fixture_sha256(case) for scenario, case in iter_harness_cases(manifest)
    } == EXPECTED_CASE_FIXTURE_SHA256

    substitutions = {
        "input_primary": str(resolve_fixture_path("linear/input.csv")),
        "output_output": str(tmp_path / "happy.jsonl"),
    }
    happy = load_settings_from_yaml_string(Template(EXPECTED_HAPPY_PATH_YAML.decode()).substitute(substitutions))
    assert happy.sources["primary"].plugin == "csv"
    assert happy.transforms[0].plugin == "passthrough"
    assert happy.sinks["output"].plugin == "json"

    substitutions.update(
        input_primary=str(resolve_fixture_path("checkpoint-deterministic-resume/input.csv")),
        output_output=str(tmp_path / "recovery.jsonl"),
        fault_marker=str(tmp_path / "fault.marker"),
    )
    recovery = load_settings_from_yaml_string(Template(EXPECTED_REOPEN_RESUME_YAML.decode()).substitute(substitutions))
    assert recovery.sources["primary"].on_success == "batch_in"
    assert recovery.aggregations[0].name == "eof_sum"
    assert recovery.aggregations[0].plugin == "dag_corpus_fail_once_eof_batch"
    assert recovery.aggregations[0].trigger.count == 100
    assert recovery.aggregations[0].options == {
        "schema": {"mode": "observed"},
        "fault_marker_path": str(tmp_path / "fault.marker"),
    }


@pytest.mark.parametrize(
    ("scenario_id", "case_id"),
    [
        ("linear", "happy-path"),
        ("multiple-independent-sources", "independent-roots"),
        ("multi-source-queue-fan-in", "queued-fan-in"),
        ("conditional-routing", "two-way-gate"),
        ("fork-coalesce-policies", "require-all-nested"),
        ("checkpoint-deterministic-resume", "reopen-resume"),
    ],
)
def test_registered_fixtures_cross_the_real_production_build_boundary(
    scenario_id: str,
    case_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_corpus_plugin_manager(monkeypatch)
    manifest = load_manifest()
    case = next(case for scenario, case in iter_harness_cases(manifest) if (scenario.id, case.id) == (scenario_id, case_id))
    rendered = render_settings(case, tmp_path).settings_yaml
    settings = load_settings_from_yaml_string(rendered)
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
    )
    graph.validate()
    graph.validate_edge_compatibility()

    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )

    assert config.sources == bundle.sources


def test_manifest_pytest_evidence_batch_collects_without_running_suites() -> None:
    manifest = load_manifest()
    # Fixed interpreter and repository-owned selectors; no shell is involved.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            *(reference.locator for reference in manifest.evidence if reference.kind == "pytest"),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_manifest_gap_ownership_and_not_applicable_reasons_follow_the_approved_rules() -> None:
    manifest = load_manifest()
    expected_not_applicable_reasons = {
        "row-union-interleave": "Row union has no supported construct, so post-build audit, recovery, and concurrency do not apply after configuration, build, contract, and runtime already fail.",
        "checkpoint-deterministic-resume": "Checkpoint/resume is a runtime lifecycle, not an authored topology.",
        "multi-worker-lease-reclaim-late-completion": "Worker multiplicity is deployment/runtime configuration, not DAG authoring.",
    }

    for scenario in manifest.scenarios:
        for dimension, cell in scenario.dimensions.items():
            if cell.status == "not_applicable":
                assert cell.reason == expected_not_applicable_reasons[scenario.id]
                continue
            if cell.status == "pass":
                continue
            if scenario.id == "row-union-interleave":
                expected_owner = "elspeth-a5b86149d4"
            elif scenario.id == "row-expansion-parent-child-recovery" and dimension == "recovery":
                expected_owner = "elspeth-7cdc4da434"
            elif dimension == "guided":
                expected_owner = "elspeth-7e2dd67275"
            elif dimension == "round_trip":
                expected_owner = "elspeth-7cf763da7c"
            elif dimension == "scale":
                expected_owner = "elspeth-cb1053fe46"
            else:
                expected_owner = "elspeth-ef29ef6ba4"
            assert cell.owner_issue == expected_owner
            assert cell.exit_gate is not None
            assert "corpus" in cell.exit_gate.lower()
            assert "pass" in cell.exit_gate.lower()


def test_row_expansion_delta_is_backed_by_repaired_cross_backend_evidence() -> None:
    manifest = load_manifest()
    references = {reference.id: reference for reference in manifest.evidence}
    unit = references["cardinality-identity-09"]
    postgres = references["cardinality-identity-10"]
    replay = references["cardinality-identity-11"]

    assert unit.locator == (
        "tests/unit/core/landscape/repository_integration/test_recorder_tokens.py"
        "::TestAtomicTokenOperations::test_expand_token_records_batch_parent_outcome_atomically"
    )
    assert unit.stages == ("runtime", "audit")
    assert postgres.locator == (
        "tests/testcontainer/core/test_token_outcome_atomicity_postgres.py"
        "::test_postgres_batch_expansion_claims_batch_once_under_contention"
    )
    assert postgres.stages == ("runtime", "audit")
    assert replay.locator == (
        "tests/unit/core/landscape/test_token_recording.py"
        "::TestExpandToken::test_batch_expansion_claim_is_scoped_to_batch_not_selected_parent"
    )
    assert replay.stages == ("runtime", "audit")

    scenario = next(item for item in manifest.scenarios if item.id == "row-expansion-parent-child-recovery")
    affected_dimensions: tuple[Dimension, ...] = ("contracts", "runtime", "audit", "recovery", "concurrency")
    assert {dimension: scenario.dimensions[dimension].evidence for dimension in affected_dimensions} == {
        "contracts": (
            "cardinality-identity-02",
            "cardinality-identity-03",
            "cardinality-identity-04",
            "cardinality-identity-05",
            "cardinality-identity-06",
            "cardinality-identity-07",
            "cardinality-identity-09",
            "cardinality-identity-10",
            "cardinality-identity-11",
        ),
        "runtime": (
            "cardinality-identity-04",
            "cardinality-identity-05",
            "cardinality-identity-06",
            "cardinality-identity-07",
            "cardinality-identity-09",
            "cardinality-identity-10",
            "cardinality-identity-11",
        ),
        "audit": (
            "cardinality-identity-02",
            "cardinality-identity-03",
            "cardinality-identity-04",
            "cardinality-identity-05",
            "cardinality-identity-07",
            "cardinality-identity-09",
            "cardinality-identity-10",
            "cardinality-identity-11",
        ),
        "recovery": ("cardinality-identity-07",),
        "concurrency": ("cardinality-identity-10",),
    }
    assert scenario.dimensions["recovery"].status == "partial"
    assert scenario.dimensions["recovery"].owner_issue == "elspeth-7cdc4da434"
    assert scenario.dimensions["recovery"].evidence == ("cardinality-identity-07",)
    assert scenario.dimensions["concurrency"].status == "partial"
    for dimension in ("contracts", "runtime", "audit", "concurrency"):
        assert scenario.dimensions[dimension].owner_issue == "elspeth-ef29ef6ba4"
    assert all(cell.owner_issue != "elspeth-a25e9c009e" for cell in scenario.dimensions.values())


def test_manifest_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, ["not", "a", "mapping"])
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_manifest(path)


@pytest.mark.parametrize(
    "schema_version",
    [None, 2, True, 1.0, "1"],
    ids=("missing", "wrong-integer", "boolean", "float", "string"),
)
def test_manifest_rejects_schema_version_that_is_not_exact_integer_one(
    tmp_path: Path,
    schema_version: object,
) -> None:
    raw = valid_manifest_dict()
    if schema_version is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = schema_version

    with pytest.raises(ValueError, match="schema_version must be exactly integer 1"):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("duplicate_key", "source"),
    [
        (
            "schema_version",
            "schema_version: 1\nschema_version: 1\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\n",
        ),
        (
            "id",
            "schema_version: 1\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\nevidence: []\nscenarios:\n  - id: linear\n    id: linear\n",
        ),
        (
            "config",
            "schema_version: 1\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\nevidence: []\nscenarios:\n  - id: linear\n    ordinal: 1\n    title: Linear\n    dimensions:\n      config: {}\n      config: {}\n",
        ),
    ],
    ids=("top-level", "scenario", "dimension"),
)
def test_manifest_rejects_duplicate_yaml_mapping_keys(
    tmp_path: Path,
    duplicate_key: str,
    source: str,
) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match=rf"duplicate YAML mapping key.*{duplicate_key}"):
        load_manifest(path)


def test_manifest_rejects_extra_keys(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    raw["verdict"] = "complete"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_scenario(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    scenarios = _raw_scenarios(raw)
    scenarios.append(deepcopy(scenarios[0]))
    with pytest.raises(ValueError, match=r"duplicate scenario id.*linear"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_missing_scenario(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_scenarios(raw).pop()
    with pytest.raises(ValueError, match="scenario IDs/order"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_wrong_scenario_id(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_scenarios(raw)[0]["id"] = "renamed-linear"
    with pytest.raises(ValueError, match="scenario IDs/order"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_reordered_scenarios(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    scenarios = _raw_scenarios(raw)
    scenarios[0], scenarios[1] = scenarios[1], scenarios[0]
    with pytest.raises(ValueError, match="scenario IDs/order"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_wrong_scenario_title(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_scenarios(raw)[0]["title"] = "Linear-ish"
    with pytest.raises(ValueError, match=r"title.*linear"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_wrong_scenario_ordinal(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_scenarios(raw)[0]["ordinal"] = 2
    with pytest.raises(ValueError, match=r"ordinal.*linear"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_missing_dimension(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_dimensions(_raw_scenarios(raw)[0]).pop("scale")
    with pytest.raises(ValueError, match=r"dimension keys/order.*linear"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_reordered_dimensions(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    scenario = _raw_scenarios(raw)[0]
    dimensions = _raw_dimensions(scenario)
    reordered = list(dimensions.items())
    reordered[0], reordered[1] = reordered[1], reordered[0]
    scenario["dimensions"] = dict(reordered)

    with pytest.raises(ValueError, match=r"dimension keys/order.*linear"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_invalid_dimension_key(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    dimensions = _raw_dimensions(_raw_scenarios(raw)[0])
    dimensions["unsupported"] = dimensions.pop("scale")
    with pytest.raises(ValidationError, match="unsupported"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_evidence_ids(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    evidence = _raw_evidence(raw)
    evidence.append(deepcopy(evidence[0]))
    with pytest.raises(ValueError, match="duplicate evidence id"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_unknown_evidence_reference(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_dimensions(_raw_scenarios(raw)[0])["config"]["evidence"] = ["missing-id"]
    with pytest.raises(ValueError, match=r"unknown evidence id.*missing-id"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_unreferenced_evidence_record(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw).append(
        {
            "id": "unreferenced-decision",
            "kind": "decision",
            "locator": "elspeth-ef29ef6ba4",
            "claim": "A valid declaration that no scenario cell references",
        }
    )

    with pytest.raises(ValueError, match=r"orphan evidence id.*unreferenced-decision"):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        ("document", "docs/architecture/dag/completeness-criteria.md"),
        ("decision", "elspeth-ef29ef6ba4"),
    ],
)
def test_manifest_rejects_pass_with_only_documentary_evidence(
    tmp_path: Path,
    kind: str,
    locator: str,
) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw).append(
        {
            "id": "non-executable",
            "kind": kind,
            "locator": locator,
            "claim": "Documents a claim without executing it",
        }
    )
    _raw_dimensions(_raw_scenarios(raw)[0])["config"]["evidence"] = ["non-executable"]
    with pytest.raises(ValueError, match=r"pass cell.*only document/decision evidence"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_pass_lifecycle_cell_without_matching_executable_stage(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    config_evidence = set(cast(list[str], _raw_dimensions(_raw_scenarios(raw)[0])["config"]["evidence"]))
    for evidence in _raw_evidence(raw):
        if evidence["id"] in config_evidence:
            evidence["stages"] = ["runtime"]

    with pytest.raises(ValueError, match=r"pass lifecycle cell linear\.config.*executable evidence declaring stage 'config'"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_harness_stages_beyond_registered_build_workflow(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    build_evidence = next(
        evidence for evidence in _raw_evidence(raw) if evidence["id"] == "harness-multiple-independent-sources-independent-roots"
    )
    build_evidence["stages"] = ["config", "build", "runtime"]

    with pytest.raises(
        ValueError,
        match=r"harness evidence.*independent-roots.*build workflow.*exact stages.*config.*build",
    ):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_harness_attached_beyond_its_workflow_even_with_other_executable_evidence(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    independent_sources = next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "multiple-independent-sources")
    _raw_dimensions(independent_sources)["runtime"]["evidence"] = [
        "harness-multiple-independent-sources-independent-roots",
        "runtime-disposition-drains",
    ]

    with pytest.raises(
        ValueError,
        match=r"harness evidence.*independent-roots.*multiple-independent-sources\.runtime.*stage 'runtime'",
    ):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize("dimension", ["concurrency", "guided", "scale"])
def test_manifest_rejects_build_harness_attached_to_non_lifecycle_dimension(tmp_path: Path, dimension: str) -> None:
    raw = valid_manifest_dict()
    independent_sources = next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "multiple-independent-sources")
    cell = deepcopy(_raw_dimensions(independent_sources)[dimension])
    cell["evidence"] = ["harness-multiple-independent-sources-independent-roots"]
    _raw_dimensions(independent_sources)[dimension] = cell

    with pytest.raises(
        ValueError,
        match=rf"harness evidence.*independent-roots.*multiple-independent-sources\.{dimension}.*build workflow.*validated lifecycle",
    ):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize("dimension", ["config", "build"])
def test_manifest_rejects_harness_attached_to_another_scenario(tmp_path: Path, dimension: str) -> None:
    raw = valid_manifest_dict()
    linear = next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "linear")
    cell = deepcopy(_raw_dimensions(linear)[dimension])
    cell["evidence"] = [
        *cast(list[str], cell.get("evidence", [])),
        "harness-multiple-independent-sources-independent-roots",
    ]
    _raw_dimensions(linear)[dimension] = cell

    with pytest.raises(
        ValueError,
        match=rf"harness evidence.*independent-roots.*locator scenario.*multiple-independent-sources.*linear\.{dimension}",
    ):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    case = _case_dict()
    _raw_scenarios(raw)[0]["cases"] = [case, deepcopy(case)]
    with pytest.raises(ValueError, match=r"duplicate case id.*linear:happy-path"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_unknown_harness_locator(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _add_harness_evidence(raw, "linear:unregistered")
    with pytest.raises(ValueError, match=r"unknown harness locator.*linear:unregistered"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_case_without_matching_harness_locator(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _remove_harness_evidence(raw, "linear:happy-path")
    _raw_scenarios(raw)[0]["cases"] = [_case_dict()]
    with pytest.raises(ValueError, match=r"harness case.*linear:happy-path.*matching evidence locator"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_registered_case_fixture_escape(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    case = _case_dict("escape")
    case["fixture"] = "../../outside.yaml"
    _register_linear_case(raw, case)

    with pytest.raises(ValueError, match=r"linear:escape.*escapes DAG scenario fixture root"):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("input_path", "error"),
    [
        ("../outside.csv", "escapes DAG scenario fixture root"),
        ("linear/missing.csv", "DAG scenario fixture does not exist"),
    ],
    ids=("escape", "missing"),
)
def test_manifest_rejects_registered_case_invalid_input_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_path: str,
    error: str,
) -> None:
    fixture_root = tmp_path / "fixtures"
    linear_root = fixture_root / "linear"
    linear_root.mkdir(parents=True)
    (linear_root / "happy-path.yaml").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)
    raw = valid_manifest_dict()
    case = _case_dict("invalid-input")
    case["input_fixtures"] = {"primary": input_path}
    _register_linear_case(raw, case)

    with pytest.raises(ValueError, match=rf"linear:invalid-input.*{error}"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_malformed_pytest_locator(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw)[0]["locator"] = "docs/architecture/dag/README.md"
    with pytest.raises(ValueError, match="repository-relative pytest locator under tests"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_missing_pytest_file(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw)[0]["locator"] = "tests/unit/test_does_not_exist.py"
    with pytest.raises(ValueError, match="pytest locator file does not exist"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_missing_pytest_node(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw)[0]["locator"] = "tests/unit/core/dag/test_builder_validation.py::test_missing_node"
    with pytest.raises(ValueError, match=r"does not select pytest node.*test_missing_node"):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    "locator",
    [
        "tests/unit/architecture/test_dag_scenario_corpus_contract.py::_reference",
        "tests/unit/core/dag/test_builder_validation.py::_BuilderValidationMockSource",
    ],
    ids=("private-helper-function", "private-helper-class"),
)
def test_manifest_rejects_non_collectable_pytest_helper(tmp_path: Path, locator: str) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw)[0]["locator"] = locator

    with pytest.raises(ValueError, match="not a pytest-collectable test node"):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_unverified_parameter_specific_pytest_locator(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    _raw_evidence(raw)[0]["locator"] = (
        "tests/unit/core/test_multi_source_foundation.py::test_plural_sources_are_canonical_and_stable_named[does-not-exist]"
    )

    with pytest.raises(ValueError, match="parameter-specific pytest locator is not supported"):
        load_manifest(write_manifest(tmp_path, raw))


def test_resolve_fixture_path_rejects_containment_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (tmp_path / "outside.yaml").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="escapes DAG scenario fixture root"):
        resolve_fixture_path("../outside.yaml")


@pytest.mark.parametrize("nested", [False, True])
def test_resolve_fixture_path_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    real_directory = fixture_root / "real"
    real_directory.mkdir()
    (real_directory / "fixture.yaml").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)
    if nested:
        (fixture_root / "linked").symlink_to(real_directory, target_is_directory=True)
        relative_path = "linked/fixture.yaml"
    else:
        (fixture_root / "linked.yaml").symlink_to(real_directory / "fixture.yaml")
        relative_path = "linked.yaml"

    with pytest.raises(ValueError, match="must not be a symlink"):
        resolve_fixture_path(relative_path)


def test_resolve_fixture_path_rejects_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="does not exist"):
        resolve_fixture_path("missing.yaml")


def test_resolve_fixture_path_rejects_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    directory = fixture_root / "directory"
    directory.mkdir()
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(ValueError, match="must be a regular file"):
        resolve_fixture_path("directory")


def test_resolve_fixture_path_accepts_contained_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    fixture = fixture_root / "scenario.yaml"
    fixture.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(loader_module, "FIXTURE_ROOT", fixture_root)

    assert resolve_fixture_path("scenario.yaml") == fixture.resolve()


def test_iter_harness_cases_flattens_constructed_manifest_in_scenario_order() -> None:
    first_case = _case()
    second_case = first_case.model_copy(update={"id": "second"})
    first_scenario = _scenario(EvidenceCell(status="pass", evidence=("evidence-1",))).model_copy(
        update={"cases": (first_case, second_case)}
    )
    second_scenario = first_scenario.model_copy(update={"id": "second-scenario", "cases": (second_case,)})
    manifest = ScenarioManifest(
        schema_version=1,
        criteria_ref="docs/architecture/dag/completeness-criteria.md",
        evidence=(_reference(),),
        scenarios=(first_scenario, second_scenario),
    )

    assert tuple((scenario.id, case.id) for scenario, case in iter_harness_cases(manifest)) == (
        ("linear", "happy-path"),
        ("linear", "second"),
        ("second-scenario", "second"),
    )
