"""Persistence records do not widen pending interpretation digest authority."""

from dataclasses import asdict

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.pending_interpretation import _pending_validation_error_records
from elspeth.web.sessions.protocol import CompositionValidationError, SessionPendingInterpretationValidationResult


@pytest.mark.parametrize("messages", [None, (), ("", "literal:code:component", "arbitrary human message")])
def test_pending_message_authority_survives_record_adapter(messages: tuple[str, ...] | None) -> None:
    validation = SessionPendingInterpretationValidationResult(candidate_digest="a" * 64, is_valid=False, validation_errors=messages)
    before = asdict(validation)
    records = _pending_validation_error_records(validation)
    assert asdict(validation) == before
    assert validation.validation_errors is messages
    assert validation.candidate_digest == "a" * 64
    if messages is None:
        assert records is None
    else:
        assert records == tuple(CompositionValidationError(message=message, error_code=None, component=None) for message in messages)


def test_pending_digest_boundary_rejects_persisted_error_records() -> None:
    record = CompositionValidationError(message="bad", error_code=None, component=None)
    with pytest.raises(AuditIntegrityError, match="exact string tuple"):
        SessionPendingInterpretationValidationResult(candidate_digest="a" * 64, is_valid=False, validation_errors=(record,))


def test_pending_digest_boundary_still_rejects_invalid_digest() -> None:
    with pytest.raises(AuditIntegrityError, match="digest"):
        SessionPendingInterpretationValidationResult(candidate_digest="not-a-digest", is_valid=False, validation_errors=("bad",))
