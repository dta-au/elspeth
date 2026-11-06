import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as _operation_store
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_action_receipt_uses_real_first_attempt_effect_and_retained_frontier as test_action_receipt_uses_real_first_attempt_effect_and_retained_frontier,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_cancelled_request_can_start_cleanup_but_business_guard_still_refuses as test_cancelled_request_can_start_cleanup_but_business_guard_still_refuses,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_expired_physical_lease_preserves_distributed_old_owner_liveness as test_expired_physical_lease_preserves_distributed_old_owner_liveness,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start as test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_first_start_requires_exact_complete_admitted_initializer as test_first_start_requires_exact_complete_admitted_initializer,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_fresh_start_has_exact_history_snapshot_and_independent_clocks as test_fresh_start_has_exact_history_snapshot_and_independent_clocks,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_historical_effect_keeps_creator_and_refuses_forged_locator as test_historical_effect_keeps_creator_and_refuses_forged_locator,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_historical_reference_facet_requires_current_sql_history_and_preserves_rows as test_historical_reference_facet_requires_current_sql_history_and_preserves_rows,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_history_insert_failure_rolls_back_actual_fence_and_segment_start as test_history_insert_failure_rolls_back_actual_fence_and_segment_start,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_independent_processes_cannot_both_claim_one_segment as test_independent_processes_cannot_both_claim_one_segment,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_logical_reservation_blocks_unrelated_writer_before_first_physical_start as test_logical_reservation_blocks_unrelated_writer_before_first_physical_start,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_owner_commit_requires_actual_current_claim_history_without_writes as test_owner_commit_requires_actual_current_claim_history_without_writes,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_post_insert_source_or_final_cas_refusal_rolls_back_whole_start as test_post_insert_source_or_final_cas_refusal_rolls_back_whole_start,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_recompose_start_retains_actual_original_ingress_without_inserting_again as test_recompose_start_retains_actual_original_ingress_without_inserting_again,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_registered_old_owner_omission_has_actual_postgres_eligibility_witness as test_registered_old_owner_omission_has_actual_postgres_eligibility_witness,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_retained_complete_action_sizes_or_atomically_refuses_overflow as test_retained_complete_action_sizes_or_atomically_refuses_overflow,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_snapshot_cas_uses_the_callers_selected_head_without_writes as test_snapshot_cas_uses_the_callers_selected_head_without_writes,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_stale_claim_refuses_without_any_row_or_fence_change as test_stale_claim_refuses_without_any_row_or_fence_change,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_stale_hint_is_rechecked_under_the_actual_session_lock as test_stale_hint_is_rechecked_under_the_actual_session_lock,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_start_refuses_current_custody_or_policy_substitution as test_start_refuses_current_custody_or_policy_substitution,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_start_source_proof_keeps_original_clock_anchor as test_start_source_proof_keeps_original_clock_anchor,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_unexpired_oldest_claim_does_not_consume_limit_one as test_unexpired_oldest_claim_does_not_consume_limit_one,
)
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_valid_shaped_snapshot_and_segment_substitutions_are_refused as test_valid_shaped_snapshot_and_segment_substitutions_are_refused,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_boundaries_require_actual_claim_without_writes as test_initial_provider_boundaries_require_actual_claim_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_observation_binds_actual_selected_input_without_writes as test_initial_provider_observation_binds_actual_selected_input_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_preparation_preserves_head_on_invalid_input as test_initial_provider_preparation_preserves_head_on_invalid_input,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_new_provider_preparation_refuses_owed_p4_scope_without_writes as test_new_provider_preparation_refuses_owed_p4_scope_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_provider_action_facades_return_the_actual_complete_checkpoint as test_provider_action_facades_return_the_actual_complete_checkpoint,
)

# Retired V1/V2 owner cross-product: only the current segment lifecycle remains.

pytestmark = pytest.mark.testcontainer
operation_store = _operation_store


def _start_same_claim_from_process(url, claim, rendezvous):
    from elspeth.web.coordination.repository import PostgresSessionOperationRepository
    from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
    from elspeth.web.sessions.engine import create_session_engine

    engine = create_session_engine(url)
    try:
        repository = PostgresSessionOperationRepository(engine)
        rendezvous.wait(timeout=20)
        try:
            return repository.start_composer_segment(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
        except ComposerOperationFenceLost:
            return None
    finally:
        engine.dispose()


def test_same_claim_concurrent_start_commits_one_actual_attempt(operation_store):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from sqlalchemy import select
    from tests.unit.web.coordination.test_composer_continuation_claim_start import queued

    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    from elspeth.web.sessions.models import session_operation_fences_table as fences

    engine, _, _, _, _ = operation_store
    authority, _record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    url = engine.url.render_as_string(hide_password=False)
    process_context = multiprocessing.get_context("spawn")
    with process_context.Manager() as manager:
        rendezvous = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=process_context) as pool:
            results = list(pool.map(_start_same_claim_from_process, [url, url], [claim, claim], [rendezvous, rendezvous]))
    assert sum(result is None for result in results) == 1
    (started,) = tuple(result for result in results if result is not None)
    with engine.connect() as conn:
        attempt = conn.execute(select(attempts)).one()
        segment = conn.execute(select(segments)).one()
        parent = conn.execute(select(jobs)).one()
        fence = conn.execute(select(fences)).one()
    assert attempt.generation == claim.ownership_generation and attempt.claim_attempt == claim.attempt
    assert parent.status == segment.state == "running"
    assert (
        attempt.session_operation_id
        == segment.session_operation_id
        == parent.session_operation_id
        == fence.operation_id
        == started.fence.operation_id
    )
    assert (
        attempt.session_operation_epoch
        == segment.session_operation_epoch
        == parent.session_operation_epoch
        == fence.operation_epoch
        == started.fence.operation_epoch
    )
