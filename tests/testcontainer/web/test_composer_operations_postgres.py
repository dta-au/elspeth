"""PostgreSQL durable composer controls on a private bounded test cluster."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_admission_binding_conflict_precedes_missing_base_membership as test_admission_binding_conflict_precedes_missing_base_membership,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_admission_replay_conflict_active_owner_and_provider as test_admission_replay_conflict_active_owner_and_provider,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_cancel_refuses_business_but_sealed_failure_can_settle as test_cancel_refuses_business_but_sealed_failure_can_settle,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_cancelled_terminal_awaiter_joins_actual_sql_and_returns_committed_terminal as test_cancelled_terminal_awaiter_joins_actual_sql_and_returns_committed_terminal,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_changed_stop_state_prevents_exact_retry as test_changed_stop_state_prevents_exact_retry,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_claim_is_queue_only_and_running_never_requeued as test_claim_is_queue_only_and_running_never_requeued,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_concurrent_same_id_is_one_immutable_admission as test_concurrent_same_id_is_one_immutable_admission,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_current_attempt_must_match_immutable_history as test_continuation_history_current_attempt_must_match_immutable_history,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_live_sql_identity_allows_mutation_without_changes as test_continuation_history_live_sql_identity_allows_mutation_without_changes,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_missing_segment_is_refused_by_actual_foreign_key as test_continuation_history_missing_segment_is_refused_by_actual_foreign_key,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_owed_p4_scope_cannot_escape_exact_active_transaction as test_continuation_history_owed_p4_scope_cannot_escape_exact_active_transaction,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_owed_p4_scope_predicate_has_exact_nominal_positive_control as test_continuation_history_owed_p4_scope_predicate_has_exact_nominal_positive_control,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_owed_scope_construction_requires_active_root_transaction as test_continuation_history_owed_scope_construction_requires_active_root_transaction,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_owed_scope_preserves_savepoints_within_same_root as test_continuation_history_owed_scope_preserves_savepoints_within_same_root,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_owed_scope_refuses_root_transaction_replacement as test_continuation_history_owed_scope_refuses_root_transaction_replacement,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_rejects_independent_sql_identity_and_liveness_substitutions as test_continuation_history_rejects_independent_sql_identity_and_liveness_substitutions,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_stop_and_deadline_are_transaction_local_controls as test_continuation_history_stop_and_deadline_are_transaction_local_controls,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_continuation_history_unknown_sync_and_noncompose_preserve_prior_behavior as test_continuation_history_unknown_sync_and_noncompose_preserve_prior_behavior,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_direct_sql_child_cancel_waits_for_actual_thread_completion as test_direct_sql_child_cancel_waits_for_actual_thread_completion,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_distinct_ids_race_has_one_active_winner as test_distinct_ids_race_has_one_active_winner,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_driver_commit_fault_reconciles_one_immutable_terminal as test_driver_commit_fault_reconciles_one_immutable_terminal,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_each_status_bundle_constraints_are_unmasked_and_null_closed as test_each_status_bundle_constraints_are_unmasked_and_null_closed,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_exact_retry_preserves_entire_assistant_audit_and_response_bundle as test_exact_retry_preserves_entire_assistant_audit_and_response_bundle,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_expired_job_refuses_business_write_at_database_clock as test_expired_job_refuses_business_write_at_database_clock,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_failed_fresh_writer_read_preserves_unknown_terminal_custody as test_failed_fresh_writer_read_preserves_unknown_terminal_custody,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_failed_terminal_has_one_exact_immutable_retry as test_failed_terminal_has_one_exact_immutable_retry,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_failed_terminal_prewrite_fence_refusal_preserves_recovery_authority as test_failed_terminal_prewrite_fence_refusal_preserves_recovery_authority,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_final_job_actor_delete_has_unmasked_restrict_control as test_final_job_actor_delete_has_unmasked_restrict_control,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_final_job_base_delete_is_restricted_but_session_cascade_succeeds as test_final_job_base_delete_is_restricted_but_session_cascade_succeeds,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_final_job_user_delete_guard_and_foreign_key_are_independent as test_final_job_user_delete_guard_and_foreign_key_are_independent,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_fresh_recovery_requires_persisted_compose_and_owned_instance as test_fresh_recovery_requires_persisted_compose_and_owned_instance,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_independent_process_admission_converges_on_one_action as test_independent_process_admission_converges_on_one_action,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_known_historical_fence_cannot_fall_back_to_sync_compose as test_known_historical_fence_cannot_fall_back_to_sync_compose,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_malformed_v2_base_refuses_before_fresh_admission_writes as test_malformed_v2_base_refuses_before_fresh_admission_writes,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_new_job_actor_and_base_foreign_keys_have_unmasked_negative_controls as test_new_job_actor_and_base_foreign_keys_have_unmasked_negative_controls,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_queued_cancel_settles_without_a_claim_and_retains_immutable_body as test_queued_cancel_settles_without_a_claim_and_retains_immutable_body,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_queued_provider_admission_cancellation_does_not_create_attempt as test_queued_provider_admission_cancellation_does_not_create_attempt,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_queued_settlement_rechecks_priority_under_owned_claim_lock as test_queued_settlement_rechecks_priority_under_owned_claim_lock,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_real_schema_cross_session_and_cross_operation_bindings_are_rejected as test_real_schema_cross_session_and_cross_operation_bindings_are_rejected,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_recompose_tool_prefix_is_eligible_bare_narration_is_not as test_recompose_tool_prefix_is_eligible_bare_narration_is_not,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_required_audit_sql_failure_preserves_canonical_integrity_classification as test_required_audit_sql_failure_preserves_canonical_integrity_classification,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_required_ingress_cancellation_retains_actual_sql_until_binding_commit as test_required_ingress_cancellation_retains_actual_sql_until_binding_commit,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_required_intermediate_cancellation_joins_actual_sql as test_required_intermediate_cancellation_joins_actual_sql,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_required_provider_sql_cancellation_joins_committed_evidence as test_required_provider_sql_cancellation_joins_committed_evidence,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_running_bundle_rejects_nullability_and_fence_holes as test_running_bundle_rejects_nullability_and_fence_holes,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_stale_claim_cannot_release_or_renew_reclaimed_work as test_stale_claim_cannot_release_or_renew_reclaimed_work,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_start_refusal_rolls_back_fence_and_has_no_user_side_effect as test_start_refusal_rolls_back_fence_and_has_no_user_side_effect,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_stop_between_fresh_read_and_retry_lock_refuses_success as test_stop_between_fresh_read_and_retry_lock_refuses_success,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_terminal_atomic_publication_and_immutability as test_terminal_atomic_publication_and_immutability,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_terminal_projection_error_replays_committed_terminal as test_terminal_projection_error_replays_committed_terminal,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_terminal_read_rejects_tampering_after_named_guard_removed as test_terminal_read_rejects_tampering_after_named_guard_removed,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_terminal_transaction_fault_windows_keep_one_bundle as test_terminal_transaction_fault_windows_keep_one_bundle,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_unchanged_state_retries_once_after_actual_sql_completion as test_unchanged_state_retries_once_after_actual_sql_completion,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v1_running_claim_token_change_is_refused_without_row_changes as test_v1_running_claim_token_change_is_refused_without_row_changes,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v1_terminal_settlement_clears_claim_token_and_preserves_attribution as test_v1_terminal_settlement_clears_claim_token_and_preserves_attribution,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v1_terminal_settlement_refuses_attribution_substitution as test_v1_terminal_settlement_refuses_attribution_substitution,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_actual_insert_conflict_replay_preserves_existing_bundle as test_v2_admission_actual_insert_conflict_replay_preserves_existing_bundle,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_child_sql_failure_rolls_back_whole_bundle_and_charge as test_v2_admission_child_sql_failure_rolls_back_whole_bundle_and_charge,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_configuration_refusals_preserve_default_v1 as test_v2_admission_configuration_refusals_preserve_default_v1,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_current_principal_and_base_refusals_before_new_writes as test_v2_admission_current_principal_and_base_refusals_before_new_writes,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_decoder_preconditions_and_async_forwarding_use_real_rows as test_v2_admission_decoder_preconditions_and_async_forwarding_use_real_rows,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_default_v1_never_consults_continuation_seed as test_v2_admission_default_v1_never_consults_continuation_seed,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_disabled_exact_replay_ignores_new_seed_policy_and_current_state as test_v2_admission_disabled_exact_replay_ignores_new_seed_policy_and_current_state,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_expired_quota_maintenance_is_separate_from_new_admission as test_v2_admission_expired_quota_maintenance_is_separate_from_new_admission,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_fresh_bundle_uses_actual_sql_mode_and_reserved_ingress as test_v2_admission_fresh_bundle_uses_actual_sql_mode_and_reserved_ingress,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_fresh_disabled_refuses_exact_body_without_new_rows as test_v2_admission_fresh_disabled_refuses_exact_body_without_new_rows,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_keeps_legacy_worker_budget_validation as test_v2_admission_keeps_legacy_worker_budget_validation,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_missing_server_policy_refuses_without_mutation as test_v2_admission_missing_server_policy_refuses_without_mutation,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_policy_constructor_is_nominal as test_v2_admission_policy_constructor_is_nominal,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_real_concurrency_has_one_atomic_fresh_winner as test_v2_admission_real_concurrency_has_one_atomic_fresh_winner,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_recompose_joins_actual_original_user as test_v2_admission_recompose_joins_actual_original_user,
)
from tests.unit.web.coordination.test_composer_operation_authority import (
    test_v2_admission_refuses_corrupt_or_substituted_seed_before_writes as test_v2_admission_refuses_corrupt_or_substituted_seed_before_writes,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_explicit_restore_uses_parked_identity_across_turns as test_explicit_restore_uses_parked_identity_across_turns,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_explicit_unavailable_target_is_terminal_refusal as test_explicit_unavailable_target_is_terminal_refusal,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_foreign_intermediate_checkpoint_refuses_recovery as test_foreign_intermediate_checkpoint_refuses_recovery,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_malformed_gate_metadata_is_quarantined as test_malformed_gate_metadata_is_quarantined,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_park_preserves_current_unpersisted_candidate as test_park_preserves_current_unpersisted_candidate,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_safe_checkpoint_skips_lossy_unknown_graph_fields as test_safe_checkpoint_skips_lossy_unknown_graph_fields,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_safe_reset_empty_fallback_and_terminal_are_atomic as test_safe_reset_empty_fallback_and_terminal_are_atomic,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_target_is_frozen_and_current_head_cas_refuses_newer_work as test_target_is_frozen_and_current_head_cas_refuses_newer_work,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_terminal_response_failure_rolls_back_graph_and_receipts as test_terminal_response_failure_rolls_back_graph_and_receipts,
)
from tests.unit.web.coordination.test_graph_recovery_persistence import (
    test_unsaveable_candidate_never_blocks_reset as test_unsaveable_candidate_never_blocks_reset,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_candidate_supersedes_older_identical_pending_review as test_atomic_candidate_supersedes_older_identical_pending_review,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_opt_out_binds_candidate_derived_head_and_assistant as test_atomic_opt_out_binds_candidate_derived_head_and_assistant,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_pipeline_replay_has_no_dml_and_ignores_later_head as test_atomic_pipeline_replay_has_no_dml_and_ignores_later_head,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_pipeline_required_ticket_refusal_is_completed_without_sql as test_atomic_pipeline_required_ticket_refusal_is_completed_without_sql,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_pipeline_review_fault_rolls_back_candidate_and_assistant as test_atomic_pipeline_review_fault_rolls_back_candidate_and_assistant,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_atomic_pipeline_review_is_candidate_bound as test_atomic_pipeline_review_is_candidate_bound,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_sealed_trust_revocation_crosses_signal_only_for_exact_authority as test_sealed_trust_revocation_crosses_signal_only_for_exact_authority,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_state_checkpoint_required_ticket_observes_actual_completion as test_state_checkpoint_required_ticket_observes_actual_completion,
)
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import (
    test_terminal_failure_required_coordinator_observes_each_actual_attempt as test_terminal_failure_required_coordinator_observes_each_actual_attempt,
)

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def operation_postgres_target():
    with postgres_test_target(mem_limit="256m", nano_cpus=500_000_000) as target:
        yield target


@pytest.fixture
def operation_store(operation_postgres_target, tmp_path):
    # Each proof has an isolated database, so bounded cluster counts cannot
    # measure a sibling test's rows. The cluster is local and disposable.
    database = f"composer_operations_{uuid4().hex}"
    admin = create_session_engine(operation_postgres_target).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(operation_postgres_target).set(database=database)
    engine = create_session_engine(url.render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
        _register_instance(engine, instance_id="test-owner", lease_delta=timedelta(minutes=10))
        repository = PostgresSessionOperationRepository(engine)
        session = repository.create_session_with_initial_fence(
            user_id="alice", title="Durable", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
        )
        authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
        service = SessionServiceImpl(
            engine,
            data_dir=tmp_path,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.pg.storage"),
            session_operation_authority=repository,
            owner_instance_id="test-owner",
        )
        yield engine, repository, authority, service, session.id
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()
