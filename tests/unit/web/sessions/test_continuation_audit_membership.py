"""Stored-JSON SQL correlation controls, not proof of paid audit authority.

The PostgreSQL module runs these same controls against its real session schema.
Full reader tests separately establish genuine authority and strict refusal.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, LargeBinary, Text, case, cast, func, select, text

from elspeth.web.composer.diagnostic_evidence import diagnostic_evidence_identity
from elspeth.web.sessions.continuation_audit_membership import (
    private_audit_membership_predicate,
    public_audit_membership_predicate,
)
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import _OUTER_ROW_MAX_BYTES
from elspeth.web.sessions.models import chat_messages_table as messages
from tests.unit.web.conftest import _make_session

_ATTEMPT = "membership-target-%_quoted'"
_OTHER = "unrelated-provider-attempt"


def _private_slot(attempt_id: str, *, failure: bool = False) -> dict[str, object]:
    identity = diagnostic_evidence_identity("provider-failure" if failure else "provider-request", attempt_id)
    return {
        "_kind": "composer_diagnostic_evidence",
        "tool_call_id": attempt_id,
        "evidence_id": identity,
        "evidence": {"tool_call_id": attempt_id, "evidence_id": identity},
    }


def _public_slots(attempt_id: str) -> list[dict[str, object]]:
    return [
        {"_kind": "llm_call", "call": {"call_id": attempt_id}},
        {"_kind": "visible_response_gap_for_source", "provider_call_id": attempt_id},
    ]


def _document(family: str, attempt_id: str) -> list[dict[str, object]]:
    return [_private_slot(attempt_id)] if family == "private" else _public_slots(attempt_id)


def _insert_documents(conn: Connection, documents: list[object]) -> tuple[str, list[str]]:
    session_id = str(uuid4())
    _make_session(conn, session_id=session_id)
    row_ids = [f"{session_id}-{index}" for index in range(len(documents))]
    for index, (row_id, document) in enumerate(zip(row_ids, documents, strict=True)):
        conn.execute(
            messages.insert().values(
                id=row_id,
                session_id=session_id,
                role="audit",
                content="SQL membership control",
                tool_calls=document,
                sequence_no=index,
                writer_principal="compose_loop",
                created_at=datetime.now(UTC),
            )
        )
    return session_id, row_ids


def _members(conn: Connection, family: str, session_id: str, *, attempt_id: str = _ATTEMPT, limit: int = 3) -> list[str]:
    predicate = private_audit_membership_predicate if family == "private" else public_audit_membership_predicate
    return list(
        conn.execute(
            select(messages.c.id)
            .where(messages.c.session_id == session_id, predicate(conn, attempt_id=attempt_id))
            .order_by(messages.c.sequence_no)
            .limit(limit)
        ).scalars()
    )


@pytest.mark.parametrize("family", ["private", "public"])
def test_exact_members_ignore_unrelated_payload_before_limit(engine: Engine, family: str) -> None:
    collisions = " ".join(
        (_ATTEMPT, diagnostic_evidence_identity("provider-request", _ATTEMPT), diagnostic_evidence_identity("provider-failure", _ATTEMPT))
    )
    unrelated = _document(family, _OTHER)
    unrelated[0]["arguments"] = {"content": collisions, "tool_call_id": _ATTEMPT}
    unrelated[0]["result"] = {"evidence_id": diagnostic_evidence_identity("provider-request", _ATTEMPT)}
    if family == "private":
        unrelated[0]["evidence"] = {
            "tool_call_id": _OTHER,
            "evidence_id": diagnostic_evidence_identity("provider-request", _OTHER),
            "wire_arguments": {"content": collisions},
        }
    else:
        unrelated[0]["call"] = {"call_id": _OTHER, "provider_request_id": collisions}
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [unrelated] * 4 + [_document(family, _ATTEMPT)])
        before = conn.execute(select(messages).where(messages.c.session_id == session_id).order_by(messages.c.sequence_no)).all()
        assert _members(conn, family, session_id, limit=2) == [row_ids[4]]
        assert _members(conn, family, session_id, attempt_id=_OTHER, limit=5) == row_ids[:4]
        assert conn.execute(select(messages).where(messages.c.session_id == session_id).order_by(messages.c.sequence_no)).all() == before


@pytest.mark.parametrize("family", ["private", "public"])
def test_duplicate_rows_remain_distinct_without_slot_multiplication(engine: Engine, family: str) -> None:
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [_document(family, _ATTEMPT)] * 2)
        assert _members(conn, family, session_id) == row_ids


def test_private_request_and_failure_members_keep_both_rows(engine: Engine) -> None:
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [[_private_slot(_ATTEMPT)], [_private_slot(_ATTEMPT, failure=True)]])
        assert _members(conn, "private", session_id) == row_ids


@pytest.mark.parametrize("family", ["private", "public"])
def test_target_after_unrelated_and_scalar_slots_remains_member(engine: Engine, family: str) -> None:
    mixed = [*_document(family, _OTHER), "not JSON text", None, 9, False, [], *_document(family, _ATTEMPT)]
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [mixed])
        assert _members(conn, family, session_id) == row_ids


@pytest.mark.parametrize("field", ["outer-call", "outer-request", "outer-failure", "inner-call", "inner-request", "inner-failure"])
def test_each_private_identity_retains_disagreeing_copies(engine: Engine, field: str) -> None:
    slot = _private_slot(_OTHER)
    if field == "outer-call":
        slot["tool_call_id"] = _ATTEMPT
    elif field.startswith("outer-"):
        slot["evidence_id"] = diagnostic_evidence_identity("provider-" + field.removeprefix("outer-"), _ATTEMPT)
    elif field == "inner-call":
        slot["evidence"] = {"tool_call_id": _ATTEMPT, "evidence_id": diagnostic_evidence_identity("provider-request", _OTHER)}
    else:
        slot["evidence"] = {
            "tool_call_id": _OTHER,
            "evidence_id": diagnostic_evidence_identity("provider-" + field.removeprefix("inner-"), _ATTEMPT),
        }
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [[slot]])
        assert _members(conn, "private", session_id) == row_ids


@pytest.mark.parametrize("target", ["call", "companion"])
def test_each_public_identity_retains_disagreeing_partner(engine: Engine, target: str) -> None:
    document = _public_slots(_OTHER)
    if target == "call":
        document[0]["call"] = {"call_id": _ATTEMPT}
    else:
        document[1]["provider_call_id"] = _ATTEMPT
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [document])
        assert _members(conn, "public", session_id) == row_ids


@pytest.mark.parametrize("family", ["private", "public"])
def test_object_root_claim_beside_valid_array_remains_duplicate_candidate(engine: Engine, family: str) -> None:
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [_document(family, _ATTEMPT), _document(family, _ATTEMPT)[0]])
        assert _members(conn, family, session_id) == row_ids


@pytest.mark.parametrize("family", ["private", "public"])
@pytest.mark.parametrize("shape", ["array", "object", "mixed"])
def test_oversized_matching_row_survives_bounded_projection(engine: Engine, family: str, shape: str) -> None:
    oversized = _document(family, _ATTEMPT)
    oversized[0]["padding"] = "x" * (_OUTER_ROW_MAX_BYTES + 1)
    document: object = oversized
    if shape == "object":
        document = oversized[0]
    elif shape == "mixed":
        document = [*_document(family, _OTHER), *oversized]
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [_document(family, _ATTEMPT), document])
        predicate = private_audit_membership_predicate if family == "private" else public_audit_membership_predicate
        size = (
            func.octet_length(cast(messages.c.tool_calls, Text))
            if conn.dialect.name == "postgresql"
            else func.length(cast(messages.c.tool_calls, LargeBinary))
        )
        bounded = case((size <= _OUTER_ROW_MAX_BYTES, messages.c.tool_calls), else_=None)
        rows = conn.execute(
            select(messages.c.id, size, bounded)
            .where(messages.c.session_id == session_id, predicate(conn, attempt_id=_ATTEMPT))
            .order_by(messages.c.sequence_no)
            .limit(3)
        ).all()
        assert [row[0] for row in rows] == row_ids
        assert rows[0][1] <= _OUTER_ROW_MAX_BYTES and rows[0][2] == _document(family, _ATTEMPT)
        assert rows[1][1] > _OUTER_ROW_MAX_BYTES and rows[1][2] is None


@pytest.mark.parametrize("family", ["private", "public"])
def test_nonidentity_metadata_is_not_an_early_admission_filter(engine: Engine, family: str) -> None:
    document = _document(family, _ATTEMPT)
    for slot in document:
        slot.update(_kind="wrong-kind", operation_id="wrong-operation", operation_epoch=-1, status="wrong-status")
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [document])
        conn.execute(messages.update().where(messages.c.id == row_ids[0]).values(writer_principal="run_diagnostics"))
        assert _members(conn, family, session_id) == row_ids


@pytest.mark.parametrize("family", ["private", "public"])
def test_scalar_roots_and_nonobject_slots_do_not_claim_membership(engine: Engine, family: str) -> None:
    documents = [
        None,
        True,
        7,
        _ATTEMPT,
        json.dumps(_document(family, _ATTEMPT)[0]),
        {},
        [],
        ["not JSON text", _ATTEMPT, json.dumps(_document(family, _ATTEMPT)[0]), 7, True, None, [], _document(family, _ATTEMPT)],
        [{"evidence": _ATTEMPT, "call": _ATTEMPT}],
        _document(family, _OTHER),
        _document(family, _ATTEMPT + "-suffix"),
        _document(family, _ATTEMPT),
    ]
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, documents)
        assert _members(conn, family, session_id) == [row_ids[-1]]


@pytest.mark.parametrize("family", ["private", "public"])
def test_identity_requires_json_string_equality(engine: Engine, family: str) -> None:
    numeric = {"tool_call_id": 7, "evidence": {"tool_call_id": 7}, "call": {"call_id": 7}, "provider_call_id": 7}
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [[numeric], _document(family, "7")])
        assert _members(conn, family, session_id, attempt_id="7") == [row_ids[1]]


@pytest.mark.parametrize("family", ["private", "public"])
def test_sqlite_malformed_json_does_not_abort_other_members(engine: Engine, family: str) -> None:
    assert engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        session_id, row_ids = _insert_documents(conn, [[], _document(family, _ATTEMPT)])
        conn.execute(text("UPDATE chat_messages SET tool_calls = :broken WHERE id = :id"), {"broken": "{broken JSON", "id": row_ids[0]})
        assert _members(conn, family, session_id) == [row_ids[1]]
