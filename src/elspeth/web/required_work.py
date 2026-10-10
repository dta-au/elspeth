"""Canonical required-work identities, actual completion custody and collision order."""

from __future__ import annotations

import asyncio
import errno
from _thread import RLock
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from math import isqrt
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast, final
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.secrets import FingerprintKeyMissingError, SecretDecryptionError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.sessions.pipeline_publication import (
    PipelineProposalSettlementResult,
    PipelinePublicationSQLResult,
    _ComposerRevocationRequired,
)
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionSQLResult

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRunning
    from elspeth.web.sessions.manual_proposal_failure import ComposerManualProposalFailure, _ClosedInvocation
    from elspeth.web.sessions.pipeline_revocation import ComposerRevocationSQLResult
    from elspeth.web.sessions.protocol import CompositionProposalRecord


class RequiredAuthorityKind(Enum):
    __hash__ = object.__hash__

    DURABLE_COMPOSE = "durable_compose"
    MANUAL_PROPOSAL = "manual_proposal"
    SYNCHRONOUS_COMPOSE = "synchronous_compose"


class ComposerRequiredStage(IntEnum):
    CREATION_BINDING = 0
    PREPARATION_READ = 1
    REVIEW_PREPARATION_VALIDATION = 2
    PROVIDER_ATTEMPT_ACCOUNTING = 3
    TOKEN_USAGE_ACCOUNTING = 4
    REQUIRED_TURN_AUDIT = 5
    TITLE_ACCOUNTING = 6
    PIPELINE_PUBLICATION = 7
    DISPATCH_AUDIT = 8
    REVOCATION_AUDIT = 9
    POSTCOMMIT_REVIEW_RECONCILIATION = 10
    REQUIRED_CONTINUATION_CHILD = 11
    TERMINAL_PUBLICATION = 12
    LEASE_RENEWAL = 13
    LEASE_CLOSE = 14


class RequiredWorkSubphase(IntEnum):
    PRODUCER = 0
    PURE_PREPARATION = 1
    SQL_INITIAL = 2
    SQL_RETRY = 3
    SQL_READBACK = 4
    PROJECTION = 5
    CANCELLATION_SIGNAL = 6
    CUSTODY_RECOVERY = 7


class RequiredWorkSource(IntEnum):
    PROPOSAL_CREATION_SQL = 0
    PROPOSAL_CREATION_PROJECTION = 1
    PREPARATION_READ_SQL = 2
    PREPARATION_READ_PROJECTION = 3
    PREPARATION_VALIDATION_PRODUCER = 4
    INTERPRETATION_VALIDATION_SQL = 5
    INTERPRETATION_VALIDATION_PROJECTION = 6
    PROVIDER_ADMISSION_SQL = 7
    PROVIDER_ADMISSION_PROJECTION = 8
    UNDISPATCHED_ATTEMPT_CANCELLATION_SQL = 9
    PROVIDER_SETTLEMENT_SQL = 10
    PROVIDER_SETTLEMENT_PROJECTION = 11
    INGRESS_SQL = 12
    INGRESS_PROJECTION = 13
    COMPOSE_CHECKPOINT_SQL = 14
    COMPOSE_CHECKPOINT_PROJECTION = 15
    REQUIRED_UNWIND_AUDIT_SQL = 16
    REQUIRED_UNWIND_AUDIT_PROJECTION = 17
    RECOVERY_PARTIAL_STATE_SQL = 18
    TITLE_ACCOUNTING_SQL = 19
    TITLE_ACCOUNTING_PROJECTION = 20
    TITLE_CONTINUATION_PRODUCER = 21
    PIPELINE_PUBLICATION_SQL = 22
    PIPELINE_PUBLICATION_PROJECTION = 23
    DISPATCH_AUDIT_SQL = 24
    DISPATCH_AUDIT_PROJECTION = 25
    TRUST_REVOCATION_SQL = 26
    TRUST_REVOCATION_PROJECTION = 27
    POSTCOMMIT_REVIEW_PROJECTION = 28
    POSTCOMMIT_REVIEW_READ_SQL = 29
    OWNED_TURN_SETUP_PRODUCER = 30
    REQUIRED_CONTINUATION_PRODUCER = 31
    CUSTODY_SUBMISSION_SETUP = 32
    OPERATION_TERMINAL_SQL = 33
    OPERATION_TERMINAL_PROJECTION = 34
    TERMINAL_WRITER_READ_SQL = 35
    TERMINAL_WRITER_READ_PROJECTION = 36
    TERMINAL_FAILURE_SQL = 37
    TERMINAL_FAILURE_PROJECTION = 38
    LEASE_ADOPTION = 39
    LEASE_RENEWAL = 40
    LEASE_CLOSE = 41
    LEASE_RELEASE = 42
    CREATION_PURE_PREPARATION = 43
    TITLE_PROVIDER_ADMISSION_SQL = 44
    TITLE_PROVIDER_ADMISSION_PROJECTION = 45
    TITLE_PROVIDER_SETTLEMENT_SQL = 46
    CUSTODY_GENERATION_RECOVERY = 47
    LOCAL_CANCELLATION_SIGNAL = 48
    DURABLE_STOP_SIGNAL = 49
    DEADLINE_SIGNAL = 50
    PROPOSAL_REJECTION_SQL = 51
    PROPOSAL_REJECTION_PROJECTION = 52


SOURCE_MAPPING = MappingProxyType(
    {
        RequiredWorkSource.PROPOSAL_CREATION_SQL: (ComposerRequiredStage.CREATION_BINDING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PROPOSAL_CREATION_PROJECTION: (ComposerRequiredStage.CREATION_BINDING, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.PREPARATION_READ_SQL: (ComposerRequiredStage.PREPARATION_READ, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PREPARATION_READ_PROJECTION: (ComposerRequiredStage.PREPARATION_READ, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER: (
            ComposerRequiredStage.REVIEW_PREPARATION_VALIDATION,
            RequiredWorkSubphase.PURE_PREPARATION,
        ),
        RequiredWorkSource.INTERPRETATION_VALIDATION_SQL: (
            ComposerRequiredStage.REVIEW_PREPARATION_VALIDATION,
            RequiredWorkSubphase.SQL_INITIAL,
        ),
        RequiredWorkSource.INTERPRETATION_VALIDATION_PROJECTION: (
            ComposerRequiredStage.REVIEW_PREPARATION_VALIDATION,
            RequiredWorkSubphase.PROJECTION,
        ),
        RequiredWorkSource.PROVIDER_ADMISSION_SQL: (ComposerRequiredStage.PROVIDER_ATTEMPT_ACCOUNTING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION: (
            ComposerRequiredStage.PROVIDER_ATTEMPT_ACCOUNTING,
            RequiredWorkSubphase.PROJECTION,
        ),
        RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL: (
            ComposerRequiredStage.PROVIDER_ATTEMPT_ACCOUNTING,
            RequiredWorkSubphase.SQL_INITIAL,
        ),
        RequiredWorkSource.PROVIDER_SETTLEMENT_SQL: (ComposerRequiredStage.TOKEN_USAGE_ACCOUNTING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION: (ComposerRequiredStage.TOKEN_USAGE_ACCOUNTING, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.INGRESS_SQL: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.INGRESS_PROJECTION: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.COMPOSE_CHECKPOINT_SQL: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.REQUIRED_UNWIND_AUDIT_PROJECTION: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.RECOVERY_PARTIAL_STATE_SQL: (ComposerRequiredStage.REQUIRED_TURN_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.TITLE_ACCOUNTING_SQL: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.TITLE_CONTINUATION_PRODUCER: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.PRODUCER),
        RequiredWorkSource.PIPELINE_PUBLICATION_SQL: (ComposerRequiredStage.PIPELINE_PUBLICATION, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION: (ComposerRequiredStage.PIPELINE_PUBLICATION, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.DISPATCH_AUDIT_SQL: (ComposerRequiredStage.DISPATCH_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.DISPATCH_AUDIT_PROJECTION: (ComposerRequiredStage.DISPATCH_AUDIT, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.TRUST_REVOCATION_SQL: (ComposerRequiredStage.REVOCATION_AUDIT, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.TRUST_REVOCATION_PROJECTION: (ComposerRequiredStage.REVOCATION_AUDIT, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.POSTCOMMIT_REVIEW_PROJECTION: (
            ComposerRequiredStage.POSTCOMMIT_REVIEW_RECONCILIATION,
            RequiredWorkSubphase.PROJECTION,
        ),
        RequiredWorkSource.POSTCOMMIT_REVIEW_READ_SQL: (
            ComposerRequiredStage.POSTCOMMIT_REVIEW_RECONCILIATION,
            RequiredWorkSubphase.SQL_INITIAL,
        ),
        RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.PRODUCER),
        RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER: (
            ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
            RequiredWorkSubphase.PRODUCER,
        ),
        RequiredWorkSource.CUSTODY_SUBMISSION_SETUP: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.PRODUCER),
        RequiredWorkSource.OPERATION_TERMINAL_SQL: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.OPERATION_TERMINAL_PROJECTION: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.TERMINAL_WRITER_READ_SQL: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.SQL_READBACK),
        RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.TERMINAL_FAILURE_SQL: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.TERMINAL_FAILURE_PROJECTION: (ComposerRequiredStage.TERMINAL_PUBLICATION, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.LEASE_ADOPTION: (ComposerRequiredStage.LEASE_RENEWAL, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.LEASE_RENEWAL: (ComposerRequiredStage.LEASE_RENEWAL, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.LEASE_CLOSE: (ComposerRequiredStage.LEASE_CLOSE, RequiredWorkSubphase.PRODUCER),
        RequiredWorkSource.LEASE_RELEASE: (ComposerRequiredStage.LEASE_CLOSE, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.CREATION_PURE_PREPARATION: (ComposerRequiredStage.CREATION_BINDING, RequiredWorkSubphase.PURE_PREPARATION),
        RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.PROJECTION),
        RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL: (ComposerRequiredStage.TITLE_ACCOUNTING, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.CUSTODY_GENERATION_RECOVERY: (
            ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
            RequiredWorkSubphase.CUSTODY_RECOVERY,
        ),
        RequiredWorkSource.LOCAL_CANCELLATION_SIGNAL: (
            ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
            RequiredWorkSubphase.CANCELLATION_SIGNAL,
        ),
        RequiredWorkSource.DURABLE_STOP_SIGNAL: (
            ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
            RequiredWorkSubphase.CANCELLATION_SIGNAL,
        ),
        RequiredWorkSource.DEADLINE_SIGNAL: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.CANCELLATION_SIGNAL),
        RequiredWorkSource.PROPOSAL_REJECTION_SQL: (ComposerRequiredStage.PIPELINE_PUBLICATION, RequiredWorkSubphase.SQL_INITIAL),
        RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION: (ComposerRequiredStage.PIPELINE_PUBLICATION, RequiredWorkSubphase.PROJECTION),
    }
)


_SOURCE_BY_ORDINAL = MappingProxyType({int.__int__(source): source for source in SOURCE_MAPPING})


def _source_for_ordinal(ordinal: int) -> RequiredWorkSource:
    if type(ordinal) is not int:
        raise AuditIntegrityError("Required-work source ordinal must be an exact integer")
    try:
        return _SOURCE_BY_ORDINAL[ordinal]
    except KeyError:
        raise ValueError(f"{ordinal} is not a valid RequiredWorkSource") from None


def _uuid(value: object) -> None:
    if type(value) is not str or str(UUID(value)) != value:
        raise AuditIntegrityError("Required-work identity must be a canonical UUID")


@final
@dataclass(frozen=True, slots=True)
class RequiredWorkAuthority:
    authority_kind: RequiredAuthorityKind
    context: SessionOperationContext
    durable_operation_id: str | None = None
    claim_attempt: int | None = None
    proposal_id: str | None = None
    invocation_id: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.authority_kind) is not RequiredAuthorityKind or type(self.context) is not SessionOperationContext:
            raise AuditIntegrityError("Required work requires nominal authority")
        _uuid(self.context.fence.session_id)
        _uuid(self.context.fence.operation_id)
        if self.proposal_id is not None:
            _uuid(self.proposal_id)
        if self.tool_call_id is not None and (type(self.tool_call_id) is not str or not self.tool_call_id):
            raise AuditIntegrityError("Required work tool identity must retain its existing nonempty domain string")
        if self.authority_kind is RequiredAuthorityKind.DURABLE_COMPOSE:
            _uuid(self.durable_operation_id)
            if (
                self.context.operation_kind is not SessionOperationKind.COMPOSE
                or type(self.claim_attempt) is not int
                or self.claim_attempt < 1
            ):
                raise AuditIntegrityError("Durable required-work authority disagrees with COMPOSE claim")
            if self.invocation_id is not None:
                raise AuditIntegrityError("Durable authority cannot invent a synchronous invocation")
        else:
            if self.durable_operation_id is not None or self.claim_attempt is not None:
                raise AuditIntegrityError("Manual/synchronous required work cannot carry durable job custody")
            _uuid(self.invocation_id)
            expected = (
                SessionOperationKind.PROPOSAL
                if self.authority_kind is RequiredAuthorityKind.MANUAL_PROPOSAL
                else SessionOperationKind.COMPOSE
            )
            if self.context.operation_kind is not expected:
                raise AuditIntegrityError("Required-work scope disagrees with its exact session-operation kind")
            if self.authority_kind is RequiredAuthorityKind.MANUAL_PROPOSAL and self.proposal_id is None:
                raise AuditIntegrityError("Manual proposal scope requires immutable proposal identity")


@final
@dataclass(frozen=True, slots=True)
class RequiredWorkKey:
    authority: RequiredWorkAuthority
    stage: ComposerRequiredStage
    subphase: RequiredWorkSubphase
    transition_ordinal: int
    semantic_ordinal: int
    invocation_ordinal: int
    sql_attempt_ordinal: int
    originating_key: RequiredWorkKey | None = None

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not RequiredWorkAuthority
            or type(self.stage) is not ComposerRequiredStage
            or type(self.subphase) is not RequiredWorkSubphase
        ):
            raise AuditIntegrityError("Unmapped required-work key")
        for ordinal in (self.transition_ordinal, self.semantic_ordinal, self.invocation_ordinal, self.sql_attempt_ordinal):
            if type(ordinal) is not int or ordinal < 0:
                raise AuditIntegrityError("Required-work ordinals must be exact nonnegative integers")
        if self.sql_attempt_ordinal not in (0, 1) or (self.subphase is RequiredWorkSubphase.SQL_RETRY) != (self.sql_attempt_ordinal == 1):
            raise AuditIntegrityError("Required-work SQL attempt disagrees with its phase")

        if self.originating_key is not None:
            origin = self.originating_key
            if (
                type(origin) is not RequiredWorkKey
                or origin.originating_key is not None
                or origin.authority != self.authority
                or origin.subphase
                not in (RequiredWorkSubphase.SQL_INITIAL, RequiredWorkSubphase.SQL_RETRY, RequiredWorkSubphase.SQL_READBACK)
                or self.stage is not ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD
                or self.subphase is not RequiredWorkSubphase.PRODUCER
                or (self.transition_ordinal, self.semantic_ordinal, self.invocation_ordinal)
                != (origin.transition_ordinal, origin.semantic_ordinal, origin.invocation_ordinal)
                or self.sql_attempt_ordinal != 0
            ):
                raise AuditIntegrityError("Submission setup key lacks exact originating SQL authority")
            return
        diagonal = (isqrt(8 * self.invocation_ordinal + 1) - 1) // 2
        recurrence = self.invocation_ordinal - diagonal * (diagonal + 1) // 2
        source_ordinal = diagonal - recurrence
        try:
            source = _source_for_ordinal(source_ordinal)
        except ValueError as error:
            raise AuditIntegrityError("Required-work invocation has no mapped source") from error
        expected_stage, expected_phase = SOURCE_MAPPING[source]
        allowed_phases: tuple[RequiredWorkSubphase, ...] = (expected_phase, RequiredWorkSubphase.PRODUCER)
        if expected_phase is RequiredWorkSubphase.SQL_INITIAL:
            allowed_phases += (RequiredWorkSubphase.SQL_RETRY,)
        if self.stage is not expected_stage or self.subphase not in allowed_phases:
            raise AuditIntegrityError("Required-work source mapping was changed")

    @property
    def source(self) -> RequiredWorkSource:
        if self.originating_key is not None:
            return RequiredWorkSource.CUSTODY_SUBMISSION_SETUP
        diagonal = (isqrt(8 * self.invocation_ordinal + 1) - 1) // 2
        recurrence = self.invocation_ordinal - diagonal * (diagonal + 1) // 2
        return _source_for_ordinal(diagonal - recurrence)

    @property
    def authority_kind(self) -> RequiredAuthorityKind:
        return self.authority.authority_kind

    @property
    def session_id(self) -> str:
        return self.authority.context.fence.session_id

    @property
    def session_operation_id(self) -> str:
        return self.authority.context.fence.operation_id

    @property
    def session_operation_epoch(self) -> int:
        return self.authority.context.fence.operation_epoch

    @property
    def session_operation_kind(self) -> SessionOperationKind:
        return self.authority.context.operation_kind

    @property
    def durable_operation_id(self) -> str | None:
        return self.authority.durable_operation_id

    @property
    def claim_attempt(self) -> int | None:
        return self.authority.claim_attempt

    @property
    def order(self) -> tuple[int, ...]:
        return (
            self.stage.value,
            self.subphase.value,
            self.transition_ordinal,
            self.semantic_ordinal,
            self.invocation_ordinal,
            self.sql_attempt_ordinal,
        )


def make_required_work_key(
    authority: RequiredWorkAuthority,
    source: RequiredWorkSource,
    *,
    transition_ordinal: int = 0,
    semantic_ordinal: int = 0,
    recurrence_ordinal: int = 0,
    sql_attempt_ordinal: int = 0,
    producer: bool = False,
) -> RequiredWorkKey:
    if type(source) is not RequiredWorkSource or type(recurrence_ordinal) is not int or recurrence_ordinal < 0:
        raise AuditIntegrityError("Unmapped required-work source")
    if source in (RequiredWorkSource.PROPOSAL_REJECTION_SQL, RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION) and (
        sql_attempt_ordinal or producer
    ):
        raise AuditIntegrityError("Rejection cannot acquire retry or producer SQL authority")
    stage, phase = SOURCE_MAPPING[source]
    if producer:
        phase = RequiredWorkSubphase.PRODUCER
    if sql_attempt_ordinal:
        if phase is not RequiredWorkSubphase.SQL_INITIAL:
            raise AuditIntegrityError("Only initial SQL sources can register exact retry")
        phase = RequiredWorkSubphase.SQL_RETRY
    total = int.__int__(source) + recurrence_ordinal
    invocation = total * (total + 1) // 2 + recurrence_ordinal
    return RequiredWorkKey(authority, stage, phase, transition_ordinal, semantic_ordinal, invocation, sql_attempt_ordinal)


@final
@dataclass(frozen=True, slots=True)
class OwnedCompletionWitness:
    key: RequiredWorkKey


class PublicationProjectionDisposition(Enum):
    PREFLIGHT_REFUSED = "preflight_refused"
    PUBLICATION_FAILED = "publication_failed"
    ELIGIBILITY_SELECTED = "eligibility_selected"


class RevocationWorkUnusedDisposition(Enum):
    BUSINESS_SELECTED = "business_selected"
    PUBLICATION_FAILED = "publication_failed"
    PREFLIGHT_REFUSED = "preflight_refused"
    SQL_FAILED_NO_VALUE = "sql_failed_no_value"


@final
@dataclass(frozen=True, slots=True)
class PublicationProjectionUnusedMetadata:
    projection_key23: RequiredWorkKey
    disposition: PublicationProjectionDisposition
    publication_ticket: RequiredWorkTicket
    actual_outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult]


@final
@dataclass(frozen=True, slots=True)
class RevocationWorkUnusedMetadata:
    target_key: RequiredWorkKey
    disposition: RevocationWorkUnusedDisposition
    publication_ticket: RequiredWorkTicket
    publication_outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult]
    revocation_ticket: RequiredWorkTicket | None = None
    revocation_outcome: RequiredSQLFinishOnce[ComposerRevocationSQLResult] | None = None


class RejectionProjectionUnusedDisposition(Enum):
    PREFLIGHT_REFUSED = "preflight_refused"
    SQL_FAILED_NO_VALUE = "sql_failed_no_value"


@final
@dataclass(frozen=True, slots=True)
class RejectionProjectionUnusedMetadata:
    projection_key52: RequiredWorkKey
    disposition: RejectionProjectionUnusedDisposition
    rejection_ticket: RequiredWorkTicket
    actual_outcome: RequiredSQLRaised


@final
@dataclass(frozen=True, slots=True)
class ComposerFailureReceipt:
    key: RequiredWorkKey
    original_root: BaseException
    original_category_witnesses: tuple[BaseException, ...]
    owned_completion_witness: OwnedCompletionWitness
    child_outcome: ChildFailureOutcome | None = None


class RequiredWorkIncomplete(AuditIntegrityError):
    """A required physical/producer outcome has no completion witness."""


class RequiredWorkTicket:
    """One prelaunch reservation; transport supplies actual executor witnesses."""

    def __init__(self, key: RequiredWorkKey, *, coordinator: RequiredWorkCoordinator | None = None) -> None:
        self._key = key
        self._lock = RLock()
        self._future: Future[Any] | None = None
        self._complete = False
        self._result: object = None
        self._finish_once_handoff: object | None = None
        self._errors: list[BaseException] = []
        self._submission_unknown = False
        self._generation_joined = False
        self._custody_errors: list[BaseException] = []
        self._child_outcome: ChildFailureOutcome | None = None
        self._coordinator = coordinator
        self._projection_started = False
        self._no_submission = False
        self._unused_metadata: (
            PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata | RejectionProjectionUnusedMetadata | None
        ) = None
        self._setup_key = (
            RequiredWorkKey(
                key.authority,
                ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
                RequiredWorkSubphase.PRODUCER,
                key.transition_ordinal,
                key.semantic_ordinal,
                key.invocation_ordinal,
                0,
                key,
            )
            if key.subphase in (RequiredWorkSubphase.SQL_INITIAL, RequiredWorkSubphase.SQL_RETRY, RequiredWorkSubphase.SQL_READBACK)
            else None
        )

    @property
    def key(self) -> RequiredWorkKey:
        return self._key

    @property
    def permits_process_drain(self) -> bool:
        return self.key.source in (
            RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
            RequiredWorkSource.PROVIDER_SETTLEMENT_SQL,
            RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL,
            RequiredWorkSource.TITLE_ACCOUNTING_SQL,
            RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
            RequiredWorkSource.OPERATION_TERMINAL_SQL,
            RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
            RequiredWorkSource.TERMINAL_FAILURE_SQL,
            RequiredWorkSource.LEASE_RENEWAL,
            RequiredWorkSource.LEASE_RELEASE,
            RequiredWorkSource.CUSTODY_GENERATION_RECOVERY,
        )

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._complete

    @property
    def pending_healthy_renewal(self) -> bool:
        with self._lock:
            return (
                self.key.source is RequiredWorkSource.LEASE_RENEWAL
                and not self._complete
                and self._future is not None
                and not self._future.done()
                and not self._submission_unknown
                and not self._custody_errors
                and not self._errors
            )

    @property
    def errors(self) -> tuple[BaseException, ...]:
        with self._lock:
            return tuple(self._errors)

    def bind_future[T](self, future: Future[T]) -> None:
        with self._lock:
            if not isinstance(future, Future) or self._future is not None or self._complete:
                raise AuditIntegrityError("Required-work Future binding is not once-only")
            self._future = future

    def observe_actual_outcome(self) -> None:
        with self._lock:
            if self._future is None or not self._future.done():
                raise RequiredWorkIncomplete("Required SQL has no actual Future completion")
            if self._complete:
                return
            try:
                self._result = self._future.result()
            except BaseException as error:
                self._errors.append(error)
            self._complete = True

    def observe_aborted_future_outcome(self, error: BaseException) -> None:
        with self._lock:
            if self._future is None or not self._future.done() or self._complete:
                raise RequiredWorkIncomplete("Aborted callable still lacks actual Future completion")
            self._future.exception()
            if all(error is not original for original in self._custody_errors):
                self._custody_errors.append(error)
            self._complete = True

    def observe_finish_once_handoff[T](self, outcome: RequiredSQLFinishOnce[T]) -> None:
        """Retain the bridge/service no-submit handoff before it can be replaced."""
        with self._lock:
            if self._finish_once_handoff is not None or not self._complete:
                raise RequiredWorkIncomplete("Finish-once handoff lacks once-only physical closure")
            if self._submission_unknown and not self._generation_joined:
                raise RequiredWorkIncomplete("Finish-once handoff cannot erase unknown custody")
            if self._future is None and not (self._no_submission or self._generation_joined):
                raise RequiredWorkIncomplete("Finish-once handoff has no physical/no-submit witness")
            if type(outcome) is not RequiredSQLReturned and type(outcome) is not RequiredSQLRaised:
                raise AuditIntegrityError("Finish-once handoff must be its nominal actual carrier")
            self._finish_once_handoff = outcome

    def observe_submission_unknown(self, error: BaseException) -> None:
        with self._lock:
            if self._complete:
                raise AuditIntegrityError("Completed ticket cannot become submission unknown")
            self._submission_unknown = True
            self._custody_errors.append(error)

    def complete_without_submission(self, error: BaseException | None = None) -> None:
        with self._lock:
            if self._future is not None or self._submission_unknown or self._complete:
                raise AuditIntegrityError("No-submission witness conflicts with required ticket custody")
            if error is not None:
                self._errors.append(error)
            self._no_submission = True
            self._complete = True

    def begin_projection(self) -> None:
        with self._lock:
            if (
                self.key.subphase is not RequiredWorkSubphase.PROJECTION
                or self._complete
                or self._projection_started
                or self._future is not None
                or self._submission_unknown
            ):
                raise AuditIntegrityError("Projection cannot begin with conflicting required custody")
            self._projection_started = True

    def complete_unused(
        self, metadata: PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata | RejectionProjectionUnusedMetadata
    ) -> None:
        if self._coordinator is None:
            raise AuditIntegrityError("Unused work has no registered coordinator")
        self._coordinator.complete_unused(self, metadata)

    def observe_aborted_invocation_exit(self, error: BaseException) -> None:
        self.complete_generation_joined(error)

    def complete_generation_joined(self, error: BaseException) -> None:
        with self._lock:
            if not self._submission_unknown or self._future is not None or self._complete:
                raise AuditIntegrityError("Generation completion witness conflicts with ticket")
            if all(error is not original for original in self._custody_errors):
                self._custody_errors.append(error)
            self._generation_joined = True
            self._complete = True

    def complete_owned(self, error: BaseException | None = None) -> None:
        self.complete_without_submission(error)

    def receipts(self) -> tuple[ComposerFailureReceipt, ...]:
        with self._lock:
            if not self._complete:
                raise RequiredWorkIncomplete("Required-work receipt requested before actual completion")
            receipts: list[ComposerFailureReceipt] = []
            for key, errors in ((self.key, self._errors), (self._setup_key, self._custody_errors)):
                if not errors:
                    continue
                if key is None:
                    raise AuditIntegrityError("Submission custody failure has no preallocated key")
                root = errors[0] if len(errors) == 1 else BaseExceptionGroup("Required-work original outcomes", errors)
                receipts.append(
                    ComposerFailureReceipt(
                        key,
                        root,
                        required_failure_leaves(root),
                        OwnedCompletionWitness(key),
                        self._child_outcome if key == self.key else None,
                    )
                )
            return tuple(receipts)

    def observe_custody_failure(self, error: BaseException) -> None:
        with self._lock:
            if self._setup_key is None:
                raise AuditIntegrityError("Non-SQL work cannot acquire submission custody failure authority")
            if all(error is not original for original in self._custody_errors):
                self._custody_errors.append(error)


def required_failure_leaves(root: BaseException) -> tuple[BaseException, ...]:
    from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
    from elspeth.web.required_executor import RequiredGenerationUnavailable
    from elspeth.web.sessions.composer_operations import ComposerRequiredRecoveryFailure

    if isinstance(root, BaseExceptionGroup):
        return tuple(leaf for child in root.exceptions for leaf in required_failure_leaves(child))
    if (
        isinstance(root, (asyncio.CancelledError, ComposerOwnedSettlementFailure, ComposerRequiredRecoveryFailure))
        and root.__cause__ is not None
    ):
        known = tuple(
            leaf
            for leaf in required_failure_leaves(root.__cause__)
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
            or (isinstance(leaf, OSError) and leaf.errno in (errno.EIO, errno.ENOSPC, errno.EROFS))
        )
        if known:
            return known
    return (root,)


def _category(leaf: BaseException, key: RequiredWorkKey) -> tuple[int, int]:
    from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    from elspeth.web.coordination.repository import SessionOperationConflictError
    from elspeth.web.required_executor import RequiredGenerationUnavailable
    from elspeth.web.sessions.composer_operations import ComposerOperationCancel, ComposerOperationCancelReason, ComposerOperationFenceLost
    from elspeth.web.sessions.protocol import StaleComposeStateError

    if isinstance(leaf, AuditIntegrityError):
        return (10, 0)
    if isinstance(leaf, (OperationalError, AsyncWorkerAdmissionTimeoutError, RequiredGenerationUnavailable)):
        return (20, 0)
    if isinstance(leaf, OSError) and leaf.errno in (errno.EIO, errno.ENOSPC, errno.EROFS):
        return (30, 0)
    if isinstance(leaf, SQLAlchemyError):
        return (40, 0)
    if isinstance(leaf, ComposerOwnedSettlementFailure):
        return (
            50
            if key.stage
            in (
                ComposerRequiredStage.PROVIDER_ATTEMPT_ACCOUNTING,
                ComposerRequiredStage.TOKEN_USAGE_ACCOUNTING,
                ComposerRequiredStage.TITLE_ACCOUNTING,
            )
            else 51,
            0,
        )
    if key.source is RequiredWorkSource.DURABLE_STOP_SIGNAL:
        return (60, 0)
    if key.source is RequiredWorkSource.DEADLINE_SIGNAL:
        return (70, 0)
    if isinstance(leaf, asyncio.CancelledError):
        if leaf.args and type(leaf.args[0]) is ComposerOperationCancel:
            if leaf.args[0].reason is ComposerOperationCancelReason.CANCEL_REQUESTED:
                return (60, 0)
            if leaf.args[0].reason is ComposerOperationCancelReason.DEADLINE:
                return (70, 0)
        return (80, 0)
    if isinstance(leaf, (SessionOperationFenceLost, ComposerOperationFenceLost)):
        return (80, 0)
    public = (
        SessionOperationFenceLost,
        SessionOperationConflictError,
        StaleComposeStateError,
        FingerprintKeyMissingError,
        SecretDecryptionError,
        HTTPException,
    )
    for ordinal, kind in enumerate(public, 1):
        if isinstance(leaf, kind):
            return (90, ordinal)
    return (100, 0)


@final
@dataclass(frozen=True, slots=True)
class ComposerFailureReduction:
    winner: ComposerFailureReceipt
    category_rank: int
    witnesses: tuple[BaseException, ...]
    secondary_receipts: tuple[ComposerFailureReceipt, ...]
    original_roots: tuple[BaseException, ...]

    def _project(self, *, request_id: str | None, timeout_seconds: float) -> ComposerOperationError:
        return _project_composer_failure_witnesses(
            self.category_rank, self.witnesses, request_id=request_id, timeout_seconds=timeout_seconds
        )

    def project(self, *, request_id: str | None, timeout_seconds: float) -> ComposerOperationError:
        return project_composer_failure_witnesses(
            self.winner.key, self.category_rank, self.witnesses, request_id=request_id, timeout_seconds=timeout_seconds
        )


def _project_composer_failure_witnesses(
    category_rank: int, witnesses: tuple[BaseException, ...], *, request_id: str | None, timeout_seconds: float
) -> ComposerOperationError:
    from elspeth.web.sessions.composer_operation_errors import (
        deadline_expired_error,
        project_composer_operation_error,
        request_cancelled_error,
        worker_lost_error,
    )

    if category_rank == 60:
        return request_cancelled_error(request_id=request_id)
    if category_rank == 70:
        return deadline_expired_error(request_id=request_id, timeout_seconds=timeout_seconds)
    if category_rank == 80:
        return worker_lost_error(request_id=request_id)
    if category_rank == 10:
        audits = tuple(w for w in witnesses if isinstance(w, AuditIntegrityError))
        first = audits[0].failed_turn
        if any(w.failed_turn != first for w in audits):
            error = project_composer_operation_error(AuditIntegrityError("Required audit metadata conflict"), request_id=request_id)
            return error.model_copy(
                update={
                    "body": {
                        "error_type": "audit_integrity_error",
                        "detail": error.body["detail"],
                        "request_id": request_id,
                        "diagnostic": "failed_turn_metadata_conflict",
                    }
                }
            )
    if category_rank == 90 and len(witnesses) > 1:
        errors = tuple(project_composer_operation_error(w, request_id=request_id) for w in witnesses)
        if any(error != errors[0] for error in errors[1:]):
            return project_composer_operation_error(RuntimeError("Conflicting public witnesses"), request_id=request_id)
    return project_composer_operation_error(witnesses[0], request_id=request_id)


def project_composer_failure_witnesses(
    key: RequiredWorkKey, category_rank: int, witnesses: tuple[BaseException, ...], *, request_id: str | None, timeout_seconds: float
) -> ComposerOperationError:
    error = _project_composer_failure_witnesses(category_rank, witnesses, request_id=request_id, timeout_seconds=timeout_seconds)
    if error.failure_code != "operation_failed":
        return error
    identity = ":".join(
        (
            key.session_id,
            key.session_operation_id,
            str(key.session_operation_epoch),
            key.durable_operation_id or key.authority.invocation_id or "",
            str(key.claim_attempt),
            str(key.order),
            str(category_rank),
        )
    )
    diagnostic_id = str(uuid5(NAMESPACE_URL, "elspeth-required-failure:" + identity))
    return error.model_copy(
        update={
            "diagnostic_id": diagnostic_id,
            "body": {
                "detail": {
                    "error_type": "operation_failed",
                    "detail": "The compose operation failed. See the application audit log for diagnostic detail.",
                    "request_id": request_id,
                    "diagnostic_id": diagnostic_id,
                }
            },
        }
    )


@final
@dataclass(frozen=True, slots=True)
class ChildFailureOutcome:
    parent: RequiredWorkCoordinator
    child: RequiredWorkCoordinator
    parent_ticket: RequiredWorkTicket
    original_root: BaseException
    child_receipts: tuple[ComposerFailureReceipt, ...]
    child_reduction: ComposerFailureReduction

    def validate(self, receipt: ComposerFailureReceipt) -> None:
        self.parent._validate_child_failure_outcome(self, receipt)


def _receipt_category(receipt: ComposerFailureReceipt) -> tuple[int, int]:
    outcome = receipt.child_outcome
    if outcome is not None:
        if type(outcome) is not ChildFailureOutcome:
            raise AuditIntegrityError("Child outcome requires registered nominal provenance")
        outcome.validate(receipt)
        child_winner = outcome.child_reduction.winner
        return _receipt_category(child_winner)
    return min(_category(w, receipt.key) for w in receipt.original_category_witnesses)


def reduce_composer_failures(receipts: tuple[ComposerFailureReceipt, ...]) -> ComposerFailureReduction:
    if not receipts:
        raise AuditIntegrityError("Required failure reduction requires observed evidence")
    if len({receipt.key for receipt in receipts}) != len(receipts):
        raise AuditIntegrityError("Duplicate required-work receipt key")
    authority = receipts[0].key.authority
    for receipt in receipts:
        if receipt.key.authority != authority or receipt.owned_completion_witness.key != receipt.key:
            raise AuditIntegrityError("Mixed or incomplete required-work authority")
        original_leaves = required_failure_leaves(receipt.original_root)
        if {id(leaf) for leaf in receipt.original_category_witnesses} != {id(leaf) for leaf in original_leaves}:
            raise AuditIntegrityError("Receipt category witness is not original evidence")
        if not receipt.original_category_witnesses:
            raise AuditIntegrityError("Required receipt lacks original category witnesses")
    ranked = sorted(receipts, key=lambda r: (*_receipt_category(r), *r.key.order))
    winner = ranked[0]
    category = _receipt_category(winner)
    witnesses = tuple(
        witness
        for receipt in ranked
        if _receipt_category(receipt) == category
        for witness in (
            receipt.child_outcome.child_reduction.witnesses
            if receipt.child_outcome is not None
            else tuple(w for w in receipt.original_category_witnesses if _category(w, receipt.key) == category)
        )
    )
    roots = tuple(r.original_root for r in sorted(receipts, key=lambda r: r.key.order))
    return ComposerFailureReduction(winner, category[0], witnesses, tuple(ranked[1:]), roots)


class RequiredWorkCoordinator:
    __slots__ = (
        "__weakref__",
        "_authority",
        "_child_outcomes",
        "_child_registrations",
        "_invocations",
        "_lock",
        "_manual_proposal_carrier",
        "_manual_proposal_close",
        "_proposal_children",
        "_release_prepared",
        "_tickets",
        "_unused_metadata",
    )

    def __init__(self, authority: RequiredWorkAuthority) -> None:
        if type(authority) is not RequiredWorkAuthority:
            raise AuditIntegrityError("Coordinator requires immutable owned authority")
        self._authority = authority
        self._tickets: dict[RequiredWorkKey, RequiredWorkTicket] = {}
        self._lock = RLock()
        self._release_prepared = False
        self._proposal_children: dict[str, RequiredWorkCoordinator] = {}
        self._child_registrations: dict[RequiredWorkTicket, RequiredWorkCoordinator] = {}
        self._child_outcomes: dict[RequiredWorkTicket, ChildFailureOutcome] = {}
        self._invocations: dict[tuple[RequiredWorkSource, int, int], int] = {}
        self._unused_metadata: dict[
            RequiredWorkTicket, PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata | RejectionProjectionUnusedMetadata
        ] = {}
        self._manual_proposal_close: _ClosedInvocation | None = None
        self._manual_proposal_carrier: ComposerManualProposalFailure | None = None

    @property
    def authority(self) -> RequiredWorkAuthority:
        return self._authority

    def reserve(
        self,
        source: RequiredWorkSource,
        *,
        transition_ordinal: int = 0,
        semantic_ordinal: int = 0,
        recurrence_ordinal: int = 0,
        sql_attempt_ordinal: int = 0,
        producer: bool = False,
    ) -> RequiredWorkTicket:
        key = make_required_work_key(
            self.authority,
            source,
            transition_ordinal=transition_ordinal,
            semantic_ordinal=semantic_ordinal,
            recurrence_ordinal=recurrence_ordinal,
            sql_attempt_ordinal=sql_attempt_ordinal,
            producer=producer,
        )
        with self._lock:
            if self._release_prepared or key in self._tickets:
                raise AuditIntegrityError("Required-work registration is duplicate or after release barrier")
            ticket = RequiredWorkTicket(key, coordinator=self)
            self._tickets[key] = ticket
            return ticket

    def _registered(self, ticket: RequiredWorkTicket, source: RequiredWorkSource) -> None:
        if (
            type(ticket) is not RequiredWorkTicket
            or self._tickets.get(ticket.key) is not ticket
            or ticket._coordinator is not self
            or ticket.key.authority != self.authority
            or ticket.key.source is not source
            or ticket.key.subphase is not SOURCE_MAPPING[source][1]
        ):
            raise AuditIntegrityError("Unused provenance lacks exact registered source authority")

    def _same_invocation(self, first: RequiredWorkTicket, second: RequiredWorkTicket) -> None:
        def recurrence(key: RequiredWorkKey) -> int:
            diagonal = (isqrt(8 * key.invocation_ordinal + 1) - 1) // 2
            return key.invocation_ordinal - diagonal * (diagonal + 1) // 2

        if (
            first.key.transition_ordinal != second.key.transition_ordinal
            or first.key.semantic_ordinal != second.key.semantic_ordinal
            or recurrence(first.key) != recurrence(second.key)
        ):
            raise AuditIntegrityError("Unused provenance crossed semantic invocation identities")

    def validate_pipeline_publication_work(
        self,
        *,
        publication_ticket: RequiredWorkTicket,
        projection_ticket: RequiredWorkTicket,
        revocation_ticket: RequiredWorkTicket,
        revocation_projection_ticket: RequiredWorkTicket,
    ) -> None:
        with self._lock:
            for ticket, source in (
                (publication_ticket, RequiredWorkSource.PIPELINE_PUBLICATION_SQL),
                (projection_ticket, RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION),
                (revocation_ticket, RequiredWorkSource.TRUST_REVOCATION_SQL),
                (revocation_projection_ticket, RequiredWorkSource.TRUST_REVOCATION_PROJECTION),
            ):
                self._registered(ticket, source)
                self._same_invocation(publication_ticket, ticket)
                with ticket._lock:
                    if ticket._complete or ticket._future is not None or ticket._submission_unknown or ticket._projection_started:
                        raise AuditIntegrityError("Publication entry cannot reuse launched required work")

    def _verify_sql_outcome[T](self, ticket: RequiredWorkTicket, outcome: RequiredSQLFinishOnce[T]) -> bool:
        with ticket._lock:
            if not ticket._complete or (ticket._submission_unknown and not ticket._generation_joined):
                raise RequiredWorkIncomplete("Unused provenance cannot outrun actual SQL custody")
            if type(outcome) is RequiredSQLRaised:
                if not any(outcome.error is original for original in (*ticket._errors, *ticket._custody_errors)):
                    raise AuditIntegrityError("Unused provenance replaced its original SQL failure")
                if ticket._future is None and not (ticket._no_submission or ticket._generation_joined):
                    raise RequiredWorkIncomplete("No-Future SQL failure lacks actual no-submission proof")
                return ticket._no_submission
            if type(outcome) is not RequiredSQLReturned or ticket._future is None or not ticket._future.done():
                raise AuditIntegrityError("Unused provenance lacks actual returned SQL value")
            if ticket._errors or outcome.value is not ticket._result:
                raise AuditIntegrityError("Unused provenance replaced its actual SQL result")
            return False

    def validate_terminal_failure_work(self, *, projection_ticket: RequiredWorkTicket) -> None:
        """Refuse a foreign, launched or reused failure-publication handoff."""
        with self._lock:
            if self._release_prepared:
                raise AuditIntegrityError("Failure projection cannot enter after release barrier")
            self._registered(projection_ticket, RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
            if (
                projection_ticket.key.transition_ordinal != 0
                or projection_ticket.key.semantic_ordinal != 0
                or projection_ticket.key.sql_attempt_ordinal != 0
            ):
                raise AuditIntegrityError("Failure projection crossed its terminal invocation")
            expected = make_required_work_key(self.authority, RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
            if projection_ticket.key != expected:
                raise AuditIntegrityError("Failure projection crossed its terminal recurrence")
            with projection_ticket._lock:
                if (
                    projection_ticket._complete
                    or projection_ticket._future is not None
                    or projection_ticket._submission_unknown
                    or projection_ticket._projection_started
                ):
                    raise AuditIntegrityError("Failure publication cannot reuse launched projection work")

    def validate_joined_lifecycle_sql_outcome[T](
        self,
        *,
        ticket: RequiredWorkTicket,
        expected_source: RequiredWorkSource,
        actual_outcome: RequiredSQLFinishOnce[T],
    ) -> None:
        """Verify an exact registered renewal/readback against its physical outcome."""
        if type(expected_source) is not RequiredWorkSource or expected_source not in (
            RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
            RequiredWorkSource.LEASE_RENEWAL,
        ):
            raise AuditIntegrityError("Lifecycle SQL outcome has an unsupported source")
        with self._lock:
            self._registered(ticket, expected_source)
            self._verify_sql_outcome(ticket, actual_outcome)

    def validate_recovery_terminal_sql_outcome[T](self, *, ticket: RequiredWorkTicket, actual_outcome: RequiredSQLFinishOnce[T]) -> None:
        """Verify the fresh-fence recovery writer's exact physical SQL carrier."""
        with self._lock:
            self._registered(ticket, RequiredWorkSource.TERMINAL_FAILURE_SQL)
            self._verify_sql_outcome(ticket, actual_outcome)

    def _publication_branch(
        self, publication_ticket: RequiredWorkTicket, outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult]
    ) -> PublicationProjectionDisposition | None:
        self._registered(publication_ticket, RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
        no_submission = self._verify_sql_outcome(publication_ticket, outcome)
        if type(outcome) is RequiredSQLRaised:
            return (
                PublicationProjectionDisposition.PREFLIGHT_REFUSED if no_submission else PublicationProjectionDisposition.PUBLICATION_FAILED
            )
        if not isinstance(outcome, RequiredSQLReturned):
            raise AuditIntegrityError("Publication SQL lacks a nominal returned branch")
        if type(outcome.value) is _ComposerRevocationRequired:
            return PublicationProjectionDisposition.ELIGIBILITY_SELECTED
        if type(outcome.value) is PipelineProposalSettlementResult:
            return None
        raise AuditIntegrityError("Publication SQL returned an unmapped nominal branch")

    def validate_proposal_rejection_work(
        self,
        *,
        rejection_ticket: RequiredWorkTicket,
        projection_ticket: RequiredWorkTicket,
        transition_ordinal: int,
        semantic_ordinal: int,
    ) -> None:
        with self._lock:
            if any(type(value) is not int or value < 0 for value in (transition_ordinal, semantic_ordinal)):
                raise AuditIntegrityError("Rejection requires explicit owned integer ordinals")
            if self._release_prepared:
                raise AuditIntegrityError("Rejection cannot begin after release preparation")
            for ticket, source in (
                (rejection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_SQL),
                (projection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION),
            ):
                self._registered(ticket, source)
                self._same_invocation(rejection_ticket, ticket)
                with ticket._lock:
                    if (
                        ticket.key.transition_ordinal != transition_ordinal
                        or ticket.key.semantic_ordinal != semantic_ordinal
                        or ticket.key.sql_attempt_ordinal != 0
                        or ticket._complete
                        or ticket._future is not None
                        or ticket._submission_unknown
                        or ticket._projection_started
                    ):
                        raise AuditIntegrityError("Rejection cannot reuse foreign/launched invocation custody")

    def validate_creation_work(
        self,
        *,
        creation_ticket: RequiredWorkTicket,
        projection_ticket: RequiredWorkTicket,
        transition_ordinal: int,
        semantic_ordinal: int,
    ) -> None:
        with self._lock:
            if self._release_prepared or any(type(value) is not int or value < 0 for value in (transition_ordinal, semantic_ordinal)):
                raise AuditIntegrityError("Creation cannot begin without explicit pre-release ordinals")
            for ticket, source in (
                (creation_ticket, RequiredWorkSource.PROPOSAL_CREATION_SQL),
                (projection_ticket, RequiredWorkSource.PROPOSAL_CREATION_PROJECTION),
            ):
                self._registered(ticket, source)
                self._same_invocation(creation_ticket, ticket)
                with ticket._lock:
                    if (
                        ticket.key.transition_ordinal != transition_ordinal
                        or ticket.key.semantic_ordinal != semantic_ordinal
                        or ticket._complete
                        or ticket._future is not None
                        or ticket._submission_unknown
                        or ticket._projection_started
                    ):
                        raise AuditIntegrityError("Creation pair is foreign or already launched")

    def verify_creation_outcome(
        self,
        *,
        creation_ticket: RequiredWorkTicket,
        actual_outcome: RequiredSQLFinishOnce[CompositionProposalRecord],
    ) -> bool:
        with self._lock:
            self._registered(creation_ticket, RequiredWorkSource.PROPOSAL_CREATION_SQL)
            if creation_ticket._finish_once_handoff is not actual_outcome:
                raise AuditIntegrityError("Creation projection replaced actual finish-once carrier")
            return self._verify_sql_outcome(creation_ticket, actual_outcome)

    def verify_rejection_returned(
        self,
        *,
        rejection_ticket: RequiredWorkTicket,
        actual_outcome: RequiredSQLReturned[PipelineRejectionSQLResult],
    ) -> None:
        with self._lock:
            self._registered(rejection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_SQL)
            if (
                type(actual_outcome) is not RequiredSQLReturned
                or rejection_ticket._finish_once_handoff is not actual_outcome
                or type(actual_outcome.value) is not PipelineRejectionSQLResult
            ):
                raise AuditIntegrityError("Rejection projection replaced actual finish-once carrier/value")
            self._verify_sql_outcome(rejection_ticket, actual_outcome)

    def issue_rejection_projection_unused(
        self,
        *,
        rejection_ticket: RequiredWorkTicket,
        projection_ticket: RequiredWorkTicket,
        actual_outcome: RequiredSQLFinishOnce[PipelineRejectionSQLResult],
    ) -> RejectionProjectionUnusedMetadata | None:
        with self._lock:
            self._registered(rejection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_SQL)
            self._registered(projection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION)
            self._same_invocation(rejection_ticket, projection_ticket)
            if projection_ticket in self._unused_metadata:
                raise AuditIntegrityError("Unused rejection projection was already issued")
            if rejection_ticket._finish_once_handoff is not actual_outcome:
                raise AuditIntegrityError("Unused rejection projection replaced actual finish-once carrier")
            if type(actual_outcome) is RequiredSQLReturned:
                self.verify_rejection_returned(rejection_ticket=rejection_ticket, actual_outcome=actual_outcome)
                return None
            no_submission = self._verify_sql_outcome(rejection_ticket, actual_outcome)
            disposition = (
                RejectionProjectionUnusedDisposition.PREFLIGHT_REFUSED
                if no_submission
                else RejectionProjectionUnusedDisposition.SQL_FAILED_NO_VALUE
            )
            metadata = RejectionProjectionUnusedMetadata(
                projection_ticket.key, disposition, rejection_ticket, cast(RequiredSQLRaised, actual_outcome)
            )
            self._unused_metadata[projection_ticket] = metadata
            return metadata

    def issue_publication_projection_unused(
        self,
        *,
        publication_ticket: RequiredWorkTicket,
        projection_ticket: RequiredWorkTicket,
        actual_outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult],
    ) -> PublicationProjectionUnusedMetadata | None:
        with self._lock:
            self._registered(projection_ticket, RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION)
            if projection_ticket in self._unused_metadata:
                raise AuditIntegrityError("Unused publication metadata was already issued")
            disposition = self._publication_branch(publication_ticket, actual_outcome)
            self._same_invocation(publication_ticket, projection_ticket)
            if disposition is None:
                return None
            metadata = PublicationProjectionUnusedMetadata(projection_ticket.key, disposition, publication_ticket, actual_outcome)
            self._unused_metadata[projection_ticket] = metadata
            return metadata

    def issue_revocation_work_unused(
        self,
        *,
        target_ticket: RequiredWorkTicket,
        publication_ticket: RequiredWorkTicket,
        publication_outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult],
        revocation_ticket: RequiredWorkTicket | None = None,
        revocation_outcome: RequiredSQLFinishOnce[ComposerRevocationSQLResult] | None = None,
    ) -> RevocationWorkUnusedMetadata:
        with self._lock:
            if type(target_ticket) is not RequiredWorkTicket:
                raise AuditIntegrityError("Unused revocation target is not nominal owned work")
            if target_ticket.key.source not in (RequiredWorkSource.TRUST_REVOCATION_SQL, RequiredWorkSource.TRUST_REVOCATION_PROJECTION):
                raise AuditIntegrityError("Unused revocation metadata has a foreign source")
            self._registered(target_ticket, target_ticket.key.source)
            if target_ticket in self._unused_metadata:
                raise AuditIntegrityError("Unused revocation metadata was already issued")
            branch = self._publication_branch(publication_ticket, publication_outcome)
            self._same_invocation(publication_ticket, target_ticket)
            if branch is PublicationProjectionDisposition.ELIGIBILITY_SELECTED:
                if target_ticket.key.source is not RequiredWorkSource.TRUST_REVOCATION_PROJECTION or revocation_ticket is None:
                    raise AuditIntegrityError("Eligibility cannot authorize unused revocation SQL")
                self._registered(revocation_ticket, RequiredWorkSource.TRUST_REVOCATION_SQL)
                self._same_invocation(publication_ticket, revocation_ticket)
                if type(revocation_outcome) is not RequiredSQLRaised:
                    raise AuditIntegrityError("Successful or unknown revocation must own its projection")
                no_submission = self._verify_sql_outcome(revocation_ticket, revocation_outcome)
                disposition = (
                    RevocationWorkUnusedDisposition.PREFLIGHT_REFUSED
                    if no_submission
                    else RevocationWorkUnusedDisposition.SQL_FAILED_NO_VALUE
                )
            else:
                if revocation_ticket is not None or revocation_outcome is not None:
                    raise AuditIntegrityError("Unused revocation cannot invent a secondary SQL outcome")
                disposition = (
                    RevocationWorkUnusedDisposition.BUSINESS_SELECTED
                    if branch is None
                    else RevocationWorkUnusedDisposition.PREFLIGHT_REFUSED
                    if branch is PublicationProjectionDisposition.PREFLIGHT_REFUSED
                    else RevocationWorkUnusedDisposition.PUBLICATION_FAILED
                )
            metadata = RevocationWorkUnusedMetadata(
                target_ticket.key, disposition, publication_ticket, publication_outcome, revocation_ticket, revocation_outcome
            )
            self._unused_metadata[target_ticket] = metadata
            return metadata

    def complete_unused(
        self,
        ticket: RequiredWorkTicket,
        metadata: PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata | RejectionProjectionUnusedMetadata,
    ) -> None:
        with self._lock, ticket._lock:
            if self._unused_metadata.get(ticket) is not metadata:
                raise AuditIntegrityError("Unused metadata was not issued for this exact ticket")
            if type(metadata) is PublicationProjectionUnusedMetadata:
                self._registered(ticket, RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION)
                if metadata.projection_key23 != ticket.key or type(metadata.disposition) is not PublicationProjectionDisposition:
                    raise AuditIntegrityError("Unused publication metadata has a foreign namespace or key")
                if self._publication_branch(metadata.publication_ticket, metadata.actual_outcome) is not metadata.disposition:
                    raise AuditIntegrityError("Unused publication branch changed")
            elif type(metadata) is RevocationWorkUnusedMetadata:
                self._registered(ticket, ticket.key.source)
                if (
                    ticket.key.source not in (RequiredWorkSource.TRUST_REVOCATION_SQL, RequiredWorkSource.TRUST_REVOCATION_PROJECTION)
                    or metadata.target_key != ticket.key
                    or type(metadata.disposition) is not RevocationWorkUnusedDisposition
                ):
                    raise AuditIntegrityError("Unused revocation metadata has a foreign namespace or key")
                self._publication_branch(metadata.publication_ticket, metadata.publication_outcome)
                if metadata.revocation_ticket is not None:
                    if metadata.revocation_outcome is None:
                        raise AuditIntegrityError("Unused revocation lost its physical failure provenance")
                    self._verify_sql_outcome(metadata.revocation_ticket, metadata.revocation_outcome)
            elif type(metadata) is RejectionProjectionUnusedMetadata:
                self._registered(ticket, RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION)
                self._registered(metadata.rejection_ticket, RequiredWorkSource.PROPOSAL_REJECTION_SQL)
                self._same_invocation(metadata.rejection_ticket, ticket)
                if (
                    metadata.projection_key52 != ticket.key
                    or type(metadata.disposition) is not RejectionProjectionUnusedDisposition
                    or metadata.rejection_ticket._finish_once_handoff is not metadata.actual_outcome
                    or type(metadata.actual_outcome) is not RequiredSQLRaised
                ):
                    raise AuditIntegrityError("Unused rejection projection has foreign carrier/namespace/key")
                no_submission = self._verify_sql_outcome(metadata.rejection_ticket, metadata.actual_outcome)
                expected_disposition = (
                    RejectionProjectionUnusedDisposition.PREFLIGHT_REFUSED
                    if no_submission
                    else RejectionProjectionUnusedDisposition.SQL_FAILED_NO_VALUE
                )
                if metadata.disposition is not expected_disposition:
                    raise AuditIntegrityError("Unused rejection projection changed its actual branch")
            else:
                raise AuditIntegrityError("Unused work requires issued nominal metadata")
            if ticket._future is not None or ticket._submission_unknown or ticket._complete or ticket._projection_started:
                raise AuditIntegrityError("Unused completion conflicts with physical or projection custody")
            ticket._unused_metadata = metadata
            ticket._no_submission = True
            ticket._complete = True

    def reserve_pair(
        self, source: RequiredWorkSource, projection: RequiredWorkSource, *, transition_ordinal: int, semantic_ordinal: int
    ) -> tuple[RequiredWorkTicket, RequiredWorkTicket]:
        with self._lock:
            identity = (source, transition_ordinal, semantic_ordinal)
            ordinal = self._invocations.get(identity, 0)
            sql = self.reserve(source, transition_ordinal=transition_ordinal, semantic_ordinal=semantic_ordinal, recurrence_ordinal=ordinal)
            projected = self.reserve(
                projection, transition_ordinal=transition_ordinal, semantic_ordinal=semantic_ordinal, recurrence_ordinal=ordinal
            )
            self._invocations[identity] = ordinal + 1
            return sql, projected

    def reserve_audit_work(
        self,
        source: RequiredWorkSource,
        projection: RequiredWorkSource,
        *,
        transition_ordinal: int,
        semantic_ordinal: int,
    ) -> tuple[RequiredWorkTicket, RequiredWorkTicket, RequiredWorkTicket]:
        if (source, projection) not in (
            (RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL, RequiredWorkSource.REQUIRED_UNWIND_AUDIT_PROJECTION),
            (RequiredWorkSource.DISPATCH_AUDIT_SQL, RequiredWorkSource.DISPATCH_AUDIT_PROJECTION),
        ):
            raise AuditIntegrityError("Audit preparation requires its closed registered SQL/projection pair")
        with self._lock:
            identity = (source, transition_ordinal, semantic_ordinal)
            ordinal = self._invocations.get(identity, 0)
            producer = self.reserve(
                source,
                transition_ordinal=transition_ordinal,
                semantic_ordinal=semantic_ordinal,
                recurrence_ordinal=ordinal,
                producer=True,
            )
            sql = self.reserve(
                source,
                transition_ordinal=transition_ordinal,
                semantic_ordinal=semantic_ordinal,
                recurrence_ordinal=ordinal,
            )
            projected = self.reserve(
                projection,
                transition_ordinal=transition_ordinal,
                semantic_ordinal=semantic_ordinal,
                recurrence_ordinal=ordinal,
            )
            self._invocations[identity] = ordinal + 1
            return producer, sql, projected

    def for_proposal(self, proposal_id: str, tool_call_id: str) -> RequiredWorkCoordinator:
        """Retain a narrower immutable proposal scope under the turn barrier."""
        authority = replace(self.authority, proposal_id=proposal_id, tool_call_id=tool_call_id)
        with self._lock:
            if self._release_prepared:
                raise AuditIntegrityError("Proposal custody cannot begin after release")
            existing = self._proposal_children.get(proposal_id)
            if existing is not None:
                if existing.authority != authority:
                    raise AuditIntegrityError("Proposal custody identity changed")
                return existing
            child = RequiredWorkCoordinator(authority)
            self._proposal_children[proposal_id] = child
            return child

    def begin_proposal_child(
        self, proposal_id: str, tool_call_id: str, *, transition_ordinal: int, semantic_ordinal: int
    ) -> tuple[RequiredWorkCoordinator, RequiredWorkTicket]:
        with self._lock:
            child = self.for_proposal(proposal_id, tool_call_id)
            if child in self._child_registrations.values():
                raise AuditIntegrityError("Proposal child producer is already registered")
            identity = (RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER, transition_ordinal, semantic_ordinal)
            recurrence = self._invocations.get(identity, 0)
            ticket = self.reserve(
                RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER,
                transition_ordinal=transition_ordinal,
                semantic_ordinal=semantic_ordinal,
                recurrence_ordinal=recurrence,
            )
            self._invocations[identity] = recurrence + 1
            self._child_registrations[ticket] = child
            return child, ticket

    def complete_proposal_child(
        self, child: RequiredWorkCoordinator, ticket: RequiredWorkTicket, original_root: BaseException | None = None
    ) -> None:
        with self._lock:
            if self._child_registrations.get(ticket) is not child or self._tickets.get(ticket.key) is not ticket:
                raise AuditIntegrityError("Proposal child completion lacks exact retained registration")
            child.assert_completed()
            receipts = child.failure_receipts()
            if receipts and original_root is None:
                raise AuditIntegrityError("Failed child cannot acquire a successful parent outcome")
            if receipts:
                if original_root is None:
                    raise AuditIntegrityError("Failed child lacks retained parent root")
                reduction = reduce_composer_failures(receipts)
                original_leaves = required_failure_leaves(original_root)
                if any(all(w is not leaf for leaf in original_leaves) for receipt in receipts for w in receipt.original_category_witnesses):
                    raise AuditIntegrityError("Parent root dropped original child failure evidence")
                outcome = ChildFailureOutcome(self, child, ticket, original_root, receipts, reduction)
                self._child_outcomes[ticket] = outcome
                ticket._child_outcome = outcome
            ticket.complete_owned(original_root)

    def _validate_child_failure_outcome(self, outcome: ChildFailureOutcome, receipt: ComposerFailureReceipt) -> None:
        with self._lock:
            ticket = outcome.parent_ticket
            child = outcome.child
            proposal_id = child.authority.proposal_id
            if proposal_id is None:
                raise AuditIntegrityError("Proposal child outcome lacks exact proposal identity")
            if (
                self._child_outcomes.get(ticket) is not outcome
                or self._child_registrations.get(ticket) is not child
                or self._tickets.get(ticket.key) is not ticket
                or receipt.key != ticket.key
                or receipt.original_root is not outcome.original_root
                or not ticket.complete
                or not child.all_completed
                or self._proposal_children.get(proposal_id) is not child
                or replace(child.authority, proposal_id=self.authority.proposal_id, tool_call_id=self.authority.tool_call_id)
                != self.authority
            ):
                raise AuditIntegrityError("Child outcome provenance is foreign, incomplete, or replaced")
            if outcome.child_reduction != reduce_composer_failures(outcome.child_receipts):
                raise AuditIntegrityError("Child outcome reduction changed")

    @property
    def tickets(self) -> tuple[RequiredWorkTicket, ...]:
        with self._lock:
            return tuple(sorted(self._tickets.values(), key=lambda ticket: ticket.key.order))

    @property
    def all_completed(self) -> bool:
        with self._lock:
            children = tuple(self._proposal_children.values())
        return all(ticket.complete for ticket in self.tickets) and all(child.all_completed for child in children)

    def assert_completed(self) -> None:
        if not self.all_completed:
            raise RequiredWorkIncomplete("Required work still owns physical/producer custody")

    def failure_receipts(self) -> tuple[ComposerFailureReceipt, ...]:
        self.assert_completed()
        return tuple(receipt for ticket in self.tickets for receipt in ticket.receipts())

    def settlement_failure_receipts(self) -> tuple[ComposerFailureReceipt, ...]:
        """Snapshot complete body work while exact healthy renewal remains owned."""
        with self._lock:
            children = tuple(self._proposal_children.values())
            tickets = self.tickets
        for child in children:
            child.assert_completed()
        receipts: list[ComposerFailureReceipt] = []
        for ticket in tickets:
            if not ticket.complete:
                if ticket.pending_healthy_renewal:
                    continue
                raise RequiredWorkIncomplete("Settlement cannot outrun required work")
            receipts.extend(ticket.receipts())
        return tuple(receipts)

    def recovery_failure_receipts(self, *, projection_ticket: RequiredWorkTicket) -> tuple[ComposerFailureReceipt, ...]:
        """Select before the first recovery write while its terminal projection stays pending."""
        with self._lock:
            self._registered(projection_ticket, RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
            with projection_ticket._lock:
                if projection_ticket._complete or not projection_ticket._projection_started:
                    raise AuditIntegrityError("Recovery selection requires its unresolved owned terminal projection")
            children = tuple(self._proposal_children.values())
            tickets = self.tickets
        for child in children:
            child.assert_completed()
        receipts: list[ComposerFailureReceipt] = []
        for ticket in tickets:
            if ticket is projection_ticket:
                continue
            if not ticket.complete:
                if ticket.pending_healthy_renewal:
                    continue
                raise RequiredWorkIncomplete("Recovery selection cannot outrun required work")
            receipts.extend(ticket.receipts())
        return tuple(receipts)

    def prepare_lease_release(self) -> RequiredWorkTicket:
        with self._lock:
            self.assert_completed()
            ticket = self.reserve(RequiredWorkSource.LEASE_RELEASE)
            self._release_prepared = True
            return ticket


class RequiredWorkRole(Enum):
    TURN = "turn"
    TITLE = "title"


@final
@dataclass(frozen=True, slots=True)
class RequiredWorkBinding:
    coordinator: RequiredWorkCoordinator
    transition_ordinal: int
    semantic_ordinal: int
    role: RequiredWorkRole
    running: ComposerOperationRunning | None = None

    def __post_init__(self) -> None:
        if type(self.coordinator) is not RequiredWorkCoordinator or type(self.role) is not RequiredWorkRole:
            raise AuditIntegrityError("Required-work binding needs its registered nominal owner")
        if any(type(value) is not int or value < 0 for value in (self.transition_ordinal, self.semantic_ordinal)):
            raise AuditIntegrityError("Required-work binding ordinals are not exact owned integers")

    def reserve_pair(self, source: RequiredWorkSource, projection: RequiredWorkSource) -> tuple[RequiredWorkTicket, RequiredWorkTicket]:
        return self.coordinator.reserve_pair(
            source, projection, transition_ordinal=self.transition_ordinal, semantic_ordinal=self.semantic_ordinal
        )

    def validate_context(self, context: SessionOperationContext | None) -> None:
        if type(context) is not SessionOperationContext or self.coordinator.authority.context != context:
            raise AuditIntegrityError("Required-work binding belongs to another exact fence")
