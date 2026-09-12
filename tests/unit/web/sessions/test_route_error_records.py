"""Actual route projections retain structured errors and fork custody checks."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.state import ValidationEntry, ValidationSummary
from elspeth.web.execution.schemas import ValidationError, ValidationReadiness, ValidationResult
from elspeth.web.sessions.protocol import CompositionStateRecord, CompositionValidationError
from elspeth.web.sessions.routes._helpers import (
    _RUNTIME_PREFLIGHT_FAILED,
    _composer_persisted_validation,
    _runtime_preflight_failure_errors,
    _state_response,
)
from elspeth.web.sessions.routes.sessions import _rewrite_fork_state_blob_custody


def _record(errors: tuple[CompositionValidationError, ...] | None) -> CompositionStateRecord:
    return CompositionStateRecord(
        id=uuid4(),
        session_id=uuid4(),
        version=1,
        sources=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata_={},
        is_valid=False,
        validation_errors=errors,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
    )


@pytest.mark.parametrize("errors", [None, (), (CompositionValidationError(message="detail", error_code=None, component=None),)])
def test_actual_state_response_preserves_null_empty_and_required_nullable_fields(errors) -> None:
    payload = _state_response(_record(errors)).model_dump(mode="json")
    expected = (
        None
        if errors is None
        else [{"message": item.message, "error_code": item.error_code, "component": item.component} for item in errors]
    )
    assert payload["validation_errors"] == expected


def test_authoring_and_runtime_errors_use_owned_identity_without_message_parsing() -> None:
    authoring = ValidationSummary(
        is_valid=False,
        errors=(ValidationEntry(component="source", message="not:an:identity", severity="high", error_code="source_invalid"),),
    )
    valid, errors = _composer_persisted_validation(authoring, None)
    assert valid is False
    assert errors == [CompositionValidationError(message="not:an:identity", error_code="source_invalid", component="source")]
    runtime = ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="node-1",
                component_type="transform",
                message="producer=x consumer=y",
                suggestion=None,
                error_code="runtime_invalid",
            )
        ],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=False, completion_ready=False, blockers=[]),
    )
    valid, errors = _composer_persisted_validation(authoring, runtime)
    assert valid is False
    assert errors == [CompositionValidationError(message="producer=x consumer=y", error_code="runtime_invalid", component="node-1")]


def test_runtime_crash_codes_only_known_sentinel_and_preserves_diagnostic_messages() -> None:
    errors = _runtime_preflight_failure_errors("ValueError", "component=secret", ("frame=path:1:method",))
    assert errors == [
        CompositionValidationError(message="runtime_preflight_failed", error_code="runtime_preflight_failed", component=None),
        CompositionValidationError(message="exception_class=ValueError", error_code=None, component=None),
        CompositionValidationError(message="exception_message=component=secret", error_code=None, component=None),
        CompositionValidationError(message="frame=path:1:method", error_code=None, component=None),
    ]
    assert _composer_persisted_validation(ValidationSummary(is_valid=True, errors=()), _RUNTIME_PREFLIGHT_FAILED) == (False, errors[:1])


@pytest.mark.parametrize("field", ["message", "component", "error_code"])
def test_route_fork_backstop_scans_all_serialized_error_fields(field: str, tmp_path: Path) -> None:
    parent_id = uuid4()
    parent_ref = str(uuid4())
    values = {"message": "ordinary", "component": None, "error_code": None}
    values[field] = f"embedded parent reference {parent_ref}"
    state = _record((CompositionValidationError(**values),))
    with pytest.raises(AuditIntegrityError, match="forked validation_errors retains parent blob custody"):
        _rewrite_fork_state_blob_custody(
            state,
            {},
            {},
            parent_blob_refs=frozenset({parent_ref}),
            data_dir=tmp_path,
            parent_session_id=parent_id,
            child_session_id=uuid4(),
        )
    rewritten = _rewrite_fork_state_blob_custody(
        state, {}, {}, parent_blob_refs=frozenset({str(uuid4())}), data_dir=tmp_path, parent_session_id=parent_id, child_session_id=uuid4()
    )
    assert rewritten is None, "no rewrite is needed when no parent custody is referenced"
