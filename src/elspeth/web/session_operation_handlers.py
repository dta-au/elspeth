"""Session-operation exception handlers shared by every ELSPETH web app.

The multi-replica session-operation substrate answers an ownership race with
one of two exceptions: :class:`SessionOperationFenceLost` when the caller's
authority is gone, and :class:`SessionOperationConflictError` when a live
lease already owns the session. Their HTTP mappings are one contract --
``create_app`` registers them, and a test app that stands in for a second
worker must register the same handlers, not a copy of their bodies.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import elspeth.contracts.errors as contract_errors
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import SessionOperationConflictError


def record_session_operation_fence_loss(request: Request, exc: SessionOperationFenceLost) -> None:
    """Retain bounded HTTP refusal diagnostics without exposing request identity.

    This is the HTTP recovery log channel used by the sibling app handlers;
    pipeline decisions and detached operation settlement remain audit-first.
    """
    route = request.scope["route"].path if "route" in request.scope else None
    try:
        structlog.get_logger(__name__).warning(
            "http_session_operation_fence_lost",
            reason=exc.reason.value,
            method=request.method,
            route=route,
        )
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception:
        # Logging is the channel of last resort: an ordinary logging-backend
        # failure must not turn the deterministic 404 into an unrelated 500.
        # Registered framework/audit integrity failures still escape.
        return


def register_session_operation_exception_handlers(app: FastAPI) -> None:
    """Install the fence-lost (404) and conflict (409) handlers on ``app``."""

    @app.exception_handler(SessionOperationFenceLost)
    async def _session_operation_fence_lost_handler(request: Request, exc: SessionOperationFenceLost) -> JSONResponse:
        """Map ownership races to the same nonleaking absence response."""
        record_session_operation_fence_loss(request, exc)
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    @app.exception_handler(SessionOperationConflictError)
    async def _session_operation_conflict_handler(_request: Request, _exc: SessionOperationConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "Session operation is already active"})


__all__ = ["record_session_operation_fence_loss", "register_session_operation_exception_handlers"]
