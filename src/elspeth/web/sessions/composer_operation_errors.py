"""Secret-safe detached-turn equivalents of the mounted HTTP handlers."""

from __future__ import annotations

import asyncio
import errno
from uuid import uuid4

import structlog
from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException

from elspeth.contracts.credential_material import scrub_credential_material
from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.secrets import FingerprintKeyMissingError, SecretDecryptionError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerRequiredRecoveryFailure
from elspeth.web.sessions.protocol import StaleComposeStateError

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_RETRYABLE_STORAGE_ERRNOS = frozenset((errno.EIO, errno.ENOSPC, errno.EROFS))


def _http_error(status: int, body: dict[str, JsonValue], error_type: str | None = None) -> ComposerOperationError:
    return ComposerOperationError(http_status=status, failure_code="http_error", error_type=error_type, body=body, diagnostic_id=None)


def request_cancelled_error(*, request_id: str | None) -> ComposerOperationError:
    return ComposerOperationError(
        http_status=499,
        failure_code="request_cancelled",
        error_type="request_cancelled",
        body={"error_type": "request_cancelled", "detail": "The composer request was stopped.", "request_id": request_id},
        diagnostic_id=None,
    )


def deadline_expired_error(*, request_id: str | None, timeout_seconds: float) -> ComposerOperationError:
    return ComposerOperationError(
        http_status=504,
        failure_code="deadline_expired",
        error_type="composer_operation_deadline_expired",
        body={
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request waited too long to start. Please resubmit.",
            "timeout_seconds": timeout_seconds,
            "request_id": request_id,
        },
        diagnostic_id=None,
    )


def worker_lost_error(*, request_id: str | None) -> ComposerOperationError:
    return ComposerOperationError(
        http_status=503,
        failure_code="worker_lost",
        error_type="composer_operation_worker_lost",
        body={
            "error_type": "composer_operation_worker_lost",
            "detail": "The server stopped while composing this request. Reload to see what was saved, then resubmit.",
            "request_id": request_id,
        },
        diagnostic_id=None,
    )


def stale_base_error(*, request_id: str | None) -> ComposerOperationError:
    return _http_error(
        409,
        {
            "error_type": "stale_compose_state",
            "detail": "The session changed before this request started. Review the current pipeline and send again.",
            "request_id": request_id,
        },
        "stale_compose_state",
    )


def _generic_failure(exc: BaseException, *, request_id: str | None) -> ComposerOperationError:
    diagnostic_id = str(uuid4())
    structlog.get_logger(__name__).error(
        "composer_operation.operation_failed", diagnostic_id=diagnostic_id, request_id=request_id, exc_class=type(exc).__name__
    )
    return ComposerOperationError(
        http_status=500,
        failure_code="operation_failed",
        error_type="operation_failed",
        body={
            "detail": {
                "error_type": "operation_failed",
                "detail": "The compose operation failed. See the application audit log for diagnostic detail.",
                "request_id": request_id,
                "diagnostic_id": diagnostic_id,
            }
        },
        diagnostic_id=diagnostic_id,
    )


def _leaves(exc: BaseException) -> tuple[BaseException, ...]:
    if isinstance(exc, BaseExceptionGroup):
        return tuple(leaf for child in exc.exceptions for leaf in _leaves(child))
    if (
        isinstance(exc, (asyncio.CancelledError, ComposerOwnedSettlementFailure, ComposerRequiredRecoveryFailure))
        and exc.__cause__ is not None
    ):
        known_causes = tuple(
            leaf
            for leaf in _leaves(exc.__cause__)
            if isinstance(
                leaf,
                (
                    AuditIntegrityError,
                    SQLAlchemyError,
                    AsyncWorkerAdmissionTimeoutError,
                    RequiredGenerationUnavailable,
                    ComposerOwnedSettlementFailure,
                ),
            )
            or (isinstance(leaf, OSError) and leaf.errno in _RETRYABLE_STORAGE_ERRNOS)
        )
        if known_causes:
            return known_causes
    return (exc,)


def _integrity_category(exc: BaseException) -> int:
    if isinstance(exc, AuditIntegrityError):
        return 0
    if isinstance(exc, (OperationalError, AsyncWorkerAdmissionTimeoutError, RequiredGenerationUnavailable)):
        return 1
    if isinstance(exc, OSError) and exc.errno in _RETRYABLE_STORAGE_ERRNOS:
        return 2
    if isinstance(exc, SQLAlchemyError):
        return 3
    return 4


def project_composer_operation_error(exc: BaseException, *, request_id: str | None) -> ComposerOperationError:
    """Integrity failures outrank ordinary HTTP or unclassified defect leaves.

    Temporal Stop/deadline/lease precedence is decided by worker custody; this
    projector handles the surviving exception without inspecting its message.
    """
    leaves = tuple(sorted(_leaves(exc), key=_integrity_category))
    for leaf in leaves:
        if isinstance(leaf, AuditIntegrityError):
            body: dict[str, JsonValue] = {
                "error_type": "audit_integrity_error",
                "detail": "ELSPETH stopped before replying because it could not verify this session's audit trail.",
                "request_id": request_id,
            }
            if leaf.failed_turn is None:
                body.update(diagnostic="no_failed_turn_metadata", reason="originated outside compose-loop annotation scope")
            else:
                failed = leaf.failed_turn
                body["failed_turn"] = {
                    "assistant_message_id": failed.assistant_message_id,
                    "tool_calls_attempted": failed.tool_calls_attempted,
                    "tool_responses_persisted": failed.tool_responses_persisted or 0,
                    "transcript_url": None,
                }
            return _http_error(500, body, "audit_integrity_error")
        if isinstance(leaf, (OperationalError, AsyncWorkerAdmissionTimeoutError, RequiredGenerationUnavailable)):
            return _http_error(
                503,
                {
                    "detail": "Database is currently unavailable. Please retry in a moment.",
                    "error_type": "database_unavailable",
                    "request_id": request_id,
                },
                "database_unavailable",
            )
        if isinstance(leaf, OSError) and leaf.errno in _RETRYABLE_STORAGE_ERRNOS:
            return _http_error(
                503,
                {
                    "detail": "Storage backend is currently unavailable. Please retry in a moment.",
                    "error_type": "storage_unavailable",
                    "request_id": request_id,
                },
                "storage_unavailable",
            )
    if any(isinstance(leaf, (SQLAlchemyError, ComposerOwnedSettlementFailure)) for leaf in leaves):
        return _generic_failure(exc, request_id=request_id)
    if len(leaves) != 1:
        return _generic_failure(exc, request_id=request_id)
    leaf = leaves[0]
    if isinstance(leaf, SessionOperationFenceLost):
        return _http_error(404, {"detail": "Session not found"})
    if isinstance(leaf, SessionOperationConflictError):
        return _http_error(409, {"detail": "Session operation is already active"})
    if isinstance(leaf, StaleComposeStateError):
        return _http_error(
            409,
            {
                "error_type": "stale_compose_state",
                "detail": "The session changed while the compose turn was running.",
                "request_id": request_id,
            },
            "stale_compose_state",
        )
    if isinstance(leaf, FingerprintKeyMissingError):
        return _http_error(
            503,
            {
                "detail": "Secret resolver is not configured: ELSPETH_FINGERPRINT_KEY is unset. Set the environment variable on the server and retry.",
                "error_type": "fingerprint_key_missing",
                "request_id": request_id,
            },
            "fingerprint_key_missing",
        )
    if isinstance(leaf, SecretDecryptionError):
        return _http_error(
            409,
            {
                "detail": "Stored secret cannot be decrypted — likely a web secret_key rotation. Re-save the secret to resolve.",
                "error_type": "secret_decryption_failed",
                "request_id": request_id,
            },
            "secret_decryption_failed",
        )
    if isinstance(leaf, HTTPException):
        try:
            detail = _JSON.validate_python(scrub_credential_material(leaf.detail), strict=True)
        except ValidationError:
            return _generic_failure(exc, request_id=request_id)
        error_type: str | None = None
        if isinstance(detail, dict):
            candidate = detail["error_type"] if "error_type" in detail else None
            if isinstance(candidate, str):
                error_type = candidate
            detail = {**detail, "request_id": request_id}
        return _http_error(leaf.status_code, {"detail": detail}, error_type)
    return _generic_failure(exc, request_id=request_id)
