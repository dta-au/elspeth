"""Closed durable composer identities, status bundles and replay integrity."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, Literal, Protocol, Self, final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.errors import ComposerOwnedSettlementFailure as ComposerOwnedSettlementFailure
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
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


class ComposerOperationSavedRevisions(BaseModel):
    """Operation-owned saved revisions, distinct from requested edits or current head."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    attribution: Literal["fenced", "unstarted", "unavailable"]
    count: int | None = Field(ge=0)
    latest_state_id: UUID | None
    latest_version: int | None = Field(ge=1)

    @model_validator(mode="after")
    def _validate_progress(self) -> Self:
        if self.attribution == "unavailable":
            if self.count is not None or self.latest_state_id is not None or self.latest_version is not None:
                raise ValueError("Unavailable progress cannot claim saved revisions")
            return self
        if self.count is None or (self.attribution == "unstarted" and self.count != 0):
            raise ValueError("Known progress requires an attributed revision count")
        if self.count == 0:
            if self.latest_state_id is not None or self.latest_version is not None:
                raise ValueError("Zero revisions cannot identify a saved state")
        elif self.latest_state_id is None or self.latest_version is None:
            raise ValueError("Saved revisions require their latest state identity and version")
        return self


type ComposerOperationPublicFailureType = Literal[
    "request_cancelled",
    "composer_operation_deadline_expired",
    "composer_operation_worker_lost",
    "operation_failed",
    "audit_integrity_error",
    "database_unavailable",
    "executor_unavailable",
    "storage_unavailable",
    "stale_compose_state",
    "composer_mutation_authority_revoked",
    "fingerprint_key_missing",
    "secret_decryption_failed",
]
COMPOSER_OPERATION_PUBLIC_FAILURE_TYPES: Final[frozenset[str]] = frozenset(
    (
        "request_cancelled",
        "composer_operation_deadline_expired",
        "composer_operation_worker_lost",
        "operation_failed",
        "audit_integrity_error",
        "database_unavailable",
        "executor_unavailable",
        "storage_unavailable",
        "stale_compose_state",
        "composer_mutation_authority_revoked",
        "fingerprint_key_missing",
        "secret_decryption_failed",
    )
)


class ComposerOperationFailureOutcome(BaseModel):
    """Closed public projection of a sealed failed operation; never its private body."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    session_id: UUID
    operation_id: str = Field(min_length=36, max_length=36)
    kind: ComposerOperationKind
    user_message_id: UUID | None
    settled_at: AwareDatetime
    failure_code: ComposerOperationFailureCode
    error_type: ComposerOperationPublicFailureType | None
    diagnostic_id: str | None
    request_id: str | None = Field(min_length=1, max_length=128)
    saved_revisions: ComposerOperationSavedRevisions

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        return require_canonical_operation_id(value)

    @model_validator(mode="after")
    def _validate_diagnostic(self) -> Self:
        if self.failure_code == "operation_failed":
            require_canonical_operation_id(self.diagnostic_id)
        elif self.diagnostic_id is not None:
            raise ValueError("Only operation_failed may carry a diagnostic id")
        return self


class ComposerOperationFailureCursor(BaseModel):
    """Ordering keys only; every page independently rechecks session and actor authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    settled_at: AwareDatetime
    operation_id: str = Field(min_length=36, max_length=36)

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        return require_canonical_operation_id(value)


def encode_composer_failure_cursor(cursor: ComposerOperationFailureCursor) -> str:
    """Serialize the owned history ordering keys for the public cursor wire."""
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode("utf-8")).decode("ascii")


def decode_composer_failure_cursor(value: str) -> ComposerOperationFailureCursor:
    """Parse a bounded wire cursor into the concrete history ordering DTO."""
    if not 1 <= len(value) <= 512:
        raise ValueError("Invalid failure history cursor")
    encoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    return ComposerOperationFailureCursor.model_validate_json(encoded, strict=True)


class ComposerOperationCancelledFailure(Protocol):
    def __call__(self, *, request_id: str | None) -> ComposerOperationError: ...


def composer_operation_request_hash(*, session_id: UUID, kind: ComposerOperationKind, request: BaseModel) -> str:
    from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest

    expected = SendMessageRequest if kind == "compose_message" else RecomposeRequest
    if kind not in COMPOSER_OPERATION_KINDS or type(request) is not expected:
        raise AuditIntegrityError("Composer operation kind and strict request DTO disagree")
    type(request).model_validate(request.model_dump(mode="python"), strict=True)
    return session_operation_request_hash(schema=COMPOSER_OPERATION_REQUEST_SCHEMA, session_id=session_id, kind=kind, request=request)


def composer_operation_v2_request_hash(*, session_id: UUID, kind: ComposerOperationKind, request: BaseModel) -> str:
    old_hash = composer_operation_request_hash(session_id=session_id, kind=kind, request=request)
    return stable_hash({"schema": "composer-operation-request.v2", "lifecycle_version": 2, "v1_request_hash": old_hash})


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
    segment_sequence: int | None = None
    ownership_generation: int | None = None

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("Composer claim session_id must be UUID")
        require_canonical_operation_id(self.operation_id)
        if type(self.claim_token) is not str or not 1 <= len(self.claim_token) <= 256:
            raise ValueError("Composer claim token must be bounded and nonempty")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("Composer claim attempt must be positive")
        segment = (self.segment_sequence, self.ownership_generation)
        if any(value is not None for value in segment) and any(type(value) is not int or value < 1 for value in segment):
            raise ValueError("partial or invalid continuation claim")


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
    deadline_at: datetime | None
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
    lifecycle_version: Literal[1, 2] = 1
    phase: Literal["queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"] | None = None
    status_revision: int = 0
    current_segment_sequence: int | None = None
    ownership_generation: int | None = None
    checkpoint_id: str | None = None
    journal_cursor: int = 0
    ingress_phase: Literal["pending", "committed"] | None = None
    reserved_user_message_id: UUID | None = None

    def __post_init__(self) -> None:
        try:
            self._validate()
        except (TypeError, ValueError, ValidationError) as exc:
            raise AuditIntegrityError("Tier 1: invalid composer operation record") from exc

    @property
    def legacy_deadline_at(self) -> datetime:
        if self.lifecycle_version != 1 or self.deadline_at is None:
            raise AuditIntegrityError("legacy deadline requested from a continuation record")
        return self.deadline_at

    def _validate(self) -> None:
        lifecycle_version: object = self.lifecycle_version
        if type(lifecycle_version) is not int or lifecycle_version not in (1, 2):
            raise ValueError("unknown composer lifecycle version")
        if self.lifecycle_version == 1:
            if self.deadline_at is None or any(
                value is not None
                for value in (
                    self.phase,
                    self.current_segment_sequence,
                    self.ownership_generation,
                    self.checkpoint_id,
                    self.ingress_phase,
                    self.reserved_user_message_id,
                )
            ):
                raise ValueError("legacy parent contains continuation state")
            self._validate_v1()
            return
        self._validate_v2()

    def _validate_v2(self) -> None:
        from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest

        if type(self.session_id) is not UUID or self.deadline_at is not None:
            raise ValueError("continuation parent must have UUID custody and no total deadline")
        require_canonical_operation_id(self.operation_id)
        if self.kind not in COMPOSER_OPERATION_KINDS or self.status not in COMPOSER_OPERATION_STATUSES:
            raise ValueError("unknown operation discriminator")
        if not is_lower_sha256_hex(self.request_hash):
            raise ValueError("invalid request digest")
        if type(self.actor_user_id) is not str or not 1 <= len(self.actor_user_id) <= 128:
            raise ValueError("invalid actor")
        if self.request_id is not None and (type(self.request_id) is not str or not 1 <= len(self.request_id) <= 128):
            raise ValueError("invalid correlation")
        if self.claim_owner_instance_id is not None and (
            type(self.claim_owner_instance_id) is not str or not 1 <= len(self.claim_owner_instance_id) <= 128
        ):
            raise ValueError("invalid owner projection")
        if self.session_operation_id is not None and (type(self.session_operation_id) is not str or not self.session_operation_id):
            raise ValueError("invalid session operation projection")
        if self.session_operation_epoch is not None and (type(self.session_operation_epoch) is not int or self.session_operation_epoch < 1):
            raise ValueError("invalid session operation epoch projection")
        for value in (self.base_state_id, self.user_message_id):
            if value is not None and type(value) is not UUID:
                raise ValueError("invalid UUID locator")
        if (
            type(self.status_revision) is not int
            or self.status_revision < 0
            or type(self.journal_cursor) is not int
            or self.journal_cursor < 0
        ):
            raise ValueError("invalid continuation counters")
        if type(self.current_segment_sequence) is not int or self.current_segment_sequence < 1:
            raise ValueError("invalid segment sequence")
        if type(self.ownership_generation) is not int or self.ownership_generation < 1:
            raise ValueError("invalid ownership generation")
        if self.checkpoint_id is None:
            raise ValueError("missing complete snapshot pointer")
        require_canonical_operation_id(self.checkpoint_id)
        if type(self.reserved_user_message_id) is not UUID or self.ingress_phase not in ("pending", "committed"):
            raise ValueError("missing ingress reservation")
        if self.ingress_phase == "pending" and self.user_message_id is not None:
            raise ValueError("pending ingress already has a user row")
        if self.ingress_phase == "committed" and self.user_message_id != self.reserved_user_message_id:
            raise ValueError("committed ingress changed reserved identity")
        if self.kind == "compose_recompose" and self.ingress_phase != "committed":
            raise ValueError("recompose ingress must be committed")
        if (
            type(self.attempt) is not int
            or self.attempt < 0
            or type(self.claim_token_present) is not bool
            or self.claim_token_present
            or self.claim_expires_at is not None
        ):
            raise ValueError("v2 parent does not own a claim")
        if (
            type(self.created_at) is not datetime
            or type(self.updated_at) is not datetime
            or restore_utc(self.updated_at) < restore_utc(self.created_at)
        ):
            raise ValueError("invalid parent timestamps")
        for timestamp in (self.started_at, self.settled_at, self.cancel_requested_at):
            if timestamp is not None and (type(timestamp) is not datetime or restore_utc(timestamp) < restore_utc(self.created_at)):
                raise ValueError("invalid optional timestamp")
        if self.status in ("queued", "running"):
            if self.phase not in ("queued", "executing", "continuation_queued", "reconciling", "retry_waiting"):
                raise ValueError("invalid active phase")
            if any(
                value is not None
                for value in (self.settled_at, self.result_schema, self.result_json, self.result_sha256, self.failure_code, self.settled_by)
            ):
                raise ValueError("active parent contains terminal result")
            if self.request_json is not None:
                validate_composer_operation_request_json(self.request_json)
                model = SendMessageRequest if self.kind == "compose_message" else RecomposeRequest
                request = model.model_validate_json(self.request_json, strict=True)
                if request.operation_id != self.operation_id or request.state_id != self.base_state_id:
                    raise ValueError("request identity/base mismatch")
                if isinstance(request, RecomposeRequest) and request.expected_user_message_id != self.reserved_user_message_id:
                    raise ValueError("recompose request changed reserved original message")
                if composer_operation_v2_request_hash(session_id=self.session_id, kind=self.kind, request=request) != self.request_hash:
                    raise ValueError("v2 request digest mismatch")
            return
        if self.phase != "settled" or self.request_json is not None or self.settled_at is None:
            raise ValueError("invalid terminal continuation bundle")
        if self.settled_by not in COMPOSER_OPERATION_SETTLED_BY or self.result_json is None or not is_lower_sha256_hex(self.result_sha256):
            raise ValueError("missing terminal receipt")
        if self.status == "completed":
            if (
                self.cancel_requested_at is not None
                or self.failure_code is not None
                or self.user_message_id is None
                or self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS
            ):
                raise ValueError("invalid completed continuation")
            response: BaseModel = MessageWithStateResponse.model_validate_json(self.result_json, strict=True)
        else:
            if self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR or self.failure_code not in COMPOSER_OPERATION_FAILURE_CODES:
                raise ValueError("invalid failed continuation")
            response = ComposerOperationError.model_validate_json(self.result_json, strict=True)
            if response.failure_code != self.failure_code:
                raise ValueError("failed continuation code mismatch")
        if strict_response_hash(response) != self.result_sha256:
            raise ValueError("terminal continuation digest mismatch")

    def _validate_v1(self) -> None:
        from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest

        if self.deadline_at is None:
            raise ValueError("Legacy operation requires its original absolute deadline")
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
