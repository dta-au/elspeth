"""Direct SQL probes shared by SQLite and PostgreSQL quota schema tests."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.web.sessions.models import quota_provider_attempts_table, sessions_table, token_usage_ledger_table
from tests.fixtures.identities import ensure_test_identity


def assert_quota_measure_constraint(conn: Connection, *, session_id: str, identity_id: str, column: str) -> None:
    ledger = {
        "entry_id": "measure",
        "identity_id": identity_id,
        "session_id": session_id,
        "source": "composer",
        "run_id": None,
        "model": "test/model",
        "recorded_at": datetime.now(UTC),
    }
    # A missing count is admitted as unknown; a zero remains a known measure.
    conn.execute(insert(token_usage_ledger_table).values(**ledger, **{column: None}))
    conn.execute(update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == "measure").values(**{column: 0}))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == "measure").values(**{column: -1}))
    assert conn.execute(select(token_usage_ledger_table.c[column])).scalar_one() == 0


def assert_quota_custody_constraint(conn: Connection, *, session_id: str, identity_id: str, mismatch: str) -> None:
    now = datetime.now(UTC)
    ledger = {
        "entry_id": "custody",
        "identity_id": identity_id,
        "session_id": session_id,
        "source": "run",
        "run_id": "run-1",
        "model": "test/model",
        "recorded_at": now,
        "prompt_tokens": 1,
        "completion_tokens": 2,
    }
    attempt = {
        "attempt_id": "attempt",
        "identity_id": identity_id,
        "session_id": session_id,
        "source": "run",
        "run_id": "run-1",
        "started_at": now,
        "settled_at": now,
        "operation_id": "operation",
        "operation_epoch": 1,
        "lease_token": "lease",
        "ledger_entry_id": "custody",
    }
    conn.execute(insert(token_usage_ledger_table).values(**ledger))
    # Valid alternative parents prevent a missing identity/session FK from
    # masquerading as proof that the compound custody relation is enforced.
    ensure_test_identity(conn, identity_id="other-identity")
    sibling = dict(conn.execute(select(sessions_table).where(sessions_table.c.id == session_id)).mappings().one())
    sibling["id"] = "other-session"
    conn.execute(insert(sessions_table).values(**sibling))
    # Positive control exercises the same matching custody tuple first.
    conn.execute(insert(quota_provider_attempts_table).values(**attempt))
    if mismatch == "second_attempt":
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(insert(quota_provider_attempts_table).values(**(attempt | {"attempt_id": "second"})))
    else:
        changes = {
            "identity_id": {"identity_id": "other-identity"},
            "session_id": {"session_id": "other-session"},
            "source": {"source": "composer", "run_id": None},
            "run_id": {"run_id": "run-2"},
        }[mismatch]
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                update(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == "attempt").values(**changes)
            )
        # The parent side must also preserve custody after settlement.
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == "custody").values(**changes))
    linked = conn.execute(select(quota_provider_attempts_table)).one()
    assert (linked.identity_id, linked.session_id, linked.source, linked.run_id) == (identity_id, session_id, "run", "run-1")
