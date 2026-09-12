"""Closed persisted composition errors retain identity and nullable presence."""

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions import protocol
from elspeth.web.sessions.schemas import CompositionValidationErrorResponse
from elspeth.web.sessions.service import _refuse_unrewritable_fork_custody


def test_composition_error_contract_exists_and_round_trips():
    assert "CompositionValidationError" in vars(protocol)
    error = protocol.CompositionValidationError(message="detail", error_code=None, component="node")
    wire = [{"message": "detail", "error_code": None, "component": "node"}]
    assert protocol.serialize_composition_validation_errors((error,)) == wire
    assert protocol.decode_stored_composition_validation_errors(wire) == (error,)
    assert protocol.serialize_composition_validation_errors(None) is None
    assert protocol.serialize_composition_validation_errors(()) == []
    assert protocol.decode_stored_composition_validation_errors(None) is None
    assert protocol.decode_stored_composition_validation_errors([]) == ()


@pytest.mark.parametrize(
    "raw",
    [
        "old",
        ["old"],
        (),
        {},
        [{"message": "detail"}],
        [{"message": "detail", "error_code": None, "component": None, "context": {}}],
        [{"message": 1, "error_code": None, "component": None}],
        [{"message": "detail", "error_code": False, "component": None}],
        [{"message": "detail", "error_code": None, "component": []}],
    ],
)
def test_current_stored_errors_reject_malformed_records(raw):
    assert "decode_stored_composition_validation_errors" in vars(protocol)
    with pytest.raises(AuditIntegrityError):
        protocol.decode_stored_composition_validation_errors(raw)


def test_owned_error_collections_are_nominal_and_detached():
    assert "CompositionValidationError" in vars(protocol)
    error = protocol.CompositionValidationError(message="detail", error_code=None, component=None)
    original = [error]
    data = protocol.CompositionStateData(validation_errors=original)
    original.clear()
    assert data.validation_errors == (error,)
    with pytest.raises(AuditIntegrityError):
        protocol.CompositionStateData(validation_errors=["old"])
    object.__setattr__(error, "component", {})
    with pytest.raises(AuditIntegrityError):
        protocol.serialize_composition_validation_errors(data.validation_errors)


@pytest.mark.parametrize("field", ["message", "component", "error_code"])
def test_fork_custody_scans_every_serialized_error_field(field):
    fields = {"message": "detail", "error_code": None, "component": None}
    fields[field] = "embedded parent/blob/path here"
    error = protocol.CompositionValidationError(**fields)
    with pytest.raises(AuditIntegrityError, match="retains parent blob custody"):
        _refuse_unrewritable_fork_custody(
            composer_meta=None, validation_errors=(error,), metadata=None, forbidden=frozenset({"parent/blob/path"})
        )
    _refuse_unrewritable_fork_custody(
        composer_meta=None, validation_errors=(error,), metadata=None, forbidden=frozenset({"different/path"})
    )


@pytest.mark.parametrize(
    "wire",
    [
        "legacy",
        {"message": "detail"},
        {"message": 3, "error_code": None, "component": None},
        {"message": "detail", "error_code": None, "component": None, "context": {}},
    ],
)
def test_http_error_record_rejects_noncontract_shapes(wire):
    with pytest.raises(ValidationError):
        CompositionValidationErrorResponse.model_validate(wire)


def test_http_error_required_nulls_survive_serialization():
    wire = {"message": "detail", "error_code": None, "component": None}
    assert CompositionValidationErrorResponse.model_validate(wire).model_dump() == wire
