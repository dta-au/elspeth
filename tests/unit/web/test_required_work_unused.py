"""Actual publication branch provenance for unused projection capabilities."""

from concurrent.futures import Future
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.required_sql_outcomes import RequiredSQLRaised
from elspeth.web.required_work import (
    PublicationProjectionDisposition,
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
    RevocationWorkUnusedDisposition,
)


@pytest.fixture
def coordinator():
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "private-token", 1), SessionOperationKind.COMPOSE)
    authority = RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1, str(uuid4()), tool_call_id="tool-id")
    return RequiredWorkCoordinator(authority)


def _pair(coordinator):
    return coordinator.reserve_pair(
        RequiredWorkSource.PIPELINE_PUBLICATION_SQL,
        RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION,
        transition_ordinal=0,
        semantic_ordinal=0,
    )


@pytest.mark.parametrize("submitted", (False, True))
def test_same_error_has_branch_selected_disposition(coordinator, submitted):
    sql, projection = _pair(coordinator)
    error = OperationalError("SQL canary", {}, RuntimeError("cause canary"))
    if submitted:
        future = Future()
        sql.bind_future(future)
        future.set_exception(error)
        sql.observe_actual_outcome()
    else:
        sql.complete_without_submission(error)
    metadata = coordinator.issue_publication_projection_unused(
        publication_ticket=sql, projection_ticket=projection, actual_outcome=RequiredSQLRaised(error, ())
    )
    assert metadata.disposition is (
        PublicationProjectionDisposition.PUBLICATION_FAILED if submitted else PublicationProjectionDisposition.PREFLIGHT_REFUSED
    )
    projection.complete_unused(metadata)
    assert projection.complete
    assert projection.receipts() == ()
    assert sql.errors == (error,)


def test_copied_metadata_and_duplicate_completion_refuse(coordinator):
    sql, projection = _pair(coordinator)
    error = RuntimeError("original")
    sql.complete_without_submission(error)
    metadata = coordinator.issue_publication_projection_unused(
        publication_ticket=sql, projection_ticket=projection, actual_outcome=RequiredSQLRaised(error, ())
    )
    with pytest.raises(AuditIntegrityError):
        projection.complete_unused(replace(metadata))
    assert not projection.complete
    projection.complete_unused(metadata)
    with pytest.raises(AuditIntegrityError):
        projection.complete_unused(metadata)


@pytest.mark.parametrize("state", ("unknown", "future", "projection"))
def test_unused_refuses_physical_or_projection_custody(coordinator, state):
    sql, projection = _pair(coordinator)
    error = RuntimeError("original")
    sql.complete_without_submission(error)
    metadata = coordinator.issue_publication_projection_unused(
        publication_ticket=sql, projection_ticket=projection, actual_outcome=RequiredSQLRaised(error, ())
    )
    if state == "unknown":
        projection.observe_submission_unknown(RuntimeError("unknown"))
    elif state == "future":
        projection.bind_future(Future())
    else:
        projection.begin_projection()
    with pytest.raises(AuditIntegrityError):
        projection.complete_unused(metadata)
    assert not projection.complete


def test_revocation_metadata_namespace_cannot_complete_publication(coordinator):
    sql, projection = _pair(coordinator)
    revocation = coordinator.reserve(RequiredWorkSource.TRUST_REVOCATION_SQL)
    error = RuntimeError("original")
    sql.complete_without_submission(error)
    outcome = RequiredSQLRaised(error, ())
    metadata = coordinator.issue_revocation_work_unused(target_ticket=revocation, publication_ticket=sql, publication_outcome=outcome)
    assert metadata.disposition is RevocationWorkUnusedDisposition.PREFLIGHT_REFUSED
    with pytest.raises(AuditIntegrityError):
        projection.complete_unused(metadata)
    revocation.complete_unused(metadata)
    assert revocation.receipts() == ()


def test_late_aborted_submission_is_publication_failure_not_preflight(coordinator):
    sql, projection = _pair(coordinator)
    error = RuntimeError("original executor submission failure")
    sql.observe_submission_unknown(error)
    outcome = RequiredSQLRaised(error, ())
    from elspeth.web.required_work import RequiredWorkIncomplete

    with pytest.raises(RequiredWorkIncomplete):
        coordinator.issue_publication_projection_unused(publication_ticket=sql, projection_ticket=projection, actual_outcome=outcome)
    sql.observe_aborted_invocation_exit(error)
    metadata = coordinator.issue_publication_projection_unused(publication_ticket=sql, projection_ticket=projection, actual_outcome=outcome)
    assert metadata.disposition is PublicationProjectionDisposition.PUBLICATION_FAILED
    projection.complete_unused(metadata)
    assert projection.complete
    assert sql.receipts()[0].original_root is error


def test_unused_refuses_different_semantic_invocation(coordinator):
    sql, _ = _pair(coordinator)
    projection = coordinator.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION, semantic_ordinal=1)
    error = RuntimeError("known preflight")
    sql.complete_without_submission(error)
    with pytest.raises(AuditIntegrityError):
        coordinator.issue_publication_projection_unused(
            publication_ticket=sql, projection_ticket=projection, actual_outcome=RequiredSQLRaised(error, ())
        )
    assert not projection.complete
