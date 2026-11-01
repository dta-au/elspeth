"""Strict durable operation documents for local acceptance transports."""

from datetime import UTC, datetime

from elspeth.web.sessions.composer_operation_errors import worker_lost_error
from elspeth.web.sessions.schemas import ChatMessageResponse, ComposerOperationSnapshot, MessageWithStateResponse


def operation_document(
    operation_id: str,
    session_id: str,
    *,
    completed: bool = True,
    content: str = "Acknowledged",
    message_id: str = "55555555-5555-4555-8555-555555555555",
) -> dict[str, object]:
    result = (
        MessageWithStateResponse(
            message=ChatMessageResponse(
                id=message_id,
                session_id=session_id,
                role="assistant",
                content=content,
                segments=[],
                created_at=datetime.now(UTC),
            ),
            proposals=[],
        )
        if completed
        else None
    )
    now = datetime.now(UTC)
    return ComposerOperationSnapshot(
        operation_id=operation_id,
        kind="compose_message",
        status="completed" if completed else "failed",
        phase="settled",
        status_revision=1,
        created_at=now,
        observed_at=now,
        poll_after_ms=100,
        cancel_requested=False,
        segment=None,
        result=result,
        error=None if completed else worker_lost_error(request_id=None),
    ).model_dump(mode="json")
