"""Guided persistence and replay expose only the nominal closed status."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.sessions.guided_replay import (
    guided_validation_errors,
    project_guided_response,
    validation_errors_for_composer_surface,
    with_guided_response_descriptor,
)
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord, CompositionValidationError, GuidedResponseDescriptor
from elspeth.web.sessions.schemas import CompositionValidationErrorResponse


def _record(is_valid=False):
    state = with_guided_response_descriptor(
        CompositionStateData(is_valid=is_valid, composer_meta={"guided_session": GuidedSession.initial().to_dict()}),
        GuidedResponseDescriptor(kind="guided_respond", next_turn=None, assistant_turn_seq=None),
    )
    return CompositionStateRecord(
        id=uuid4(),
        session_id=uuid4(),
        version=1,
        sources=None,
        source=None,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_=None,
        is_valid=is_valid,
        validation_errors=state.validation_errors,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=state.composer_meta,
    )


def test_guided_status_is_exact_nominal_record():
    errors = guided_validation_errors(is_valid=False)
    assert errors == (
        CompositionValidationError(message="guided_composition_invalid", error_code="guided_composition_invalid", component=None),
    )
    assert guided_validation_errors(is_valid=True) is None
    with pytest.raises(TypeError):
        guided_validation_errors(is_valid=1)


@pytest.mark.parametrize("key", ["guided_session", "guided_operation_replay"])
def test_guided_surface_withholds_raw_details(key):
    private = (CompositionValidationError(message="secret path", error_code="private_code", component="private_node"),)
    assert validation_errors_for_composer_surface(
        composer_meta={key: {}}, is_valid=False, validation_errors=private
    ) == guided_validation_errors(is_valid=False)
    assert validation_errors_for_composer_surface(composer_meta={key: {}}, is_valid=True, validation_errors=private) is None
    assert validation_errors_for_composer_surface(composer_meta={}, is_valid=False, validation_errors=private) is private


@pytest.mark.parametrize("is_valid", [False, True])
def test_replay_constructs_strict_nested_http_status(is_valid):
    response = project_guided_response(_record(is_valid), payloads=())
    state = response.composition_state
    assert state is not None
    if is_valid:
        assert state.validation_errors is None
    else:
        assert type(state.validation_errors[0]) is CompositionValidationErrorResponse
        assert state.model_dump(mode="json")["validation_errors"] == [
            {"message": "guided_composition_invalid", "error_code": "guided_composition_invalid", "component": None}
        ]


@pytest.mark.parametrize("errors", [None, (), (CompositionValidationError(message="private", error_code=None, component=None),)])
def test_replay_rejects_incorrect_owned_status(errors):
    record = replace(_record(), validation_errors=errors)
    with pytest.raises(AuditIntegrityError, match="invalid closed validation status"):
        project_guided_response(record, payloads=())


@pytest.mark.parametrize(
    "errors",
    [
        ("guided_composition_invalid",),
        ({"message": "guided_composition_invalid", "error_code": "guided_composition_invalid", "component": None},),
    ],
)
def test_replay_rejects_postconstruction_non_nominal_status(errors):
    record = _record()
    object.__setattr__(record, "validation_errors", errors)
    with pytest.raises(AuditIntegrityError, match="owned records"):
        project_guided_response(record, payloads=())
