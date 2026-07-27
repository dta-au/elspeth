from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import Any, cast, get_args
from urllib.parse import unquote, urlsplit

import pytest
import tests.fixtures.dag_scenario_corpus.harness as corpus_harness
import tests.fixtures.dag_scenario_corpus.loader as loader_module
import tests.fixtures.dag_scenario_corpus.schema as corpus_schema
import yaml
from markdown_it import MarkdownIt
from pydantic import BaseModel, ValidationError
from tests.fixtures.dag_scenario_corpus.harness import compute_fixture_sha256, render_settings, semantic_runtime_projection
from tests.fixtures.dag_scenario_corpus.loader import (
    DEFAULT_MANIFEST_PATH,
    REPOSITORY_ROOT,
    iter_harness_cases,
    load_manifest,
    resolve_fixture_path,
)
from tests.fixtures.dag_scenario_corpus.plugins import (
    CorpusAlwaysErrorTransform,
    CorpusAlwaysFailSink,
    CorpusBranchLossTransform,
    CorpusEOFBatchSumTransform,
    CorpusFailOnceEOFBatchTransform,
    CorpusInputSchema,
    CorpusOutputSchema,
    CorpusRetryOnceTransform,
    install_corpus_plugin_manager,
)
from tests.fixtures.dag_scenario_corpus.schema import (
    EXPECTED_DIMENSIONS,
    EXPECTED_SCENARIOS,
    AggregationEOFRecoveryEvidence,
    AuditEvidence,
    AuditRecordCount,
    BuildExpectation,
    CellStatus,
    ConfigEvidence,
    Dimension,
    EvidenceCell,
    EvidenceKind,
    EvidenceReference,
    ExpansionChildEnqueueRecoveryEvidence,
    GraphEvidence,
    GraphNodeTypeCount,
    HarnessCaseSpec,
    ParallelSinkFinalizationRecoveryEvidence,
    PendingSinkRedriveRecoveryEvidence,
    RecoveryEvidence,
    RecoveryKind,
    RunExpectation,
    RuntimeEvidence,
    ScenarioManifest,
    ScenarioRunEvidence,
    ScenarioSpec,
    SinkBoundaryEffectProjection,
    SinkBoundaryRecoveryEvidence,
    SinkBoundaryWorkProjection,
    Stage,
    SummaryRunExpectation,
    Workflow,
)

from elspeth.contracts import Determinism, PipelineRow, PluginSchema, RunStatus
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
    "linear": ("pass", "pass", "pass", "pass", "pass", "partial", "unknown", "pass", "partial", "partial", "partial"),
    "multiple-independent-sources": (
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
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
        "pass",
        "pass",
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
        "pass",
        "pass",
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
        "pass",
        "pass",
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
        "pass",
        "pass",
        "pass",
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
        "pass",
        "pass",
        "pass",
        "unknown",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "parallel-coalesces": (
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
        "partial",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "aggregation-immutable-batch": (
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
        "unknown",
        "pass",
        "fail",
        "unknown",
        "unknown",
    ),
    "row-expansion-parent-child-recovery": (
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
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
        "pass",
        "pass",
        "pass",
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
        "pass",
        "pass",
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
    "conditional-routing-destination-negatives": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b1_conditional_routing_rejects_missing_boolean_gate_destination",
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b1_conditional_routing_rejects_invalid_gate_destination_during_production_build",
    ),
    "coalesce-policy-merge-contract-matrix": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_coalesce_full_matrix_declares_exact_contracts",
    ),
    "coalesce-union-policy-identity": (
        "tests/unit/core/test_dag.py"
        "::TestCoalesceNodes::test_union_collision_policy_binds_coalesce_config_and_identity_with_default_compatibility",
    ),
    "sequential-nested-second-merge-schema-negative": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_sequential_nested_rejects_incompatible_second_merge_schema",
    ),
    "parallel-coalesces-branch-ownership-negative": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_parallel_coalesces_reject_cross_claimed_branch",
    ),
    "composed-coalesces-exact-contracts": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_composed_coalesces_execute_exact_semantic_production_oracles",
    ),
    "composed-coalesces-repeat-run-boundary": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_composed_coalesces_repeat_run_semantic_boundary",
    ),
    "composed-coalesces-canonical-identity": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b2_composed_coalesces_raw_identity_converges_across_equivalent_runs",
    ),
    "b3-stateful-runtime-exact-contracts": (
        "tests/integration/core/dag/test_dag_scenario_production_path.py::test_b3_stateful_runtime_cases_pin_exact_contracts",
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
EXPECTED_EVIDENCE_REGISTRY_SHA256 = "9da6b219ef4d0969b81f17b7a5e402067bfb661fd258dbf948ceb8f7e86ecf21"
EXPECTED_CASE_REGISTRY_SHA256 = "7f604fbbe23d0db4d1e22e355888935d8bcf6b8c28d226bb23e3b8319b6ba8ca"
B2_COALESCE_POSITIVE_CASE_IDS = (
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
B2_COALESCE_NEGATIVE_CASE_IDS = (
    "require-all-lost-c",
    "quorum-impossible-lost-c",
    "best-effort-all-lost",
    "first-all-lost",
    "union-collision-last-wins",
    "union-collision-first-wins",
    "union-collision-fail",
)
B2_COALESCE_CASE_IDS = B2_COALESCE_POSITIVE_CASE_IDS + B2_COALESCE_NEGATIVE_CASE_IDS
EXPECTED_CASE_FIXTURE_SHA256 = {
    "linear:happy-path": "12adb2d878a143756243fb56138b50b1e86ab21c6b3f439c2c79dd037ddf96e4",
    "multiple-independent-sources:independent-roots": "10b5d812e415dddd67d088fc771da3d4623d75fc3d2e4041562a4e4ae02741c0",
    "multi-source-queue-fan-in:queued-fan-in": "ccff919ce91062633679fcbe577194b4ce3c852a90c1f8f97622ac371b377c4e",
    "conditional-routing:two-way-gate": "e8b931a998d752ca7a461abb7b41edeb3f3251542d4349ebc66e9f450c316720",
    "conditional-routing:error-route-and-discard": "27dbf1f2d1908a6f6f3df8166bff152e56977d93ffc4061c91c48871c26a282b",
    "fork-multiple-terminals-partial-failure:one-terminal-fails": "e0505f84e778047f4d68a47e27f442d82824b898cf58fe5cb084842cfbbdb925",
    "fork-coalesce-policies:require-all-union": "aeb887b17d3f6acc17e1fe71e24d1bad8314f6dbe34649e69930d09ff7b31404",
    "fork-coalesce-policies:require-all-nested": "98048b43b03a9870f117c8b2705ccd592e9b9c26329cfcc3891554c9b5778545",
    "fork-coalesce-policies:require-all-select": "254560eaa7db76b9f3303a7dc52cd362421bd6dc0277c95fa6f629445284e3df",
    "fork-coalesce-policies:first-union": "f032d03c31c3c02366cbbbbff3fdd37c6d70739954b9487f05e5945d974b1f91",
    "fork-coalesce-policies:first-nested": "1e46bd2315844ddc82e8af6063c20c4b9b72cb55c5c363049961bd46a3c06e3d",
    "fork-coalesce-policies:first-select": "9403ceeeb06be9888a744790d008b34508519df1d5c9b53af1dd9e34b796c310",
    "fork-coalesce-policies:quorum-union-lost-c": "5ee4b0a931384f6b5d154b58d25b072a2da87e8efb930e64b63f807d9f11f43c",
    "fork-coalesce-policies:quorum-nested-lost-c": "5de0b560220f2a164885154339cfc4077e7a964eb5f88a0c6be0b075eabfaf24",
    "fork-coalesce-policies:quorum-select-lost-c": "0e8e60c5b080e26dbcc62a8e4c13d8ad4573b1aa710c6836a2355e0d0160997a",
    "fork-coalesce-policies:best-effort-union-lost-c": "5a3fa6474d61f68f69cc95ad42cec2afac1c4b7e77c61c56fb1434af47d80805",
    "fork-coalesce-policies:best-effort-nested-lost-c": "c71d40f53eed56b2c71a68a613001174dd47cb0d9301114e8de98b14aab4043c",
    "fork-coalesce-policies:best-effort-select-lost-c": "4dce6f4213b8e9e600d687fd54d023744369d52e42c5e796c807e02474846af7",
    "fork-coalesce-policies:require-all-lost-c": "afc049b9cef368104d733c9cd26ad2d380948280eb96660fe80defdcb690c6f1",
    "fork-coalesce-policies:quorum-impossible-lost-c": "287dd6b0c9f4f0fea2e2d6ac5c1736663d4ec254278057eb2312db30718bbed2",
    "fork-coalesce-policies:best-effort-all-lost": "d8d42b88e86263c89cf48bbca8f27bcd02dc3f9a54244726ca53165afdf8ad14",
    "fork-coalesce-policies:first-all-lost": "36d4b7a025847dd3a5ad0f7c066e9f260e7a16c1267a3010f329995ce730fb1e",
    "fork-coalesce-policies:union-collision-last-wins": "986df56cc9ca6ceeab7ccd5472f0c5605e296d48054112487d72b05174d2a6dd",
    "fork-coalesce-policies:union-collision-first-wins": "8a20e5eceb01859427ac5e60d0e370f8ba100a7b9ce0903b2ca6e28f288073c7",
    "fork-coalesce-policies:union-collision-fail": "973269df09a38f4beabc778c2b06365a10363444229530974e71888f98a4d57f",
    "sequential-nested-fork-coalesce:two-sequential-require-all": "0a2ddc91942fe2a2466bfe1d7f486d8915c7b48e149b286c7a4c5eddcc52347e",
    "parallel-coalesces:two-parallel-require-all": "83e6e7edd9f34379d23a1f9b267b49d66524a6ce61c3e96b6b831046260cdfe2",
    "parallel-coalesces:resume-after-left-finalize": "83e6e7edd9f34379d23a1f9b267b49d66524a6ce61c3e96b6b831046260cdfe2",
    "aggregation-immutable-batch:eof-immutable-membership": "0a6a82b9fbe15356ccf0437bf34b72e3324b6884b8990752c05943e0179fb9cb",
    "aggregation-immutable-batch:resume-after-eof-flush-fault": "c72db99d6e9394db19beaa46770fbdd67ef86ae5b0364bf83094b01bd33945d8",
    "row-expansion-parent-child-recovery:json-explode-parent-child": "bf40f9a9fd913518566c36bd27a0530d6edaed40dde67b69642eabacc48716ed",
    "row-expansion-parent-child-recovery:resume-after-child-enqueue": "ebd85363be5af5e19acaa2af087ef5dddc10693fe7ac0674ff960d2d4f94a8e6",
    "retry-quarantine-discard-routed-errors:retry-then-success": "add6f84b856bf06915c6275a005bfb4aef5ad50068f7c93038b6b7d99970ab90",
    "retry-quarantine-discard-routed-errors:source-quarantine-routed": "286d04abef045d70a846b65bfc348792ff6242aff237451f30050565d8e5e639",
    "retry-quarantine-discard-routed-errors:transform-discard": "fb4d0c91d4612e6dcb0d9903f097db8b3e50310386811ddaee15d1749de23948",
    "retry-quarantine-discard-routed-errors:transform-error-route": "d4321f033f305d215563d2bfb62cb190df8a3eff87e10d8ac9805e9e9b45ca71",
    "sink-write-pending-redrive:write-once": "e8344036a8baf85bba035264683e47f3502d17336db55bef5174c87d468577de",
    "sink-write-pending-redrive:pending-redrive-reopen": "e8344036a8baf85bba035264683e47f3502d17336db55bef5174c87d468577de",
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
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-multi-source-queue-fan-in-queued-fan-in",
        "multi-source-queue-fan-in:queued-fan-in",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-conditional-routing-two-way-gate",
        "conditional-routing:two-way-gate",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-conditional-routing-error-route-and-discard",
        "conditional-routing:error-route-and-discard",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-fork-multiple-terminals-partial-failure-one-terminal-fails",
        "fork-multiple-terminals-partial-failure:one-terminal-fails",
        ("config", "build", "runtime", "audit"),
    ),
    *(
        (
            f"harness-fork-coalesce-policies-{case_id}",
            f"fork-coalesce-policies:{case_id}",
            (("config", "build", "runtime", "audit") if case_id == "union-collision-fail" else ("config", "build", "runtime")),
        )
        for case_id in B2_COALESCE_CASE_IDS
    ),
    (
        "harness-sequential-nested-fork-coalesce-two-sequential-require-all",
        "sequential-nested-fork-coalesce:two-sequential-require-all",
        ("config", "build", "runtime"),
    ),
    (
        "harness-parallel-coalesces-two-parallel-require-all",
        "parallel-coalesces:two-parallel-require-all",
        ("config", "build", "runtime"),
    ),
    (
        "harness-parallel-coalesces-resume-after-left-finalize",
        "parallel-coalesces:resume-after-left-finalize",
        ("config", "build", "runtime", "audit", "recovery"),
    ),
    (
        "harness-aggregation-immutable-batch-eof-immutable-membership",
        "aggregation-immutable-batch:eof-immutable-membership",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-aggregation-immutable-batch-resume-after-eof-flush-fault",
        "aggregation-immutable-batch:resume-after-eof-flush-fault",
        ("config", "build", "runtime", "audit", "recovery"),
    ),
    (
        "harness-row-expansion-parent-child-recovery-json-explode-parent-child",
        "row-expansion-parent-child-recovery:json-explode-parent-child",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-row-expansion-parent-child-recovery-resume-after-child-enqueue",
        "row-expansion-parent-child-recovery:resume-after-child-enqueue",
        ("config", "build", "runtime", "audit", "recovery"),
    ),
    *(
        (
            f"harness-retry-quarantine-discard-routed-errors-{case_id}",
            f"retry-quarantine-discard-routed-errors:{case_id}",
            ("config", "build", "runtime", "audit"),
        )
        for case_id in ("retry-then-success", "source-quarantine-routed", "transform-discard", "transform-error-route")
    ),
    (
        "harness-sink-write-pending-redrive-write-once",
        "sink-write-pending-redrive:write-once",
        ("config", "build", "runtime", "audit"),
    ),
    (
        "harness-sink-write-pending-redrive-pending-redrive-reopen",
        "sink-write-pending-redrive:pending-redrive-reopen",
        ("config", "build", "runtime", "audit", "recovery"),
    ),
)

EXPECTED_INPUT_CSV = b"id,value\n1,10\n2,20\n3,30\n"
EXPECTED_ORDERS_CSV = b"id,value\n1,10\n2,20\n3,30\n"
EXPECTED_REFUNDS_CSV = b"id,value\n101,-5\n102,-10\n103,-15\n"
EXPECTED_SEQUENTIAL_COALESCE_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: first_fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
concurrency:
  max_workers: 1
gates:
  - name: first_fork
    input: first_fork_input
    condition: "True"
    routes: {"true": fork, "false": output}
    fork_to: [branch_a1, branch_a2]
  - name: second_fork
    input: merge_a
    condition: "True"
    routes: {"true": fork, "false": output}
    fork_to: [branch_b1, branch_b2]
coalesce:
  - name: merge_a
    branches: {branch_a1: branch_a1, branch_a2: branch_a2}
    policy: require_all
    merge: nested
  - name: merge_b
    branches: {branch_b1: branch_b1, branch_b2: branch_b2}
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
EXPECTED_PARALLEL_COALESCE_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
concurrency:
  max_workers: 1
gates:
  - name: parallel_fork
    input: fork_input
    condition: "True"
    routes: {"true": fork, "false": left}
    fork_to: [left_a, left_b, right_a, right_b]
coalesce:
  - name: merge_left
    branches: {left_a: left_a, left_b: left_b}
    policy: require_all
    merge: nested
    on_success: left
  - name: merge_right
    branches: {right_a: right_a, right_b: right_b}
    policy: require_all
    merge: nested
    on_success: right
sinks:
  left:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_left}
      format: jsonl
      schema: {mode: observed}
  right:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_right}
      format: jsonl
      schema: {mode: observed}
"""

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

EXPECTED_ERROR_ROUTE_AND_DISCARD_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: routing
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
gates:
  - name: select_failure_policy
    input: routing
    condition: "row['value'] >= 20"
    routes: {"true": routed_error, "false": discard}
transforms:
  - name: fail_selected
    plugin: dag_corpus_always_error
    input: routed_error
    on_success: errors
    on_error: errors
    options:
      schema: {mode: observed}
sinks:
  errors:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_errors}
      format: jsonl
      schema: {mode: observed}
"""

EXPECTED_ONE_TERMINAL_FAILS_YAML = b"""sources:
  primary:
    plugin: csv
    on_success: fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
gates:
  - name: fork_terminals
    input: fork_input
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [failing, survivor]
sinks:
  failing:
    plugin: dag_corpus_always_fail_sink
    on_write_failure: discard
    options:
      path: ${output_failing}
      schema: {mode: observed}
  survivor:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_survivor}
      format: jsonl
      schema: {mode: observed}
"""


def _expected_coalesce_matrix_yaml(case_id: str) -> bytes:
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
    policy_options = f"    policy: {policy}\n"
    if policy == "quorum":
        policy_options += "    quorum_count: 2\n"
    if policy == "best_effort":
        policy_options += "    timeout_seconds: 60\n"
    policy_options += f"    merge: {merge}\n"
    if merge == "select":
        policy_options += "    select_branch: path_a\n"

    template = """sources:
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
    fork_to: [path_a, path_c, path_b]
transforms:
  - name: mark_a
    plugin: value_transform
    input: path_a
    on_success: merge_a
    on_error: discard
    options:
      schema: {mode: observed}
      operations: [{target: branch_marker, expression: "'a'"}]
  - name: __PATH_C_NAME__
    plugin: __PATH_C_PLUGIN__
    input: path_c
    on_success: merge_c
    on_error: discard
    options:
      schema: {mode: observed}
__PATH_C_OPERATION__
  - name: mark_b
    plugin: value_transform
    input: path_b
    on_success: merge_b
    on_error: discard
    options:
      schema: {mode: observed}
      operations: [{target: branch_marker, expression: "'b'"}]
coalesce:
  - name: merge_paths
    branches: {path_a: merge_a, path_b: merge_b, path_c: merge_c}
__POLICY_OPTIONS__
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
    return (
        template.replace("__PATH_C_NAME__", "lose_c" if loses_path_c else "mark_c")
        .replace("__PATH_C_PLUGIN__", "dag_corpus_branch_loss" if loses_path_c else "value_transform")
        .replace(
            "__PATH_C_OPERATION__\n",
            "" if loses_path_c else "      operations: [{target: branch_marker, expression: \"'c'\"}]\n",
        )
        .replace("__POLICY_OPTIONS__\n", policy_options)
        .encode()
    )


EXPECTED_COALESCE_MATRIX_YAMLS = {case_id: _expected_coalesce_matrix_yaml(case_id) for case_id in B2_COALESCE_POSITIVE_CASE_IDS}


def _expected_all_lost_coalesce_yaml(policy: str) -> bytes:
    policy_options = f"    policy: {policy}\n"
    if policy == "best_effort":
        policy_options += "    timeout_seconds: 60\n"
    policy_options += "    merge: nested\n"
    template = """sources:
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
    fork_to: [path_a, path_c, path_b]
transforms:
  - name: lose_a
    plugin: dag_corpus_always_error
    input: path_a
    on_success: merge_a
    on_error: discard
    options:
      schema: {mode: observed}
  - name: lose_c
    plugin: dag_corpus_always_error
    input: path_c
    on_success: merge_c
    on_error: discard
    options:
      schema: {mode: observed}
  - name: lose_b
    plugin: dag_corpus_always_error
    input: path_b
    on_success: merge_b
    on_error: discard
    options:
      schema: {mode: observed}
coalesce:
  - name: merge_paths
    branches: {path_a: merge_a, path_b: merge_b, path_c: merge_c}
__POLICY_OPTIONS__
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
    return template.replace("__POLICY_OPTIONS__\n", policy_options).encode()


_EXPECTED_PARTIAL_LOSS_NESTED = _expected_coalesce_matrix_yaml("quorum-nested-lost-c")
_EXPECTED_COLLISION_UNION = _expected_coalesce_matrix_yaml("require-all-union")
EXPECTED_COALESCE_NEGATIVE_YAMLS = {
    "require-all-lost-c": _EXPECTED_PARTIAL_LOSS_NESTED.replace(
        b"    policy: quorum\n    quorum_count: 2\n",
        b"    policy: require_all\n",
    ),
    "quorum-impossible-lost-c": _EXPECTED_PARTIAL_LOSS_NESTED.replace(b"    quorum_count: 2\n", b"    quorum_count: 3\n"),
    "best-effort-all-lost": _expected_all_lost_coalesce_yaml("best_effort"),
    "first-all-lost": _expected_all_lost_coalesce_yaml("first"),
    "union-collision-last-wins": _EXPECTED_COLLISION_UNION.replace(
        b"    merge: union\n",
        b"    merge: union\n    union_collision_policy: last_wins\n",
    ),
    "union-collision-first-wins": _EXPECTED_COLLISION_UNION.replace(
        b"    merge: union\n",
        b"    merge: union\n    union_collision_policy: first_wins\n",
    ),
    "union-collision-fail": _EXPECTED_COLLISION_UNION.replace(
        b"    merge: union\n",
        b"    merge: union\n    union_collision_policy: fail\n",
    ),
}
EXPECTED_COALESCE_YAMLS = EXPECTED_COALESCE_MATRIX_YAMLS | EXPECTED_COALESCE_NEGATIVE_YAMLS


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


def test_recovery_workflow_requires_a_closed_recovery_kind() -> None:
    values = _case().model_dump(mode="json")
    values["workflow"] = "recovery"
    with pytest.raises(ValidationError, match="recovery workflow requires recovery_kind"):
        HarnessCaseSpec.model_validate(values)

    values["recovery_kind"] = "parallel_sink_finalization"
    assert HarnessCaseSpec.model_validate(values).recovery_kind == "parallel_sink_finalization"


def test_non_recovery_workflow_forbids_recovery_kind() -> None:
    values = _case().model_dump(mode="json")
    values["recovery_kind"] = "eof_aggregation"

    with pytest.raises(ValidationError, match="recovery_kind is valid only for the recovery workflow"):
        HarnessCaseSpec.model_validate(values)


def test_sink_boundary_recovery_requires_a_closed_production_fault_declaration() -> None:
    values = _case().model_dump(mode="json")
    values.update(
        workflow="recovery",
        recovery_kind="sink_boundary",
        recovery_fault={
            "kind": "sink_effect",
            "seam": "before_effect",
            "sink_name": "output",
            "occurrence": 1,
        },
    )

    case = HarnessCaseSpec.model_validate(values)

    assert case.recovery_fault is not None
    assert case.recovery_fault.model_dump(mode="json") == {
        "kind": "sink_effect",
        "seam": "before_effect",
        "sink_name": "output",
        "occurrence": 1,
    }
    assert get_args(corpus_schema.RecoveryFaultKind) == ("sink_effect",)
    assert get_args(corpus_schema.RecoveryFaultSeam) == ("before_effect",)

    missing = deepcopy(values)
    missing["recovery_fault"] = None
    with pytest.raises(ValidationError, match="sink-boundary recovery requires recovery_fault"):
        HarnessCaseSpec.model_validate(missing)

    wrong_workflow = deepcopy(values)
    wrong_workflow["recovery_kind"] = "pending_sink_redrive"
    with pytest.raises(ValidationError, match="recovery_fault is valid only for sink-boundary recovery"):
        HarnessCaseSpec.model_validate(wrong_workflow)

    for field, replacement in (
        ("kind", "checkpoint_eof"),
        ("seam", "eof_flush_before_transform_result"),
        ("occurrence", 2),
    ):
        invalid = deepcopy(values)
        cast(dict[str, object], invalid["recovery_fault"])[field] = replacement
        with pytest.raises(ValidationError):
            HarnessCaseSpec.model_validate(invalid)


def test_response_lost_oracle_rejects_jointly_corrupted_semantic_witness() -> None:
    corrupted_evidence = json.dumps(
        {"classification": "plausible_but_not_response_lost"},
        sort_keys=True,
        separators=(",", ":"),
    )
    corrupted_hash = hashlib.sha256(corrupted_evidence.encode()).hexdigest()
    request_hash = "1" * 64

    with pytest.raises(AssertionError, match="response-lost semantic evidence"):
        corpus_harness._validate_durable_sink_effect_attempt_call_material(
            effect_id="2" * 64,
            attempt={
                "_evidence_json": corrupted_evidence,
                "evidence_hash": corrupted_hash,
                "request_hash": request_hash,
                "state": "response_lost",
            },
            call={
                "error_json": corrupted_evidence,
                "request_hash": request_hash,
                "response_hash": None,
                "status": "error",
            },
        )


def test_generic_recovery_terminal_status_contract_accepts_completed_with_failures() -> None:
    corpus_harness._assert_expected_terminal_run_status(
        actual_status=RunStatus.COMPLETED_WITH_FAILURES,
        expected_status=RunStatus.COMPLETED_WITH_FAILURES,
    )

    with pytest.raises(AssertionError) as exc_info:
        corpus_harness._assert_expected_terminal_run_status(
            actual_status=RunStatus.COMPLETED,
            expected_status=RunStatus.COMPLETED_WITH_FAILURES,
        )
    assert str(exc_info.value) == (
        "DAG recovery corpus expected terminal run status 'completed_with_failures', but persisted <RunStatus.COMPLETED: 'completed'>"
    )


@pytest.mark.parametrize(
    ("mutation", "rejection_field"),
    (
        ("noncanonical-evidence", "response-lost semantic evidence"),
        ("call-status", "response-lost call.status"),
        ("response-hash", "response-lost call.response_hash"),
        ("error-json", "response-lost call.error_json"),
    ),
)
def test_response_lost_oracle_rejects_independent_material_drift(
    mutation: str,
    rejection_field: str,
) -> None:
    canonical_evidence = '{"classification":"response_lost"}'
    request_hash = "1" * 64
    attempt: dict[str, object] = {
        "_evidence_json": canonical_evidence,
        "evidence_hash": hashlib.sha256(canonical_evidence.encode()).hexdigest(),
        "request_hash": request_hash,
        "state": "response_lost",
    }
    call: dict[str, object] = {
        "error_json": canonical_evidence,
        "request_hash": request_hash,
        "response_hash": None,
        "status": "error",
    }
    if mutation == "noncanonical-evidence":
        noncanonical_evidence = '{ "classification": "response_lost" }'
        attempt["_evidence_json"] = noncanonical_evidence
        attempt["evidence_hash"] = hashlib.sha256(noncanonical_evidence.encode()).hexdigest()
        call["error_json"] = noncanonical_evidence
    elif mutation == "call-status":
        call["status"] = "success"
    elif mutation == "response-hash":
        call["response_hash"] = "3" * 64
    else:
        call["error_json"] = '{"classification":"different"}'

    with pytest.raises(AssertionError) as exc_info:
        corpus_harness._validate_durable_sink_effect_attempt_call_material(
            effect_id="2" * 64,
            attempt=attempt,
            call=call,
        )
    assert str(exc_info.value) == (
        f"DAG corpus durable sink_effect_attempt integrity: {rejection_field} differs from authoritative material"
    )


@pytest.mark.parametrize(
    ("location", "field", "value", "qualified_field"),
    (
        ("effect", "artifact_id", None, "effect.artifact_id"),
        ("effect", "effect_id", 17, "effect.effect_id"),
        ("effect", "sink_node_id", "", "effect.sink_node_id"),
        ("member", "effect_id", None, "member.effect_id"),
        ("member", "token_id", None, "member.token_id"),
        ("member", "row_id", None, "member.row_id"),
    ),
)
def test_sink_boundary_effect_projection_rejects_non_text_sql_identity(
    location: str,
    field: str,
    value: object,
    qualified_field: str,
) -> None:
    effects: list[dict[str, object]] = [
        {
            "effect_id": "effect-1",
            "sink_node_id": "sink-output",
            "artifact_id": "artifact-1",
            "state": "in_flight",
        }
    ]
    members: list[dict[str, object]] = [
        {
            "effect_id": "effect-1",
            "token_id": "token-1",
            "row_id": "row-1",
        }
    ]
    target = effects[0] if location == "effect" else members[0]
    target[field] = value

    with pytest.raises(AssertionError) as exc_info:
        corpus_harness._sink_boundary_effect_projection(
            tuple(effects),
            tuple(members),
            sink_names_by_node_id={"sink-output": "output"},
        )
    assert str(exc_info.value) == (f"sink-boundary recovery {qualified_field} must be non-empty SQL text, got {value!r}")


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
        ("accepted", corpus_schema.OutputArtifactExpectation(filename="accepted.jsonl", presence="required")),
        ("rejected", corpus_schema.OutputArtifactExpectation(filename="rejected.jsonl", presence="required")),
    )
    assert case.model_dump(mode="json")["output_artifacts"] == {
        "accepted": {"filename": "accepted.jsonl", "presence": "required"},
        "rejected": {"filename": "rejected.jsonl", "presence": "required"},
    }
    with pytest.raises(TypeError):
        case.output_artifacts["accepted"] = "decoy.jsonl"  # type: ignore[index]


def test_per_sink_artifact_binding_normalizes_required_and_absent_expectations() -> None:
    values = _plural_binding_case_values()
    values["output_artifacts"] = {
        "absent": {"filename": "absent.jsonl", "presence": "absent"},
        "required": "required.jsonl",
    }

    case = HarnessCaseSpec.model_validate(values)

    assert case.output_artifacts["absent"].filename == "absent.jsonl"
    assert case.output_artifacts["absent"].presence == "absent"
    assert case.output_artifacts["required"].filename == "required.jsonl"
    assert case.output_artifacts["required"].presence == "required"
    assert case.model_dump(mode="json")["output_artifacts"] == {
        "absent": {"filename": "absent.jsonl", "presence": "absent"},
        "required": {"filename": "required.jsonl", "presence": "required"},
    }


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
        schema_version=2,
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


def _valid_sink_finalization_recovery() -> ParallelSinkFinalizationRecoveryEvidence:
    return ParallelSinkFinalizationRecoveryEvidence(
        fault_seam="after_finalize_before_response",
        fault_count=1,
        first_sink="left",
        second_sink="right",
        source_exhausted_before=True,
        completed_coalesces_before=2,
        first_sink_rows_before=3,
        first_effect_id_before="a" * 64,
        first_effect_id_after="a" * 64,
        first_artifact_id_before="b" * 64,
        first_artifact_id_after="b" * 64,
        first_attempt_ids_before=("attempt-a", "attempt-b", "attempt-c"),
        first_attempt_ids_after=("attempt-a", "attempt-b", "attempt-c"),
        first_effect_unchanged=True,
        first_artifact_unchanged=True,
        first_attempts_unchanged=True,
        first_sink_republished=False,
        second_effect_absent_before=True,
        second_artifact_absent_before=True,
        second_attempt_count_before=0,
        second_effect_id_after="c" * 64,
        second_artifact_id_after="d" * 64,
        second_attempt_ids_after=("attempt-d", "attempt-e", "attempt-f"),
        final_output_rows=6,
        durable_export_parity=True,
        held_barrier_proven=False,
    )


def _valid_pending_sink_redrive_recovery() -> PendingSinkRedriveRecoveryEvidence:
    return PendingSinkRedriveRecoveryEvidence(
        fault_seam="before_sink_effect_reservation",
        fault_count=1,
        source_exhausted_before=True,
        work_item_id_before="work-1",
        work_item_id_claimed="work-1",
        work_item_id_after="work-1",
        token_id_before="token-1",
        token_id_claimed="token-1",
        token_id_after="token-1",
        row_id_before="row-1",
        row_id_claimed="row-1",
        row_id_after="row-1",
        row_payload_hash_before="a" * 64,
        row_payload_hash_claimed="a" * 64,
        row_payload_hash_after="a" * 64,
        pending_sink_name_before="output",
        pending_sink_name_claimed="output",
        pending_sink_name_after="output",
        pending_outcome_before="success",
        pending_outcome_claimed="success",
        pending_outcome_after="success",
        pending_path_before="default_flow",
        pending_path_claimed="default_flow",
        pending_path_after="default_flow",
        pending_error_hash_before=None,
        pending_error_hash_claimed=None,
        pending_error_hash_after=None,
        pending_error_message_before=None,
        pending_error_message_claimed=None,
        pending_error_message_after=None,
        scheduler_attempt_before=1,
        scheduler_attempt_claimed=1,
        scheduler_attempt_after=1,
        lease_owner_before="worker-before",
        lease_cleared_before_reclaim=True,
        reclaimed_by_fresh_owner=True,
        reclaimed_lease_owner_after="worker-after",
        expired_lease_recovery_events=1,
        recover_event_work_item_id="work-1",
        recover_event_token_id="token-1",
        recover_event_from_status="leased",
        recover_event_to_status="pending_sink",
        recover_event_from_attempt=1,
        recover_event_to_attempt=1,
        recover_event_from_lease_owner="worker-before",
        recover_event_to_lease_owner=None,
        sink_effects_before=0,
        artifacts_before=0,
        sink_effects_after=1,
        sink_effect_members_after=1,
        sink_effect_attempts_after=3,
        artifacts_after=1,
        publications_after=1,
        effect_id_after="b" * 64,
        member_effect_id_after="b" * 64,
        attempt_effect_ids_after=("b" * 64,) * 3,
        artifact_id_after="c" * 64,
        artifact_effect_id_after="b" * 64,
        effect_attempt_ids_after=("attempt-a", "attempt-b", "attempt-c"),
        terminal_outcome="success",
        terminal_work_status="terminal",
        final_output_rows=1,
        durable_export_parity=True,
        provisional_until_deferred_platform_rebase=True,
    )


def _valid_sink_boundary_recovery() -> SinkBoundaryRecoveryEvidence:
    before_work = SinkBoundaryWorkProjection(
        work_item_id="work-1",
        token_id="token-1",
        row_id="row-1",
        row_payload_sha256="1" * 64,
        row_payload_state="live",
        row_payload_anchor_sha256=None,
        node_id="queue-1",
        attempt=1,
        status="pending_sink",
        pending_sink_name="output",
        pending_outcome="success",
        pending_path="default_flow",
        pending_error_hash=None,
        pending_error_message=None,
    )
    after_work = before_work.model_copy(
        update={
            "row_payload_sha256": "2" * 64,
            "row_payload_state": "purged",
            "row_payload_anchor_sha256": hashlib.sha256(before_work.token_id.encode()).hexdigest(),
            "status": "terminal",
        }
    )
    before_effect = SinkBoundaryEffectProjection(
        effect_id="4" * 64,
        sink_name="output",
        sink_node_id="sink-output",
        artifact_id="5" * 64,
        state="in_flight",
        member_token_ids=("token-1",),
        member_row_ids=("row-1",),
    )
    after_effect = before_effect.model_copy(update={"state": "finalized"})
    return SinkBoundaryRecoveryEvidence(
        fault={
            "kind": "sink_effect",
            "seam": "before_effect",
            "sink_name": "output",
            "occurrence": 1,
        },
        fault_count=1,
        initial_run_status="failed",
        source_names_exhausted_before=("primary",),
        checkpoint_topology_hash="6" * 64,
        fresh_topology_hash="6" * 64,
        lease_live_before_close=True,
        token_ids_before=("token-1",),
        token_ids_after=("token-1",),
        work_before=(before_work,),
        work_after=(after_work,),
        effects_before=(before_effect,),
        effects_after=(after_effect,),
        effect_count_before=1,
        effect_member_count_before=1,
        artifact_count_before=0,
        publication_count_before=0,
        effect_count_after=1,
        artifact_count_after=1,
        publication_count_after=1,
        resume_marker_count=1,
        resume_marker_event_type="leader_acquire",
        resume_marker_entry_point="resume",
        resume_marker_worker_id="resume-worker",
        resume_marker_leader_epoch=2,
        durable_identity_reused=True,
        durable_export_parity=True,
        provisional_until_deferred_platform_rebase=True,
    )


def _valid_aggregation_eof_recovery() -> AggregationEOFRecoveryEvidence:
    members = tuple({"ordinal": ordinal, "token_key": f"primary:{ordinal}#0"} for ordinal in range(3))
    return AggregationEOFRecoveryEvidence(
        fault_seam="eof_flush_before_transform_result",
        fault_count=1,
        source_exhausted_before=True,
        original_batch_id_before="batch-original",
        original_batch_id_after="batch-original",
        recovery_batch_id_after="batch-retry",
        member_token_ids_before=("token-0", "token-1", "token-2"),
        member_token_ids_after=("token-0", "token-1", "token-2"),
        original_batch_identity_preserved=True,
        member_identity_reused=True,
        membership_unchanged=True,
        result_token_absent_before=True,
        sink_effect_absent_before=True,
        final_batches=(
            {
                "key": "aggregation:eof_sum@stable|0",
                "aggregation_node_key": "aggregation:eof_sum@stable",
                "attempt": 0,
                "status": "failed",
                "trigger_type": "end_of_source",
                "trigger_reason": "source_exhausted",
                "members": members,
            },
            {
                "key": "aggregation:eof_sum@stable|1",
                "aggregation_node_key": "aggregation:eof_sum@stable",
                "attempt": 1,
                "status": "completed",
                "trigger_type": "end_of_source",
                "trigger_reason": "source_exhausted",
                "members": members,
            },
        ),
        final_output_rows=1,
        final_output_json='{"count":3,"value":60}',
        durable_export_parity=True,
        provisional_until_deferred_platform_rebase=True,
    )


def _valid_terminal_equivalence_projection() -> dict[str, object]:
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
        "tokens": (
            {
                "key": "primary:0#0",
                "row_key": "primary:0",
                "parent_set": (),
                "branch_name": None,
            },
        ),
        "terminal_node_states": (
            {
                "key": "primary:0#0|source:primary@stable|0",
                "token_key": "primary:0#0",
                "node_key": "source:primary@stable",
                "step_index": 0,
                "status": "completed",
                "context_after": None,
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
                "error_hash": None,
            },
        ),
        "terminal_scheduler_work": (
            {
                "key": "primary:0#0|sink:output@stable",
                "token_key": "primary:0#0",
                "node_key": "sink:output@stable",
                "final_status": "terminal",
            },
        ),
        "sink_outputs": ({"sink_name": "output", "rows": ('{"count":3,"value":60}',)},),
        "rows_processed": 1,
        "rows_succeeded": 1,
        "rows_failed": 0,
        "output_rows": 1,
    }


def _valid_terminal_resume_idempotence() -> BaseModel:
    projection = _valid_terminal_equivalence_projection()
    return corpus_schema.TerminalResumeIdempotenceEvidence(
        fault_seam="eof_flush_before_transform_result",
        fault_count=1,
        source_exhausted_before=True,
        resumed_run_id="run-resumed",
        control_terminal_projection=projection,
        resumed_terminal_projection=projection,
        terminal_projection_equal=True,
        fresh_object_lifetimes=4,
        resumed_full_projection_sha256="b" * 64,
        second_resume_error_type="NonResumableRunError",
        second_resume_error_run_id="run-resumed",
        second_resume_error_reason="Run is terminal (status 'completed'); successful terminal runs are immutable",
        database_sha256_before="c" * 64,
        database_sha256_after="c" * 64,
        durable_records_sha256_before="d" * 64,
        durable_records_sha256_after="d" * 64,
        portable_export_sha256_before="e" * 64,
        portable_export_sha256_after="e" * 64,
        output_tree_sha256_before="f" * 64,
        output_tree_sha256_after="f" * 64,
        artifact_digests_before=({"path": "output.jsonl", "sha256": "1" * 64},),
        artifact_digests_after=({"path": "output.jsonl", "sha256": "1" * 64},),
        zero_mutation=True,
        provisional_until_deferred_platform_rebase=True,
    )


def _valid_expansion_child_enqueue_recovery() -> ExpansionChildEnqueueRecoveryEvidence:
    parent_token_ids = ("parent-0", "parent-1", "parent-2")
    child_token_ids = tuple(f"child-{ordinal}" for ordinal in range(6))
    parent_work_ids = ("parent-work-0", "parent-work-1", "parent-work-2")
    child_work_ids = tuple(f"child-work-{ordinal}" for ordinal in range(6))
    scheduler_work_ids = (*parent_work_ids, *child_work_ids)
    child_counts = (2, 1, 3)
    child_offset = 0
    expansions: list[dict[str, object]] = []
    for parent_ordinal, child_count in enumerate(child_counts):
        children = tuple(
            {"ordinal": child_ordinal, "token_key": f"primary:{parent_ordinal}#{child_ordinal + 1}"} for child_ordinal in range(child_count)
        )
        expansions.append(
            {
                "key": f"expand|primary:{parent_ordinal}#0",
                "parent_token_key": f"primary:{parent_ordinal}#0",
                "expected_child_count": child_count,
                "children": children,
            }
        )
        child_offset += child_count
    assert child_offset == len(child_token_ids)
    return ExpansionChildEnqueueRecoveryEvidence(
        fault_seam="after_source_exhausted_before_sink_flush",
        fault_count=1,
        source_exhausted_before=True,
        parent_token_ids_before=parent_token_ids,
        parent_token_ids_after=parent_token_ids,
        child_token_ids_before=child_token_ids,
        child_token_ids_after=child_token_ids,
        expand_group_ids_before=("group-0", "group-1", "group-2"),
        expand_group_ids_after=("group-0", "group-1", "group-2"),
        scheduler_work_ids_before=scheduler_work_ids,
        scheduler_work_ids_after=scheduler_work_ids,
        parent_scheduler_work_ids_before=parent_work_ids,
        parent_scheduler_work_ids_after=parent_work_ids,
        child_scheduler_work_ids_before=child_work_ids,
        child_scheduler_work_ids_after=child_work_ids,
        parent_identity_unchanged=True,
        child_identity_unchanged=True,
        group_identity_unchanged=True,
        scheduler_identity_unchanged=True,
        pending_children_before=6,
        sink_effect_absent_before=True,
        artifact_absent_before=True,
        final_expansions=tuple(expansions),
        final_output_rows=6,
        durable_export_parity=True,
        provisional_until_deferred_platform_rebase=True,
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


def test_b3_batch_projection_requires_dense_immutable_member_ordinals() -> None:
    batch = corpus_schema.StableBatchProjection(
        key="aggregation:eof_sum@stable|0",
        aggregation_node_key="aggregation:eof_sum@stable",
        attempt=0,
        status="completed",
        trigger_type="end_of_source",
        trigger_reason="source_exhausted",
        members=(
            {"ordinal": 0, "token_key": "primary:0#0"},
            {"ordinal": 1, "token_key": "primary:1#0"},
            {"ordinal": 2, "token_key": "primary:2#0"},
        ),
    )

    assert tuple((member.ordinal, member.token_key) for member in batch.members) == (
        (0, "primary:0#0"),
        (1, "primary:1#0"),
        (2, "primary:2#0"),
    )
    with pytest.raises(ValidationError, match="batch member ordinals must be dense from zero"):
        corpus_schema.StableBatchProjection(
            key="aggregation:eof_sum@stable|0",
            aggregation_node_key="aggregation:eof_sum@stable",
            attempt=0,
            status="completed",
            trigger_type="end_of_source",
            trigger_reason="source_exhausted",
            members=(
                {"ordinal": 0, "token_key": "primary:0#0"},
                {"ordinal": 2, "token_key": "primary:1#0"},
            ),
        )


def test_b3_intermediate_outcome_projection_is_separate_from_terminal_disposition() -> None:
    outcome = corpus_schema.StableIntermediateOutcomeProjection(
        key="primary:0#0|buffered|00000000",
        token_key="primary:0#0",
        ordinal=0,
        path="buffered",
        batch_key="aggregation:eof_sum@stable|0",
    )

    assert outcome.path == "buffered"
    assert outcome.batch_key == "aggregation:eof_sum@stable|0"


def test_b3_expansion_projection_binds_dense_children_to_parent_and_expected_count() -> None:
    expansion = corpus_schema.StableExpansionProjection(
        key="expand|primary:0#0",
        parent_token_key="primary:0#0",
        expected_child_count=2,
        children=(
            {"ordinal": 0, "token_key": "primary:0#1"},
            {"ordinal": 1, "token_key": "primary:0#2"},
        ),
    )

    assert expansion.expected_child_count == len(expansion.children) == 2
    with pytest.raises(ValidationError, match="expansion children must exactly match expected_child_count"):
        expansion.model_copy(update={"expected_child_count": 3}, deep=True).__class__.model_validate(
            {**expansion.model_dump(mode="python"), "expected_child_count": 3}
        )


@pytest.mark.parametrize(
    ("model", "field"),
    (
        (corpus_schema.StableValidationErrorProjection, "row_data"),
        (corpus_schema.StableTransformErrorProjection, "error_details"),
    ),
)
def test_b3_error_projections_require_canonical_json(model: type[BaseModel], field: str) -> None:
    values: dict[str, object]
    if model is corpus_schema.StableValidationErrorProjection:
        values = {
            "key": "primary:0|source:primary@stable|0",
            "node_key": "source:primary@stable",
            "row_key": "primary:0",
            "row_hash": "a" * 64,
            "row_data": '{"id":"bad"}',
            "error": "Input should be a valid integer",
            "schema_mode": "fixed",
            "destination": "quarantine",
            "violation_type": "type_mismatch",
            "original_field_name": "id",
            "normalized_field_name": "id",
            "expected_type": "int",
            "actual_type": "str",
        }
    else:
        values = {
            "key": "primary:0#0|transform:fail@stable",
            "token_key": "primary:0#0",
            "transform_node_key": "transform:fail@stable",
            "row_hash": "b" * 64,
            "row_data": '{"id":1}',
            "error_details": '{"error":"boom","reason":"invalid_input"}',
            "destination": "discard",
        }
    parsed = model.model_validate(values)
    assert getattr(parsed, field).startswith("{")
    values[field] = '{"z":1, "a":2}'
    with pytest.raises(ValidationError, match=f"{field} must use canonical JSON"):
        model.model_validate(values)


def test_b3_stable_projection_rejects_batch_and_expansion_cross_reference_drift() -> None:
    values = _exact_runtime_projection_values()
    values["batches"] = (
        {
            "key": "aggregation:eof_sum@stable|0",
            "aggregation_node_key": "aggregation:eof_sum@stable",
            "attempt": 0,
            "status": "completed",
            "trigger_type": "end_of_source",
            "trigger_reason": "source_exhausted",
            "members": ({"ordinal": 0, "token_key": "missing"},),
        },
    )

    with pytest.raises(ValidationError, match="batch members must reference projected tokens"):
        corpus_schema.StableRunProjection.model_validate(values)

    values = _exact_runtime_projection_values()
    values["intermediate_outcomes"] = (
        {
            "key": "primary:0#0|buffered|00000000",
            "token_key": "primary:0#0",
            "ordinal": 0,
            "path": "buffered",
            "batch_key": "aggregation:eof_sum@missing|0",
        },
    )

    with pytest.raises(ValidationError, match="intermediate outcomes must reference projected batches"):
        corpus_schema.StableRunProjection.model_validate(values)


def test_b3_scheduler_event_ordering_accepts_one_exact_reentered_status_chain() -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "mark_pending_sink_terminal", "from_status": "leased", "to_status": "terminal"},
        {"event_type": "claim_pending_sink", "from_status": "pending_sink", "to_status": "leased"},
        {"event_type": "enqueue", "from_status": None, "to_status": "ready"},
        {"event_type": "mark_pending_sink", "from_status": "leased", "to_status": "pending_sink"},
        {"event_type": "claim_ready", "from_status": "ready", "to_status": "leased"},
    ]

    ordered = corpus_harness._ordered_scheduler_events(events, work_key="primary:0#2")

    assert tuple(event["event_type"] for event in ordered) == (
        "enqueue",
        "claim_ready",
        "mark_pending_sink",
        "claim_pending_sink",
        "mark_pending_sink_terminal",
    )


def test_b3_scheduler_event_ordering_rejects_two_complete_chains() -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "enqueue", "from_status": None, "to_status": "ready"},
        {"event_type": "claim_ready:first", "from_status": "ready", "to_status": "leased"},
        {"event_type": "recover", "from_status": "leased", "to_status": "ready"},
        {"event_type": "claim_ready:second", "from_status": "ready", "to_status": "leased"},
        {"event_type": "terminal", "from_status": "leased", "to_status": "terminal"},
    ]

    with pytest.raises(AssertionError, match="exactly one complete transition chain"):
        corpus_harness._ordered_scheduler_events(events, work_key="ambiguous")


def test_b3_scheduler_event_ordering_rejects_an_unconsumed_stray_event() -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "enqueue", "from_status": None, "to_status": "ready"},
        {"event_type": "terminal", "from_status": "ready", "to_status": "terminal"},
        {"event_type": "stray", "from_status": "blocked", "to_status": "terminal"},
    ]

    with pytest.raises(AssertionError, match="exactly one complete transition chain"):
        corpus_harness._ordered_scheduler_events(events, work_key="stray")


def test_b3_expansion_work_partition_rejects_swapped_parent_child_statuses() -> None:
    work_items = (
        ("work-parent", "parent", "pending_sink"),
        ("work-child", "child", "terminal"),
    )

    with pytest.raises(AssertionError, match="exact parent/child scheduler status partition"):
        corpus_harness._partition_expansion_work(
            work_items,
            parent_token_ids=("parent",),
            child_token_ids=("child",),
            parent_status="terminal",
            child_status="pending_sink",
        )


def test_b3_exact_counter_projection_counts_every_immutable_batch_member_row() -> None:
    values = _exact_run_expectation_values()
    values["rows_processed"] = 3
    projection = cast(dict[str, object], values["projection"])
    projection["rows"] = tuple(
        {
            "key": f"primary:{ordinal}",
            "source_name": "primary",
            "source_row_index": ordinal,
            "ingest_sequence": ordinal,
            "source_data_hash": chr(ord("a") + ordinal) * 64,
        }
        for ordinal in range(3)
    )
    projection["tokens"] = (
        {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
        },
        {"key": "primary:1#0", "row_key": "primary:1", "parents": ()},
        {"key": "primary:2#0", "row_key": "primary:2", "parents": ()},
    )
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "batch_consumed",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        },
        {
            "key": "primary:1#0",
            "token_key": "primary:1#0",
            "outcome": "transient",
            "path": "batch_consumed",
            "sink_name": None,
        },
        {
            "key": "primary:2#0",
            "token_key": "primary:2#0",
            "outcome": "transient",
            "path": "batch_consumed",
            "sink_name": None,
        },
    )
    projection["batches"] = (
        {
            "key": "aggregation:eof_sum@stable|0",
            "aggregation_node_key": "aggregation:eof_sum@stable",
            "attempt": 0,
            "status": "completed",
            "trigger_type": "end_of_source",
            "trigger_reason": "source_exhausted",
            "members": tuple({"ordinal": ordinal, "token_key": f"primary:{ordinal}#0"} for ordinal in range(3)),
        },
    )

    expectation = RunExpectation.model_validate(values)

    assert expectation.rows_processed == 3


def test_stable_token_projection_preserves_durable_parent_ordinal_sequence() -> None:
    token = corpus_schema.StableTokenProjection(
        key="primary:0#2",
        row_key="primary:0",
        parents=(
            {"ordinal": 1, "parent_key": "primary:0#1"},
            {"ordinal": 3, "parent_key": "primary:0#0"},
        ),
    )

    assert tuple((parent.ordinal, parent.parent_key) for parent in token.parents) == (
        (1, "primary:0#1"),
        (3, "primary:0#0"),
    )
    with pytest.raises(ValidationError, match="token parent keys must be unique"):
        corpus_schema.StableTokenProjection(
            key="primary:0#2",
            row_key="primary:0",
            parents=(
                {"ordinal": 0, "parent_key": "primary:0#0"},
                {"ordinal": 1, "parent_key": "primary:0#0"},
            ),
        )
    with pytest.raises(ValidationError, match="token parent ordinals must be unique"):
        corpus_schema.StableTokenProjection(
            key="primary:0#2",
            row_key="primary:0",
            parents=(
                {"ordinal": 1, "parent_key": "primary:0#0"},
                {"ordinal": 1, "parent_key": "primary:0#1"},
            ),
        )
    with pytest.raises(ValidationError, match="token parents must be sorted by durable ordinal"):
        corpus_schema.StableTokenProjection(
            key="primary:0#2",
            row_key="primary:0",
            parents=(
                {"ordinal": 3, "parent_key": "primary:0#0"},
                {"ordinal": 1, "parent_key": "primary:0#1"},
            ),
        )


def test_harness_preserves_sparse_durable_parent_ordinals_before_semantic_set_projection() -> None:
    token_keys = {"parent-a": "primary:0#1", "parent-b": "primary:0#0"}

    links = corpus_harness._ordered_parent_links(
        "merged-token",
        [(3, "parent-a"), (1, "parent-b")],
        token_keys,
    )

    assert tuple((link.ordinal, link.parent_key) for link in links) == (
        (1, "primary:0#0"),
        (3, "primary:0#1"),
    )
    assert tuple(sorted(link.parent_key for link in links)) == (
        "primary:0#0",
        "primary:0#1",
    )
    with pytest.raises(AssertionError, match="duplicate durable parent ordinals"):
        corpus_harness._ordered_parent_links(
            "merged-token",
            [(1, "parent-a"), (1, "parent-b")],
            token_keys,
        )


def test_semantic_runtime_token_projection_uses_an_explicit_parent_set() -> None:
    token = corpus_schema.SemanticTokenProjection(
        key="primary:0#2",
        row_key="primary:0",
        parent_set=("primary:0#0", "primary:0#1"),
    )

    assert token.parent_set == ("primary:0#0", "primary:0#1")
    with pytest.raises(ValidationError, match="semantic token parent_set must be unique and sorted"):
        corpus_schema.SemanticTokenProjection(
            key="primary:0#2",
            row_key="primary:0",
            parent_set=("primary:0#1", "primary:0#0"),
        )


def test_semantic_runtime_projection_rejects_unknown_disposition_token_with_closed_validation_error() -> None:
    values = _exact_runtime_projection_values()
    values.pop("audit_records")
    tokens = cast(tuple[dict[str, object], ...], values["tokens"])
    values["tokens"] = tuple(
        {
            "key": token["key"],
            "row_key": token["row_key"],
            "parent_set": tuple(parent["parent_key"] for parent in cast(tuple[dict[str, object], ...], token["parents"])),
        }
        for token in tokens
    )
    values["terminal_dispositions"] = (
        {
            "key": "missing",
            "token_key": "missing",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        },
    )

    with pytest.raises(ValidationError, match="semantic terminal dispositions must exactly cover tokens"):
        corpus_schema.SemanticRuntimeProjection.model_validate(values)


def test_semantic_runtime_projection_rejects_duplicate_dispositions_for_one_token() -> None:
    values = _exact_runtime_projection_values()
    values.pop("audit_records")
    tokens = cast(tuple[dict[str, object], ...], values["tokens"])
    values["tokens"] = tuple(
        {
            "key": token["key"],
            "row_key": token["row_key"],
            "parent_set": tuple(parent["parent_key"] for parent in cast(tuple[dict[str, object], ...], token["parents"])),
        }
        for token in tokens
    )
    values["terminal_dispositions"] = (
        {
            "key": "outcome-a",
            "token_key": "primary:0#0",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        },
        {
            "key": "outcome-b",
            "token_key": "primary:0#0",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        },
    )

    with pytest.raises(ValidationError, match="semantic terminal dispositions must exactly cover tokens one-to-one"):
        corpus_schema.SemanticRuntimeProjection.model_validate(values)


def _semantic_scenario_raw(raw: dict[str, object]) -> dict[str, object]:
    return next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "sequential-nested-fork-coalesce")


@pytest.mark.parametrize("workflow", ("build", "recovery"))
def test_semantic_runtime_expectation_is_run_workflow_only(workflow: str) -> None:
    raw = valid_manifest_dict()
    scenario = _semantic_scenario_raw(raw)
    case = cast(list[dict[str, object]], scenario["cases"])[0]
    case["workflow"] = workflow

    with pytest.raises(ValidationError, match="semantic_runtime expectation is valid only for the run workflow"):
        corpus_schema.ScenarioManifest.model_validate(raw)


@pytest.mark.parametrize("dimension", ("audit", "recovery"))
def test_semantic_runtime_expectation_cannot_promote_audit_or_recovery_to_pass(dimension: str) -> None:
    raw = valid_manifest_dict()
    scenario = _semantic_scenario_raw(raw)
    dimensions = _raw_dimensions(scenario)
    dimensions[dimension] = {
        "status": "pass",
        "evidence": ["harness-sequential-nested-fork-coalesce-two-sequential-require-all"],
    }

    with pytest.raises(ValidationError, match=rf"semantic_runtime expectation cannot satisfy an? {dimension} pass"):
        corpus_schema.ScenarioManifest.model_validate(raw)


def test_semantic_runtime_case_does_not_block_separate_exact_audit_evidence() -> None:
    raw = valid_manifest_dict()
    scenario = _semantic_scenario_raw(raw)
    exact_case = deepcopy(cast(list[dict[str, object]], _raw_scenarios(raw)[0]["cases"])[0])
    exact_case["id"] = "exact-audit-control"
    cast(list[dict[str, object]], scenario["cases"]).append(exact_case)
    _raw_evidence(raw).append(
        {
            "id": "harness-sequential-exact-audit-control",
            "kind": "harness",
            "locator": "sequential-nested-fork-coalesce:exact-audit-control",
            "claim": "Separate exact case proves audit identity",
            "stages": ["config", "build", "runtime", "audit"],
        }
    )
    _raw_dimensions(scenario)["audit"] = {
        "status": "pass",
        "evidence": ["harness-sequential-exact-audit-control"],
    }

    manifest = corpus_schema.ScenarioManifest.model_validate(raw)

    assert manifest.scenarios[6].dimensions["audit"].status == "pass"


def test_semantic_runtime_harness_evidence_cannot_claim_audit_stage() -> None:
    raw = valid_manifest_dict()
    evidence = next(
        reference
        for reference in _raw_evidence(raw)
        if reference["locator"] == "sequential-nested-fork-coalesce:two-sequential-require-all"
    )
    evidence["stages"] = ["config", "build", "runtime", "audit"]

    with pytest.raises(ValidationError, match="semantic_runtime harness evidence cannot claim audit or recovery stages"):
        corpus_schema.ScenarioManifest.model_validate(raw)


def test_composed_coalesce_semantic_ledger_is_structured_and_identity_is_regression_guarded() -> None:
    manifest = load_manifest()
    semantic_cases = tuple(
        (scenario, case)
        for scenario, case in iter_harness_cases(manifest)
        if isinstance(case.expected, corpus_schema.SemanticRunExpectation)
    )

    assert len(semantic_cases) == 20
    identity_regression = next(reference for reference in manifest.evidence if reference.id == "composed-coalesces-canonical-identity")
    assert (identity_regression.kind, identity_regression.locator, identity_regression.stages) == (
        "pytest",
        "tests/integration/core/dag/test_dag_scenario_production_path.py"
        "::test_b2_composed_coalesces_raw_identity_converges_across_equivalent_runs",
        ("audit",),
    )
    evidence_by_locator = {reference.locator: reference for reference in manifest.evidence}
    for scenario, case in semantic_cases:
        expected = case.expected
        assert isinstance(expected, corpus_schema.SemanticRunExpectation)
        assert len(expected.projection_sha256) == 64
        assert all(value > 0 for value in expected.projection_counts.model_dump().values())
        audit = scenario.dimensions["audit"]
        assert audit.status == "pass"
        assert audit.owner_issue is None
        assert identity_regression.id in audit.evidence
        assert evidence_by_locator[f"{scenario.id}:{case.id}"].stages == (
            "config",
            "build",
            "runtime",
        )


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


def _exact_runtime_evidence_values(projection: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "exact",
        "attempted": True,
        "run_id": "run-1",
        "status": "completed",
        "rows_processed": 1,
        "rows_succeeded": 1,
        "rows_failed": 0,
        "output_rows": 1,
        "sink_outputs": ({"sink_name": "output", "rows": ('{"id":1,"value":10}',)},),
        "durable_projection": projection,
    }


def _exact_audit_evidence_values(projection: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "exact",
        "attempted": True,
        "total_records": 7,
        "record_counts": (
            {"record_type": "node_state", "count": 1},
            {"record_type": "row", "count": 1},
            {"record_type": "run", "count": 1},
            {"record_type": "scheduler_event", "count": 2},
            {"record_type": "token", "count": 1},
            {"record_type": "token_outcome", "count": 1},
        ),
        "source_operation_count": 0,
        "portable_projection": projection,
    }


def _failed_runtime_projection_values() -> dict[str, object]:
    projection = _exact_runtime_projection_values()
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "failure",
            "path": "unrouted",
            "sink_name": None,
        },
    )
    return projection


def _scenario_exact_evidence_values(
    *,
    runtime: dict[str, object],
    audit: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "scenario_id": "fork-coalesce-policies",
        "case_id": "union-collision-fail",
        "fixture_sha256": "fixture-sha",
        "config": {"loaded": True, "settings_sha256": "settings-sha"},
        "graph": {
            "accepted": True,
            "node_count": 2,
            "edge_count": 1,
            "node_type_counts": (
                {"node_type": "sink", "count": 1},
                {"node_type": "source", "count": 1},
            ),
            "edge_labels": ("on_success",),
            "topology_hash": "topology-sha",
        },
        "runtime": runtime,
        "audit": audit,
        "recovery": {
            "attempted": False,
            "database_reopened": False,
            "can_resume": False,
            "source_replayed": False,
            "checkpoint_removed": False,
        },
        "completed_stages": ("config", "build", "runtime", "audit"),
    }


def _projection_with_disconnected_terminal(*, cyclic: bool) -> dict[str, object]:
    projection = _exact_runtime_projection_values()
    projection["tokens"] = (
        {
            "key": "primary:0#0",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#1"},) if cyclic else (),
        },
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
        },
        {"key": "primary:0#2", "row_key": "primary:0", "parents": ()},
    )
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "transient",
            "path": "batch_consumed",
            "sink_name": None,
        },
        {
            "key": "primary:0#2",
            "token_key": "primary:0#2",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        },
    )
    return projection


def test_exact_runtime_projection_expectation_is_discriminated_immutable_and_canonical() -> None:
    expectation = RunExpectation.model_validate(_exact_run_expectation_values())

    assert expectation.kind == "exact"
    assert expectation.sink_outputs[0].rows == ('{"id":1,"value":10}',)
    assert expectation.projection.tokens[0].parents == ()
    with pytest.raises(ValidationError, match="frozen"):
        expectation.__setattr__("rows_processed", 2)


def test_exact_coalesce_counters_exclude_consumed_branch_tokens() -> None:
    values = _exact_run_expectation_values()
    projection = cast(dict[str, object], values["projection"])
    projection["tokens"] = (
        {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": (
                {"ordinal": 0, "parent_key": "primary:0#2"},
                {"ordinal": 1, "parent_key": "primary:0#3"},
                {"ordinal": 2, "parent_key": "primary:0#4"},
            ),
        },
        {
            "key": "primary:0#2",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
        },
        {
            "key": "primary:0#3",
            "row_key": "primary:0",
            "parents": ({"ordinal": 1, "parent_key": "primary:0#0"},),
        },
        {
            "key": "primary:0#4",
            "row_key": "primary:0",
            "parents": ({"ordinal": 2, "parent_key": "primary:0#0"},),
        },
    )
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "success",
            "path": "coalesced",
            "sink_name": "output",
        },
        *(
            {
                "key": f"primary:0#{ordinal}",
                "token_key": f"primary:0#{ordinal}",
                "outcome": "success",
                "path": "coalesced",
                "sink_name": None,
            }
            for ordinal in (2, 3, 4)
        ),
    )

    expectation = RunExpectation.model_validate(values)
    runtime = RuntimeEvidence.model_validate(_exact_runtime_evidence_values(projection))

    assert expectation.rows_succeeded == 1
    assert runtime.rows_succeeded == 1


def test_exact_projection_counts_distinct_successful_terminal_publications_for_one_row() -> None:
    values = _exact_run_expectation_values()
    values["rows_succeeded"] = 2
    projection = cast(dict[str, object], values["projection"])
    projection["tokens"] = (
        {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
        },
        {
            "key": "primary:0#2",
            "row_key": "primary:0",
            "parents": ({"ordinal": 1, "parent_key": "primary:0#0"},),
        },
    )
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "left",
        },
        {
            "key": "primary:0#2",
            "token_key": "primary:0#2",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "right",
        },
    )

    expectation = RunExpectation.model_validate(values)

    assert expectation.rows_processed == 1
    assert expectation.rows_succeeded == 2


def _completed_coalesce_projection(*, lost_branch: bool = False) -> corpus_schema.StableRunProjection:
    node_key = "coalesce:merge@stable"
    expected_branches = ["path_a", "path_b"]
    if lost_branch:
        expected_branches.append("path_c")
    context = {
        "arrival_order": [{"branch": "path_a"}, {"branch": "path_b"}],
        "branches_arrived": ["path_a", "path_b"],
        "branches_lost": {"path_c": {}} if lost_branch else {},
        "expected_branches": expected_branches,
        "merge_strategy": "union",
        "policy": "quorum" if lost_branch else "require_all",
        "union_field_origins": {"id": "path_a"},
    }
    tokens: list[dict[str, object]] = [
        {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": (
                {"ordinal": 0, "parent_key": "primary:0#2"},
                {"ordinal": 1, "parent_key": "primary:0#3"},
            ),
        },
        {
            "key": "primary:0#2",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
            "branch_name": "path_a",
        },
        {
            "key": "primary:0#3",
            "row_key": "primary:0",
            "parents": ({"ordinal": 1, "parent_key": "primary:0#0"},),
            "branch_name": "path_b",
        },
    ]
    dispositions: list[dict[str, object]] = [
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "success",
            "path": "coalesced",
            "sink_name": "output",
        },
        {
            "key": "primary:0#2",
            "token_key": "primary:0#2",
            "outcome": "success",
            "path": "coalesced",
            "sink_name": None,
        },
        {
            "key": "primary:0#3",
            "token_key": "primary:0#3",
            "outcome": "success",
            "path": "coalesced",
            "sink_name": None,
        },
    ]
    if lost_branch:
        tokens.append(
            {
                "key": "primary:0#4",
                "row_key": "primary:0",
                "parents": ({"ordinal": 2, "parent_key": "primary:0#0"},),
                "branch_name": "path_c",
            }
        )
        dispositions.append(
            {
                "key": "primary:0#4",
                "token_key": "primary:0#4",
                "outcome": "failure",
                "path": "unrouted",
                "sink_name": None,
            }
        )
    node_config: dict[str, object] = {
        "branches": {branch: f"merge_{branch}" for branch in expected_branches},
        "merge": "union",
        "policy": context["policy"],
    }
    if lost_branch:
        node_config["quorum_count"] = 2
    values = {
        "rows": (
            {
                "key": "primary:0",
                "source_name": "primary",
                "source_row_index": 0,
                "ingest_sequence": 0,
                "source_data_hash": "a" * 64,
            },
        ),
        "tokens": tuple(tokens),
        "node_states": tuple(
            {
                "key": f"primary:0#{ordinal}|{node_key}|1|0",
                "token_key": f"primary:0#{ordinal}",
                "node_key": node_key,
                "step_index": 1,
                "attempt": 0,
                "status": "completed",
                "context_after": json.dumps(context, sort_keys=True, separators=(",", ":")),
            }
            for ordinal in (2, 3)
        ),
        "routes": (),
        "terminal_dispositions": tuple(dispositions),
        "scheduler_work": (),
        "audit_records": (
            {
                "key": f"node|{node_key}",
                "record_type": "node",
                "material": json.dumps({"config": node_config}, sort_keys=True, separators=(",", ":")),
                "references": (),
            },
        ),
    }
    return corpus_schema.StableRunProjection.model_validate(values)


@pytest.mark.parametrize("lost_branch", (False, True))
def test_completed_coalesce_projection_ties_arrivals_to_exact_merged_parents(lost_branch: bool) -> None:
    projection = _completed_coalesce_projection(lost_branch=lost_branch)

    completed_coalesce_states = tuple(
        state for state in projection.node_states if state.status == "completed" and state.node_key.startswith("coalesce:")
    )
    assert completed_coalesce_states


def test_completed_coalesce_projection_allows_merged_token_to_fork_again() -> None:
    values = _completed_coalesce_projection().model_dump(mode="python")
    tokens = list(cast(tuple[dict[str, object], ...], values["tokens"]))
    dispositions = list(cast(tuple[dict[str, object], ...], values["terminal_dispositions"]))
    values["tokens"] = tokens
    values["terminal_dispositions"] = dispositions
    merged_token = next(token for token in tokens if len(cast(tuple[dict[str, object], ...], token["parents"])) == 2)
    merged_key = cast(str, merged_token["key"])
    merged_disposition = next(disposition for disposition in dispositions if disposition["token_key"] == merged_key)
    merged_disposition.update(outcome="transient", path="fork_parent", sink_name=None)
    tokens.append(
        {
            "key": "primary:0#5",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": merged_key},),
            "branch_name": "next_branch",
        }
    )
    dispositions.append(
        {
            "key": "primary:0#5",
            "token_key": "primary:0#5",
            "outcome": "success",
            "path": "default_flow",
            "sink_name": "output",
        }
    )

    projection = corpus_schema.StableRunProjection.model_validate(values)

    projected_merged = next(disposition for disposition in projection.terminal_dispositions if disposition.token_key == merged_key)
    assert (projected_merged.outcome, projected_merged.path, projected_merged.sink_name) == (
        "transient",
        "fork_parent",
        None,
    )


def test_completed_coalesce_projection_rejects_merged_token_missing_consumed_parent() -> None:
    values = _completed_coalesce_projection().model_dump(mode="python")
    tokens = cast(list[dict[str, object]], values["tokens"])
    merged_token = next(token for token in tokens if len(cast(tuple[dict[str, object], ...], token["parents"])) == 2)
    merged_token["parents"] = cast(tuple[dict[str, object], ...], merged_token["parents"])[:-1]

    with pytest.raises(
        ValidationError,
        match=r"completed coalesce .* consumed token set must exactly parent one merged token",
    ):
        corpus_schema.StableRunProjection.model_validate(values)


def test_completed_coalesce_projection_rejects_arrived_branch_not_bound_to_consumed_token() -> None:
    values = _completed_coalesce_projection(lost_branch=True).model_dump(mode="python")
    states = cast(tuple[dict[str, object], ...], values["node_states"])
    for state in states:
        if state["status"] != "completed" or not cast(str, state["node_key"]).startswith("coalesce:"):
            continue
        context = json.loads(cast(str, state["context_after"]))
        context["arrival_order"][1]["branch"] = "path_c"
        context["branches_arrived"] = ["path_a", "path_c"]
        context["branches_lost"] = {"path_b": context["branches_lost"]["path_c"]}
        context["lost_branch_expected_fields"] = {"path_b": ["branch_marker"]}
        state["context_after"] = json.dumps(context, sort_keys=True, separators=(",", ":"))

    with pytest.raises(ValidationError, match="arrived branches must bind exactly to consumed upstream tokens"):
        corpus_schema.StableRunProjection.model_validate(values)


def test_completed_coalesce_projection_requires_exact_lost_branch_complement() -> None:
    values = _completed_coalesce_projection(lost_branch=True).model_dump(mode="python")
    states = cast(tuple[dict[str, object], ...], values["node_states"])
    for state in states:
        if state["status"] != "completed" or not cast(str, state["node_key"]).startswith("coalesce:"):
            continue
        context = json.loads(cast(str, state["context_after"]))
        context["branches_lost"] = {}
        state["context_after"] = json.dumps(context, sort_keys=True, separators=(",", ":"))

    with pytest.raises(ValidationError, match="branches_lost must exactly complement arrived branches"):
        corpus_schema.StableRunProjection.model_validate(values)


def test_completed_union_projection_rejects_provenance_from_lost_branch() -> None:
    values = _completed_coalesce_projection(lost_branch=True).model_dump(mode="python")
    states = cast(tuple[dict[str, object], ...], values["node_states"])
    for state in states:
        if state["status"] != "completed" or not cast(str, state["node_key"]).startswith("coalesce:"):
            continue
        context = json.loads(cast(str, state["context_after"]))
        context["union_field_origins"]["id"] = "path_c"
        state["context_after"] = json.dumps(context, sort_keys=True, separators=(",", ":"))

    with pytest.raises(ValidationError, match="union provenance origins must reference arrived branches"):
        corpus_schema.StableRunProjection.model_validate(values)


def test_exact_runtime_evidence_allows_intentionally_absent_sink_artifacts() -> None:
    values = _exact_runtime_evidence_values(_exact_runtime_projection_values())
    values["output_rows"] = 0
    values["sink_outputs"] = ()

    runtime = RuntimeEvidence.model_validate(values)

    assert runtime.durable_projection is not None
    assert runtime.sink_outputs == ()
    assert runtime.output_rows == 0


def test_exact_failed_run_expectation_declares_exact_production_exception() -> None:
    values = _exact_run_expectation_values()
    values["status"] = "failed"
    values["expected_error"] = {"exception_type": "CoalesceCollisionError"}

    expectation = RunExpectation.model_validate(values)

    assert expectation.status == "failed"
    assert expectation.expected_error is not None
    assert expectation.expected_error.exception_type == "CoalesceCollisionError"


def test_expected_run_error_is_forbidden_for_nonfailed_status() -> None:
    values = _exact_run_expectation_values()
    values["expected_error"] = {"exception_type": "CoalesceCollisionError"}

    with pytest.raises(ValidationError, match="expected_error requires status=failed"):
        RunExpectation.model_validate(values)


def test_observed_run_error_is_forbidden_for_completed_runtime() -> None:
    values = _exact_runtime_evidence_values(_exact_runtime_projection_values())
    values["observed_error"] = {"exception_type": "CoalesceCollisionError"}

    with pytest.raises(ValidationError, match="observed_error requires status=failed"):
        RuntimeEvidence.model_validate(values)


def test_observed_run_error_is_forbidden_for_summary_runtime() -> None:
    values = {
        "kind": "summary",
        "attempted": True,
        "run_id": "run-1",
        "status": "failed",
        "observed_error": {"exception_type": "CoalesceCollisionError"},
    }

    with pytest.raises(ValidationError, match="observed_error requires kind=exact"):
        RuntimeEvidence.model_validate(values)


def test_failed_expected_error_evidence_types_portable_export_unavailable_by_policy() -> None:
    projection = _failed_runtime_projection_values()
    runtime = _exact_runtime_evidence_values(projection)
    runtime.update(
        status="failed",
        rows_succeeded=0,
        rows_failed=1,
        output_rows=0,
        sink_outputs=(),
        observed_error={"exception_type": "CoalesceCollisionError"},
    )
    audit = _exact_audit_evidence_values(projection)
    audit.update(
        kind="unavailable_by_policy",
        portable_projection=None,
        portable_export_unavailable={
            "run_status": "failed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
        },
    )

    evidence = ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=runtime, audit=audit))

    assert evidence.runtime.observed_error is not None
    assert evidence.audit.kind == "unavailable_by_policy"
    assert evidence.audit.portable_export_unavailable is not None


def test_portable_export_unavailable_by_policy_rejects_completed_terminal() -> None:
    values = {
        "kind": "unavailable_by_policy",
        "attempted": True,
        "total_records": 1,
        "record_counts": ({"record_type": "run", "count": 1},),
        "source_operation_count": 1,
        "portable_export_unavailable": {
            "run_status": "completed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
        },
    }

    with pytest.raises(ValidationError, match="run_status"):
        AuditEvidence.model_validate(values)


def test_portable_export_unavailable_by_policy_rejects_empty_audit_evidence() -> None:
    values = {
        "kind": "unavailable_by_policy",
        "attempted": True,
        "total_records": 0,
        "record_counts": (),
        "source_operation_count": 0,
        "portable_export_unavailable": {
            "run_status": "failed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
        },
    }

    with pytest.raises(ValidationError, match="unavailable_by_policy requires non-empty durable audit evidence"):
        AuditEvidence.model_validate(values)


def test_unavailable_export_audit_counts_must_match_exact_durable_projection() -> None:
    projection = _failed_runtime_projection_values()
    runtime = _exact_runtime_evidence_values(projection)
    runtime.update(
        status="failed",
        rows_succeeded=0,
        rows_failed=1,
        output_rows=0,
        sink_outputs=(),
        observed_error={"exception_type": "CoalesceCollisionError"},
    )
    audit = _exact_audit_evidence_values(projection)
    audit.update(
        kind="unavailable_by_policy",
        portable_projection=None,
        portable_export_unavailable={
            "run_status": "failed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
        },
    )
    counts = [dict(record) for record in cast(tuple[dict[str, object], ...], audit["record_counts"])]
    next(record for record in counts if record["record_type"] == "row")["count"] = 2
    audit["record_counts"] = tuple(counts)
    audit["total_records"] = cast(int, audit["total_records"]) + 1

    with pytest.raises(ValidationError, match="audit record count for row must match exact durable projection"):
        ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=runtime, audit=audit))


def test_unavailable_export_source_operation_count_must_match_exact_durable_projection() -> None:
    projection = _failed_runtime_projection_values()
    runtime = _exact_runtime_evidence_values(projection)
    runtime.update(
        status="failed",
        rows_succeeded=0,
        rows_failed=1,
        output_rows=0,
        sink_outputs=(),
        observed_error={"exception_type": "CoalesceCollisionError"},
    )
    audit = _exact_audit_evidence_values(projection)
    audit.update(
        kind="unavailable_by_policy",
        source_operation_count=1,
        portable_projection=None,
        portable_export_unavailable={
            "run_status": "failed",
            "exception_type": "ValueError",
            "reason": "Audit export requires an immutable export-terminal run",
        },
    )

    with pytest.raises(ValidationError, match="audit source_operation_count must match exact durable projection"):
        ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=runtime, audit=audit))


def test_expected_error_runtime_rejects_exportable_exact_audit() -> None:
    projection = _failed_runtime_projection_values()
    runtime = _exact_runtime_evidence_values(projection)
    runtime.update(
        status="failed",
        rows_succeeded=0,
        rows_failed=1,
        output_rows=0,
        sink_outputs=(),
        observed_error={"exception_type": "CoalesceCollisionError"},
    )
    audit = _exact_audit_evidence_values(projection)

    with pytest.raises(ValidationError, match="observed expected-error runtime requires portable export unavailable_by_policy"):
        ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=runtime, audit=audit))


def test_stable_node_state_preserves_canonical_context_and_error_json() -> None:
    values = _exact_runtime_projection_values()
    states = cast(list[dict[str, object]], values["node_states"])
    state = states[0]
    state["context_after"] = '{"branches_arrived":["path_a","path_b"],"wait_duration_ms":"$DURATION_MS"}'
    state["error"] = '{"failure_reason":"quorum_impossible:need=3,max_possible=2"}'

    projection = corpus_schema.StableRunProjection.model_validate(values)

    assert projection.node_states[0].context_after == state["context_after"]
    assert projection.node_states[0].error == state["error"]


def test_stable_node_state_rejects_noncanonical_context_and_error_json() -> None:
    for field in ("context_after", "error"):
        values = _exact_runtime_projection_values()
        states = cast(list[dict[str, object]], values["node_states"])
        state = states[0]
        state[field] = '{"z":1, "a":2}'

        with pytest.raises(ValidationError, match=f"{field} must use canonical JSON"):
            corpus_schema.StableRunProjection.model_validate(values)


def test_absent_optional_audit_hash_preserves_pre_b3_serialization_shape() -> None:
    disposition = corpus_schema.StableTerminalDisposition(
        key="primary:0#0",
        token_key="primary:0#0",
        outcome="success",
        path="default_flow",
        sink_name="output",
    )

    assert disposition.model_dump(mode="json") == {
        "key": "primary:0#0",
        "token_key": "primary:0#0",
        "outcome": "success",
        "path": "default_flow",
        "sink_name": "output",
    }


def test_semantic_projection_excludes_only_audit_error_hash_while_raw_projection_retains_it() -> None:
    values = _exact_runtime_projection_values()
    dispositions = cast(tuple[dict[str, object], ...], values["terminal_dispositions"])
    dispositions[0]["error_hash"] = "a" * 16
    raw = corpus_schema.StableRunProjection.model_validate(values)
    without_hash = raw.model_copy(
        update={
            "terminal_dispositions": tuple(disposition.model_copy(update={"error_hash": None}) for disposition in raw.terminal_dispositions)
        }
    )

    assert raw.terminal_dispositions[0].error_hash == "a" * 16
    assert "error_hash" in raw.terminal_dispositions[0].model_dump(mode="json")
    assert semantic_runtime_projection(raw) == semantic_runtime_projection(without_hash)
    assert semantic_runtime_projection(raw).node_states == raw.node_states


def test_empty_stateful_families_preserve_pre_b3_semantic_shape_and_hash() -> None:
    raw = corpus_schema.StableRunProjection.model_validate(_exact_runtime_projection_values())
    semantic = semantic_runtime_projection(raw)

    assert tuple(semantic.model_dump(mode="json")) == (
        "rows",
        "tokens",
        "node_states",
        "routes",
        "terminal_dispositions",
        "scheduler_work",
    )
    assert corpus_harness.semantic_runtime_projection_sha256(semantic) == (
        "c5c717143eb397468f065d99ee7ef9464305f349c154f211867202e2e9d1a02d"
    )
    assert corpus_harness.semantic_runtime_projection_counts(semantic).model_dump(mode="json") == {
        "rows": 1,
        "tokens": 1,
        "parent_links": 0,
        "node_states": 1,
        "routes": 0,
        "terminal_dispositions": 1,
        "scheduler_work": 1,
    }


@pytest.mark.parametrize(
    ("scenario_id", "case_id", "family", "count_field"),
    (
        ("aggregation-immutable-batch", "eof-immutable-membership", "intermediate_outcomes", "intermediate_outcomes"),
        ("aggregation-immutable-batch", "eof-immutable-membership", "batches", "batches"),
        ("row-expansion-parent-child-recovery", "json-explode-parent-child", "expansions", "expansions"),
        ("retry-quarantine-discard-routed-errors", "source-quarantine-routed", "validation_errors", "validation_errors"),
        ("retry-quarantine-discard-routed-errors", "transform-discard", "transform_errors", "transform_errors"),
    ),
)
def test_nonempty_stateful_family_changes_semantic_projection_and_hash(
    scenario_id: str,
    case_id: str,
    family: str,
    count_field: str,
) -> None:
    manifest = load_manifest()
    case = next(case for scenario, case in iter_harness_cases(manifest) if (scenario.id, case.id) == (scenario_id, case_id))
    assert isinstance(case.expected, RunExpectation)
    raw = case.expected.projection
    semantic = semantic_runtime_projection(raw)

    assert getattr(semantic, family) == getattr(raw, family)
    assert getattr(corpus_harness.semantic_runtime_projection_counts(semantic), count_field) > 0
    if family == "batches":
        first_batch, *remaining = semantic.batches
        mutated_value = (
            first_batch.model_copy(update={"trigger_reason": "semantic-mutation"}),
            *remaining,
        )
    else:
        mutated_value = ()
    mutated = semantic.model_copy(update={family: mutated_value})

    assert corpus_harness.semantic_runtime_projection_sha256(mutated) != (corpus_harness.semantic_runtime_projection_sha256(semantic))


def test_exact_failure_evidence_requires_typed_node_transform_and_validation_errors() -> None:
    projection = _exact_runtime_projection_values()
    states = cast(tuple[dict[str, object], ...], projection["node_states"])
    states[0]["error"] = '{"error":"boom","reason":"invalid_input"}'
    projection["validation_errors"] = (
        {
            "key": "primary:0|source:primary@stable|0",
            "node_key": "source:primary@stable",
            "row_key": "primary:0",
            "row_hash": "b" * 64,
            "row_data": None,
            "error": "invalid source row",
            "schema_mode": "fixed",
            "destination": "quarantine",
            "violation_type": None,
            "original_field_name": None,
            "normalized_field_name": None,
            "expected_type": None,
            "actual_type": None,
        },
    )
    projection["transform_errors"] = (
        {
            "key": "primary:0#0|transform:fail@stable",
            "token_key": "primary:0#0",
            "transform_node_key": "transform:fail@stable",
            "row_hash": "c" * 64,
            "row_data": None,
            "error_details": '{"error":"boom","reason":"invalid_input"}',
            "destination": "discard",
        },
    )
    runtime = _exact_runtime_evidence_values(projection)
    audit = _exact_audit_evidence_values(projection)
    counts = list(cast(tuple[dict[str, object], ...], audit["record_counts"]))
    counts.extend(
        (
            {"record_type": "transform_error", "count": 1},
            {"record_type": "validation_error", "count": 1},
        )
    )
    audit["record_counts"] = tuple(sorted(counts, key=lambda record: cast(str, record["record_type"])))
    audit["total_records"] = cast(int, audit["total_records"]) + 2

    evidence = ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=runtime, audit=audit))
    assert evidence.runtime.durable_projection is not None
    assert evidence.runtime.durable_projection.node_states[0].error == states[0]["error"]
    assert len(evidence.runtime.durable_projection.transform_errors) == 1
    assert len(evidence.runtime.durable_projection.validation_errors) == 1

    incomplete = deepcopy(projection)
    incomplete["transform_errors"] = ()
    incomplete_runtime = _exact_runtime_evidence_values(incomplete)
    incomplete_audit = dict(audit, portable_projection=incomplete)
    with pytest.raises(ValidationError, match="audit record count for transform_error must match exact durable projection"):
        ScenarioRunEvidence.model_validate(_scenario_exact_evidence_values(runtime=incomplete_runtime, audit=incomplete_audit))


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

    with pytest.raises(ValidationError, match="rows_processed must equal distinct projected rows with terminal outcomes"):
        RunExpectation.model_validate(values)


@pytest.mark.parametrize("container", ("expectation", "runtime"))
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "rows_succeeded",
            0,
            "rows_succeeded must equal projected successful terminal publications",
        ),
        ("rows_failed", 1, "rows_failed must equal projected failed terminal dispositions"),
    ),
)
def test_exact_projection_rejects_contradictory_terminal_counters(
    container: str,
    field: str,
    value: int,
    message: str,
) -> None:
    projection = _exact_runtime_projection_values()
    validator: type[RunExpectation] | type[RuntimeEvidence]
    if container == "expectation":
        values = _exact_run_expectation_values()
        validator = RunExpectation
    else:
        values = _exact_runtime_evidence_values(projection)
        validator = RuntimeEvidence
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        validator.model_validate(values)


def test_run_expectation_rejects_unjustified_transient_fork_parent() -> None:
    values = _exact_run_expectation_values()
    values["rows_succeeded"] = 0
    projection = cast(dict[str, object], values["projection"])
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
    )

    with pytest.raises(ValidationError, match="transient fork_parent token must parent a projected child token"):
        RunExpectation.model_validate(values)


def test_runtime_evidence_rejects_unjustified_transient_fork_parent() -> None:
    projection = _exact_runtime_projection_values()
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
    )
    values = {
        "kind": "exact",
        "attempted": True,
        "run_id": "run-1",
        "status": "completed",
        "rows_processed": 1,
        "rows_succeeded": 0,
        "rows_failed": 0,
        "output_rows": 1,
        "sink_outputs": ({"sink_name": "output", "rows": ('{"id":1,"value":10}',)},),
        "durable_projection": projection,
    }

    with pytest.raises(ValidationError, match="transient fork_parent token must parent a projected child token"):
        RuntimeEvidence.model_validate(values)


def test_exact_projection_rejects_zero_terminal_outcomes_when_fork_parent_has_child() -> None:
    values = _exact_run_expectation_values()
    values["rows_succeeded"] = 0
    projection = cast(dict[str, object], values["projection"])
    projection["tokens"] = (
        {"key": "primary:0#0", "row_key": "primary:0", "parents": ()},
        {
            "key": "primary:0#1",
            "row_key": "primary:0",
            "parents": ({"ordinal": 0, "parent_key": "primary:0#0"},),
        },
    )
    projection["terminal_dispositions"] = (
        {
            "key": "primary:0#0",
            "token_key": "primary:0#0",
            "outcome": "transient",
            "path": "fork_parent",
            "sink_name": None,
        },
        {
            "key": "primary:0#1",
            "token_key": "primary:0#1",
            "outcome": "transient",
            "path": "batch_consumed",
            "sink_name": None,
        },
    )

    with pytest.raises(ValidationError, match="non-empty projection must contain a terminal success or failure outcome"):
        RunExpectation.model_validate(values)


@pytest.mark.parametrize("container", ("expectation", "runtime"))
def test_exact_projection_rejects_fork_parent_with_only_disconnected_terminal(container: str) -> None:
    projection = _projection_with_disconnected_terminal(cyclic=False)
    validator: type[RunExpectation] | type[RuntimeEvidence]
    if container == "expectation":
        values = _exact_run_expectation_values()
        values["projection"] = projection
        validator = RunExpectation
    else:
        values = _exact_runtime_evidence_values(projection)
        validator = RuntimeEvidence

    with pytest.raises(ValidationError, match="transient fork_parent token must reach a terminal descendant"):
        validator.model_validate(values)


@pytest.mark.parametrize("container", ("expectation", "runtime"))
def test_exact_projection_rejects_cyclic_token_parent_graph(container: str) -> None:
    projection = _projection_with_disconnected_terminal(cyclic=True)
    validator: type[RunExpectation] | type[RuntimeEvidence]
    if container == "expectation":
        values = _exact_run_expectation_values()
        values["projection"] = projection
        validator = RunExpectation
    else:
        values = _exact_runtime_evidence_values(projection)
        validator = RuntimeEvidence

    with pytest.raises(ValidationError, match="projected token parent graph must be acyclic"):
        validator.model_validate(values)


def test_expected_dimension_and_scenario_constants_are_exact_and_ordered() -> None:
    assert EXPECTED_DIMENSIONS == EXPECTED_DIMENSION_VALUES
    assert EXPECTED_SCENARIOS == EXPECTED_SCENARIO_VALUES


def test_closed_vocabularies_are_exact() -> None:
    assert get_args(CellStatus) == ("pass", "partial", "fail", "unknown", "not_applicable")
    assert get_args(Dimension) == EXPECTED_DIMENSION_VALUES
    assert get_args(EvidenceKind) == ("harness", "pytest", "document", "decision")
    assert get_args(Stage) == ("config", "build", "runtime", "audit", "recovery")
    assert get_args(Workflow) == ("run", "recovery", "build")
    assert get_args(RecoveryKind) == (
        "eof_aggregation",
        "expansion_child_enqueue",
        "parallel_sink_finalization",
        "pending_sink_redrive",
        "sink_boundary",
        "terminal_resume_idempotence",
    )


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


def test_parallel_sink_finalization_recovery_pins_stable_identity_and_honest_ceiling() -> None:
    sink_recovery = _valid_sink_finalization_recovery()
    values = _valid_recovery().model_dump(mode="json")
    values["sink_finalization"] = sink_recovery.model_dump(mode="json")
    evidence = RecoveryEvidence.model_validate(values)

    assert evidence.sink_finalization == sink_recovery
    assert sink_recovery.first_effect_id_before == sink_recovery.first_effect_id_after
    assert sink_recovery.first_artifact_id_before == sink_recovery.first_artifact_id_after
    assert sink_recovery.first_attempt_ids_before == sink_recovery.first_attempt_ids_after
    assert sink_recovery.held_barrier_proven is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("first_effect_id_after", "e" * 64, "effect identity must be stable"),
        ("first_artifact_id_after", "e" * 64, "artifact identity must be stable"),
        ("first_attempt_ids_after", ("attempt-a", "attempt-b", "attempt-z"), "attempt identities must be stable"),
        ("second_effect_id_after", "a" * 64, "distinct effect identities"),
        ("second_artifact_id_after", "b" * 64, "distinct artifact identities"),
    ],
)
def test_parallel_sink_finalization_recovery_rejects_identity_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _valid_sink_finalization_recovery().model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        ParallelSinkFinalizationRecoveryEvidence.model_validate(values)


def test_pending_sink_redrive_recovery_pins_exact_preserved_bundle_and_effect_counts() -> None:
    pending_redrive = _valid_pending_sink_redrive_recovery()
    values = _valid_recovery().model_dump(mode="json")
    values["pending_sink_redrive"] = pending_redrive.model_dump(mode="json")

    evidence = RecoveryEvidence.model_validate(values)

    assert evidence.pending_sink_redrive == pending_redrive
    assert pending_redrive.scheduler_attempt_before == pending_redrive.scheduler_attempt_after == 1
    assert pending_redrive.sink_effects_before == pending_redrive.artifacts_before == 0
    assert pending_redrive.sink_effects_after == pending_redrive.artifacts_after == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("work_item_id_after", "work-drift", "preserve exact work item identity"),
        ("work_item_id_claimed", "work-drift", "preserve exact work item identity"),
        ("token_id_after", "token-drift", "preserve exact token identity"),
        ("token_id_claimed", "token-drift", "preserve exact token identity"),
        ("row_id_after", "row-drift", "preserve exact row identity"),
        ("row_id_claimed", "row-drift", "preserve exact row identity"),
        ("row_payload_hash_after", "d" * 64, "preserve exact row payload identity"),
        ("row_payload_hash_claimed", "d" * 64, "preserve exact row payload identity"),
        ("pending_sink_name_after", "other", "preserve exact sink name identity"),
        ("pending_sink_name_claimed", "other", "preserve exact sink name identity"),
        ("recover_event_work_item_id", "work-drift", "identify the exact recovered work item and token"),
        ("recover_event_token_id", "token-drift", "identify the exact recovered work item and token"),
        ("recover_event_from_lease_owner", "other-worker", "clear the exact expired lease owner"),
        ("reclaimed_lease_owner_after", "worker-before", "reclaimed by a fresh lease owner"),
        ("member_effect_id_after", "d" * 64, "member must retain the sole effect identity"),
        ("attempt_effect_ids_after", ("b" * 64, "d" * 64, "b" * 64), "attempts must retain the sole effect identity"),
        ("artifact_effect_id_after", "d" * 64, "artifact must retain the sole effect identity"),
        ("effect_attempt_ids_after", ("attempt-a", "attempt-a", "attempt-c"), "three unique sorted sink-effect attempt identities"),
    ),
)
def test_pending_sink_redrive_recovery_rejects_identity_event_and_attempt_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _valid_pending_sink_redrive_recovery().model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        PendingSinkRedriveRecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("scheduler_attempt_after", 2),
        ("scheduler_attempt_claimed", 2),
        ("pending_outcome_claimed", "discard"),
        ("pending_path_claimed", "error_route"),
        ("pending_error_hash_claimed", "unexpected-error"),
        ("pending_error_message_claimed", "unexpected error"),
        ("pending_error_hash_before", "unexpected-error"),
        ("pending_error_message_after", "unexpected error"),
        ("sink_effects_before", 1),
        ("publications_after", 0),
    ),
)
def test_pending_sink_redrive_recovery_rejects_non_exact_boundary_values(field: str, value: object) -> None:
    values = _valid_pending_sink_redrive_recovery().model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError):
        PendingSinkRedriveRecoveryEvidence.model_validate(values)


def test_sink_boundary_recovery_pins_exact_work_effect_and_resume_identity() -> None:
    proof = _valid_sink_boundary_recovery()
    values = _valid_recovery().model_dump(mode="json")
    values["sink_boundary"] = proof.model_dump(mode="json")

    evidence = RecoveryEvidence.model_validate(values)

    assert evidence.sink_boundary == proof


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("work-id", "preserve scheduler work identities"),
        ("effect-artifact", "preserve exact effect and member identity"),
        ("effect-state", "finalize the original effect identity"),
        ("payload-state", "transition pending-sink payloads"),
        ("payload-anchor", "pending-sink purge anchor must equal the token identity hash"),
    ),
)
def test_sink_boundary_recovery_rejects_identity_and_payload_drift(mutation: str, message: str) -> None:
    values = _valid_sink_boundary_recovery().model_dump(mode="json")
    if mutation == "work-id":
        cast(list[dict[str, object]], values["work_after"])[0]["work_item_id"] = "reminted-work"
    elif mutation == "effect-artifact":
        cast(list[dict[str, object]], values["effects_after"])[0]["artifact_id"] = "7" * 64
    elif mutation == "effect-state":
        cast(list[dict[str, object]], values["effects_after"])[0]["state"] = "in_flight"
    elif mutation == "payload-state":
        cast(list[dict[str, object]], values["work_after"])[0].update(
            row_payload_state="live",
            row_payload_anchor_sha256=None,
        )
    else:
        cast(list[dict[str, object]], values["work_after"])[0]["row_payload_anchor_sha256"] = "3" * 64

    with pytest.raises(ValidationError, match=message):
        SinkBoundaryRecoveryEvidence.model_validate(values)


def test_sink_boundary_recovery_preserves_non_token_anchor_for_already_terminal_work() -> None:
    proof = _valid_sink_boundary_recovery()
    terminal_work = proof.work_before[0].model_copy(
        update={
            "work_item_id": "work-0",
            "token_id": "token-0",
            "row_id": "row-0",
            "row_payload_sha256": "8" * 64,
            "row_payload_state": "purged",
            "row_payload_anchor_sha256": "9" * 64,
            "node_id": "barrier-1",
            "status": "terminal",
            "pending_sink_name": None,
            "pending_outcome": None,
            "pending_path": None,
            "pending_error_hash": None,
            "pending_error_message": None,
        }
    )
    values = proof.model_dump(mode="json")
    values["token_ids_before"] = ["token-0", "token-1"]
    values["token_ids_after"] = ["token-0", "token-1"]
    cast(list[dict[str, object]], values["work_before"]).insert(0, terminal_work.model_dump(mode="json"))
    cast(list[dict[str, object]], values["work_after"]).insert(0, terminal_work.model_dump(mode="json"))

    validated = SinkBoundaryRecoveryEvidence.model_validate(values)

    assert validated.work_before[0].row_payload_anchor_sha256 == "9" * 64
    assert validated.work_after[0] == validated.work_before[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("original_batch_id_after", "batch-drift", "preserve the original failed batch identity"),
        ("recovery_batch_id_after", "batch-original", "distinct retry batch attempt"),
        ("member_token_ids_after", ("token-0", "token-2", "token-1"), "preserve immutable ordered batch membership"),
        ("membership_unchanged", False, "Input should be True"),
    ),
)
def test_eof_aggregation_recovery_rejects_identity_and_flag_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _valid_aggregation_eof_recovery().model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        AggregationEOFRecoveryEvidence.model_validate(values)


def test_eof_aggregation_recovery_rejects_duplicate_raw_member_ids() -> None:
    values = _valid_aggregation_eof_recovery().model_dump(mode="json")
    values["member_token_ids_before"] = ("token-0", "token-0", "token-2")
    values["member_token_ids_after"] = ("token-0", "token-0", "token-2")

    with pytest.raises(ValidationError, match="exactly three unique batch members"):
        AggregationEOFRecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("wrong-attempt", "failed-attempt then completed-retry"),
        ("missing-member", "exact three-member membership"),
        ("stable-member-drift", "reuse exact stable member identity and order"),
        ("invalid-ordinal", "ordinals must be dense from zero"),
    ),
)
def test_eof_aggregation_recovery_rejects_invalid_final_batch_evidence(mutation: str, message: str) -> None:
    values = _valid_aggregation_eof_recovery().model_dump(mode="json")
    batches = cast(list[dict[str, object]], values["final_batches"])
    if mutation == "wrong-attempt":
        batches[1]["attempt"] = 2
    elif mutation == "missing-member":
        batches[1]["members"] = cast(list[object], batches[1]["members"])[:2]
    elif mutation == "stable-member-drift":
        members = cast(list[dict[str, object]], batches[1]["members"])
        members[2]["token_key"] = "primary:99#0"
    else:
        members = cast(list[dict[str, object]], batches[1]["members"])
        members[0]["ordinal"] = 1

    with pytest.raises(ValidationError, match=message):
        AggregationEOFRecoveryEvidence.model_validate(values)


def test_terminal_resume_idempotence_pins_terminal_equivalence_and_every_no_mutation_view() -> None:
    proof = _valid_terminal_resume_idempotence()
    values = _valid_recovery().model_dump(mode="json")
    values["terminal_resume_idempotence"] = proof.model_dump(mode="json")

    evidence = RecoveryEvidence.model_validate(values)

    assert evidence.terminal_resume_idempotence == proof
    assert proof.control_terminal_projection == proof.resumed_terminal_projection
    assert proof.fresh_object_lifetimes == 4
    assert proof.database_sha256_before == proof.database_sha256_after
    assert proof.durable_records_sha256_before == proof.durable_records_sha256_after
    assert proof.portable_export_sha256_before == proof.portable_export_sha256_after
    assert proof.output_tree_sha256_before == proof.output_tree_sha256_after
    assert proof.artifact_digests_before == proof.artifact_digests_after


def test_terminal_resume_case_requires_manifest_pinned_full_history_hash() -> None:
    manifest = load_manifest()
    case = next(
        case
        for scenario in manifest.scenarios
        if scenario.id == "checkpoint-deterministic-resume"
        for case in scenario.cases
        if case.id == "reopen-resume"
    )
    values = case.model_dump(mode="json")
    expected = cast(dict[str, object], values["expected"])
    expected.pop("resumed_full_projection_sha256", None)

    with pytest.raises(ValidationError, match="terminal-resume recovery requires a pinned full-history hash"):
        HarnessCaseSpec.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("second_resume_error_run_id", "run-other", "same completed run"),
        ("database_sha256_after", "2" * 64, "database bytes"),
        ("durable_records_sha256_after", "2" * 64, "durable records"),
        ("portable_export_sha256_after", "2" * 64, "portable export"),
        ("output_tree_sha256_after", "2" * 64, "output tree"),
        ("artifact_digests_after", ({"path": "output.jsonl", "sha256": "2" * 64},), "artifact bytes"),
        ("zero_mutation", False, "Input should be True"),
    ),
)
def test_terminal_resume_idempotence_rejects_identity_and_no_mutation_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    values = _valid_terminal_resume_idempotence().model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        corpus_schema.TerminalResumeIdempotenceEvidence.model_validate(values)


def test_terminal_resume_idempotence_rejects_semantic_terminal_drift() -> None:
    values = _valid_terminal_resume_idempotence().model_dump(mode="json")
    resumed = cast(dict[str, object], values["resumed_terminal_projection"])
    sink_outputs = cast(list[dict[str, object]], resumed["sink_outputs"])
    sink_outputs[0]["rows"] = ('{"count":2,"value":60}',)

    with pytest.raises(ValidationError, match="terminal projections must be exactly equal"):
        corpus_schema.TerminalResumeIdempotenceEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "bad_key", "message"),
    (
        ("terminal_node_states", "node-key-drift", "terminal node-state key"),
        ("terminal_scheduler_work", "work-key-drift", "terminal scheduler-work key"),
    ),
)
def test_terminal_equivalence_projection_rejects_derived_key_drift(
    field: str,
    bad_key: str,
    message: str,
) -> None:
    values = _valid_terminal_equivalence_projection()
    records = cast(tuple[dict[str, object], ...], values[field])
    records[0]["key"] = bad_key

    with pytest.raises(ValidationError, match=message):
        corpus_schema.TerminalEquivalenceProjection.model_validate(values)


def test_terminal_equivalence_projection_rejects_completed_batch_key_drift() -> None:
    values = _valid_terminal_equivalence_projection()
    values["completed_batches"] = (
        {
            "key": "batch-key-drift",
            "aggregation_node_key": "aggregation:eof_sum@stable",
            "trigger_type": "end_of_source",
            "trigger_reason": None,
            "member_token_keys": ("primary:0#0",),
        },
    )

    with pytest.raises(ValidationError, match="terminal batch key"):
        corpus_schema.TerminalEquivalenceProjection.model_validate(values)


@pytest.mark.parametrize(
    "field",
    (
        "parent_token_ids",
        "child_token_ids",
        "expand_group_ids",
        "scheduler_work_ids",
        "parent_scheduler_work_ids",
        "child_scheduler_work_ids",
    ),
)
def test_expansion_recovery_rejects_pre_post_identity_drift(field: str) -> None:
    values = _valid_expansion_child_enqueue_recovery().model_dump(mode="json")
    after_field = f"{field}_after"
    after = cast(list[str], values[after_field])
    after[-1] = f"{after[-1]}-drift"

    with pytest.raises(ValidationError, match=r"must preserve .* identities"):
        ExpansionChildEnqueueRecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    "field",
    (
        "parent_token_ids",
        "child_token_ids",
        "expand_group_ids",
        "scheduler_work_ids",
        "parent_scheduler_work_ids",
        "child_scheduler_work_ids",
    ),
)
def test_expansion_recovery_rejects_duplicate_identities(field: str) -> None:
    values = _valid_expansion_child_enqueue_recovery().model_dump(mode="json")
    before_field = f"{field}_before"
    after_field = f"{field}_after"
    duplicated = cast(list[str], values[before_field])
    duplicated[-1] = duplicated[0]
    values[after_field] = duplicated

    with pytest.raises(ValidationError, match=r"requires unique .* identities"):
        ExpansionChildEnqueueRecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing-parent", "exactly 3 parents, 6 children, and 3 groups"),
        ("missing-scheduler", "exactly nine stable scheduler work identities"),
        ("token-overlap", "parent and child token identities must be disjoint"),
        ("wrong-work-cardinality", "exactly 3 parent and 6 child scheduler identities"),
        ("wrong-work-partition", "exactly partition parent and child work"),
        ("wrong-group-shape", "exact 2/1/3 child groups"),
        ("invalid-child-ordinal", "ordinals must be dense from zero"),
        ("false-identity-flag", "Input should be True"),
    ),
)
def test_expansion_recovery_rejects_inexact_partition_cardinality_and_shape(mutation: str, message: str) -> None:
    values = _valid_expansion_child_enqueue_recovery().model_dump(mode="json")
    if mutation == "missing-parent":
        values["parent_token_ids_before"] = cast(list[str], values["parent_token_ids_before"])[:2]
        values["parent_token_ids_after"] = cast(list[str], values["parent_token_ids_after"])[:2]
    elif mutation == "missing-scheduler":
        values["scheduler_work_ids_before"] = cast(list[str], values["scheduler_work_ids_before"])[:-1]
        values["scheduler_work_ids_after"] = cast(list[str], values["scheduler_work_ids_after"])[:-1]
    elif mutation == "token-overlap":
        child_ids = cast(list[str], values["child_token_ids_before"])
        child_ids[0] = cast(list[str], values["parent_token_ids_before"])[0]
        values["child_token_ids_after"] = child_ids
    elif mutation == "wrong-work-cardinality":
        parent_work = cast(list[str], values["parent_scheduler_work_ids_before"])
        child_work = cast(list[str], values["child_scheduler_work_ids_before"])
        values["parent_scheduler_work_ids_before"] = parent_work[:2]
        values["parent_scheduler_work_ids_after"] = parent_work[:2]
        values["child_scheduler_work_ids_before"] = [parent_work[2], *child_work]
        values["child_scheduler_work_ids_after"] = [parent_work[2], *child_work]
    elif mutation == "wrong-work-partition":
        child_work = cast(list[str], values["child_scheduler_work_ids_before"])
        child_work[0] = cast(list[str], values["parent_scheduler_work_ids_before"])[0]
        values["child_scheduler_work_ids_after"] = child_work
    elif mutation == "wrong-group-shape":
        expansions = cast(list[dict[str, object]], values["final_expansions"])
        expansions[0], expansions[1] = expansions[1], expansions[0]
    elif mutation == "invalid-child-ordinal":
        expansions = cast(list[dict[str, object]], values["final_expansions"])
        children = cast(list[dict[str, object]], expansions[0]["children"])
        children[0]["ordinal"] = 1
    else:
        values["scheduler_identity_unchanged"] = False

    with pytest.raises(ValidationError, match=message):
        ExpansionChildEnqueueRecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    "seams",
    (
        ("sink_finalization", "aggregation_eof"),
        ("sink_finalization", "expansion_child_enqueue"),
        ("sink_finalization", "pending_sink_redrive"),
        ("sink_finalization", "sink_boundary"),
        ("sink_finalization", "terminal_resume_idempotence"),
        ("aggregation_eof", "expansion_child_enqueue"),
        ("aggregation_eof", "pending_sink_redrive"),
        ("aggregation_eof", "terminal_resume_idempotence"),
        ("expansion_child_enqueue", "pending_sink_redrive"),
        ("expansion_child_enqueue", "terminal_resume_idempotence"),
        ("pending_sink_redrive", "terminal_resume_idempotence"),
        (
            "sink_finalization",
            "aggregation_eof",
            "expansion_child_enqueue",
            "pending_sink_redrive",
            "sink_boundary",
            "terminal_resume_idempotence",
        ),
    ),
)
def test_recovery_evidence_rejects_multiple_seam_specific_proofs(seams: tuple[str, ...]) -> None:
    proofs = {
        "sink_finalization": _valid_sink_finalization_recovery(),
        "aggregation_eof": _valid_aggregation_eof_recovery(),
        "expansion_child_enqueue": _valid_expansion_child_enqueue_recovery(),
        "pending_sink_redrive": _valid_pending_sink_redrive_recovery(),
        "sink_boundary": _valid_sink_boundary_recovery(),
        "terminal_resume_idempotence": _valid_terminal_resume_idempotence(),
    }
    values = _valid_recovery().model_dump(mode="json")
    for seam in seams:
        values[seam] = proofs[seam].model_dump(mode="json")

    with pytest.raises(ValidationError, match="at most one seam-specific proof"):
        RecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("seam", "proof"),
    (
        ("sink_finalization", _valid_sink_finalization_recovery()),
        ("aggregation_eof", _valid_aggregation_eof_recovery()),
        ("expansion_child_enqueue", _valid_expansion_child_enqueue_recovery()),
        ("pending_sink_redrive", _valid_pending_sink_redrive_recovery()),
        ("sink_boundary", _valid_sink_boundary_recovery()),
        ("terminal_resume_idempotence", _valid_terminal_resume_idempotence()),
    ),
)
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("database_reopened", False),
        ("can_resume", False),
        ("source_replayed", True),
        ("checkpoint_removed", False),
    ),
)
def test_seam_specific_recovery_rejects_contradictory_public_resume_flags(
    seam: str,
    proof: BaseModel,
    field: str,
    value: bool,
) -> None:
    values = _valid_recovery().model_dump(mode="json")
    values[seam] = proof.model_dump(mode="json")
    values[field] = value

    with pytest.raises(ValidationError, match="requires successful fresh public resume flags"):
        RecoveryEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("field", "proof"),
    (
        ("sink_finalization", _valid_sink_finalization_recovery()),
        ("aggregation_eof", _valid_aggregation_eof_recovery()),
        ("expansion_child_enqueue", _valid_expansion_child_enqueue_recovery()),
        ("pending_sink_redrive", _valid_pending_sink_redrive_recovery()),
        ("sink_boundary", _valid_sink_boundary_recovery()),
        ("terminal_resume_idempotence", _valid_terminal_resume_idempotence()),
    ),
)
def test_unattempted_recovery_forbids_seam_specific_proof(field: str, proof: BaseModel) -> None:
    values: dict[str, object] = {
        "attempted": False,
        "database_reopened": False,
        "can_resume": False,
        "source_replayed": False,
        "checkpoint_removed": False,
        field: proof.model_dump(mode="json"),
    }
    with pytest.raises(ValidationError, match="unattempted"):
        RecoveryEvidence.model_validate(values)


def test_scenario_run_evidence_accepts_the_complete_observed_shape() -> None:
    evidence = ScenarioRunEvidence(
        schema_version=2,
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
    for dimension in ("runtime", "audit"):
        cell = deepcopy(_raw_dimensions(scenario)[dimension])
        cell["evidence"] = [*cast(list[str], cell.get("evidence", [])), evidence_id]
        _raw_dimensions(scenario)[dimension] = cell


def test_manifest_has_exact_inventory_status_matrix_and_registered_cases() -> None:
    manifest = load_manifest()

    assert manifest.schema_version == 2
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
        ("conditional-routing", "error-route-and-discard"),
        ("fork-multiple-terminals-partial-failure", "one-terminal-fails"),
        *(("fork-coalesce-policies", case_id) for case_id in B2_COALESCE_CASE_IDS),
        ("sequential-nested-fork-coalesce", "two-sequential-require-all"),
        ("parallel-coalesces", "two-parallel-require-all"),
        ("parallel-coalesces", "resume-after-left-finalize"),
        ("aggregation-immutable-batch", "resume-after-eof-flush-fault"),
        ("aggregation-immutable-batch", "eof-immutable-membership"),
        ("row-expansion-parent-child-recovery", "resume-after-child-enqueue"),
        ("row-expansion-parent-child-recovery", "json-explode-parent-child"),
        ("retry-quarantine-discard-routed-errors", "retry-then-success"),
        ("retry-quarantine-discard-routed-errors", "source-quarantine-routed"),
        ("retry-quarantine-discard-routed-errors", "transform-discard"),
        ("retry-quarantine-discard-routed-errors", "transform-error-route"),
        ("sink-write-pending-redrive", "write-once"),
        ("sink-write-pending-redrive", "pending-redrive-reopen"),
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
    assert len(manifest.evidence) == 100
    assert len(assessment_evidence) == 61
    assert len(harness_evidence) == 39
    assert len({reference.id for reference in manifest.evidence}) == 100
    assert len({reference.locator for reference in manifest.evidence}) == 100
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
        ("conditional-routing", "error-route-and-discard"),
        ("fork-multiple-terminals-partial-failure", "one-terminal-fails"),
        *(("fork-coalesce-policies", case_id) for case_id in B2_COALESCE_CASE_IDS),
        ("sequential-nested-fork-coalesce", "two-sequential-require-all"),
        ("parallel-coalesces", "two-parallel-require-all"),
        ("parallel-coalesces", "resume-after-left-finalize"),
        ("aggregation-immutable-batch", "resume-after-eof-flush-fault"),
        ("aggregation-immutable-batch", "eof-immutable-membership"),
        ("row-expansion-parent-child-recovery", "resume-after-child-enqueue"),
        ("row-expansion-parent-child-recovery", "json-explode-parent-child"),
        ("retry-quarantine-discard-routed-errors", "retry-then-success"),
        ("retry-quarantine-discard-routed-errors", "source-quarantine-routed"),
        ("retry-quarantine-discard-routed-errors", "transform-discard"),
        ("retry-quarantine-discard-routed-errors", "transform-error-route"),
        ("sink-write-pending-redrive", "write-once"),
        ("sink-write-pending-redrive", "pending-redrive-reopen"),
        ("checkpoint-deterministic-resume", "reopen-resume"),
    )
    normalized_cases = json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(normalized_cases).hexdigest() == EXPECTED_CASE_REGISTRY_SHA256
    linear_case = cases[0][1]
    assert linear_case["input_fixtures"] == {"primary": "linear/input.csv"}
    assert linear_case["output_artifacts"] == {"output": {"filename": "output.jsonl", "presence": "required"}}
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
            ("multiple-independent-sources", "runtime"),
            ("multiple-independent-sources", "audit"),
        ),
        "harness-multi-source-queue-fan-in-queued-fan-in": (
            ("multi-source-queue-fan-in", "config"),
            ("multi-source-queue-fan-in", "build"),
            ("multi-source-queue-fan-in", "runtime"),
            ("multi-source-queue-fan-in", "audit"),
        ),
        "harness-conditional-routing-two-way-gate": (
            ("conditional-routing", "config"),
            ("conditional-routing", "build"),
            ("conditional-routing", "runtime"),
            ("conditional-routing", "audit"),
        ),
        "harness-conditional-routing-error-route-and-discard": (
            ("conditional-routing", "config"),
            ("conditional-routing", "build"),
            ("conditional-routing", "runtime"),
            ("conditional-routing", "audit"),
        ),
        "harness-fork-multiple-terminals-partial-failure-one-terminal-fails": (
            ("fork-multiple-terminals-partial-failure", "runtime"),
            ("fork-multiple-terminals-partial-failure", "audit"),
        ),
        **{
            f"harness-fork-coalesce-policies-{case_id}": (
                (
                    ("fork-coalesce-policies", "config"),
                    ("fork-coalesce-policies", "build"),
                    ("fork-coalesce-policies", "runtime"),
                    ("fork-coalesce-policies", "audit"),
                )
                if case_id == "union-collision-fail"
                else (
                    ("fork-coalesce-policies", "config"),
                    ("fork-coalesce-policies", "build"),
                    ("fork-coalesce-policies", "runtime"),
                )
            )
            for case_id in B2_COALESCE_CASE_IDS
        },
        "harness-sequential-nested-fork-coalesce-two-sequential-require-all": (
            ("sequential-nested-fork-coalesce", "build"),
            ("sequential-nested-fork-coalesce", "runtime"),
        ),
        "harness-parallel-coalesces-two-parallel-require-all": (
            ("parallel-coalesces", "build"),
            ("parallel-coalesces", "runtime"),
        ),
        "harness-parallel-coalesces-resume-after-left-finalize": (("parallel-coalesces", "recovery"),),
        "harness-aggregation-immutable-batch-eof-immutable-membership": (
            ("aggregation-immutable-batch", "runtime"),
            ("aggregation-immutable-batch", "audit"),
        ),
        "harness-aggregation-immutable-batch-resume-after-eof-flush-fault": (("aggregation-immutable-batch", "recovery"),),
        "harness-row-expansion-parent-child-recovery-json-explode-parent-child": (
            ("row-expansion-parent-child-recovery", "runtime"),
            ("row-expansion-parent-child-recovery", "audit"),
        ),
        "harness-row-expansion-parent-child-recovery-resume-after-child-enqueue": (("row-expansion-parent-child-recovery", "recovery"),),
        **{
            f"harness-retry-quarantine-discard-routed-errors-{case_id}": (
                ("retry-quarantine-discard-routed-errors", "runtime"),
                ("retry-quarantine-discard-routed-errors", "audit"),
            )
            for case_id in ("retry-then-success", "source-quarantine-routed", "transform-discard", "transform-error-route")
        },
        "harness-sink-write-pending-redrive-write-once": (
            ("sink-write-pending-redrive", "runtime"),
            ("sink-write-pending-redrive", "audit"),
        ),
        "harness-sink-write-pending-redrive-pending-redrive-reopen": (("sink-write-pending-redrive", "recovery"),),
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
    always_error = manager.create_transform("dag_corpus_always_error", {"schema": {"mode": "observed"}})
    branch_loss = manager.create_transform("dag_corpus_branch_loss", {"schema": {"mode": "observed"}})
    always_fail = manager.create_sink(
        "dag_corpus_always_fail_sink",
        {"path": str(tmp_path / "failing.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
    )

    assert (csv_source.name, passthrough.name, json_sink.name, custom.name, always_error.name, branch_loss.name, always_fail.name) == (
        "csv",
        "passthrough",
        "json",
        "dag_corpus_fail_once_eof_batch",
        "dag_corpus_always_error",
        "dag_corpus_branch_loss",
        "dag_corpus_always_fail_sink",
    )
    registered_transform: object = manager.get_transform_by_name("dag_corpus_fail_once_eof_batch")
    assert registered_transform is CorpusFailOnceEOFBatchTransform
    registered_always_error: object = manager.get_transform_by_name("dag_corpus_always_error")
    assert registered_always_error is CorpusAlwaysErrorTransform
    registered_branch_loss: object = manager.get_transform_by_name("dag_corpus_branch_loss")
    assert registered_branch_loss is CorpusBranchLossTransform
    registered_always_fail: object = manager.get_sink_by_name("dag_corpus_always_fail_sink")
    assert registered_always_fail is CorpusAlwaysFailSink
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
    assert CorpusAlwaysErrorTransform.name == "dag_corpus_always_error"
    assert CorpusAlwaysErrorTransform.determinism is Determinism.DETERMINISTIC
    assert CorpusAlwaysErrorTransform.input_schema is CorpusInputSchema
    assert CorpusAlwaysErrorTransform.output_schema is CorpusInputSchema
    assert CorpusAlwaysErrorTransform.on_error == "discard"
    assert CorpusBranchLossTransform.name == "dag_corpus_branch_loss"
    assert CorpusBranchLossTransform.determinism is Determinism.DETERMINISTIC
    assert CorpusBranchLossTransform.input_schema is CorpusInputSchema
    assert CorpusBranchLossTransform.output_schema is None
    assert CorpusBranchLossTransform.on_error == "discard"


def test_corpus_always_error_transform_returns_stable_non_retryable_error() -> None:
    result = CorpusAlwaysErrorTransform({"schema": {"mode": "observed"}}).process(_corpus_rows()[0], object())

    assert result.status == "error"
    assert result.reason == {
        "reason": "invalid_input",
        "error": "injected DAG corpus routed error",
    }
    assert result.retryable is False


def test_corpus_branch_loss_transform_returns_stable_non_retryable_error() -> None:
    result = CorpusBranchLossTransform({"schema": {"mode": "observed"}}).process(_corpus_rows()[0], object())

    assert result.status == "error"
    assert result.reason == {
        "reason": "invalid_input",
        "error": "injected DAG corpus branch loss",
    }
    assert result.retryable is False


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


def test_b3_eof_batch_sum_transform_produces_exact_fresh_result() -> None:
    transform = CorpusEOFBatchSumTransform({"schema": {"mode": "observed"}})
    rows = _corpus_rows()

    buffered = transform.process(rows[0], object())
    result = transform.process(rows, object())

    assert buffered.row is rows[0]
    assert buffered.success_reason == {"action": "buffer"}
    assert result.row is not None
    assert result.row.to_dict() == {"value": 60, "count": 3}
    assert result.success_reason == {"action": "batch_sum"}


def test_b3_retry_once_transform_raises_transiently_then_returns_same_row() -> None:
    transform = CorpusRetryOnceTransform({"schema": {"mode": "observed"}})
    row = _corpus_rows()[0]

    with pytest.raises(ConnectionError, match="injected DAG corpus retryable failure"):
        transform.process(row, object())
    second = transform.process(row, object())

    assert second.status == "success"
    assert second.row is row
    assert second.success_reason == {"action": "retry_recovered"}


def test_b3_routed_transform_error_fixture_keeps_one_exportable_control_disposition() -> None:
    fixture = yaml.safe_load(
        resolve_fixture_path("retry-quarantine-discard-routed-errors/transform-error-route.yaml").read_text(encoding="utf-8")
    )

    assert fixture["sources"]["primary"]["on_success"] == "select_error"
    assert fixture["gates"] == [
        {
            "name": "select_error",
            "input": "select_error",
            "condition": "row['id'] == 1",
            "routes": {"true": "error_in", "false": "discard"},
        }
    ]
    assert fixture["transforms"][0]["on_success"] == fixture["transforms"][0]["on_error"] == "error_output"


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
        "conditional-routing/error-route-and-discard.yaml": EXPECTED_ERROR_ROUTE_AND_DISCARD_YAML,
        "conditional-routing/input.csv": EXPECTED_INPUT_CSV,
        "fork-multiple-terminals-partial-failure/one-terminal-fails.yaml": EXPECTED_ONE_TERMINAL_FAILS_YAML,
        "fork-multiple-terminals-partial-failure/input.csv": EXPECTED_INPUT_CSV,
        **{f"fork-coalesce-policies/{case_id}.yaml": expected for case_id, expected in EXPECTED_COALESCE_YAMLS.items()},
        "fork-coalesce-policies/matrix-input.csv": b"id,value\n1,10\n",
        "fork-coalesce-policies/input.csv": EXPECTED_INPUT_CSV,
        "sequential-nested-fork-coalesce/two-sequential-require-all.yaml": EXPECTED_SEQUENTIAL_COALESCE_YAML,
        "sequential-nested-fork-coalesce/input.csv": EXPECTED_INPUT_CSV,
        "parallel-coalesces/two-parallel-require-all.yaml": EXPECTED_PARALLEL_COALESCE_YAML,
        "parallel-coalesces/input.csv": EXPECTED_INPUT_CSV,
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
        ("conditional-routing", "error-route-and-discard"),
        ("fork-multiple-terminals-partial-failure", "one-terminal-fails"),
        *(("fork-coalesce-policies", case_id) for case_id in B2_COALESCE_CASE_IDS),
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
            elif scenario.id == "retry-quarantine-discard-routed-errors" and dimension == "contracts":
                expected_owner = "elspeth-67b44040ee"
            elif scenario.id == "retry-quarantine-discard-routed-errors" and dimension == "runtime":
                expected_owner = "elspeth-6f6bbbec00"
            elif scenario.id == "retry-quarantine-discard-routed-errors" and dimension == "audit":
                expected_owner = "elspeth-2e66723070"
            elif scenario.id == "checkpoint-deterministic-resume" and dimension == "contracts":
                expected_owner = "elspeth-f321e3ff21"
            elif scenario.id == "checkpoint-deterministic-resume" and dimension == "recovery":
                expected_owner = "elspeth-245b21351b"
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
            "b3-stateful-runtime-exact-contracts",
        ),
        "runtime": (
            "cardinality-identity-04",
            "cardinality-identity-05",
            "cardinality-identity-06",
            "cardinality-identity-07",
            "cardinality-identity-09",
            "cardinality-identity-10",
            "cardinality-identity-11",
            "harness-row-expansion-parent-child-recovery-json-explode-parent-child",
            "b3-stateful-runtime-exact-contracts",
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
            "harness-row-expansion-parent-child-recovery-json-explode-parent-child",
            "b3-stateful-runtime-exact-contracts",
        ),
        "recovery": ("harness-row-expansion-parent-child-recovery-resume-after-child-enqueue",),
        "concurrency": ("cardinality-identity-10",),
    }
    assert scenario.dimensions["recovery"].status == "pass"
    assert scenario.dimensions["recovery"].owner_issue is None
    assert scenario.dimensions["recovery"].evidence == ("harness-row-expansion-parent-child-recovery-resume-after-child-enqueue",)
    assert scenario.dimensions["concurrency"].status == "partial"
    for dimension in ("contracts", "runtime", "audit"):
        assert scenario.dimensions[dimension].status == "pass"
        assert scenario.dimensions[dimension].owner_issue is None
    assert scenario.dimensions["concurrency"].owner_issue == "elspeth-ef29ef6ba4"
    assert all(cell.owner_issue != "elspeth-a25e9c009e" for cell in scenario.dimensions.values())


def test_manifest_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, ["not", "a", "mapping"])
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_manifest(path)


@pytest.mark.parametrize(
    "schema_version",
    [None, 1, True, 2.0, "2"],
    ids=("missing", "wrong-integer", "boolean", "float", "string"),
)
def test_manifest_rejects_schema_version_that_is_not_exact_integer_two(
    tmp_path: Path,
    schema_version: object,
) -> None:
    raw = valid_manifest_dict()
    if schema_version is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = schema_version

    with pytest.raises(ValueError, match="schema_version must be exactly integer 2"):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("duplicate_key", "source"),
    [
        (
            "schema_version",
            "schema_version: 2\nschema_version: 2\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\n",
        ),
        (
            "id",
            "schema_version: 2\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\nevidence: []\nscenarios:\n  - id: linear\n    id: linear\n",
        ),
        (
            "config",
            "schema_version: 2\ncriteria_ref: docs/architecture/dag/completeness-criteria.md\nevidence: []\nscenarios:\n  - id: linear\n    ordinal: 1\n    title: Linear\n    dimensions:\n      config: {}\n      config: {}\n",
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


def test_manifest_rejects_incomplete_harness_stages_for_registered_run_workflow(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    run_evidence = next(
        evidence for evidence in _raw_evidence(raw) if evidence["id"] == "harness-multiple-independent-sources-independent-roots"
    )
    run_evidence["stages"] = ["config", "build", "runtime"]

    with pytest.raises(
        ValueError,
        match=r"harness evidence.*independent-roots.*run workflow.*exact stages.*config.*build.*runtime.*audit",
    ):
        load_manifest(write_manifest(tmp_path, raw))


def test_manifest_rejects_harness_attached_beyond_its_workflow_even_with_other_executable_evidence(tmp_path: Path) -> None:
    raw = valid_manifest_dict()
    independent_sources = next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "multiple-independent-sources")
    recovery_cell = deepcopy(_raw_dimensions(independent_sources)["recovery"])
    recovery_cell["evidence"] = [
        *cast(list[str], recovery_cell.get("evidence", [])),
        "harness-multiple-independent-sources-independent-roots",
    ]
    _raw_dimensions(independent_sources)["recovery"] = recovery_cell

    with pytest.raises(
        ValueError,
        match=r"harness evidence.*independent-roots.*multiple-independent-sources\.recovery.*stage 'recovery'",
    ):
        load_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize("dimension", ["concurrency", "guided", "scale"])
def test_manifest_rejects_run_harness_attached_to_non_lifecycle_dimension(tmp_path: Path, dimension: str) -> None:
    raw = valid_manifest_dict()
    independent_sources = next(scenario for scenario in _raw_scenarios(raw) if scenario["id"] == "multiple-independent-sources")
    cell = deepcopy(_raw_dimensions(independent_sources)[dimension])
    cell["evidence"] = ["harness-multiple-independent-sources-independent-roots"]
    _raw_dimensions(independent_sources)[dimension] = cell

    with pytest.raises(
        ValueError,
        match=rf"harness evidence.*independent-roots.*multiple-independent-sources\.{dimension}.*run workflow.*validated lifecycle",
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
    for dimension in ("runtime", "audit"):
        _raw_dimensions(_raw_scenarios(raw)[0])[dimension]["evidence"] = ["runtime-disposition-drains"]
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
        schema_version=2,
        criteria_ref="docs/architecture/dag/completeness-criteria.md",
        evidence=(_reference(),),
        scenarios=(first_scenario, second_scenario),
    )

    assert tuple((scenario.id, case.id) for scenario, case in iter_harness_cases(manifest)) == (
        ("linear", "happy-path"),
        ("linear", "second"),
        ("second-scenario", "second"),
    )
