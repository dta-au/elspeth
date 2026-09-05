"""Session-operation exception handlers shared by every ELSPETH web app.

The multi-replica session-operation substrate answers an ownership race with
one of two exceptions: :class:`SessionOperationFenceLost` when the caller's
authority is gone, and :class:`SessionOperationConflictError` when a live
lease already owns the session. Their HTTP mappings are one contract --
``create_app`` registers them, and a test app that stands in for a second
worker must register the same handlers, not a copy of their bodies.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import SessionOperationConflictError


def register_session_operation_exception_handlers(app: FastAPI) -> None:
    """Install the fence-lost (404) and conflict (409) handlers on ``app``."""

    @app.exception_handler(SessionOperationFenceLost)
    async def _session_operation_fence_lost_handler(_request: Request, _exc: SessionOperationFenceLost) -> JSONResponse:
        """Map ownership races to the same nonleaking absence response."""
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    @app.exception_handler(SessionOperationConflictError)
    async def _session_operation_conflict_handler(_request: Request, _exc: SessionOperationConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "Session operation is already active"})


__all__ = ["register_session_operation_exception_handlers"]
