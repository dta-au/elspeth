"""Failed tool custody, real P4 and terminal SQL on the supported PostgreSQL driver."""

from datetime import timedelta

import psycopg2.extensions
import pytest
from sqlalchemy import select, update
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_live_phases import (
    _advance,
    _phase_case,
    test_real_stop_reads_and_publishes_only_the_actual_completed_prefix,
)
from tests.unit.web.sessions.test_continuation_p4_product import (
    _contains_identity,
    test_actual_unavailable_unwind_keeps_original_failure_and_one_physical_owner,
)
from tests.unit.web.sessions.test_continuation_tool_failure import (
    test_actual_advisor_failure_keeps_successful_refusal_separate_from_crash,
    test_actual_advisor_feedback_then_primary_failure_keeps_distinct_origins,
    test_actual_advisor_feedback_uses_successful_audit_projection_and_cold_source,
    test_actual_advisor_issued_argument_refusal_uses_recorded_projected_wire,
    test_actual_advisor_origin_survives_inner_audit_failure,
    test_actual_advisor_timeout_is_retained_feedback_without_primary_terminal,
    test_actual_argument_feedback_factory_divergence_retains_native_cause,
    test_actual_argument_feedback_then_primary_crash_keeps_distinct_custody,
    test_actual_cold_failed_prefix_finishes_p4_and_terminal_under_new_owner,
    test_actual_cold_feedback_refuses_copied_or_cross_cohort_sources,
    test_actual_context_refusal_has_portable_allowance_proof,
    test_actual_driver_entry_read_failure_keeps_native_with_successful_single_close,
    test_actual_driver_retains_consumed_failure_through_verify_and_single_close,
    test_actual_failed_prefix_cold_read_reproves_new_generation_without_replay,
    test_actual_failed_prefix_terminal_uses_two_slots_and_exact_bundle,
    test_actual_feedback_cross_registry_duplicate_refuses_either_order,
    test_actual_feedback_current_reader_opposes_foreign_source_after_positive,
    test_actual_feedback_provenance_precedes_ordinary_excess,
    test_actual_feedback_rejects_later_coherent_rich_payload_and_hash,
    test_actual_feedback_rejects_later_semantically_equal_but_different_ordered_body,
    test_actual_feedback_requires_committed_creator_event_receipt_and_issue,
    test_actual_feedback_unchanged_later_checkpoint_admits_current_and_public_cold_source,
    test_actual_issued_feedback_preserves_rich_ordered_wire_through_cold_read,
    test_actual_lazy_feedback_registration_defers_size_to_real_capture_owner,
    test_actual_oversize_feedback_keeps_credential_refusal_before_size_classification,
    test_actual_session_await_cancellation_observes_before_audit,
    test_actual_session_feedback_binds_only_its_original_outer_outcome_once,
    test_actual_session_handler_crash_uses_native_custody_and_owed_p4,
    test_actual_session_inner_publication_preserves_owner_before_outer_completion,
    test_actual_session_success_then_finalizer_failure_retains_returned_object,
    test_actual_set_pipeline_required_control_rebind_has_exact_owner,
    test_actual_success_return_then_audit_failure_is_not_a_thrown_handler,
    test_actual_successful_refusal_requires_original_aliases_and_correlation,
    test_actual_terminal_uncertainty_keeps_pending_projection_and_native_custody,
    test_private_feedback_registration_does_not_admit_rolled_back_completion,
    test_real_dispatch_singleton_preserves_explicit_cause,
    test_real_failed_dispatch_retains_origin_before_fallible_audit,
    test_real_issued_progress_cancellation_keeps_primary_and_cause,
    test_real_prepared_terminal_append_reproves_exact_candidate,
)

from elspeth.web.coordination.repository import PostgresSessionOperationRepository

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_advisor_failure_keeps_successful_refusal_separate_from_crash",
    "test_actual_advisor_feedback_then_primary_failure_keeps_distinct_origins",
    "test_actual_advisor_feedback_uses_successful_audit_projection_and_cold_source",
    "test_actual_advisor_issued_argument_refusal_uses_recorded_projected_wire",
    "test_actual_advisor_origin_survives_inner_audit_failure",
    "test_actual_advisor_timeout_is_retained_feedback_without_primary_terminal",
    "test_actual_argument_feedback_factory_divergence_retains_native_cause",
    "test_actual_argument_feedback_then_primary_crash_keeps_distinct_custody",
    "test_actual_cold_failed_prefix_finishes_p4_and_terminal_under_new_owner",
    "test_actual_cold_feedback_refuses_copied_or_cross_cohort_sources",
    "test_actual_context_refusal_has_portable_allowance_proof",
    "test_actual_driver_entry_read_failure_keeps_native_with_successful_single_close",
    "test_actual_driver_retains_consumed_failure_through_verify_and_single_close",
    "test_actual_failed_prefix_cold_read_reproves_new_generation_without_replay",
    "test_actual_failed_prefix_terminal_uses_two_slots_and_exact_bundle",
    "test_actual_failed_source_preserves_creator_after_different_peer_generation",
    "test_actual_feedback_cross_registry_duplicate_refuses_either_order",
    "test_actual_feedback_current_reader_opposes_foreign_source_after_positive",
    "test_actual_feedback_provenance_precedes_ordinary_excess",
    "test_actual_feedback_rejects_later_coherent_rich_payload_and_hash",
    "test_actual_feedback_rejects_later_semantically_equal_but_different_ordered_body",
    "test_actual_feedback_requires_committed_creator_event_receipt_and_issue",
    "test_actual_feedback_unchanged_later_checkpoint_admits_current_and_public_cold_source",
    "test_actual_issued_feedback_preserves_rich_ordered_wire_through_cold_read",
    "test_actual_lazy_feedback_registration_defers_size_to_real_capture_owner",
    "test_actual_oversize_feedback_keeps_credential_refusal_before_size_classification",
    "test_actual_session_await_cancellation_observes_before_audit",
    "test_actual_session_feedback_binds_only_its_original_outer_outcome_once",
    "test_actual_session_handler_crash_uses_native_custody_and_owed_p4",
    "test_actual_session_inner_publication_preserves_owner_before_outer_completion",
    "test_actual_session_success_then_finalizer_failure_retains_returned_object",
    "test_actual_set_pipeline_required_control_rebind_has_exact_owner",
    "test_actual_success_return_then_audit_failure_is_not_a_thrown_handler",
    "test_actual_successful_refusal_requires_original_aliases_and_correlation",
    "test_actual_terminal_uncertainty_keeps_pending_projection_and_native_custody",
    "test_actual_unavailable_unwind_keeps_original_failure_and_one_physical_owner",
    "test_private_feedback_registration_does_not_admit_rolled_back_completion",
    "test_real_dispatch_singleton_preserves_explicit_cause",
    "test_real_failed_dispatch_retains_origin_before_fallible_audit",
    "test_real_issued_progress_cancellation_keeps_primary_and_cause",
    "test_real_prepared_terminal_append_reproves_exact_candidate",
    "test_real_stop_reads_and_publishes_only_the_actual_completed_prefix",
]


@pytest.fixture(autouse=True)
def _actual_postgres_backend(operation_store, record_property):
    engine, repository, _authority, _sessions, _sid = operation_store
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg2"
    assert type(repository) is PostgresSessionOperationRepository
    with engine.connect() as conn:
        assert isinstance(conn.connection.driver_connection, psycopg2.extensions.connection)
    record_property("failed_tool_actual_backend", "postgresql+psycopg2")


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", (False, True))
@pytest.mark.parametrize("change", ("none", "creator", "issue_event"))
async def test_actual_failed_source_preserves_creator_after_different_peer_generation(
    operation_store, tmp_path, monkeypatch, applied, change
):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer import tool_batch
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation_tool_failure import read_tool_failure_source_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import composer_continuation_events_table as events

    engine, _repository, first_authority, _sessions, sid = operation_store
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, first, _primary, sdk):
        original = RuntimeError("actual cross-peer failed tool")

        def tool(*args, **kwargs):
            raise original

        monkeypatch.setattr(tool_batch, "execute_tool", tool)
        await _advance(owner)
        if applied:
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
        source = first_authority.read_continuation_tool_failure(first, env_ref_names=owner.runtime.env_ref_names)
        assert source is not None and source.applied is applied
        await owner.lease.close()
        _engine, repository, _authority, _peer_service, _sid = new_owners(operation_store)
        peer = "failed-tool-cold-peer"
        _register_instance(engine, instance_id=peer, lease_delta=timedelta(minutes=10))
        authority = ComposerAsyncOperationAuthority(
            engine,
            owner_instance_id=peer,
            claim_lease_seconds=first_authority._claim_lease_seconds,
            continuation_policy=first_authority._continuation_policy,
        )
        assert (
            repository.recover_composer_segment(
                session_id=sid,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is True
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(claim, owner_instance_id=peer, lease_seconds=30, auth_provider_type="local")
        second = ComposerOperationRunning(claim, context)
        try:
            assert claim.ownership_generation == first.claim.ownership_generation + 1
            assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
            positive = authority.read_continuation_tool_failure(second, env_ref_names=owner.runtime.env_ref_names)
            assert positive is not None and positive.issue == source.issue and positive.invocation == source.invocation
            assert positive.issue.creator_operation_id != context.fence.operation_id
            if change == "none":
                assert len(sdk) == 1
                return
            with locked_session_transaction(engine, str(sid)) as conn:
                with conn.begin_nested() as savepoint:
                    if change == "creator":
                        key = f"tool:{source.response.reference.digest}:{source.issue.tool_index}"
                        row = conn.execute(
                            select(effects).where(
                                effects.c.session_id == str(sid),
                                effects.c.operation_id == first.claim.operation_id,
                                effects.c.effect_key == key,
                            )
                        ).one()
                        import json

                        descriptor = json.loads(row.action_json)
                        descriptor["creator_operation_epoch"] += 1
                        assert (
                            conn.execute(
                                update(effects)
                                .where(
                                    effects.c.session_id == str(sid),
                                    effects.c.operation_id == first.claim.operation_id,
                                    effects.c.effect_key == key,
                                )
                                .values(action_json=canonical_json(descriptor))
                            ).rowcount
                            == 1
                        )
                    else:
                        assert change == "issue_event"
                        assert (
                            conn.execute(
                                update(events)
                                .where(
                                    events.c.session_id == str(sid),
                                    events.c.operation_id == first.claim.operation_id,
                                    events.c.sequence == source.issue.cursor,
                                )
                                .values(tag="received")
                            ).rowcount
                            == 1
                        )
                    with pytest.raises(AuditIntegrityError):
                        read_tool_failure_source_on_connection(conn, second, env_ref_names=owner.runtime.env_ref_names)
                    savepoint.rollback()
                restored = read_tool_failure_source_on_connection(conn, second, env_ref_names=owner.runtime.env_ref_names)
                assert restored is not None and restored.issue == source.issue
            assert len(sdk) == 1
        finally:
            repository.release(context)
