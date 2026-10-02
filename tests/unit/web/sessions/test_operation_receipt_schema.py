"""Mode-neutral durable receipts for ordinary session mutations."""

from __future__ import annotations

from sqlalchemy import inspect, text

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import metadata


def test_fork_and_revert_have_a_separate_mode_neutral_receipt_schema() -> None:
    engine = create_session_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    inspector = inspect(engine)

    assert "session_operation_receipts" in inspector.get_table_names()
    assert "session_operation_receipt_events" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("session_operation_receipts")}
    assert {
        "session_id",
        "operation_id",
        "kind",
        "request_hash",
        "status",
        "lease_token",
        "lease_expires_at",
        "attempt",
        "result_state_id",
        "result_session_id",
        "response_hash",
        "failure_code",
    } <= columns
    assert "proposal_id" not in columns
    assert "result_message_id" not in columns
    with engine.connect() as conn:
        triggers = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN "
                    "('session_operation_receipts', 'session_operation_receipt_events')"
                )
            )
        }
    assert triggers == {
        "trg_session_operation_receipts_terminal_immutable",
        "trg_session_operation_receipt_events_no_update",
        "trg_session_operation_receipt_events_no_delete",
    }
