"""Database-level author/approver separation on the closed-local schema."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from elspeth.web.sessions.models import approvals_table
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session


def _approval_row(session_id: str, approver: str) -> dict[str, object]:
    return {
        "approval_id": str(uuid4()),
        "session_id": session_id,
        "state_id": str(uuid4()),
        "binding_json": {},
        "requested_by_identity_id": "alice",
        "approver_identity_id": approver,
        "requested_at": datetime.now(UTC),
    }


def test_author_is_approver_is_refused_by_the_schema(closed_local_app: TestClient) -> None:
    session_id = str(uuid4())
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="alice")
    with pytest.raises(IntegrityError, match=r"ck_approvals_author_is_not_approver|CHECK constraint failed"), engine.begin() as conn:
        conn.execute(insert(approvals_table).values(**_approval_row(session_id, "alice")))
    with engine.connect() as conn:
        assert conn.execute(select(approvals_table.c.approval_id)).all() == []


def test_author_approver_constraint_admits_a_distinct_identity(closed_local_app: TestClient) -> None:
    session_id = str(uuid4())
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="alice")
        ensure_test_identity(conn, identity_id="bob")
        row = _approval_row(session_id, "bob")
        conn.execute(insert(approvals_table).values(**row))
    with engine.connect() as conn:
        assert conn.execute(select(approvals_table.c.approver_identity_id)).scalar_one() == "bob"
