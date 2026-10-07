"""Closed durable composer identities, status bundles and replay integrity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, Literal, Protocol, Self, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.errors import ComposerOwnedSettlementFailure as ComposerOwnedSettlementFailure
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.sessions.operation_codec import session_operation_request_hash, strict_response_hash
from elspeth.web.sessions.time_normalization import restore_utc

type ComposerOperationKind = Literal["compose_message", "compose_recompose"]
type ComposerOperationStatus = Literal["queued", "running", "completed", "failed"]
type ComposerOperationFailureCode = Literal["http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired"]
type ComposerOperationSettledBy = Literal[
    "owner_terminal", "settle_unstarted", "request_cancel", "settle_lost", "settle_own_lapsed", "settle_lost_inactive_session"
]
COMPOSER_OPERATION_KINDS: Final[frozenset[str]] = frozenset(("compose_message", "compose_recompose"))
COMPOSER_OPERATION_STATUSES: Final[frozenset[str]] = frozenset(("queued", "running", "completed", "failed"))
COMPOSER_OPERATION_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    ("http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired")
)
COMPOSER_OPERATION_SETTLED_BY: Final[frozenset[str]] = frozenset(
    ("owner_terminal", "settle_unstarted", "request_cancel", "settle_lost", "settle_own_lapsed", "settle_lost_inactive_session")
)
COMPOSER_OPERATION_REQUEST_SCHEMA: Final = "composer-operation-request.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS: Final = "message_with_state.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_ERROR: Final = "composer_operation_error.v1"
# 65,536 Unicode codepoints at JSON's worst-case 12-byte surrogate escaping,
# plus closed DTO framing/UUID fields. Both UTF-8 and ensure_ascii encodings fit.
COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH: Final[int] = 786_560


class ComposerRequiredRecoveryFailure(SQLAlchemyError):
    """Required recovery storage failed after its audit cohort completed."""

    def __init__(self, original: SQLAlchemyError) -> None:
        super().__init__("Required composer recovery storage failed")
        self.__cause__ = original
        self.audit_cohort_durable = True


class ComposerOperationCancelReason(Enum):
    CANCEL_REQUESTED = "cancel_requested"
    LEASE_LOST = "lease_lost"
    SHUTDOWN = "shutdown"
    DEADLINE = "deadline"


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationCancel:
    reason: ComposerOperationCancelReason

    def __post_init__(self) -> None:
        if type(self.reason) is not ComposerOperationCancelReason:
            raise TypeError("Composer cancellation requires an owned reason")


COMPOSER_CANCEL_REQUESTED: Final = ComposerOperationCancel(ComposerOperationCancelReason.CANCEL_REQUESTED)
COMPOSER_LEASE_LOST: Final = ComposerOperationCancel(ComposerOperationCancelReason.LEASE_LOST)
COMPOSER_SHUTDOWN: Final = ComposerOperationCancel(ComposerOperationCancelReason.SHUTDOWN)
COMPOSER_DEADLINE: Final = ComposerOperationCancel(ComposerOperationCancelReason.DEADLINE)


def require_canonical_operation_id(value: object) -> str:
    """Require the canonical UUID spelling without coercion."""
    if type(value) is not str or len(value) != 36:
        raise ValueError("Composer operation id must be a canonical UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("Composer operation id must be a canonical UUID")
    return value


class ComposerOperationError(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    http_status: int = Field(ge=400, le=599)
    failure_code: ComposerOperationFailureCode
    error_type: str | None
    body: dict[str, JsonValue]
    diagnostic_id: str | None

    @model_validator(mode="after")
    def _validate_diagnostic(self) -> Self:
        if self.failure_code == "operation_failed":
            require_canonical_operation_id(self.diagnostic_id)
        elif self.diagnostic_id is not None:
            raise ValueError("Only operation_failed may carry a diagnostic id")
        return self


class ComposerOperationCancelledFailure(Protocol):
    def __call__(self, *, request_id: str | None) -> ComposerOperationError: ...


def composer_operation_request_hash(*, session_id: UUID, kind: ComposerOperationKind, request: BaseModel) -> str:
    from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest

    expected = SendMessageRequest if kind == "compose_message" else RecomposeRequest
    if kind not in COMPOSER_OPERATION_KINDS or type(request) is not expected:
        raise AuditIntegrityError("Composer operation kind and strict request DTO disagree")
    type(request).model_validate(request.model_dump(mode="python"), strict=True)
    return session_operation_request_hash(schema=COMPOSER_OPERATION_REQUEST_SCHEMA, session_id=session_id, kind=kind, request=request)


def validate_composer_operation_request_json(request_json: str) -> None:
    if type(request_json) is not str or len(request_json.encode("utf-8")) > COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH:
        raise ValueError("Composer operation request_json exceeds its UTF-8 byte bound")


def composer_operation_result_hash(result_json: str) -> str:
    from elspeth.web.sessions.schemas import MessageWithStateResponse

    try:
        response: BaseModel = MessageWithStateResponse.model_validate_json(result_json, strict=True)
    except ValidationError:
        response = ComposerOperationError.model_validate_json(result_json, strict=True)
    return strict_response_hash(response)


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationClaim:
    session_id: UUID
    operation_id: str
    claim_token: str
    attempt: int

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("Composer claim session_id must be UUID")
        require_canonical_operation_id(self.operation_id)
        if type(self.claim_token) is not str or not 1 <= len(self.claim_token) <= 256:
            raise ValueError("Composer claim token must be bounded and nonempty")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("Composer claim attempt must be positive")


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationRunning:
    claim: ComposerOperationClaim
    session_operation_context: SessionOperationContext

    def __post_init__(self) -> None:
        if type(self.claim) is not ComposerOperationClaim or type(self.session_operation_context) is not SessionOperationContext:
            raise TypeError("Composer running custody requires owned claim and context")
        if (
            self.session_operation_context.fence.session_id != str(self.claim.session_id)
            or self.session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
        ):
            raise ValueError("Composer running custody disagrees with its COMPOSE fence")


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationRecord:
    session_id: UUID
    operation_id: str
    kind: ComposerOperationKind
    status: ComposerOperationStatus
    request_hash: str
    actor_user_id: str
    request_id: str | None
    base_state_id: UUID | None
    deadline_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    settled_at: datetime | None
    cancel_requested_at: datetime | None
    claim_token_present: bool
    claim_owner_instance_id: str | None
    claim_expires_at: datetime | None
    attempt: int
    session_operation_id: str | None
    session_operation_epoch: int | None
    user_message_id: UUID | None
    failure_code: ComposerOperationFailureCode | None
    settled_by: ComposerOperationSettledBy | None
    result_schema: str | None
    result_json: str | None
    result_sha256: str | None
    request_json: str | None

    def __post_init__(self) -> None:
        try:
            self._validate()
        except (TypeError, ValueError, ValidationError) as exc:
            raise AuditIntegrityError("Tier 1: invalid composer operation record") from exc

    def _validate(self) -> None:
        from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest

        if type(self.session_id) is not UUID:
            raise ValueError("Invalid session identity")
        require_canonical_operation_id(self.operation_id)
        if self.kind not in COMPOSER_OPERATION_KINDS or self.status not in COMPOSER_OPERATION_STATUSES:
            raise ValueError("Unknown operation discriminator")
        if not is_lower_sha256_hex(self.request_hash):
            raise ValueError("Invalid request digest")
        if type(self.actor_user_id) is not str or not 1 <= len(self.actor_user_id) <= 128:
            raise ValueError("Invalid actor")
        if self.request_id is not None and (type(self.request_id) is not str or not 1 <= len(self.request_id) <= 128):
            raise ValueError("Invalid request correlation")
        if type(self.attempt) is not int or self.attempt < 0 or type(self.claim_token_present) is not bool:
            raise ValueError("Invalid claim attempt")
        for value in (self.base_state_id, self.user_message_id):
            if value is not None and type(value) is not UUID:
                raise ValueError("Invalid UUID locator")
        for timestamp in (self.deadline_at, self.created_at, self.updated_at):
            if type(timestamp) is not datetime:
                raise ValueError("Invalid timestamp")
        if restore_utc(self.updated_at) < restore_utc(self.created_at) or restore_utc(self.deadline_at) <= restore_utc(self.created_at):
            raise ValueError("Invalid timestamp ordering")
        for optional_timestamp in (self.started_at, self.settled_at, self.cancel_requested_at, self.claim_expires_at):
            if optional_timestamp is not None and (
                type(optional_timestamp) is not datetime or restore_utc(optional_timestamp) < restore_utc(self.created_at)
            ):
                raise ValueError("Invalid optional timestamp")
        if self.settled_at is not None and self.started_at is not None and restore_utc(self.settled_at) < restore_utc(self.started_at):
            raise ValueError("Settlement precedes start")
        if self.claim_owner_instance_id is not None and (
            type(self.claim_owner_instance_id) is not str or not 1 <= len(self.claim_owner_instance_id) <= 128
        ):
            raise ValueError("Invalid owner")
        fence = (self.session_operation_id, self.session_operation_epoch, self.started_at)
        if any(x is not None for x in fence) and any(x is None for x in fence):
            raise ValueError("Partial started fence bundle")
        started = self.started_at is not None
        if started:
            if type(self.session_operation_id) is not str or not self.session_operation_id:
                raise ValueError("Invalid session operation id")
            if type(self.session_operation_epoch) is not int or self.session_operation_epoch < 1 or self.attempt < 1:
                raise ValueError("Invalid started epoch/attempt")
            if self.claim_owner_instance_id is None:
                raise ValueError("Started operation lost owner")
        if self.status in ("queued", "running"):
            if any(
                x is not None
                for x in (self.settled_at, self.result_schema, self.result_json, self.result_sha256, self.failure_code, self.settled_by)
            ):
                raise ValueError("Live operation has terminal fields")
            # request_json is deliberately omitted by poll reads; full reads validate it below.
            if self.status == "queued":
                if started or self.user_message_id is not None:
                    raise ValueError("Queued operation has started fields")
                claimed = (self.claim_token_present, self.claim_owner_instance_id is not None, self.claim_expires_at is not None)
                if any(claimed) and not all(claimed):
                    raise ValueError("Partial queued claim")
                if self.claim_token_present and self.attempt < 1:
                    raise ValueError("Claimed operation has no attempt")
            elif not started or not self.claim_token_present or self.claim_expires_at is not None:
                raise ValueError("Invalid running bundle")
            if self.request_json is not None:
                validate_composer_operation_request_json(self.request_json)
                model = SendMessageRequest if self.kind == "compose_message" else RecomposeRequest
                request = model.model_validate_json(self.request_json, strict=True)
                if request.operation_id != self.operation_id or request.state_id != self.base_state_id:
                    raise ValueError("Request identity/base mismatch")
                if composer_operation_request_hash(session_id=self.session_id, kind=self.kind, request=request) != self.request_hash:
                    raise ValueError("Request digest mismatch")
                if (
                    self.status == "running"
                    and isinstance(request, RecomposeRequest)
                    and request.expected_user_message_id != self.user_message_id
                ):
                    raise ValueError("Recompose user binding mismatch")
            return
        if self.request_json is not None or self.claim_token_present or self.claim_expires_at is not None or self.settled_at is None:
            raise ValueError("Invalid terminal custody")
        if self.settled_by not in COMPOSER_OPERATION_SETTLED_BY or not is_lower_sha256_hex(self.result_sha256) or self.result_json is None:
            raise ValueError("Invalid terminal result")
        if self.status == "completed":
            if not started or self.cancel_requested_at is not None or self.failure_code is not None or self.user_message_id is None:
                raise ValueError("Invalid completed bundle")
            if self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS:
                raise ValueError("Invalid completed schema")
            response: BaseModel = MessageWithStateResponse.model_validate_json(self.result_json, strict=True)
        else:
            if self.failure_code not in COMPOSER_OPERATION_FAILURE_CODES or self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR:
                raise ValueError("Invalid failed schema/code")
            response = ComposerOperationError.model_validate_json(self.result_json, strict=True)
            if response.failure_code != self.failure_code:
                raise ValueError("Failed response disagrees with row")
        if strict_response_hash(response) != self.result_sha256:
            raise ValueError("Terminal digest mismatch")


@final
@dataclass(frozen=True, slots=True)
class ComposerOperationAssistantWrite:
    message_id: UUID
    content: str
    raw_content: str | None
    composition_state_id: UUID | None


class ComposerOperationConflictError(RuntimeError):
    def __init__(self, *, session_id: UUID, operation_id: str) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        super().__init__("Composer operation id was already used for another request")


class ComposerOperationActiveError(ComposerOperationConflictError):
    def __init__(self, *, session_id: UUID, operation_id: str, kind: ComposerOperationKind) -> None:
        self.kind = kind
        super().__init__(session_id=session_id, operation_id=operation_id)


class ComposerOperationCapacityError(RuntimeError):
    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Composer operation queue is full")


class ComposerOperationFenceLost(ComposerOperationConflictError):
    def __init__(self, *, session_id: UUID, operation_id: str, attempt: int | None = None) -> None:
        self.attempt = attempt
        super().__init__(session_id=session_id, operation_id=operation_id)


class ComposerOperationCancelledBeforeStart(RuntimeError):
    def __init__(self, claim: ComposerOperationClaim) -> None:
        self.claim = claim
        super().__init__("Composer operation was cancelled before start")


class ComposerOperationCancelledDuringTurn(ComposerOperationCancelledBeforeStart):
    pass


class ComposerOperationPreconditionRefused(RuntimeError):
    def __init__(self, *, error: ComposerOperationError) -> None:
        self.error = error
        super().__init__("Composer operation start precondition refused")


class ComposerTurnDeadlineExpired(RuntimeError):
    def __init__(self, *, session_id: UUID, operation_id: str, remaining_seconds: float, budget_seconds_at_running: float) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        self.remaining_seconds = remaining_seconds
        self.budget_seconds_at_running = budget_seconds_at_running
        super().__init__("Composer job budget expired before provider dispatch")
