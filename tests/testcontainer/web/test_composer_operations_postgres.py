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
