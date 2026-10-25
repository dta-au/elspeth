"""Explicit database cases for the two conditional terminal-run requirements."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Connection, insert, select

from elspeth.web.sessions.models import composition_states_table, runs_table, sessions_table
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.composition_state import stored_default_pipeline_metadata

RUN_CHECK_SQL = {
    "ck_runs_completed_landscape_run_id": (
        "status NOT IN ('completed', 'completed_with_failures', 'empty') OR landscape_run_id IS NOT NULL"
    ),
    "ck_runs_failed_error": "status != 'failed' OR error IS NOT NULL",
}
RUN_CHECK_NAMES = frozenset(
    {
        "ck_runs_status",
        "ck_runs_id_nonblank",
        "ck_runs_session_id_nonblank",
        "ck_runs_ownership_all_or_none",
        "ck_runs_cancellation_source",
        "ck_runs_saga_state",
        "ck_runs_recovery_required_reason",
        *RUN_CHECK_SQL,
    }
)

# These are independent explicit cases, not derived from the model's CHECKs.
VALID_RUN_FIELDS = (
    pytest.param("pending", None, None, id="pending-nulls"),
    pytest.param("running", None, None, id="running-nulls"),
    pytest.param("cancelled", None, None, id="cancelled-nulls"),
    pytest.param("failed", "failure", None, id="failed-without-landscape"),
    pytest.param("completed", None, "landscape", id="completed"),
    pytest.param("completed_with_failures", None, "landscape", id="completed-with-failures"),
    pytest.param("empty", None, "landscape", id="empty"),
    pytest.param("completed", None, "", id="sql-allows-empty-landscape"),
    pytest.param("failed", "", None, id="sql-allows-empty-error"),
)
INVALID_RUN_FIELDS = (
    pytest.param("completed", None, None, "ck_runs_completed_landscape_run_id", id="completed-missing-landscape"),
    pytest.param("completed_with_failures", None, None, "ck_runs_completed_landscape_run_id", id="partial-missing-landscape"),
    pytest.param("empty", None, None, "ck_runs_completed_landscape_run_id", id="empty-missing-landscape"),
    pytest.param("failed", None, None, "ck_runs_failed_error", id="failed-missing-error"),
)


def seed_run_owner(conn: Connection) -> tuple[str, str]:
    """Create valid same-session parents, including real T5e metadata."""
    ensure_test_identity(conn, identity_id="run-check-user")
    now = datetime.now(UTC)
    session_id, state_id = str(uuid4()), str(uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=session_id,
            user_id="run-check-user",
            auth_provider_type="local",
            title="Terminal run checks",
            created_at=now,
            updated_at=now,
        )
    )
    conn.execute(
        insert(composition_states_table).values(
            id=state_id,
            session_id=session_id,
            version=1,
            is_valid=True,
            metadata_=stored_default_pipeline_metadata(),
            provenance="session_seed",
            created_at=now,
        )
    )
    return session_id, state_id


def run_values(session_id: str, state_id: str, *, status: str, error: str | None, landscape_run_id: str | None) -> dict[str, object]:
    now = datetime.now(UTC)
    terminal = status not in {"pending", "running"}
    return {
        "id": str(uuid4()),
        "session_id": session_id,
        "state_id": state_id,
        "status": status,
        "started_at": now,
        "finished_at": now if terminal else None,
        "error": error,
        "landscape_run_id": landscape_run_id,
        "saga_state": "terminal" if terminal else "draft",
    }


def run_rows(conn: Connection) -> tuple[tuple[object, ...], ...]:
    """Complete physical rows, ordered by identity, for rollback/no-write checks."""
    return tuple(tuple(row) for row in conn.execute(select(runs_table).order_by(runs_table.c.id)))


def assert_run_fields(conn: Connection, *, run_id: object, status: str, error: str | None, landscape_run_id: str | None) -> None:
    row = (
        conn.execute(
            select(runs_table.c.id, runs_table.c.status, runs_table.c.error, runs_table.c.landscape_run_id).where(runs_table.c.id == run_id)
        )
        .mappings()
        .one()
    )
    assert dict(row) == {"id": run_id, "status": status, "error": error, "landscape_run_id": landscape_run_id}
