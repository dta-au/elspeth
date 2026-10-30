"""Native PostgreSQL membership and identity controls for U39 discovery."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as _operation_store
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows
from tests.unit.web.sessions.test_continuation_recovery_candidates import _started
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_discovery_rejects_nonfinite_or_non_nominal_bounds_before_sql as test_discovery_rejects_nonfinite_or_non_nominal_bounds_before_sql,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_discovery_requires_exact_current_segment_attempt_and_fence as test_discovery_requires_exact_current_segment_attempt_and_fence,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_expired_physical_requires_expired_postgres_membership as test_expired_physical_requires_expired_postgres_membership,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_filter_before_bound_with_stable_tie_order as test_filter_before_bound_with_stable_tie_order,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_missing_postgres_member_never_proves_death_but_release_does as test_missing_postgres_member_never_proves_death_but_release_does,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_released_exact_fence_returns_original_identity_without_writes as test_released_exact_fence_returns_original_identity_without_writes,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_stop_and_archive_are_observed_for_locked_writer_disposition as test_stop_and_archive_are_observed_for_locked_writer_disposition,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import (
    test_terminal_parent_excluded_even_if_segment_still_running as test_terminal_parent_excluded_even_if_segment_still_running,
)
from tests.unit.web.sessions.test_continuation_recovery_candidates import test_v1_running_parent_excluded as test_v1_running_parent_excluded

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation_recovery_candidates import ContinuationRecoveryCandidate, list_recoverable_segments
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import web_instances_table

operation_store = _operation_store

pytestmark = pytest.mark.testcontainer


@pytest.mark.parametrize(
    ("owner_state", "unrelated_expired"),
    [
        pytest.param("live", True, id="own-live-unrelated-expired"),
        pytest.param("expired", False, id="own-expired-unrelated-live"),
        pytest.param("missing", True, id="own-missing-unrelated-expired"),
    ],
)
def test_postgres_takeover_requires_exact_owners_membership(operation_store, owner_state, unrelated_expired):
    engine, _, _, _, sid = operation_store
    _, record, claim, context = _started(operation_store)
    unrelated_owner = "unrelated-owner"
    _register_instance(
        engine,
        instance_id=unrelated_owner,
        lease_delta=timedelta(days=-1 if unrelated_expired else 1),
    )
    with engine.begin() as conn:
        now = database_now(conn)
        physical = conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(days=1)))
        assert physical.rowcount == 1
        owner = (
            delete(web_instances_table).where(web_instances_table.c.instance_id == "test-owner")
            if owner_state == "missing"
            else update(web_instances_table)
            .where(web_instances_table.c.instance_id == "test-owner")
            .values(lease_expires_at=now + timedelta(days=-1 if owner_state == "expired" else 1))
        )
        assert conn.execute(owner).rowcount == 1

    # Measure the opposed native rows: an unrelated membership cannot stand
    # in for the original physical owner's present, expired membership.
    with engine.connect() as conn:
        now = database_now(conn)
        fence = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
        membership_rows = conn.execute(select(web_instances_table.c.instance_id, web_instances_table.c.lease_expires_at)).tuples().all()
        memberships = dict(membership_rows)
    assert fence.released_at is None and fence.lease_expires_at <= now
    assert fence.owner_instance_id == "test-owner" != unrelated_owner
    assert (memberships[unrelated_owner] <= now) is unrelated_expired
    if owner_state == "missing":
        assert "test-owner" not in memberships
    else:
        assert (memberships["test-owner"] <= now) is (owner_state == "expired")

    before = _admission_rows(engine)
    candidates = list_recoverable_segments(engine, limit=1)
    if owner_state == "expired":
        assert candidates == (
            ContinuationRecoveryCandidate(
                session_id=sid,
                operation_id=record.operation_id,
                expected_generation=claim.ownership_generation,
                segment_sequence=claim.segment_sequence,
                actor_user_id="alice",
                auth_provider_type="local",
                claim_attempt=claim.attempt,
                session_operation_id=context.fence.operation_id,
                session_operation_epoch=context.fence.operation_epoch,
                session_operation_lease_token=context.fence.lease_token,
                owner_instance_id="test-owner",
                checkpoint_id=record.checkpoint_id,
                cancel_requested_at=None,
                session_archived_at=None,
            ),
        )
    else:
        assert candidates == (), "an unrelated expired member must not prove this owner dead"
    assert _admission_rows(engine) == before
