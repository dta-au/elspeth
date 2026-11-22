"""A stored history row the reader cannot account for stops the turn, and the error says which row.

``AdvisorTranscriptHistory.from_records`` is fail-closed: a corrupt or foreign
row is a defect in the store, and composing does not proceed past it. The
refusal has to name the row, because the same refusal repeats on every later
turn of the session until someone repairs that row.

Each case starts from a row the real writers produce (the readable shapes are
in ``test_advisor_history_stored_row_shapes.py``) and damages one thing.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from tests.unit.web.composer.test_advisor_history_stored_row_shapes import _invocation, _stored_row


def _tool_row(
    session_id: UUID, *, sequence_no: int = 1, tool_call_id: str = "call-1", damage: Callable[[dict[str, Any]], None] | None = None
) -> Any:
    content, envelope = redacted_tool_invocation_content_and_envelope(_invocation("success"))
    envelope = deepcopy(envelope)
    if damage is not None:
        damage(envelope)
    return _stored_row(session_id, "tool", content, sequence_no=sequence_no, tool_call_id=tool_call_id, tool_calls=[envelope])


def _current(session_id: UUID, *, sequence_no: int = 2) -> Any:
    return _stored_row(session_id, "user", "Continue", sequence_no=sequence_no, writer="route_user_message")


def _result_hash_does_not_match(session_id: UUID) -> tuple[list[Any], Any]:
    def damage(envelope: dict[str, Any]) -> None:
        envelope["invocation"]["result_hash"] = "0" * 64

    bad = _tool_row(session_id, damage=damage)
    return [bad, _current(session_id)], bad


def _audit_names_another_call(session_id: UUID) -> tuple[list[Any], Any]:
    bad = _tool_row(session_id, tool_call_id="call-2")
    return [bad, _current(session_id)], bad


def _envelope_of_an_unknown_kind(session_id: UUID) -> tuple[list[Any], Any]:
    def damage(envelope: dict[str, Any]) -> None:
        envelope["_kind"] = "not-a-kind-any-writer-stores"

    bad = _tool_row(session_id, damage=damage)
    return [bad, _current(session_id)], bad


def _row_from_another_session(session_id: UUID) -> tuple[list[Any], Any]:
    bad = _tool_row(uuid4())
    return [bad, _current(session_id)], bad


def _row_listed_twice(session_id: UUID) -> tuple[list[Any], Any]:
    row = _tool_row(session_id)
    return [row, row, _current(session_id, sequence_no=3)], row


def _row_out_of_stored_order(session_id: UUID) -> tuple[list[Any], Any]:
    first = _tool_row(session_id, sequence_no=5)
    bad = _current(session_id, sequence_no=2)
    return [first, bad], bad


def _assistant_with_a_malformed_tool_call(session_id: UUID) -> tuple[list[Any], Any]:
    bad = _stored_row(session_id, "assistant", "", sequence_no=1, tool_calls=[{"id": 5, "type": "function"}])
    return [bad, _current(session_id)], bad


@pytest.mark.parametrize(
    "corrupt",
    [
        _result_hash_does_not_match,
        _audit_names_another_call,
        _envelope_of_an_unknown_kind,
        _row_from_another_session,
        _row_listed_twice,
        _row_out_of_stored_order,
        _assistant_with_a_malformed_tool_call,
    ],
    ids=lambda corrupt: corrupt.__name__.strip("_").replace("_", "-"),
)
def test_a_corrupt_stored_row_stops_the_turn_and_the_error_names_the_row(corrupt: Callable[[UUID], tuple[list[Any], Any]]) -> None:
    session_id = uuid4()
    records, bad_row = corrupt(session_id)
    current = records[-1]

    with pytest.raises(AuditIntegrityError) as refused:
        AdvisorTranscriptHistory.from_records(
            session_id=str(session_id),
            actor_user_id="actor",
            current_request_id=str(current.id),
            request="Continue",
            records=records,
        )

    assert str(bad_row.id) in str(refused.value)


@pytest.mark.parametrize("entry", [None, 7, "not an object", []], ids=["null", "number", "text", "list"])
def test_a_stored_tool_call_entry_that_is_not_an_object_is_refused_as_an_integrity_fault_naming_the_row(entry: Any) -> None:
    """The entry cannot be inspected at all, so it is refused before any reader touches it."""
    session_id = uuid4()
    bad_row = _stored_row(session_id, "tool", "{}", sequence_no=1, tool_call_id="call-1", tool_calls=[entry])
    current = _current(session_id)

    with pytest.raises(AuditIntegrityError, match="tool_calls entry is not an object") as refused:
        AdvisorTranscriptHistory.from_records(
            session_id=str(session_id),
            actor_user_id="actor",
            current_request_id=str(current.id),
            request="Continue",
            records=[bad_row, current],
        )

    assert str(bad_row.id) in str(refused.value)


def test_a_programming_error_while_reading_a_well_formed_row_is_not_reported_as_a_corrupt_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a stored row's own shape is an integrity fault. A bug in the reader must surface as itself."""
    session_id = uuid4()
    earlier = _stored_row(session_id, "user", "An earlier request", sequence_no=1)
    current = _current(session_id)

    def broken_reader(**_kwargs: Any) -> Any:
        raise TypeError("a defect in the reader, not in the row")

    monkeypatch.setattr("elspeth.web.composer.advisor_transcript.replay_composer_control_message", broken_reader)

    with pytest.raises(TypeError, match="a defect in the reader"):
        AdvisorTranscriptHistory.from_records(
            session_id=str(session_id),
            actor_user_id="actor",
            current_request_id=str(current.id),
            request="Continue",
            records=[earlier, current],
        )


def test_a_history_without_the_current_request_is_refused_without_blaming_a_row() -> None:
    """Nothing is wrong with any stored row here; the request the turn is for is simply not among them."""
    session_id = uuid4()
    row = _tool_row(session_id)

    with pytest.raises(AuditIntegrityError) as refused:
        AdvisorTranscriptHistory.from_records(
            session_id=str(session_id),
            actor_user_id="actor",
            current_request_id=str(uuid4()),
            request="Continue",
            records=[row],
        )

    assert str(row.id) not in str(refused.value)
