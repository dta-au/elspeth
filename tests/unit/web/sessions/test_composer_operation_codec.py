"""Golden parity and immutable composer semantic bindings."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.sessions.composer_operations import composer_operation_request_hash, composer_operation_result_hash
from elspeth.web.sessions.operation_codec import session_operation_request_hash, strict_response_hash
from elspeth.web.sessions.operation_receipts import operation_receipt_request_hash, operation_receipt_response_hash
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest

SID = UUID("11111111-1111-4111-8111-111111111111")
OP = "22222222-2222-4222-8222-222222222222"


class GoldenRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    operation_id: str
    state_id: str | None = None
    defaulted: int = 7


class GoldenResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str
    nullable: str | None = None


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("session_fork", "6230f838488dfa5a99bf0c62f0729a1c7f6d03932d9f8e80fd31293bc9fa9b6e"),
        ("state_revert", "6eb90da7a4b5364e03698f1d1df7d7db4929ce5328cd4e3fa0c137c7387cff19"),
    ],
)
def test_unmodified_receipt_golden_vectors(kind, expected) -> None:
    for request in (GoldenRequest(operation_id="first"), GoldenRequest(operation_id="second", state_id=None)):
        assert operation_receipt_request_hash(session_id=SID, kind=kind, request=request) == expected
        assert (
            session_operation_request_hash(schema="session-operation-receipt-request.v1", session_id=SID, kind=kind, request=request)
            == expected
        )
    # Deliberately omit the default/null fields: the golden gate must go red.
    mutated = stable_hash({"schema": "session-operation-receipt-request.v1", "session_id": str(SID), "kind": kind, "request": {}})
    with pytest.raises(AssertionError):
        assert mutated == expected


def test_unmodified_response_golden_vector() -> None:
    expected = "cd569a6d11ba2d43462d160f7c4810ec5a10ff105af122e7c6ea02745080ea7d"
    response = GoldenResponse(value="café")
    assert operation_receipt_response_hash(response) == expected
    assert strict_response_hash(response) == expected
    assert stable_hash({"value": "café"}) != expected


def test_composer_hash_binds_session_content_kind_and_base_excluding_only_action_id() -> None:
    first = SendMessageRequest(operation_id=OP, content="hello")
    digest = composer_operation_request_hash(session_id=SID, kind="compose_message", request=first)
    explicit_null = SendMessageRequest(operation_id=str(SID), content="hello", state_id=None)
    assert composer_operation_request_hash(session_id=SID, kind="compose_message", request=explicit_null) == digest
    for request in (
        SendMessageRequest(operation_id=OP, content="changed"),
        SendMessageRequest(operation_id=OP, content="hello", state_id=SID),
    ):
        assert composer_operation_request_hash(session_id=SID, kind="compose_message", request=request) != digest
    assert composer_operation_request_hash(session_id=UUID(OP), kind="compose_message", request=first) != digest
    retry = RecomposeRequest(operation_id=OP, expected_user_message_id=SID)
    assert composer_operation_request_hash(session_id=SID, kind="compose_recompose", request=retry) != digest
    with pytest.raises(AuditIntegrityError):
        composer_operation_request_hash(session_id=SID, kind="compose_recompose", request=first)


def test_codec_refuses_non_strict_and_mutated_owned_response() -> None:
    class Loose(BaseModel):
        operation_id: str

    with pytest.raises(AuditIntegrityError):
        session_operation_request_hash(
            schema="composer-operation-request.v1", session_id=SID, kind="compose_message", request=Loose(operation_id=OP)
        )
    response = GoldenResponse(value="ok").model_copy(update={"value": 5})
    with pytest.raises(ValueError):
        strict_response_hash(response)
    with pytest.raises(ValueError):
        composer_operation_result_hash(
            json.dumps({"http_status": 500, "failure_code": "unknown", "body": {}, "error_type": None, "diagnostic_id": None})
        )


def test_segment_claim_requires_both_positive_sequence_and_generation():
    from elspeth.web.sessions.composer_operations import ComposerOperationClaim

    legacy = ComposerOperationClaim(session_id=SID, operation_id=OP, claim_token="x" * 32, attempt=1)
    assert legacy.segment_sequence is None and legacy.ownership_generation is None
    claim = ComposerOperationClaim(
        session_id=SID, operation_id=OP, claim_token="x" * 32, attempt=1, segment_sequence=2, ownership_generation=3
    )
    assert (claim.segment_sequence, claim.ownership_generation) == (2, 3)
    for sequence, generation in ((2, None), (None, 2), (0, 1), (1, 0), (-1, 1), (True, 1), (1, True), (1, 2.0)):
        with pytest.raises(ValueError, match="continuation claim"):
            ComposerOperationClaim(
                session_id=SID,
                operation_id=OP,
                claim_token="x" * 32,
                attempt=1,
                segment_sequence=sequence,
                ownership_generation=generation,
            )


@pytest.mark.parametrize(
    "kind,request_model,expected_v1,expected_v2",
    [
        (
            "compose_message",
            SendMessageRequest(operation_id=OP, content="hello"),
            "d66457da97ff7b5fb111645661567e863339b662d19bddaac88e0b2752161d1d",
            "79029fcd8d488212a4c8ccbfb3b7ae4c47dc11b5fd95b5bef2d054a918ed5ddd",
        ),
        (
            "compose_recompose",
            RecomposeRequest(operation_id=OP, expected_user_message_id=SID),
            "8b968928e8bf0174b81a177126afbe5d26604d8094bfe9b922171054eda77c6b",
            "426b877705b4f744b5eeed8f9951783d80539f5bc0dcb9eb02b3f8a364d4ffab",
        ),
    ],
)
def test_v2_request_hash_wraps_preserved_native_v1_golden(kind, request_model, expected_v1, expected_v2):
    from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash

    assert composer_operation_request_hash(session_id=SID, kind=kind, request=request_model) == expected_v1
    assert composer_operation_v2_request_hash(session_id=SID, kind=kind, request=request_model) == expected_v2
    changed_id = request_model.model_copy(update={"operation_id": str(SID)})
    assert composer_operation_v2_request_hash(session_id=SID, kind=kind, request=changed_id) == expected_v2
    assert composer_operation_v2_request_hash(session_id=UUID(OP), kind=kind, request=request_model) != expected_v2
