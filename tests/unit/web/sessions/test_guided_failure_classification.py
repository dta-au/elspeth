"""Owned exception taxonomy retains conflict subclass semantics."""

import pytest
from fastapi import HTTPException

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.protocol import ComposerServiceError
from elspeth.web.composer.service import ComposerAdmissionRefused
from elspeth.web.sessions.protocol import GuidedOperationFailed, GuidedOperationSettlementConflictError
from elspeth.web.sessions.routes._helpers import (
    _FREEFORM_PLANNER_FAILURE_HTTP,
    _freeform_planner_failure_code,
    freeform_planner_progress_reason,
)
from elspeth.web.sessions.routes.composer.guided_plan import _guided_full_failed_progress_event, _guided_full_failure_code
from elspeth.web.sessions.routes.guided_operations import raise_guided_operation_failure


def test_missing_cost_is_actionable_configuration_failure_on_both_surfaces() -> None:
    exc = PipelinePlannerError("private provider diagnostic", code="COST_UNAVAILABLE")
    code = _guided_full_failure_code(exc)
    assert code == "cost_unavailable"
    assert _freeform_planner_failure_code(exc) == code
    with pytest.raises(HTTPException) as caught:
        raise_guided_operation_failure(GuidedOperationFailed(failure_code=code))
    assert caught.value.status_code == 503
    assert caught.value.detail["failure_code"] == code
    copy = caught.value.detail["detail"]
    assert "pricing" in copy
    assert "administrator" in copy
    assert "Retry the request" not in copy
    assert "private provider diagnostic" not in copy
    assert _FREEFORM_PLANNER_FAILURE_HTTP[code] == (503, copy)
    progress = _guided_full_failed_progress_event(code)
    assert "pricing" in progress.likely_next
    assert progress.reason == "service_setup_failed"
    assert freeform_planner_progress_reason(exc.code) == "service_setup_failed"
    assert exc.code == "COST_UNAVAILABLE"


def test_admission_refusal_is_a_permanent_access_failure() -> None:
    code = _guided_full_failure_code(ComposerAdmissionRefused("Composer admission refused: identity_disabled."))
    assert code == "admission_refused"
    with pytest.raises(HTTPException) as caught:
        raise_guided_operation_failure(GuidedOperationFailed(failure_code=code))
    assert caught.value.status_code == 403
    assert caught.value.detail["failure_code"] == "admission_refused"
    assert "administrator" in caught.value.detail["detail"]
    assert "retry" not in caught.value.detail["detail"].lower()
    progress = _guided_full_failed_progress_event(code)
    assert "administrator" in progress.likely_next
    assert progress.reason != "provider_unavailable"
    assert _guided_full_failure_code(ComposerServiceError("provider unavailable")) == "provider_unavailable"


def test_guided_full_failure_code_preserves_conflict_subclasses() -> None:
    class SpecificSettlementConflict(GuidedOperationSettlementConflictError):
        pass

    assert _guided_full_failure_code(SpecificSettlementConflict()) == "stale_conflict"
    assert _guided_full_failure_code(RuntimeError("unclassified fault")) == "operation_failed"


def test_guided_full_failure_code_preserves_integrity_subclasses() -> None:
    """Any ``AuditIntegrityError`` subtype keeps the ``integrity_error`` code.

    The registered Tier-1 family is subclassed across the tree; an exact-type
    check would silently downgrade a subclass to ``operation_failed`` and lose
    the integrity signal in the durable failure record.
    """

    class SpecificIntegrityFailure(AuditIntegrityError):
        pass

    assert _guided_full_failure_code(AuditIntegrityError("custody proof unreadable")) == "integrity_error"
    assert _guided_full_failure_code(SpecificIntegrityFailure("subclassed integrity failure")) == "integrity_error"
