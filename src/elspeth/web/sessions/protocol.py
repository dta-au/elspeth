"""SessionService protocol and record dataclasses.

Record types are frozen dataclasses representing database rows.
CompositionStateData is the input DTO for saving new state versions.
"""

# ID Convention: Record dataclasses use UUID for type safety. The database
# stores IDs as String (TEXT). SessionServiceImpl converts between UUID
# and str at the query/record boundary. Callers work with UUID
# exclusively; the storage representation is an implementation detail.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, final, get_args, runtime_checkable
from uuid import UUID

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import (
    BlobAtomicDeletionObligation,
    BlobCreationObligation,
    BlobDeletionPlan,
    BlobForkPlanEntry,
    BlobRecord,
    BlobReplacementPlan,
    BlobRunLinkDirection,
    BlobRunLinkRecord,
)
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    ChargeableOperation,
)
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
    InterpretationSurfaceOrigin,
    validate_surface_provenance,
)
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields, require_int
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex
from elspeth.web.coordination.approval_authority import ApprovalGateInputs
from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    ArchiveManifestRelation,
    CancellationSource,
    CompatibilityKey,
    InstanceState,
    RecoveryRequiredReason,
    RunOwnershipFence,
    RunSagaState,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationKind,
    StartPermitState,
)
from elspeth.web.sessions.pipeline_publication import PipelineProposalSettlementResult as PipelineProposalSettlementResult
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerOperationBinding

if TYPE_CHECKING:
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import PipelineProposal
    from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageEntry, TokenUsageSource
    from elspeth.web.execution.envelope import RunExecutionInput
    from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkTicket
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
    from elspeth.web.sessions.composer_operations import (
        ComposerOperationAssistantWrite,
        ComposerOperationClaim,
        ComposerOperationError,
        ComposerOperationRecord,
        ComposerOperationRunning,
    )
    from elspeth.web.sessions.pipeline_finish_once import ComposerPipelineFinishOnce
    from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
    from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineCreationFinishOnce, PipelineRejectionFinishOnce
    from elspeth.web.sessions.schemas import MessageWithStateResponse

ChatMessageRole = Literal["user", "assistant", "system", "tool", "audit"]


@dataclass(slots=True)
class RedactedPipelineArguments:
    """Carry the manifest-redacted JSON object into proposal persistence.

    The service independently recomputes the redaction from the owned plan and
    compares it before writing. Keep the supplied object here without copying
    so that comparison checks exactly what the caller supplied.
    """

    value: object

    def __post_init__(self) -> None:
        if type(self.value) not in (dict, MappingProxyType):
            raise TypeError("redacted pipeline arguments must be a JSON object or its frozen view")
        canonical_json(self.value)


ComposerTrustMode = Literal["explicit_approve", "auto_commit"]
ComposerDensityDefault = Literal["high", "medium", "low"]
ProposalLifecycleStatus = Literal["pending", "committed", "rejected"]
PipelineProposalRejectionReason = Literal[
    "operator_rejected",
    "candidate_executor_mismatch",
    "validation_failed",
    "policy_changed",
    "base_conflict",
    "request_cancelled",
    "superseded",
]
ProposalEventType = Literal[
    "proposal.created",
    "proposal.accepted",
    "proposal.rejected",
    "trust_mode.changed",
    # Non-terminal: the proposal stays pending. Records an auto-commit
    # blocked by the settlement-boundary trust-mode recheck
    # (elspeth-01d4c6e683) — the dispatch tool row persists BEFORE the
    # settlement transaction, so without this event a mid-turn trust-mode
    # downgrade left the trail showing a successful dispatch against a
    # still-pending proposal with nothing recording the block.
    "auto_commit.revoked",
]
# ``audit`` is an internal-only role for breadcrumb rows that have no real
# OpenAI tool-response or assistant parent (LLM-call audit envelopes,
# pre-flight redaction failures, etc.). They MUST be filtered out of any
# user-facing chat response and any composer prompt-history rebuild —
# enforced at ``_is_composer_audit_tool_message`` /
# ``_composer_conversation_messages`` and the public messages route.
# Phase 2.2 (elspeth-0de989c56d): four-value terminal taxonomy.
# `completed_with_failures` and `empty` join the previous three so an
# operator scanning `/api/runs/{rid}` can distinguish "ran cleanly" from
# "ran but no row succeeded" without opening output files.  Mirrors the
# L0 RunStatus enum widening; row-count biconditional enforced in
# RunRecord.__post_init__.
SessionRunStatus = Literal["pending", "running", "completed", "completed_with_failures", "failed", "empty", "cancelled"]
TerminalSessionRunStatus = Literal["completed", "completed_with_failures", "failed", "empty", "cancelled"]
OperatorCompletionSessionRunStatus = Literal["completed", "completed_with_failures", "empty"]
SessionRunEventType = Literal["progress", "error", "completed", "cancelled", "failed"]

LANDSCAPE_RECONCILIATION_PENDING_SUFFIX = "[landscape-reconciliation:pending]"
LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX = "[landscape-reconciliation:complete]"
LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX = "[landscape-reconciliation:absent]"

# Closed enum mirroring the ``ck_chat_messages_writer_principal`` CHECK
# constraint in ``web/sessions/models.py``. The Python Literal and the SQL
# CHECK are paired contracts: extending one without the other lets the
# dataclass validator pass while the DB rejects the row (or vice versa).
# The order here mirrors the CHECK declaration in ``models.py`` for visual
# diff clarity. Adding a value is a governance action — see the
# closed-list-of-permitted-writers comment block at the
# ``audit_access_log_table`` definition for the same posture.
ChatMessageWriterPrincipal = Literal[
    "compose_loop",
    "route_user_message",
    "route_system_message",
    "admin_tool",
    "session_fork",
    # LLM audit rows persisted by POST /api/runs/{run_id}/diagnostics/
    # evaluate. A distinct principal (elspeth-0fcf68d50f): these writes
    # originate outside any compose turn, so attributing them to
    # ``compose_loop`` misrepresented the audit trail's writer custody.
    "run_diagnostics",
]

# Closed enum mirroring the ``ck_composition_states_provenance`` CHECK
# constraint in ``web/sessions/models.py``. Same paired-contract posture as
# ``ChatMessageWriterPrincipal``: extending one without the other lets the
# Python writer pass while the DB rejects the row (or vice versa). Order
# mirrors the CHECK declaration (models.py L257) for visual diff clarity.
# Adding a value is a governance action — see the dormant-value friction
# block at the ``composition_states_table`` definition for the activation
# contract (spec amendment + integration test + GitHub issue).
CompositionStateProvenance = Literal[
    "tool_call",
    "convergence_persist",
    "plugin_crash_persist",
    "preflight_persist",
    # DORMANT (no live writer). Formerly written by the first-run tutorial's
    # pre-execution template normalizer, removed for tutorial-vs-regular
    # backend parity (the composer already emits ``row.field`` templates).
    # Retained in the closed list + CHECK constraint so historical audit rows
    # written under the old behavior remain representable; re-activating it is
    # a governance action (see the NO SILENT EXTENSION block in ``models.py``).
    "tutorial_normalization",
    "post_compose",
    "session_seed",
    "session_fork",
    "interpretation_resolve",
]

AuditAccessWriterPrincipal = Literal["audit_grade_view", "admin_tool", "workflow_inspect"]
AUDIT_GRADE_VIEW_WRITER_PRINCIPAL: Literal["audit_grade_view"] = "audit_grade_view"
WORKFLOW_INSPECT_WRITER_PRINCIPAL: Literal["workflow_inspect"] = "workflow_inspect"
WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE = "/api/workflow/inspect/{session_id}/{state_id}"
AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST: frozenset[str] = frozenset(
    {
        "include_tool_rows",
        "include_llm_audit",
        "include_raw_content",
        "include_rejection_reasons",
        "limit",
        "offset",
    }
)

CHAT_MESSAGE_ROLE_VALUES: frozenset[str] = frozenset(get_args(ChatMessageRole))
COMPOSER_TRUST_MODE_VALUES: frozenset[str] = frozenset(get_args(ComposerTrustMode))
COMPOSER_DENSITY_DEFAULT_VALUES: frozenset[str] = frozenset(get_args(ComposerDensityDefault))
PROPOSAL_LIFECYCLE_STATUS_VALUES: frozenset[str] = frozenset(get_args(ProposalLifecycleStatus))
PROPOSAL_EVENT_TYPE_VALUES: frozenset[str] = frozenset(get_args(ProposalEventType))
CHAT_MESSAGE_WRITER_PRINCIPAL_VALUES: frozenset[str] = frozenset(get_args(ChatMessageWriterPrincipal))
COMPOSITION_STATE_PROVENANCE_VALUES: frozenset[str] = frozenset(get_args(CompositionStateProvenance))
SESSION_RUN_STATUS_VALUES: frozenset[str] = frozenset(get_args(SessionRunStatus))
SESSION_TERMINAL_RUN_STATUS_VALUES: frozenset[str] = frozenset(get_args(TerminalSessionRunStatus))
OPERATOR_COMPLETION_RUN_STATUS_VALUES: frozenset[str] = frozenset(get_args(OperatorCompletionSessionRunStatus))
SESSION_RUN_EVENT_TYPE_VALUES: frozenset[str] = frozenset(get_args(SessionRunEventType))
_RUN_COUNTER_FIELDS: tuple[str, ...] = (
    "rows_processed",
    "rows_succeeded",
    "rows_failed",
    "rows_routed_success",
    "rows_routed_failure",
    "rows_quarantined",
)


@final
@dataclass(frozen=True, slots=True)
class WebInstanceRecord:
    """Persistent membership projection; database time owns lease validity."""

    instance_id: str
    deployment_target: str
    deployment_generation: str
    compatibility_key: CompatibilityKey
    image_digest: str
    revision_label: str
    state: InstanceState
    started_at: datetime
    last_heartbeat_at: datetime
    lease_expires_at: datetime


@final
@dataclass(frozen=True, slots=True)
class SessionOperationFenceRecord:
    """Persistent operation authority, including retained release evidence."""

    fence: SessionOperationFence
    operation_kind: SessionOperationKind
    owner_instance_id: str
    lease_expires_at: datetime
    released_at: datetime | None


@final
@dataclass(frozen=True, slots=True)
class RunCoordinationRecord:
    """Sessions-side run ownership and monotonic saga projection."""

    ownership: RunOwnershipFence | None
    owner_lease_expires_at: datetime | None
    saga_state: RunSagaState
    cancel_requested_at: datetime | None
    cancellation_source: CancellationSource | None
    recovery_required_reason: RecoveryRequiredReason | None


@final
@dataclass(frozen=True, slots=True)
class RunStartPermitRecord:
    """Durable start-versus-cancel decision and immutable permit subject."""

    run_id: str
    state: StartPermitState
    permit_id: str | None
    permit_epoch: int | None
    subject_hash: str | None
    issued_at: datetime | None
    cancelled_at: datetime | None
    admission_decision: ChargeableAdmissionDecision | None = None
    execution_refusal: ChargeableAdmissionDecision | None = None


OperationReceiptKind = Literal["session_fork", "state_revert"]
OperationReceiptFailureCode = Literal[
    "stale_conflict", "integrity_error", "custody_error", "quota_exceeded", "operation_failed", "request_cancelled"
]


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptFence:
    """Exact durable request lease for an ordinary session mutation."""

    session_id: UUID
    operation_id: str
    lease_token: str
    attempt: int

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("OperationReceiptFence.session_id must be an exact UUID")
        if type(self.operation_id) is not str or not 1 <= len(self.operation_id) <= 128:
            raise TypeError("OperationReceiptFence.operation_id must be a bounded non-empty string")
        if type(self.lease_token) is not str or not 1 <= len(self.lease_token) <= 256:
            raise TypeError("OperationReceiptFence.lease_token must be a bounded non-empty string")
        if type(self.attempt) is not int or self.attempt < 1:
            raise TypeError("OperationReceiptFence.attempt must be a positive exact integer")


@final
@dataclass(frozen=True, slots=True)
class StateRevertReceiptResult:
    state_id: UUID


@final
@dataclass(frozen=True, slots=True)
class SessionForkReceiptResult:
    session_id: UUID


type OperationReceiptResult = StateRevertReceiptResult | SessionForkReceiptResult


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptClaimed:
    fence: OperationReceiptFence
    lease_expires_at: datetime


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptTakenOver:
    fence: OperationReceiptFence
    prior_attempt: int
    lease_expires_at: datetime


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptActive:
    attempt: int
    lease_expires_at: datetime
    expired: bool = False


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptCompleted:
    result: OperationReceiptResult
    response_hash: str


@final
@dataclass(frozen=True, slots=True)
class OperationReceiptFailed:
    failure_code: OperationReceiptFailureCode
    failure_diagnostics: tuple[str, ...] = ()


type OperationReceiptOutcome = (
    OperationReceiptClaimed | OperationReceiptTakenOver | OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed
)


class OperationReceiptConflictError(RuntimeError):
    def __init__(self, *, session_id: UUID, operation_id: str) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        super().__init__("Operation id is already bound to a different request")


class OperationReceiptSettlementConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Operation expected head changed before settlement")


class OperationReceiptFenceLostError(RuntimeError):
    def __init__(self, fence: OperationReceiptFence) -> None:
        self.session_id = fence.session_id
        self.operation_id = fence.operation_id
        self.attempt = fence.attempt
        super().__init__("Operation receipt fence is no longer current")


@final
@dataclass(frozen=True, slots=True)
class SessionForkParentAuthority:
    """Exact parent session-operation and durable receipt authority pair."""

    parent_context: SessionOperationContext
    receipt_fence: OperationReceiptFence

    def __post_init__(self) -> None:
        if type(self.parent_context) is not SessionOperationContext:
            raise AuditIntegrityError("SessionForkParentAuthority.parent_context must be exact")
        if type(self.receipt_fence) is not OperationReceiptFence:
            raise AuditIntegrityError("SessionForkParentAuthority.receipt_fence must be exact")
        if self.parent_context.operation_kind is not SessionOperationKind.SESSION_FORK:
            raise AuditIntegrityError("SessionForkParentAuthority requires SESSION_FORK context")
        if self.parent_context.fence.session_id != str(self.receipt_fence.session_id):
            raise AuditIntegrityError("Session fork authorities must name the same parent session")


@final
@dataclass(frozen=True, slots=True)
class SessionForkAuthority:
    """Exact parent, child, and receipt authority for one fork attempt."""

    parent: SessionForkParentAuthority
    child_context: SessionOperationContext

    def __post_init__(self) -> None:
        if type(self.parent) is not SessionForkParentAuthority:
            raise AuditIntegrityError("SessionForkAuthority.parent must be exact")
        if type(self.child_context) is not SessionOperationContext:
            raise AuditIntegrityError("SessionForkAuthority.child_context must be exact")
        if self.child_context.operation_kind is not SessionOperationKind.SESSION_FORK:
            raise AuditIntegrityError("SessionForkAuthority child requires SESSION_FORK context")
        parent_fence = self.parent.parent_context.fence
        child_fence = self.child_context.fence
        if parent_fence.session_id == child_fence.session_id:
            raise AuditIntegrityError("Session fork parent and child must be different sessions")
        if child_fence.operation_epoch < 2:
            raise AuditIntegrityError("Session fork child authority begins at epoch 2")


@final
@dataclass(frozen=True, slots=True)
class SessionForkChildCreation:
    """Caller-provided child contents with all authority identity omitted."""

    user_id: str
    auth_provider_type: AuthProviderType
    title: str
    created_at: datetime
    archived_at: datetime
    forked_from_message_id: UUID

    def __post_init__(self) -> None:
        if type(self.user_id) is not str or not self.user_id:
            raise AuditIntegrityError("SessionForkChildCreation.user_id must be non-empty")
        if self.auth_provider_type not in get_args(AuthProviderType):
            raise AuditIntegrityError("SessionForkChildCreation.auth_provider_type is invalid")
        if type(self.title) is not str:
            raise AuditIntegrityError("SessionForkChildCreation.title must be exact")
        if not isinstance(self.created_at, datetime) or not isinstance(self.archived_at, datetime):
            raise AuditIntegrityError("SessionForkChildCreation timestamps must be datetimes")
        if type(self.forked_from_message_id) is not UUID:
            raise AuditIntegrityError("SessionForkChildCreation.forked_from_message_id must be UUID")


@final
@dataclass(frozen=True, slots=True)
class SessionForkChildMessageCreation:
    """One child message whose custody and sequence are repository-owned."""

    id: UUID
    role: ChatMessageRole
    content: str
    raw_content: str | None
    tool_calls: Sequence[Mapping[str, Any]] | None
    tool_call_id: str | None
    parent_assistant_id: UUID | None
    writer_principal: ChatMessageWriterPrincipal
    created_at: datetime
    composition_state_id: UUID | None

    def __post_init__(self) -> None:
        if type(self.id) is not UUID:
            raise AuditIntegrityError("SessionForkChildMessageCreation.id must be UUID")
        if self.role not in CHAT_MESSAGE_ROLE_VALUES:
            raise AuditIntegrityError("SessionForkChildMessageCreation.role is invalid")
        if self.writer_principal not in CHAT_MESSAGE_WRITER_PRINCIPAL_VALUES:
            raise AuditIntegrityError("SessionForkChildMessageCreation.writer_principal is invalid")
        if type(self.content) is not str:
            raise AuditIntegrityError("SessionForkChildMessageCreation.content must be exact")
        if not isinstance(self.created_at, datetime):
            raise AuditIntegrityError("SessionForkChildMessageCreation.created_at must be datetime")
        if self.tool_calls is not None:
            freeze_fields(self, "tool_calls")


class SessionReceiptInProgressError(RuntimeError):
    """Archival cannot remove a live ordinary mutation receipt."""

    def __init__(self, *, session_id: UUID, kind: OperationReceiptKind) -> None:
        self.session_id = session_id
        self.kind = kind
        super().__init__(f"Session has an in-progress {kind} operation")


@final
@dataclass(frozen=True, slots=True)
class RunDiagnosticsAuditAuthority:
    """Handle-free write authority for run-diagnostics LLM audit rows.

    Run-diagnostics evaluation holds no operation lease or token. The
    authority is instead the run row's own custody of its session/state
    binding, and it MUST be re-proven durably — run row still exists with
    exactly this binding, owning session present and not archived —
    inside the same locked transaction that appends the audit row
    (elspeth-0fcf68d50f).
    """

    run_id: UUID
    session_id: UUID
    state_id: UUID

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise AuditIntegrityError("RunDiagnosticsAuditAuthority.run_id must be a UUID")
        if type(self.session_id) is not UUID:
            raise AuditIntegrityError("RunDiagnosticsAuditAuthority.session_id must be a UUID")
        if type(self.state_id) is not UUID:
            raise AuditIntegrityError("RunDiagnosticsAuditAuthority.state_id must be a UUID")


@final
@dataclass(frozen=True, slots=True)
class RunDiagnosticsAuditDraft:
    """One row of a run-diagnostics audit cohort.

    Role (``audit``), ``writer_principal`` (``run_diagnostics``) and
    ``composition_state_id`` (``authority.state_id``) are all derived
    from the authority at write time, never carried per row — the only
    per-row facts are the content and its audit envelope. The whole
    cohort settles in one locked transaction under one custody proof
    (elspeth-90231248dc), so a mid-cohort failure leaves zero rows
    durable rather than a prefix that reads as a complete record.
    """

    content: str
    tool_calls: tuple[Mapping[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise AuditIntegrityError("RunDiagnosticsAuditDraft.content must be an exact string")
        if self.tool_calls is not None and type(self.tool_calls) is not tuple:
            raise AuditIntegrityError("RunDiagnosticsAuditDraft.tool_calls must be a tuple or None")
        if self.tool_calls is not None:
            freeze_fields(self, "tool_calls")


RUN_DIAGNOSTICS_AUTHORITY_LOSS_REASONS: frozenset[str] = frozenset({"session_missing", "session_archived", "run_missing", "run_rebound"})


class RunDiagnosticsAuthorityLostError(RuntimeError):
    """The run/session/state binding behind a diagnostics write no longer holds."""

    def __init__(self, authority: RunDiagnosticsAuditAuthority, *, reason: str) -> None:
        if reason not in RUN_DIAGNOSTICS_AUTHORITY_LOSS_REASONS:
            raise AuditIntegrityError(f"RunDiagnosticsAuthorityLostError reason {reason!r} is outside the closed vocabulary")
        self.run_id = authority.run_id
        self.session_id = authority.session_id
        self.state_id = authority.state_id
        self.reason = reason
        super().__init__(f"Run-diagnostics audit authority is no longer current: {reason}")


# Legal run status transitions. Implementations MUST reject any
# transition not in this table.
#
# Wrapped in MappingProxyType so importers cannot mutate the module-level
# table at runtime: ``LEGAL_RUN_TRANSITIONS["completed"] = frozenset({"running"})``
# raises TypeError rather than silently redefining terminal-state policy
# for every downstream consumer.  The inline dict has no retained alias,
# so the proxy is the only reference — there is no mutable back-door.
#
# Phase 2.2: pending → empty is legal (a run that begins and immediately
# finds an empty source skips the running state); running → every terminal
# value is legal (the row-count predicate decides which terminal value).
LEGAL_RUN_TRANSITIONS: Mapping[SessionRunStatus, frozenset[SessionRunStatus]] = MappingProxyType(
    {
        "pending": frozenset({"running", "completed_with_failures", "failed", "empty", "cancelled"}),
        "running": frozenset({"completed", "completed_with_failures", "failed", "empty", "cancelled"}),
        "completed": frozenset(),  # terminal
        "completed_with_failures": frozenset(),  # terminal
        "failed": frozenset(),  # terminal
        "empty": frozenset(),  # terminal
        "cancelled": frozenset(),  # terminal
    }
)

if frozenset(LEGAL_RUN_TRANSITIONS.keys()) != SESSION_RUN_STATUS_VALUES:
    raise AssertionError(
        f"LEGAL_RUN_TRANSITIONS keys {frozenset(LEGAL_RUN_TRANSITIONS.keys())} "
        f"must match SessionRunStatus values {SESSION_RUN_STATUS_VALUES}"
    )
if any(not allowed.issubset(SESSION_RUN_STATUS_VALUES) for allowed in LEGAL_RUN_TRANSITIONS.values()):
    raise AssertionError("LEGAL_RUN_TRANSITIONS contains a target not present in SessionRunStatus")
# elspeth-879f6de6bd: enforce that the empty-frozenset entries in
# LEGAL_RUN_TRANSITIONS exactly match the TerminalSessionRunStatus Literal.
# A drift here would silently re-introduce the recovery defect: a status
# could be terminal in the state machine (no legal outgoing transition)
# but absent from SESSION_TERMINAL_RUN_STATUS_VALUES (so the recovery
# guard would miss it and attempt an illegal transition), or vice versa.
_legal_transitions_terminal = frozenset(s for s, allowed in LEGAL_RUN_TRANSITIONS.items() if not allowed)
if _legal_transitions_terminal != SESSION_TERMINAL_RUN_STATUS_VALUES:
    raise AssertionError(
        f"LEGAL_RUN_TRANSITIONS terminal entries {sorted(_legal_transitions_terminal)} "
        f"must match TerminalSessionRunStatus {sorted(SESSION_TERMINAL_RUN_STATUS_VALUES)}"
    )


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    """Represents a row from the run_events table."""

    id: UUID
    run_id: UUID
    sequence: int
    timestamp: datetime
    event_type: SessionRunEventType
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.id) is not UUID:
            raise AuditIntegrityError("RunEventRecord.id must be an exact UUID")
        if type(self.run_id) is not UUID:
            raise AuditIntegrityError("RunEventRecord.run_id must be an exact UUID")
        if type(self.sequence) is not int or self.sequence < 1:
            raise AuditIntegrityError("RunEventRecord.sequence must be a positive exact integer")
        if type(self.timestamp) is not datetime or self.timestamp.utcoffset() is None:
            raise AuditIntegrityError("RunEventRecord.timestamp must be an aware exact datetime")
        event_type: object = self.event_type
        if type(event_type) is not str or event_type not in SESSION_RUN_EVENT_TYPE_VALUES:
            raise AuditIntegrityError("RunEventRecord.event_type is invalid")
        data: object = self.data
        if not isinstance(data, Mapping):
            raise AuditIntegrityError("RunEventRecord.data must be a mapping")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        freeze_fields(self, "data")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Represents a row from the sessions table.

    All fields are scalars or datetime -- no freeze guard needed.
    """

    id: UUID
    user_id: str
    auth_provider_type: AuthProviderType
    title: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    forked_from_session_id: UUID | None = None
    forked_from_message_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ComposerSessionPreferencesRecord:
    """Represents composer preferences stored on the sessions row."""

    session_id: UUID
    trust_mode: ComposerTrustMode
    density_default: ComposerDensityDefault
    interpretation_review_disabled: bool
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.trust_mode not in COMPOSER_TRUST_MODE_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: sessions.trust_mode is {self.trust_mode!r}, expected one of {sorted(COMPOSER_TRUST_MODE_VALUES)}"
            )
        if self.density_default not in COMPOSER_DENSITY_DEFAULT_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: sessions.density_default is {self.density_default!r}, expected one of {sorted(COMPOSER_DENSITY_DEFAULT_VALUES)}"
            )


@dataclass(frozen=True, slots=True)
class ComposerSessionPreferencesTransition:
    """Result of a per-session composer-preferences PATCH.

    Carries both the value the PATCH overwrote (``prior``) and the value
    the PATCH wrote (``current``). Both are loaded inside the same write
    transaction as the audit + state update, so there is no TOCTOU
    window between read and write — see Phase 8 plan §"Service signature
    precondition (B2 — load-bearing)" for the rationale and the
    explicitly-rejected route-handler read-before-write alternative.

    The Phase 8b telemetry consumer reads ``prior.trust_mode`` to
    compute the ``from_mode`` attribute on
    ``composer.session.switched_total``; the B1 audit-payload extension
    records ``prior.trust_mode`` into ``proposal_events_table.payload``
    so the telemetry counter remains a strict subset of audit-recorded
    reality (the logging-telemetry-policy skill §The Superset Rule).

    ``frozen=True`` leaves container contents mutable through the attribute
    reference, so container fields are deep-frozen in ``__post_init__``.
    Both fields here hold immutable frozen dataclass instances; there are no
    container fields, so no deep-freeze guard is required.
    """

    prior: ComposerSessionPreferencesRecord
    current: ComposerSessionPreferencesRecord


@dataclass(frozen=True, slots=True)
class PipelineProposalPublicMetadata:
    """Safe reload metadata for canonical pipeline proposals."""

    draft_hash: str
    base: Mapping[str, Any]
    repair_count: int
    skill_hash: str
    audit_payload_hash: str
    custody_result: Literal["not_required", "ready"]

    def __post_init__(self) -> None:
        freeze_fields(self, "base")


@dataclass(frozen=True, slots=True)
class CompositionProposalRecord:
    """Represents a durable pending/committed/rejected composer proposal."""

    id: UUID
    session_id: UUID
    tool_call_id: str
    user_message_id: UUID | None
    composer_model_identifier: str | None
    composer_model_version: str | None
    composer_provider: str | None
    composer_skill_hash: str | None
    tool_arguments_hash: str | None
    tool_name: str
    status: ProposalLifecycleStatus
    summary: str
    rationale: str
    affects: Sequence[str]
    arguments_json: Mapping[str, Any]
    arguments_redacted_json: Mapping[str, Any]
    base_state_id: UUID | None
    committed_state_id: UUID | None
    audit_event_id: UUID | None
    created_at: datetime
    updated_at: datetime
    pipeline_metadata: PipelineProposalPublicMetadata | None = None

    def __post_init__(self) -> None:
        if self.status not in PROPOSAL_LIFECYCLE_STATUS_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: composition_proposals.status is {self.status!r}, expected one of {sorted(PROPOSAL_LIFECYCLE_STATUS_VALUES)}"
            )
        composer_provenance = (
            self.composer_model_identifier,
            self.composer_model_version,
            self.composer_provider,
            self.composer_skill_hash,
            self.tool_arguments_hash,
        )
        if any(value is None for value in composer_provenance) and any(value is not None for value in composer_provenance):
            raise AuditIntegrityError("Tier 1: composition_proposals composer provenance fields must be all populated or all NULL")
        freeze_fields(self, "affects", "arguments_json", "arguments_redacted_json")


@dataclass(frozen=True, slots=True)
class ProposalEventRecord:
    """Represents an immutable composer proposal lifecycle event."""

    id: UUID
    session_id: UUID
    proposal_id: UUID | None
    event_type: ProposalEventType
    # Actor format is originator:role:id for request-scoped actors
    # (composer-web:user:{user_id}, user:{user_id}); system actors use
    # system:{component}.
    actor: str
    payload: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.event_type not in PROPOSAL_EVENT_TYPE_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: proposal_events.event_type is {self.event_type!r}, expected one of {sorted(PROPOSAL_EVENT_TYPE_VALUES)}"
            )
        freeze_fields(self, "payload")


@dataclass(frozen=True, slots=True)
class AuthoritativePipelineProposal:
    """Verified immutable pipeline authority reconstructed from row and events."""

    row: CompositionProposalRecord
    proposal: PipelineProposal
    creation_event_id: UUID
    custody_result: Literal["not_required", "ready"]
    composer_operation: ComposerOperationBinding | None = None
    creation_schema: Literal["pipeline_proposal_created.v2", "pipeline_proposal_created.v3"] = "pipeline_proposal_created.v2"
    creation_actor: str | None = None


@dataclass(frozen=True, slots=True)
class AuthoritativeCompositionProposal:
    """One exact creation-event classification used by generic routes."""

    row: CompositionProposalRecord
    pipeline: AuthoritativePipelineProposal | None


@final
@dataclass(frozen=True, slots=True)
class TransitionAssistantDraft:
    """Assistant content that must commit with transition consumption."""

    content: str
    raw_content: str | None

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise AuditIntegrityError("TransitionAssistantDraft.content must be an exact string")
        if self.raw_content is not None and type(self.raw_content) is not str:
            raise AuditIntegrityError("TransitionAssistantDraft.raw_content must be an exact string or None")


@dataclass(frozen=True, slots=True)
class PipelineDispatchRecovery:
    """One durable successful dispatch available to resume settlement."""

    binding: PipelineDispatchAuditBinding
    executor_content_hash: str


class PendingInterpretationPolicy(StrEnum):
    RECONCILE = "reconcile"
    PIPELINE_CANDIDATE = "pipeline_candidate"


@dataclass(frozen=True, slots=True)
class PendingInterpretationCreationResult:
    event: InterpretationEventRecord
    produced_state: CompositionStateRecord | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMessageRecord:
    """Represents a row from the chat_messages table.

    tool_calls uses the stored LiteLLM array format and may contain nested
    mutable lists/dicts -- requires freeze guard when not None.

    raw_content stores the model's pre-synthesis prose when the visible
    content was augmented (operator-facing suffix appended) or replaced
    (false-completion-claim path) by ``_finalize_no_tool_response``. It
    is persisted for audit provenance and is returned in
    ChatMessageResponse only when the caller opts in via
    ``?include_raw_content=true``; otherwise the response field is
    ``null`` (the field is always present in the response shape).

    Producer contract: when raw_content is set, ``content`` MUST start
    with raw_content (all composer synthesis shapes are augmentations
    post-elspeth-9cfbad6901). Mechanically enforced at producer
    construction by
    ``web.composer.service._enforce_augmentation_prefix_invariant``.
    Consumers (notably ``routes._composer_history_content``) rely on
    the contract to detect synthesis structurally without a field-level
    discriminator; the field-level decoupling is tracked at
    ``elspeth-7ae1732ab2``.
    """

    id: UUID
    session_id: UUID
    role: ChatMessageRole
    content: str
    created_at: datetime
    writer_principal: ChatMessageWriterPrincipal
    sequence_no: int | None = None
    raw_content: str | None = None
    tool_calls: Sequence[Mapping[str, Any]] | None = None
    composition_state_id: UUID | None = None
    tool_call_id: str | None = None
    parent_assistant_id: UUID | None = None
    operation_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.role not in CHAT_MESSAGE_ROLE_VALUES:
            raise AuditIntegrityError(f"Tier 1: chat_messages.role is {self.role!r}, expected one of {sorted(CHAT_MESSAGE_ROLE_VALUES)}")
        # Tier-1 read guard: ``writer_principal`` mirrors the
        # ``ck_chat_messages_writer_principal`` CHECK constraint. Reading a
        # value outside the closed enum from our own session DB means
        # something catastrophic happened (constraint disabled, direct SQL
        # write, schema drift). Crash with a Tier-1 audit-integrity error
        # rather than letting a Literal-typed field carry a wider str at
        # runtime — same posture as the role guard above.
        if self.writer_principal not in CHAT_MESSAGE_WRITER_PRINCIPAL_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: chat_messages.writer_principal is {self.writer_principal!r}, "
                f"expected one of {sorted(CHAT_MESSAGE_WRITER_PRINCIPAL_VALUES)}"
            )
        # tool_call_id / parent_assistant_id are scalar fields and need no
        # freeze guard — scalar-only records have nothing to deep-freeze.
        # Only ``tool_calls`` carries mutable contents.
        if self.tool_calls is not None:
            freeze_fields(self, "tool_calls")


@dataclass(frozen=True, slots=True)
class MessageIngressFresh:
    """A newly accepted user row with its same-transaction transcript."""

    operation_id: UUID
    message: ChatMessageRecord
    transcript: tuple[ChatMessageRecord, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompositionRejectionEventRecord:
    """One ``composition_rejection_events`` row, projected for the audit-grade view.

    elspeth-3e28029d2f persisted the reason a composer tool call was refused
    (operator ruling 2026-09-02: session data, the private audit-attribution
    surface like ``chat_messages.raw_content``). This record is the READ side,
    returned only to the owner-only, access-logged audit-grade messages view
    behind ``include_rejection_reasons``. ``planner_payload`` is deliberately
    not carried: whether it may cross HTTP has not been ruled.

    Tier 1: built from our own session DB, so a wrong scalar type is
    corruption and crashes rather than coercing.
    """

    id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    error_code: str | None
    message: str
    composition_state_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.id) is not str
            or type(self.session_id) is not str
            or type(self.tool_call_id) is not str
            or type(self.tool_name) is not str
            or type(self.message) is not str
            or (self.error_code is not None and type(self.error_code) is not str)
            or (self.composition_state_id is not None and type(self.composition_state_id) is not str)
        ):
            raise AuditIntegrityError("Tier 1: composition_rejection_events row has an invalid scalar")
        if type(self.created_at) is not datetime:
            raise AuditIntegrityError("Tier 1: composition_rejection_events.created_at is not a datetime")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompositionValidationError:
    """One surface-permitted validation diagnostic stored with session state."""

    message: str
    error_code: str | None
    component: str | None

    def __post_init__(self) -> None:
        if type(self.message) is not str or (self.error_code is not None and type(self.error_code) is not str):
            raise AuditIntegrityError("Invalid composition validation error scalar")
        if self.component is not None and type(self.component) is not str:
            raise AuditIntegrityError("Invalid composition validation error scalar")


class CompositionValidationErrorWire(TypedDict):
    message: str
    error_code: str | None
    component: str | None


def serialize_composition_validation_error(value: CompositionValidationError) -> CompositionValidationErrorWire:
    """Encode an owned record explicitly, rechecking its current invariants."""
    if type(value) is not CompositionValidationError:
        raise AuditIntegrityError("Composition validation errors require owned records")
    value.__post_init__()
    return {"message": value.message, "error_code": value.error_code, "component": value.component}


def serialize_composition_validation_errors(
    values: Sequence[CompositionValidationError] | None,
) -> list[CompositionValidationErrorWire] | None:
    if values is None:
        return None
    if type(values) not in (list, tuple):
        raise AuditIntegrityError("Invalid composition validation error collection")
    return [serialize_composition_validation_error(value) for value in values]


def decode_stored_composition_validation_errors(value: object) -> tuple[CompositionValidationError, ...] | None:
    """Decode current-epoch SQL JSON; malformed audit records fail closed."""
    if value is None:
        return None
    if not isinstance(value, list) or type(value) is not list:
        raise AuditIntegrityError("Invalid stored composition validation error collection")
    errors = []
    for item in value:
        if not isinstance(item, dict) or type(item) is not dict or set(item) != {"message", "error_code", "component"}:
            raise AuditIntegrityError("Invalid stored composition validation error record")
        message, error_code, component = item["message"], item["error_code"], item["component"]
        if type(message) is not str or (error_code is not None and type(error_code) is not str):
            raise AuditIntegrityError("Invalid stored composition validation error scalar")
        if component is not None and type(component) is not str:
            raise AuditIntegrityError("Invalid stored composition validation error scalar")
        errors.append(CompositionValidationError(message=message, error_code=error_code, component=component))
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class CompositionStateData:
    """Input DTO for saving a new composition state version.

    Contains mutable container fields -- requires freeze guard.
    """

    source: InitVar[Mapping[str, Any] | None] = None
    sources: Mapping[str, Mapping[str, Any]] | None = None
    nodes: Sequence[Mapping[str, Any]] | None = None
    edges: Sequence[Mapping[str, Any]] | None = None
    outputs: Sequence[Mapping[str, Any]] | None = None
    metadata_: Mapping[str, Any] | None = None
    is_valid: bool = False
    validation_errors: Sequence[CompositionValidationError] | None = None
    # Operational/audit meta describing how this state was reached. Distinct
    # from ``metadata_`` which carries user-facing PipelineMetadata. ``None``
    # is honest for revert/fork paths and for non-compose write paths.
    composer_meta: Mapping[str, Any] | None = None

    def __post_init__(self, source: Mapping[str, Any] | None) -> None:
        serialize_composition_validation_errors(self.validation_errors)
        if source is not None:
            if self.sources is not None:
                raise AuditIntegrityError("CompositionStateData accepts either source or sources, not both")
            object.__setattr__(self, "sources", {"source": source})
        non_none = []
        if self.sources is not None:
            non_none.append("sources")
        if self.nodes is not None:
            non_none.append("nodes")
        if self.edges is not None:
            non_none.append("edges")
        if self.outputs is not None:
            non_none.append("outputs")
        if self.metadata_ is not None:
            non_none.append("metadata_")
        if self.validation_errors is not None:
            non_none.append("validation_errors")
        if self.composer_meta is not None:
            non_none.append("composer_meta")
        if non_none:
            freeze_fields(self, *non_none)


@final
@dataclass(frozen=True, slots=True)
class SessionCompositionStateCreation:
    """One ordinary COMPOSE checkpoint with repository-owned versioning."""

    id: UUID
    data: CompositionStateData
    provenance: CompositionStateProvenance
    created_at: datetime
    derived_from_state_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.id) is not UUID:
            raise AuditIntegrityError("SessionCompositionStateCreation.id must be UUID")
        if type(self.data) is not CompositionStateData:
            raise AuditIntegrityError("SessionCompositionStateCreation.data must be exact")
        provenance: object = self.provenance
        if type(provenance) is not str or provenance not in COMPOSITION_STATE_PROVENANCE_VALUES:
            raise AuditIntegrityError("SessionCompositionStateCreation.provenance is invalid")
        created_at: object = self.created_at
        if type(created_at) is not datetime or created_at.tzinfo is None or created_at.utcoffset() is None:
            raise AuditIntegrityError("SessionCompositionStateCreation.created_at must be an aware exact datetime")
        derived_from_state_id: object = self.derived_from_state_id
        if derived_from_state_id is not None and type(derived_from_state_id) is not UUID:
            raise AuditIntegrityError("SessionCompositionStateCreation.derived_from_state_id must be UUID or None")


@final
@dataclass(frozen=True, slots=True)
class SessionForkChildStateCreation:
    """One fork child checkpoint with repository-owned version allocation."""

    id: UUID
    data: CompositionStateData
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.id) is not UUID:
            raise AuditIntegrityError("SessionForkChildStateCreation.id must be UUID")
        if type(self.data) is not CompositionStateData:
            raise AuditIntegrityError("SessionForkChildStateCreation.data must be exact")
        if not isinstance(self.created_at, datetime):
            raise AuditIntegrityError("SessionForkChildStateCreation.created_at must be datetime")


@dataclass(frozen=True, slots=True)
class CompositionStateRecord:
    """Represents a row from the composition_states table.

    Contains mutable container fields -- requires freeze guard.
    """

    id: UUID
    session_id: UUID
    version: int
    nodes: Sequence[Mapping[str, Any]] | None
    edges: Sequence[Mapping[str, Any]] | None
    outputs: Sequence[Mapping[str, Any]] | None
    metadata_: Mapping[str, Any] | None
    is_valid: bool
    validation_errors: Sequence[CompositionValidationError] | None
    created_at: datetime
    derived_from_state_id: UUID | None
    # Operational/audit meta describing how this state was reached. Distinct
    # from ``metadata_`` which carries user-facing PipelineMetadata. ``None``
    # is honest for revert/fork paths and for non-compose write paths.
    composer_meta: Mapping[str, Any] | None = None
    sources: Mapping[str, Mapping[str, Any]] | None = None
    source: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        serialize_composition_validation_errors(self.validation_errors)
        non_none = []
        if self.source is not None:
            non_none.append("source")
        if self.sources is not None:
            non_none.append("sources")
        if self.nodes is not None:
            non_none.append("nodes")
        if self.edges is not None:
            non_none.append("edges")
        if self.outputs is not None:
            non_none.append("outputs")
        if self.metadata_ is not None:
            non_none.append("metadata_")
        if self.validation_errors is not None:
            non_none.append("validation_errors")
        if self.composer_meta is not None:
            non_none.append("composer_meta")
        if non_none:
            freeze_fields(self, *non_none)


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationSiteSnapshot:
    """One pending review site and its immutable surfacing-state snapshot."""

    event: InterpretationEventRecord
    surfacing_state: CompositionStateRecord | None

    def __post_init__(self) -> None:
        if type(self.event) is not InterpretationEventRecord:
            raise AuditIntegrityError("pending interpretation snapshot event must be exact")
        if self.surfacing_state is not None and type(self.surfacing_state) is not CompositionStateRecord:
            raise AuditIntegrityError("pending interpretation surfacing state must be exact or None")


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationSnapshot:
    """Locked, handle-free inputs for one pending-review policy decision."""

    anchor_state: CompositionStateRecord
    live_state: CompositionStateRecord
    pending_sites: tuple[SessionPendingInterpretationSiteSnapshot, ...]
    review_disabled: bool
    opt_out_marker_exists: bool
    #: Already-SUPERSEDED user-approved rows for the same site, newest first.
    #: The state-commit sweep (``dead_site_supersession``) can retire this
    #: site's card in the commit that extinguished it, and a delayed surfacer
    #: still owns reconciliation of that historical site: it must return the
    #: terminal row rather than convert a successful state commit into an
    #: error response.
    superseded_events: tuple[InterpretationEventRecord, ...] = ()

    def __post_init__(self) -> None:
        if type(self.anchor_state) is not CompositionStateRecord or type(self.live_state) is not CompositionStateRecord:
            raise AuditIntegrityError("pending interpretation state snapshots must be exact")
        if self.anchor_state.session_id != self.live_state.session_id:
            raise AuditIntegrityError("pending interpretation state snapshots must belong to one session")
        if type(self.pending_sites) is not tuple or any(
            type(site) is not SessionPendingInterpretationSiteSnapshot for site in self.pending_sites
        ):
            raise AuditIntegrityError("pending interpretation sites must be an exact tuple")
        if type(self.superseded_events) is not tuple or any(
            type(event) is not InterpretationEventRecord for event in self.superseded_events
        ):
            raise AuditIntegrityError("superseded interpretation events must be an exact tuple")
        if type(self.review_disabled) is not bool or type(self.opt_out_marker_exists) is not bool:
            raise AuditIntegrityError("pending interpretation policy flags must be exact booleans")


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationDecision:
    """Exact DML decision returned by pure pending-review policy code."""

    result_event_id: UUID
    superseded_event_ids: tuple[UUID, ...] = ()
    insert_event: bool = False
    choice: InterpretationChoice | None = None
    accepted_value: str | None = None
    resolved_at: datetime | None = None
    arguments_hash: str | None = None
    hash_domain_version: str | None = None
    interpretation_source: InterpretationSource | None = None
    approved_prompt_artifact_hash: str | None = None
    ensure_opt_out_marker: bool = False
    appended_state: SessionCompositionStateCreation | None = None

    def __post_init__(self) -> None:
        if type(self.result_event_id) is not UUID:
            raise AuditIntegrityError("pending interpretation result event id must be a UUID")
        if type(self.superseded_event_ids) is not tuple or any(type(event_id) is not UUID for event_id in self.superseded_event_ids):
            raise AuditIntegrityError("superseded interpretation ids must be an exact UUID tuple")
        if len(set(self.superseded_event_ids)) != len(self.superseded_event_ids):
            raise AuditIntegrityError("superseded interpretation ids must be unique")
        if type(self.insert_event) is not bool or type(self.ensure_opt_out_marker) is not bool:
            raise AuditIntegrityError("pending interpretation decision flags must be exact booleans")
        if not self.insert_event:
            if (
                any(
                    value is not None
                    for value in (
                        self.choice,
                        self.accepted_value,
                        self.resolved_at,
                        self.arguments_hash,
                        self.hash_domain_version,
                        self.interpretation_source,
                        self.approved_prompt_artifact_hash,
                        self.appended_state,
                    )
                )
                or self.ensure_opt_out_marker
            ):
                raise AuditIntegrityError("reuse decisions cannot carry insertion fields")
            return
        if type(self.choice) is not InterpretationChoice or type(self.interpretation_source) is not InterpretationSource:
            raise AuditIntegrityError("insert decisions require exact choice and source values")
        if self.appended_state is not None and type(self.appended_state) is not SessionCompositionStateCreation:
            raise AuditIntegrityError("pending interpretation appended state must be exact or None")
        if self.choice is InterpretationChoice.PENDING and self.interpretation_source is InterpretationSource.USER_APPROVED:
            if (
                self.accepted_value is not None
                or self.resolved_at is not None
                or self.arguments_hash is not None
                or self.hash_domain_version is not None
                or self.approved_prompt_artifact_hash is not None
                or self.ensure_opt_out_marker
                or self.appended_state is not None
            ):
                raise AuditIntegrityError("pending user-approved decisions cannot carry resolution or state fields")
            return
        if self.choice is InterpretationChoice.OPTED_OUT and self.interpretation_source is InterpretationSource.AUTO_INTERPRETED_OPT_OUT:
            if type(self.accepted_value) is not str:
                raise AuditIntegrityError("automatic opt-out decisions require an exact accepted value")
            if type(self.resolved_at) is not datetime or self.resolved_at.utcoffset() is None:
                raise AuditIntegrityError("automatic opt-out decisions require an aware resolved_at")
            if not is_lower_sha256_hex(self.arguments_hash) or self.hash_domain_version != "v2":
                raise AuditIntegrityError("automatic opt-out decisions require the v2 lowercase SHA-256 argument binding")
            if not self.ensure_opt_out_marker or self.appended_state is None:
                raise AuditIntegrityError("automatic opt-out decisions require the marker and appended state")
            if self.approved_prompt_artifact_hash is not None and not is_lower_sha256_hex(self.approved_prompt_artifact_hash):
                raise AuditIntegrityError("approved prompt artifact hash must be lowercase SHA-256 or None")
            return
        raise AuditIntegrityError("pending interpretation decision choice/source pairing is invalid")


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationValidationCandidate:
    """Immutable candidate state offered to the validation-only seam."""

    digest: str
    base_state: CompositionStateRecord
    data: CompositionStateData

    def __post_init__(self) -> None:
        if not is_lower_sha256_hex(self.digest):
            raise AuditIntegrityError("pending interpretation validation candidate digest must be lowercase SHA-256")
        if type(self.base_state) is not CompositionStateRecord or type(self.data) is not CompositionStateData:
            raise AuditIntegrityError("pending interpretation validation candidate state must be exact")


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationValidationResult:
    """Digest-bound validation outcome with no authority to describe writes."""

    candidate_digest: str
    is_valid: bool
    validation_errors: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if not is_lower_sha256_hex(self.candidate_digest):
            raise AuditIntegrityError("pending interpretation validation result digest must be lowercase SHA-256")
        if type(self.is_valid) is not bool:
            raise AuditIntegrityError("pending interpretation validation result is_valid must be exact")
        if self.validation_errors is not None and (
            type(self.validation_errors) is not tuple or any(type(message) is not str for message in self.validation_errors)
        ):
            raise AuditIntegrityError("pending interpretation validation errors must be an exact string tuple or None")


SessionPendingInterpretationValidator = Callable[
    [SessionPendingInterpretationValidationCandidate],
    SessionPendingInterpretationValidationResult,
]


@final
@dataclass(frozen=True, slots=True)
class SessionPendingInterpretationCommand:
    """Immutable pending-review creation facts; policy and DML stay repository-owned."""

    event_id: UUID
    opt_out_marker_event_id: UUID
    composition_state_id: UUID
    affected_node_id: str
    tool_call_id: str
    user_term: str
    kind: InterpretationKind
    llm_draft: str
    surface_origin: InterpretationSurfaceOrigin
    model_identifier: str | None
    model_version: str | None
    provider: str | None
    composer_skill_hash: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        # Every field is read directly: this is a type ELSPETH owns, so a
        # reflective getattr would only hide a misspelt field name behind a
        # confident AttributeError-free probe (ADR-032; masquerade gate).
        for field_name, identifier in (
            ("event_id", self.event_id),
            ("opt_out_marker_event_id", self.opt_out_marker_event_id),
            ("composition_state_id", self.composition_state_id),
        ):
            if type(identifier) is not UUID:
                raise AuditIntegrityError(f"SessionPendingInterpretationCommand.{field_name} must be a UUID")
        if self.event_id == self.opt_out_marker_event_id:
            raise AuditIntegrityError("pending interpretation event ids must be distinct")
        if type(self.kind) is not InterpretationKind:
            raise AuditIntegrityError("SessionPendingInterpretationCommand.kind must be exact")
        if type(self.created_at) is not datetime or self.created_at.utcoffset() is None:
            raise AuditIntegrityError("SessionPendingInterpretationCommand.created_at must be timezone-aware")
        if type(self.surface_origin) is not InterpretationSurfaceOrigin:
            raise AuditIntegrityError("SessionPendingInterpretationCommand.surface_origin must be exact")
        # LLM provenance is present exactly when an LLM raised the surface; a
        # server-route origin carries None, never a label.
        try:
            validate_surface_provenance(
                self.surface_origin,
                model_identifier=self.model_identifier,
                model_version=self.model_version,
                provider=self.provider,
                composer_skill_hash=self.composer_skill_hash,
                context="SessionPendingInterpretationCommand",
            )
        except ValueError as exc:
            raise AuditIntegrityError(str(exc)) from exc
        nonblank_provenance_fields = tuple(
            (field_name, text)
            for field_name, text in (
                ("model_identifier", self.model_identifier),
                ("model_version", self.model_version),
                ("provider", self.provider),
            )
            if text is not None
        )
        nonblank_text_fields = (
            ("affected_node_id", self.affected_node_id),
            ("tool_call_id", self.tool_call_id),
            ("user_term", self.user_term),
            *nonblank_provenance_fields,
        )
        text_fields = (
            *nonblank_text_fields,
            ("llm_draft", self.llm_draft),
            *((("composer_skill_hash", self.composer_skill_hash),) if self.composer_skill_hash is not None else ()),
        )
        for field_name, text in text_fields:
            if type(text) is not str:
                raise AuditIntegrityError(f"SessionPendingInterpretationCommand.{field_name} must be an exact string")
        for field_name, text in nonblank_text_fields:
            if not text.strip():
                raise AuditIntegrityError(f"SessionPendingInterpretationCommand.{field_name} must be nonblank")


@final
@dataclass(frozen=True, slots=True)
class TransitionResponseSettlement:
    """One transition-consumption state and its visible response."""

    state: CompositionStateRecord
    message: ChatMessageRecord

    def __post_init__(self) -> None:
        if type(self.state) is not CompositionStateRecord:
            raise AuditIntegrityError("TransitionResponseSettlement.state must be exact")
        if type(self.message) is not ChatMessageRecord:
            raise AuditIntegrityError("TransitionResponseSettlement.message must be exact")
        if self.state.session_id != self.message.session_id:
            raise AuditIntegrityError("TransitionResponseSettlement rows must belong to the same session")
        if self.message.role != "assistant" or self.message.composition_state_id != self.state.id:
            raise AuditIntegrityError("TransitionResponseSettlement message must be an assistant bound to its state")


@final
@dataclass(frozen=True, slots=True)
class StagedForkSession:
    """Persisted child cohort returned by initial staging or takeover."""

    session: SessionRecord
    messages: tuple[ChatMessageRecord, ...]
    state: CompositionStateRecord | None
    blob_plan: tuple[BlobForkPlanEntry, ...]
    authority: SessionForkAuthority

    def __post_init__(self) -> None:
        if type(self.session) is not SessionRecord or self.session.archived_at is None:
            raise AuditIntegrityError("StagedForkSession.session must be an archived exact SessionRecord")
        if type(self.authority) is not SessionForkAuthority:
            raise AuditIntegrityError("StagedForkSession.authority must be exact")
        if self.authority.child_context.fence.session_id != str(self.session.id):
            raise AuditIntegrityError("StagedForkSession authority must name its child")
        if type(self.messages) is not tuple or any(type(message) is not ChatMessageRecord for message in self.messages):
            raise AuditIntegrityError("StagedForkSession.messages must be an exact ChatMessageRecord tuple")
        if type(self.blob_plan) is not tuple or any(type(entry) is not BlobForkPlanEntry for entry in self.blob_plan):
            raise AuditIntegrityError("StagedForkSession.blob_plan must be an exact BlobForkPlanEntry tuple")
        if len({entry.source_blob_id for entry in self.blob_plan}) != len(self.blob_plan):
            raise AuditIntegrityError("StagedForkSession.blob_plan must not repeat source blob ids")
        if any(message.session_id != self.session.id for message in self.messages):
            raise AuditIntegrityError("StagedForkSession messages must belong to the staged child")
        if self.state is not None and (type(self.state) is not CompositionStateRecord or self.state.session_id != self.session.id):
            raise AuditIntegrityError("StagedForkSession state must belong to the staged child")


@final
@dataclass(frozen=True, slots=True)
class SessionForkSettlementCommand:
    """Atomic staged-child rewrite, activation, and operation completion."""

    authority: SessionForkAuthority
    expected_current_state_id: UUID | None
    edited_message_id: UUID
    rewritten_state_id: UUID | None
    rewritten_state: CompositionStateData | None
    response_hash: str
    actor: str

    def __post_init__(self) -> None:
        if type(self.authority) is not SessionForkAuthority:
            raise AuditIntegrityError("SessionForkSettlementCommand.authority must be exact")
        if type(self.edited_message_id) is not UUID:
            raise AuditIntegrityError("SessionForkSettlementCommand.edited_message_id must be a UUID")
        if self.expected_current_state_id is not None and type(self.expected_current_state_id) is not UUID:
            raise AuditIntegrityError("SessionForkSettlementCommand.expected_current_state_id must be a UUID or None")
        if (self.rewritten_state_id is None) != (self.rewritten_state is None):
            raise AuditIntegrityError("SessionForkSettlementCommand rewritten state id and payload must be paired")
        if self.rewritten_state_id is not None and type(self.rewritten_state_id) is not UUID:
            raise AuditIntegrityError("SessionForkSettlementCommand.rewritten_state_id must be a UUID or None")
        if self.rewritten_state is not None and type(self.rewritten_state) is not CompositionStateData:
            raise AuditIntegrityError("SessionForkSettlementCommand.rewritten_state must be exact")
        if self.rewritten_state is not None and self.expected_current_state_id is None:
            raise AuditIntegrityError("Session fork cannot rewrite an absent staged state")
        if not is_lower_sha256_hex(self.response_hash):
            raise AuditIntegrityError("SessionForkSettlementCommand.response_hash must be lowercase SHA-256")
        if type(self.actor) is not str or not self.actor:
            raise AuditIntegrityError("SessionForkSettlementCommand.actor must be non-empty")

    @property
    def fence(self) -> OperationReceiptFence:
        return self.authority.parent.receipt_fence

    @property
    def child_session_id(self) -> UUID:
        return UUID(self.authority.child_context.fence.session_id)


@final
@dataclass(frozen=True, slots=True)
class PreparedInterpretationEventDraft:
    """One fully attributed review event for atomic state settlement.

    This DTO is the narrow state-producing-route contract: the state id is allocated by the service
    and every draft is validated and inserted in that same transaction.
    """

    event_id: UUID
    affected_node_id: str
    tool_call_id: str
    user_term: str
    kind: InterpretationKind
    llm_draft: str
    surface_origin: InterpretationSurfaceOrigin
    model_identifier: str | None
    model_version: str | None
    provider: str | None
    composer_skill_hash: str | None

    def __post_init__(self) -> None:
        if type(self.event_id) is not UUID:
            raise AuditIntegrityError("PreparedInterpretationEventDraft.event_id must be a UUID")
        if type(self.surface_origin) is not InterpretationSurfaceOrigin:
            raise AuditIntegrityError("PreparedInterpretationEventDraft.surface_origin must be exact")
        try:
            validate_surface_provenance(
                self.surface_origin,
                model_identifier=self.model_identifier,
                model_version=self.model_version,
                provider=self.provider,
                composer_skill_hash=self.composer_skill_hash,
                context="PreparedInterpretationEventDraft",
            )
        except ValueError as exc:
            raise AuditIntegrityError(str(exc)) from exc
        for field_name, value in (
            ("affected_node_id", self.affected_node_id),
            ("tool_call_id", self.tool_call_id),
            ("user_term", self.user_term),
            ("llm_draft", self.llm_draft),
            *(
                (field_name, value)
                for field_name, value in (
                    ("model_identifier", self.model_identifier),
                    ("model_version", self.model_version),
                    ("provider", self.provider),
                    ("composer_skill_hash", self.composer_skill_hash),
                )
                if value is not None
            ),
        ):
            if type(value) is not str or not value:
                raise AuditIntegrityError(f"PreparedInterpretationEventDraft.{field_name} must be a non-empty exact string")
        if type(self.kind) is not InterpretationKind:
            raise AuditIntegrityError("PreparedInterpretationEventDraft.kind must be an InterpretationKind")


@dataclass(frozen=True, slots=True)
class AuditAccessLogRecord:
    """Represents a row from the audit_access_log table.

    ``query_args`` is a privacy-gated, closed allowlist mapping captured
    at the audit-grade messages route boundary. It may contain mutable
    JSON structures after SQLAlchemy deserialisation, so freeze it.
    """

    id: str
    timestamp: datetime
    session_id: str
    requesting_principal: str
    request_path: str
    query_args: Mapping[str, str]
    ip_address: str | None
    writer_principal: AuditAccessWriterPrincipal

    def __post_init__(self) -> None:
        if self.writer_principal not in {"audit_grade_view", "admin_tool", "workflow_inspect"}:
            raise AuditIntegrityError("audit_access_log.writer_principal is invalid")
        freeze_fields(self, "query_args")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Represents a row from the runs table.

    All fields are scalars, datetime, or None -- no freeze guard needed.
    """

    id: UUID
    session_id: UUID
    state_id: UUID
    status: SessionRunStatus
    started_at: datetime
    finished_at: datetime | None
    rows_processed: int
    rows_succeeded: int
    rows_failed: int
    rows_routed_success: int
    rows_routed_failure: int
    rows_quarantined: int
    error: str | None
    landscape_run_id: str | None
    pipeline_yaml: str | None
    cancel_requested_at: datetime | None = None
    cancellation_source: CancellationSource | None = None
    saga_state: RunSagaState = RunSagaState.DRAFT
    recovery_required_reason: RecoveryRequiredReason | None = None

    def __post_init__(self) -> None:
        self._validate_counters()
        if self.status not in SESSION_RUN_STATUS_VALUES:
            raise AuditIntegrityError(f"Tier 1: runs.status is {self.status!r}, expected one of {sorted(SESSION_RUN_STATUS_VALUES)}")
        if self.status in SESSION_TERMINAL_RUN_STATUS_VALUES and self.finished_at is None:
            raise AuditIntegrityError(f"Tier 1: terminal runs.finished_at is NULL for status={self.status!r}")
        # Phase 2.2 (elspeth-0de989c56d): the four operator-completion terminal
        # values (completed / completed_with_failures / empty) all imply the
        # run reached the engine-completion path and produced a Landscape
        # audit record.  `failed` may or may not have a Landscape ID — the
        # engine takes the failed path on exceptions before any Landscape
        # write, so requiring a Landscape ID would crash legitimate
        # exception-bounded shapes.  `cancelled` is signal-bounded; same
        # rationale.
        if self.status in {"completed", "completed_with_failures", "empty"} and not self.landscape_run_id:
            raise AuditIntegrityError(f"Tier 1: status={self.status!r} run is missing landscape_run_id")
        if self.status == "failed" and not self.error:
            raise AuditIntegrityError("Tier 1: failed run is missing error")

    def _validate_counters(self) -> None:
        for field_name, value in (
            ("rows_processed", self.rows_processed),
            ("rows_succeeded", self.rows_succeeded),
            ("rows_failed", self.rows_failed),
            ("rows_routed_success", self.rows_routed_success),
            ("rows_routed_failure", self.rows_routed_failure),
            ("rows_quarantined", self.rows_quarantined),
        ):
            try:
                require_int(value, f"runs.{field_name}", min_value=0)
            except (TypeError, ValueError) as exc:
                raise AuditIntegrityError(f"Tier 1: {exc}") from exc

        if self.rows_routed_success > self.rows_succeeded:
            raise AuditIntegrityError(
                "Tier 1: rows_routed_success must be a subset of rows_succeeded "
                f"(got rows_routed_success={self.rows_routed_success}, rows_succeeded={self.rows_succeeded})"
            )
        if self.rows_routed_failure > self.rows_failed:
            raise AuditIntegrityError(
                "Tier 1: rows_routed_failure must be a subset of rows_failed "
                f"(got rows_routed_failure={self.rows_routed_failure}, rows_failed={self.rows_failed})"
            )
        if self.rows_quarantined > self.rows_failed:
            raise AuditIntegrityError(
                "Tier 1: rows_quarantined must be a subset of rows_failed "
                f"(got rows_quarantined={self.rows_quarantined}, rows_failed={self.rows_failed})"
            )


class InvalidForkTargetError(Exception):
    """Raised when attempting to fork from a non-user message.

    Route handlers catching this error should return 422.
    """

    def __init__(self, message_id: str, role: str) -> None:
        self.message_id = message_id
        self.role = role
        super().__init__(f"Can only fork from user messages, got role '{role}' for message {message_id}")


class SessionNotFoundError(ValueError):
    """Raised when a session id has no matching sessions row.

    Subclasses ``ValueError`` so older callers that still catch
    ``ValueError`` retain compatibility. New IDOR-sensitive route helpers
    catch this narrower type so unrelated value-construction failures do
    not collapse into not-found responses.
    """

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class IllegalRunTransitionError(ValueError):
    """Raised when ``update_run_status`` receives a transition forbidden by
    ``LEGAL_RUN_TRANSITIONS``.

    Subclasses ``ValueError`` for backwards-compatible reraise behaviour, but
    callers performing cancelled-race recovery (``ExecutionService._run_pipeline``)
    catch *this* class only — never the bare ``ValueError`` — so that the four
    other Tier-1 invariant breaches raised by ``update_run_status``
    (run-not-found, landscape_run_id overwrite, completed-without-landscape,
    failed-without-error) propagate without traversing a get_run round-trip
    that could mask the original fault.

    Why a subclass of ValueError (not Exception): existing test fixtures and
    one production call site at the run-lifecycle repository assert
    ``pytest.raises(ValueError)`` on illegal transitions; subclassing keeps
    those green while letting recovery code narrow on identity.
    """

    def __init__(self, current_status: str, target_status: str, allowed: frozenset[str]) -> None:
        self.current_status = current_status
        self.target_status = target_status
        self.allowed = allowed
        super().__init__(f"Illegal run transition: {current_status!r} → {target_status!r}. Allowed: {sorted(allowed)}")


class RunAlreadyActiveError(Exception):
    """Raised when attempting to create a run while one is already active.

    Seam contract D: HTTP handlers catching this error MUST return 409 with
    {"detail": str(exc), "error_type": "run_already_active"} -- not a bare
    HTTPException. See seam-contracts.md for the canonical error shape.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id} already has an active run")


class ProposalStateConflictError(ValueError):
    """An ordinary proposal is no longer pending at the rejection CAS."""


class StaleComposeStateError(RuntimeError):
    """Compose result was based on a no-longer-current composition state.

    Raised by ``SessionServiceProtocol.persist_compose_turn_async`` (and
    its concrete implementation ``SessionServiceImpl.persist_compose_turn``)
    when the session's current composition state changed between the LLM
    call and the persist attempt. Defined here on the protocol module so
    Phase 3 callers can catch the error without importing the concrete
    service class — the symbol is part of the public contract, not an
    implementation detail.

    Mirrors :class:`elspeth.contracts.errors.AuditIntegrityError`'s
    placement on the contracts layer: protocol-level error shapes belong
    on the abstraction, not on the concrete service module.
    """


class TrustModeAutoCommitRevokedError(RuntimeError):
    """Auto-commit authority was revoked before pipeline settlement.

    Raised by ``SessionServiceProtocol.settle_pipeline_composition_proposal``
    when the caller passed ``required_trust_mode`` and the session's durable
    trust mode no longer matches at the moment of the pending->committed
    transition. The check runs INSIDE the settlement write transaction,
    under the same per-session write lock ``update_composer_preferences``
    serialises on, so a preference downgrade is either visible here (and
    the proposal lands on the review path) or became durable only after
    the commit (and legitimately governs future turns) — there is no
    interleaving in between (elspeth-01d4c6e683, commit-boundary half).

    Not an integrity failure: the proposal remains pending and reviewable;
    callers translate this into the ordinary review-path response.
    """

    def __init__(self, session_id: str, *, required: str, current: str) -> None:
        self.session_id = session_id
        self.required = required
        self.current = current
        super().__init__(
            f"Session {session_id} trust mode is {current!r}; settlement required {required!r} — auto-commit authority revoked"
        )


class InterpretationResolveError(ValueError):
    """Base class for expected interpretation-resolution failures."""


class InterpretationEventNotFoundError(InterpretationResolveError):
    """No event exists for the requested ``(session_id, event_id)`` pair."""


class InterpretationEventAlreadyResolvedError(InterpretationResolveError):
    """The event exists but is no longer pending."""


class InterpretationNodeMissingError(InterpretationResolveError):
    """The affected node disappeared from the live composition state."""


class InterpretationNodePluginMutatedError(InterpretationResolveError):
    """The affected node still exists but is no longer an LLM transform."""


class InterpretationPlaceholderConsumedError(InterpretationResolveError):
    """The affected LLM node no longer carries the expected placeholder."""


class InterpretationSourceDataContractDriftError(InterpretationResolveError):
    """The source demand changed after its data-contract card was shown."""

    def __init__(
        self,
        *,
        source_name: str,
        reviewed_fields: tuple[str, ...],
        current_fields: tuple[str, ...] | None,
    ) -> None:
        self.source_name = source_name
        self.reviewed_fields = reviewed_fields
        self.current_fields = current_fields
        reviewed = f"[{', '.join(reviewed_fields)}]"
        current = "no active demanded fields" if current_fields is None else f"[{', '.join(current_fields)}]"
        super().__init__(
            f"The demanded-field contract for source {source_name!r} changed from {reviewed} to {current} "
            "after this review was shown. Reload the session and review the current source data contract."
        )


class InterpretationDraftMismatchError(InterpretationResolveError):
    """The pending requirement exists but its draft is not the surfaced draft.

    Raised by the ``create_pending_interpretation_event`` writer when the
    under-lock re-read of the persisted head finds the review site's staged
    draft differs from the ``llm_draft`` the caller asserted. The tool
    handler converts it to ARG_ERROR (stale composition state or reviewed
    content); it must never surface as a raw 500 (elspeth-9c01c943a5).
    """


class InterpretationUnsupportedChoiceError(InterpretationResolveError):
    """The requested choice is valid generally but unsupported for this kind."""


class AuditAccessLogWriteError(RuntimeError):
    """Required access disclosure could not be recorded.

    ``include_tool_rows=true`` exposes audit-grade transcript rows. If
    that access cannot be written to ``audit_access_log`` first, callers
    must fail closed and return no transcript rows. Workflow inspection has
    the same requirement before exposing another identity's composition.
    """


class WorkflowInspectDenied(RuntimeError):
    """The caller has no live request-scoped authority to inspect a state."""


class ToolCallIDMismatchError(RuntimeError):
    """Assistant ``tool_calls`` and persisted tool rows disagreed on
    the set of tool-call IDs for one compose turn.

    Carries the four mutually-exclusive failure axes (missing, extra,
    duplicate-in-assistant, duplicate-in-rows) so the diagnostic
    string identifies WHICH violation fired without forcing the
    caller to re-derive it.

    Defined on the protocol module alongside
    :class:`StaleComposeStateError` because both are pre-DB exceptions
    referenced by ``SessionServiceProtocol.persist_compose_turn_async``.
    Phase 3 callers can catch the error without importing the concrete
    service class — the symbol is part of the public contract.
    """

    def __init__(
        self,
        *,
        missing: frozenset[str],
        extra: frozenset[str],
        duplicates_in_assistant: frozenset[str],
        duplicates_in_rows: frozenset[str],
    ) -> None:
        self.missing = missing
        self.extra = extra
        self.duplicates_in_assistant = duplicates_in_assistant
        self.duplicates_in_rows = duplicates_in_rows
        super().__init__(
            "persist_compose_turn: assistant tool_calls and tool rows "
            "disagree on the tool-call ID set "
            f"(missing={sorted(missing)!r}, extra={sorted(extra)!r}, "
            f"duplicates_in_assistant={sorted(duplicates_in_assistant)!r}, "
            f"duplicates_in_rows={sorted(duplicates_in_rows)!r}). "
            "Refusing to persist a turn that would leave the audit "
            "trail with an asymmetric assistant/tool transcript."
        )


class SessionArchiveDisposition(StrEnum):
    """Database disposition selected by the fenced archive decision."""

    PHYSICAL_DELETE = "physical_delete"
    SOFT_ARCHIVED = "soft_archived"


class AuditAccessLogAuthority(Protocol):
    """Handle-free authority for audited transcript and workflow reads."""

    def record_audit_grade_view(
        self,
        *,
        session_id: str,
        requesting_principal: str,
        auth_provider_type: AuthProviderType,
        request_path: str,
        query_args: Mapping[str, str],
        ip_address: str | None,
    ) -> AuditAccessLogRecord: ...

    def record_workflow_inspect(
        self,
        *,
        session_id: str,
        state_id: str,
        requesting_principal: str,
        ip_address: str | None,
    ) -> AuditAccessLogRecord: ...


class RunDiagnosticsAuditMutationAuthority(Protocol):
    """Handle-free authority for run-diagnostics audit appends."""

    def append_audit_message(
        self,
        *,
        authority: RunDiagnosticsAuditAuthority,
        content: str,
        tool_calls: Sequence[Mapping[str, Any]] | None,
    ) -> ChatMessageRecord: ...

    def append_audit_messages(
        self,
        *,
        authority: RunDiagnosticsAuditAuthority,
        rows: Sequence[RunDiagnosticsAuditDraft],
    ) -> tuple[ChatMessageRecord, ...]: ...


class SessionOperationSessionMutations(Protocol):
    """Session-row mutations available inside one exact operation fence."""

    def assess_chargeable_operation(
        self, *, policy: ChargeableAdmissionPolicy, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision: ...

    def record_plugin_crash_breadcrumb(self) -> None: ...

    def mark_session_updated(self, *, updated_at: datetime) -> None: ...

    def set_title(self, *, title: str, updated_at: datetime) -> None: ...

    def record_composition_rejection(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        error_code: str | None,
        message: str,
        planner_payload: str,
        composition_state_id: str | None,
        created_at: datetime,
    ) -> None: ...

    def decide_and_soft_archive(
        self,
        *,
        archived_at: datetime,
    ) -> SessionArchiveDisposition: ...


class SessionOperationCompositionMutations(Protocol):
    """Composition-state mutations under one exact COMPOSE operation fence."""

    def append_state(
        self,
        creation: SessionCompositionStateCreation,
    ) -> CompositionStateRecord: ...


class SessionOperationInterpretationMutations(Protocol):
    """Interpretation audit mutations under one exact operation fence."""

    def create_or_reconcile_pending(
        self,
        command: SessionPendingInterpretationCommand,
        validator: SessionPendingInterpretationValidator,
    ) -> InterpretationEventRecord: ...

    # Settlement creates fresh candidate-bound evidence and returns any opt-out-produced state.
    def create_pipeline_candidate_pending(
        self,
        command: SessionPendingInterpretationCommand,
        validator: SessionPendingInterpretationValidator,
    ) -> PendingInterpretationCreationResult: ...

    def record_session_opt_out(
        self,
        *,
        event_id: UUID,
        actor: str,
        opted_out_at: datetime,
    ) -> tuple[InterpretationEventRecord, bool]: ...

    def record_auto_interpreted_no_surfaces_event(
        self,
        *,
        event_id: UUID,
        actor: str,
        kind: InterpretationKind,
        model_identifier: str,
        model_version: str,
        provider: str,
        composer_skill_hash: str,
        created_at: datetime,
    ) -> InterpretationEventRecord: ...

    def resolve_pending_event(
        self,
        *,
        event_id: UUID,
        choice: InterpretationChoice,
        accepted_value: str | None,
        resolved_at: datetime,
        actor: str,
        arguments_hash: str,
        hash_domain_version: str,
        runtime_model_identifier: str | None,
        runtime_model_version: str | None,
        approved_prompt_artifact_hash: str | None,
    ) -> None: ...


class SessionOperationRunMutations(Protocol):
    """Run mutations available inside one exact EXECUTE operation fence."""

    def check_approval_binding(self, *, state_id: UUID, approval: ApprovalGateInputs) -> AdmissionRefusalReason | None: ...

    def issue_start_permit(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    def assess_start_admission(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    def observe_start_permit_for_cleanup(self, *, run_id: UUID) -> RunStartPermitRecord: ...

    def complete_admission_refusal(self, *, run_id: UUID) -> None: ...

    def rebind_run_ownership(self, *, run_id: UUID) -> RunSagaState: ...

    def mark_recovery_outputs_finalized(self, *, run_id: UUID) -> None: ...

    def mark_recovery_required(self, *, run_id: UUID, reason: RecoveryRequiredReason) -> None: ...

    def append_terminal_run_event_once(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: SessionRunEventType,
        data: Mapping[str, Any],
    ) -> RunEventRecord: ...

    def create_pending_run(
        self,
        *,
        run_id: UUID,
        state_id: UUID,
        pipeline_yaml: str | None,
        started_at: datetime,
        execution_input: RunExecutionInput | None = None,
    ) -> RunRecord: ...

    def transition_run_status(
        self,
        *,
        run_id: UUID,
        status: SessionRunStatus,
        error: str | None,
        landscape_run_id: str | None,
        rows_processed: int | None,
        rows_succeeded: int | None,
        rows_failed: int | None,
        rows_routed_success: int | None,
        rows_routed_failure: int | None,
        rows_quarantined: int | None,
    ) -> None: ...

    def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: SessionRunEventType,
        data: Mapping[str, Any],
    ) -> RunEventRecord: ...

    def list_run_events_after(
        self,
        *,
        run_id: UUID,
        after_sequence: int,
    ) -> tuple[RunEventRecord, ...]: ...


class GlobalRunRecoveryAuthority(Protocol):
    """Handle-free authority for cross-session run recovery writes.

    Implementations own discovery, lock ordering, database-clock decisions,
    and compare-and-swap mutations. Callers receive immutable run snapshots;
    no database handle or connection-bearing callback crosses this boundary.
    """

    def cancel_orphaned_run_records(
        self,
        *,
        max_age_seconds: int | None,
        exclude_run_ids: frozenset[str],
        reason: str | None,
    ) -> tuple[RunRecord, ...]: ...

    def mark_landscape_reconciliation_outcomes(
        self,
        *,
        complete_run_ids: frozenset[UUID],
        absent_run_ids: frozenset[UUID],
    ) -> None: ...


class SessionOperationBlobMutations(Protocol):
    """Blob/run-custody mutations available inside one exact operation fence."""

    def read_blob(self, *, blob_id: UUID) -> BlobRecord: ...

    def prepare_blob_replacement(
        self,
        *,
        replacement_id: UUID,
        expected: BlobRecord,
        replacement: BlobRecord,
        staging_path: str,
        backup_path: str,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobReplacementPlan: ...

    def read_blob_replacement(self, *, blob_id: UUID) -> BlobReplacementPlan | None: ...

    def list_blob_replacements(self) -> tuple[BlobReplacementPlan, ...]: ...

    def mark_blob_replacement_staged(self, *, plan: BlobReplacementPlan) -> BlobReplacementPlan: ...

    def commit_blob_replacement(
        self,
        *,
        plan: BlobReplacementPlan,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobReplacementPlan: ...

    def retire_blob_replacement(self, *, plan: BlobReplacementPlan) -> bool: ...

    def abort_blob_replacement(self, *, plan: BlobReplacementPlan) -> bool: ...

    def reserve_pending_output_blob(self, *, record: BlobRecord) -> BlobRecord: ...

    def finalize_pending_output_blob(
        self,
        *,
        blob_id: UUID,
        status: Literal["ready", "error"],
        size_bytes: int | None,
        content_hash: str | None,
        max_storage_per_session: int,
    ) -> BlobRecord: ...

    def reserve_blob(
        self,
        *,
        record: BlobRecord,
        max_storage_per_session: int,
        idempotent: bool,
    ) -> bool: ...

    def mark_blob_ready(
        self,
        *,
        blob_id: UUID,
    ) -> BlobRecord: ...

    def discard_pending_blob(
        self,
        *,
        blob_id: UUID,
    ) -> bool: ...

    def list_abandoned_blob_reservations(self) -> tuple[BlobCreationObligation, ...]: ...

    def retire_abandoned_blob_reservation(self, *, obligation: BlobCreationObligation) -> bool: ...

    def prepare_blob_deletion(
        self,
        *,
        blob_id: UUID,
        tombstone_path: str,
        blob_snapshot_hash: str,
        expected_file_present: bool,
        expected_file_size: int | None,
        expected_file_hash: str | None,
        accepting_proposal_id: UUID | None,
    ) -> BlobDeletionPlan: ...

    def mark_blob_deletion_staged(self, *, plan: BlobDeletionPlan) -> BlobDeletionPlan: ...

    def commit_blob_deletion(
        self,
        *,
        plan: BlobDeletionPlan,
        accepting_proposal_id: UUID | None,
    ) -> BlobDeletionPlan: ...

    def read_blob_deletion(self, *, blob_id: UUID) -> BlobDeletionPlan | None: ...

    def read_atomic_blob_deletion(self, *, blob_id: UUID) -> BlobAtomicDeletionObligation | None: ...

    def retire_atomic_blob_deletion(self, *, obligation: BlobAtomicDeletionObligation) -> bool: ...

    def list_blob_deletions(self) -> tuple[BlobDeletionPlan, ...]: ...

    def retire_blob_deletion(self, *, plan: BlobDeletionPlan) -> bool: ...

    def abort_blob_deletion(self, *, plan: BlobDeletionPlan) -> bool: ...

    def insert_blob_run_link(
        self,
        *,
        blob_id: UUID,
        run_id: UUID,
        direction: BlobRunLinkDirection,
    ) -> bool: ...

    def list_blob_run_links(self, *, blob_id: UUID) -> tuple[BlobRunLinkRecord, ...]: ...

    def list_run_output_blobs(self, *, run_id: UUID) -> tuple[BlobRecord, ...]: ...

    def list_pending_run_output_blobs(self, *, run_id: UUID) -> tuple[BlobRecord, ...]: ...

    def mark_run_output_blob_ready(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
        size_bytes: int,
        content_hash: str,
        max_storage_per_session: int,
    ) -> BlobRecord: ...

    def mark_run_output_blob_error(
        self,
        *,
        run_id: UUID,
        blob_id: UUID,
    ) -> BlobRecord: ...

    def insert_blob_inline_resolutions(
        self,
        *,
        run_id: UUID,
        attempt: int,
        resolutions: Sequence[ResolvedBlobContent],
        resolved_at: datetime,
    ) -> None: ...


class SessionOperationComposerCompletionMutations(Protocol):
    """Completion-audit writes under one exact BLOB_READ operation fence."""

    def mark_ready_for_review(
        self,
        *,
        composition_state_id: UUID,
        actor: str,
        created_at: datetime,
        payload_digest: str,
        expires_at: datetime,
    ) -> None: ...

    def record_yaml_export(
        self,
        *,
        composition_state_id: UUID,
        actor: str,
        created_at: datetime,
    ) -> None: ...


class SessionOperationMutationTransaction(Protocol):
    """Read-only capability composition over one private fenced transaction."""

    @property
    def database_now(self) -> datetime: ...

    @property
    def session(self) -> SessionOperationSessionMutations: ...

    @property
    def composition_states(self) -> SessionOperationCompositionMutations: ...

    @property
    def interpretations(self) -> SessionOperationInterpretationMutations: ...

    @property
    def runs(self) -> SessionOperationRunMutations: ...

    @property
    def blobs(self) -> SessionOperationBlobMutations: ...

    @property
    def composer_completion(self) -> SessionOperationComposerCompletionMutations: ...


class SessionForkChildMutations(Protocol):
    """Child-session writes permitted during one atomic fork creation."""

    def insert_child_state(self, creation: SessionForkChildStateCreation) -> None: ...

    def append_child_messages(
        self,
        messages: tuple[SessionForkChildMessageCreation, ...],
    ) -> None: ...


class SessionForkParentReceiptMutations(Protocol):
    """Exact parent-receipt binding permitted during one atomic fork creation."""

    def bind_fork_receipt(
        self,
        *,
        originating_message_id: UUID,
    ) -> None: ...


@runtime_checkable
class SessionForkCreationTransaction(Protocol):
    """Pair-session transaction restricted to one durable fork staging cohort."""

    @property
    def child_mutations(self) -> SessionForkChildMutations: ...

    @property
    def parent_receipt_mutations(self) -> SessionForkParentReceiptMutations: ...

    def require_parent_fork_receipt(
        self,
        fence: OperationReceiptFence,
    ) -> tuple[Mapping[str, Any], datetime]: ...

    def read_parent_session(self) -> Any | None: ...

    def read_parent_message(self, message_id: UUID) -> Any | None: ...

    def read_parent_state(self, state_id: UUID) -> Any | None: ...

    def read_parent_ready_blobs(self) -> tuple[Any, ...]: ...

    def read_parent_blob_custody(self) -> tuple[Any, ...]: ...

    def read_parent_proposal(self, proposal_id: UUID) -> Any | None: ...

    def read_parent_proposal_creation_events(
        self,
        proposal_id: UUID,
    ) -> tuple[Any, ...]: ...

    def read_parent_proposal_rebase_events(
        self,
        proposal_id: UUID,
    ) -> tuple[Any, ...]: ...

    def count_parent_proposal_terminal_events(self, proposal_id: UUID) -> int: ...

    def read_child_snapshot(self) -> tuple[Any | None, tuple[Any, ...], Any | None]: ...


@runtime_checkable
class SessionOperationAuthority(Protocol):
    """Persistent per-session operation authority without database-handle leakage.

    Implementations own their transactions.  Callers receive only immutable
    records/operation contexts; a raw SQLAlchemy engine or connection is never
    part of the public authority surface.
    """

    def start_composer_async_operation(
        self,
        claim: ComposerOperationClaim,
        *,
        owner_instance_id: str,
        lease_seconds: int,
        auth_provider_type: str,
    ) -> SessionOperationContext:
        """Atomically bind one queued claim to a new COMPOSE fence."""
        ...

    def create_session_with_initial_fence(
        self,
        *,
        user_id: str,
        title: str,
        auth_provider_type: AuthProviderType,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionRecord: ...

    def acquire(
        self,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionOperationContext: ...

    def renew(
        self,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext: ...

    def compare_and_swap(self, context: SessionOperationContext) -> None: ...

    def validate_fork_child_lease(
        self,
        authority: SessionForkAuthority,
    ) -> SessionOperationContext: ...

    def renew_fork_child_lease(
        self,
        authority: SessionForkAuthority,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext: ...

    def reconcile_blob_reservation(
        self,
        context: SessionOperationContext,
        *,
        expected: BlobRecord,
    ) -> BlobRecord | None: ...

    def mutate[T](
        self,
        context: SessionOperationContext,
        mutation: Callable[[SessionOperationMutationTransaction], T],
    ) -> T: ...

    def release(self, context: SessionOperationContext) -> None: ...

    def archive_delete(self, context: SessionOperationContext) -> None: ...

    def reconcile_archive_delete(self, context: SessionOperationContext) -> ArchiveDeleteReconciliation: ...

    def archive_cleanup_is_consumed(self, session_id: UUID) -> bool:
        """Prove terminal absence without manufacturing authority from disk."""
        ...

    def classify_archive_manifest(
        self,
        current_context: SessionOperationContext,
        *,
        manifest_operation_id: UUID | str,
        manifest_operation_epoch: int,
    ) -> ArchiveManifestRelation: ...

    def mutate_fork_creation[T](
        self,
        parent_authority: SessionForkParentAuthority,
        child: SessionForkChildCreation,
        mutation: Callable[[SessionForkCreationTransaction, SessionForkAuthority], T],
    ) -> T: ...


class SessionServiceProtocol(Protocol):
    """Protocol for session persistence operations."""

    @property
    def session_operation_authority(self) -> SessionOperationAuthority: ...

    @property
    def session_operation_owner_instance_id(self) -> str: ...

    @property
    def session_operation_lease_seconds(self) -> int: ...

    async def create_session(
        self,
        user_id: str,
        title: str,
        auth_provider_type: AuthProviderType,
    ) -> SessionRecord: ...

    def get_session_for_stream(self, session_id: UUID) -> SessionRecord:
        """Synchronous scope read, dispatched with actual worker completion custody."""
        ...

    async def get_session(self, session_id: UUID) -> SessionRecord: ...

    async def reserve_operation_receipt(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: OperationReceiptKind,
        request_hash: str,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> OperationReceiptOutcome: ...

    async def get_operation_receipt(
        self, *, session_id: UUID, operation_id: str, kind: OperationReceiptKind, request_hash: str
    ) -> OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed | None: ...

    async def renew_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> OperationReceiptFence: ...

    async def bind_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        originating_message_id: UUID | None = None,
        result_session_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
    ) -> None: ...

    async def complete_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        result: OperationReceiptResult,
        response_hash: str,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> OperationReceiptCompleted: ...

    async def fail_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        failure_code: OperationReceiptFailureCode,
        actor: str,
        session_operation_context: SessionOperationContext,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> OperationReceiptFailed: ...

    async def revert_state_for_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        state_id: UUID,
        expected_current_state_id: UUID,
        expected_current_state_version: int,
        actor: str,
        response_hash_factory: Callable[[CompositionStateRecord], str],
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord: ...

    async def update_session_title(
        self,
        session_id: UUID,
        title: str,
        *,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> SessionRecord: ...

    async def list_sessions(
        self,
        user_id: str,
        auth_provider_type: AuthProviderType,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[SessionRecord]: ...

    async def archive_session(self, session_id: UUID) -> None: ...

    async def get_composer_preferences(
        self,
        session_id: UUID,
    ) -> ComposerSessionPreferencesRecord: ...

    async def update_composer_preferences(
        self,
        session_id: UUID,
        *,
        trust_mode: ComposerTrustMode,
        density_default: ComposerDensityDefault,
        actor: str,
    ) -> ComposerSessionPreferencesTransition: ...

    async def create_composition_proposal(
        self,
        *,
        session_id: UUID,
        tool_call_id: str,
        tool_name: str,
        summary: str,
        rationale: str,
        affects: Sequence[str],
        arguments_json: Mapping[str, Any],
        arguments_redacted_json: Mapping[str, Any],
        base_state_id: UUID | None,
        actor: str,
        user_message_id: UUID | None = None,
        composer_model_identifier: str | None = None,
        composer_model_version: str | None = None,
        composer_provider: str | None = None,
        composer_skill_hash: str | None = None,
        tool_arguments_hash: str | None = None,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord: ...

    async def create_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        plan: PipelinePlanResult,
        summary: str,
        rationale: str,
        affects: Sequence[str],
        arguments_redacted_json: Mapping[str, Any],
        actor: str,
        composer_model_identifier: str,
        composer_model_version: str,
        composer_provider: str,
        user_message_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> CompositionProposalRecord: ...

    async def create_pipeline_composition_proposal_finish_once(
        self,
        *,
        session_id: UUID,
        plan: PipelinePlanResult,
        summary: str,
        rationale: str,
        affects: Sequence[str],
        arguments_redacted_json: RedactedPipelineArguments,
        actor: str,
        composer_model_identifier: str,
        composer_model_version: str,
        composer_provider: str,
        user_message_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket,
        running: ComposerOperationRunning | None = None,
    ) -> PipelineCreationFinishOnce: ...

    async def reject_pipeline_composition_proposal_finish_once(
        self,
        *,
        expected: PipelineRejectionExpected,
        coordinator: RequiredWorkCoordinator,
        rejection_work: RequiredWorkTicket,
        rejection_projection_work: RequiredWorkTicket,
        transition_ordinal: int,
        semantic_ordinal: int,
    ) -> PipelineRejectionFinishOnce: ...

    async def get_authoritative_pipeline_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        required_work: RequiredWorkTicket | None = None,
    ) -> AuthoritativePipelineProposal: ...

    async def get_authoritative_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        required_work: RequiredWorkTicket | None = None,
    ) -> AuthoritativeCompositionProposal: ...

    async def settle_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        state: CompositionStateData,
        candidate_content_hash: str,
        executor_content_hash: str,
        final_composer_metadata: Mapping[str, Any] | None,
        dispatch: PipelineDispatchAuditBinding,
        actor: str,
        transition_assistant: TransitionAssistantDraft | None = None,
        required_trust_mode: ComposerTrustMode | None = None,
        session_operation_context: SessionOperationContext,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...] = (),
        running: ComposerOperationRunning | None = None,
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineProposalSettlementResult: ...

    async def settle_pipeline_composition_proposal_finish_once(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        state: CompositionStateData,
        candidate_content_hash: str,
        executor_content_hash: str,
        final_composer_metadata: Mapping[str, Any] | None,
        dispatch: PipelineDispatchAuditBinding,
        actor: str,
        session_operation_context: SessionOperationContext,
        coordinator: RequiredWorkCoordinator,
        required_work: RequiredWorkTicket,
        publication_projection_work: RequiredWorkTicket,
        revocation_required_work: RequiredWorkTicket,
        revocation_projection_work: RequiredWorkTicket,
        transition_assistant: TransitionAssistantDraft | None = None,
        required_trust_mode: ComposerTrustMode | None = None,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...] = (),
        running: ComposerOperationRunning | None = None,
    ) -> ComposerPipelineFinishOnce: ...

    async def replay_pipeline_composition_proposal(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...],
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineProposalSettlementResult: ...

    async def get_pipeline_dispatch_recovery(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineDispatchRecovery | None: ...

    async def reject_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        reason: PipelineProposalRejectionReason,
        dispatch: PipelineDispatchAuditBinding | None,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord: ...

    async def list_composition_proposals(
        self,
        session_id: UUID,
        *,
        status: ProposalLifecycleStatus | None = None,
    ) -> list[CompositionProposalRecord]: ...

    async def reject_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord: ...

    async def accept_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        expected_current_state_id: UUID | None,
        state: CompositionStateData | None,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord: ...

    async def has_applied_blob_proposal_effect(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        session_operation_context: SessionOperationContext,
    ) -> bool: ...

    async def list_proposal_events(
        self,
        session_id: UUID,
    ) -> list[ProposalEventRecord]: ...

    async def create_pending_interpretation_event(
        self,
        *,
        session_id: UUID,
        composition_state_id: UUID,
        affected_node_id: str,
        tool_call_id: str,
        user_term: str,
        kind: InterpretationKind,
        llm_draft: str,
        model_identifier: str | None,
        model_version: str | None,
        provider: str | None,
        composer_skill_hash: str | None,
        session_operation_context: SessionOperationContext,
        created_at: datetime | None = None,
        surface_origin: InterpretationSurfaceOrigin = InterpretationSurfaceOrigin.COMPOSER_LLM,
    ) -> InterpretationEventRecord:
        """Insert a PENDING interpretation event.

        ``surface_origin`` defaults to the composer LLM because that default
        cannot mis-state provenance: the writer rejects ``composer_llm``
        without all four LLM provenance fields, and rejects every server-route
        origin that carries any of them.

        ``kind`` must be supplied explicitly by the caller. Implementations
        MUST validate the affected component in the parent composition state
        before INSERT (writer-boundary check per the engine-patterns-reference
        skill §Offensive Programming Examples): ``invented_source`` targets
        the synthetic ``source`` component and requires persisted
        source-authoring metadata;
        ``pipeline_decision`` targets the node that implements the reviewed
        shape decision; prompt/vague transform kinds target real LLM nodes in
        ``composition_states.nodes``. Raises
        ``ValueError`` on a missing state, malformed target, unknown node, or
        non-``InterpretationKind`` kind.
        """
        ...

    async def resolve_interpretation_event(
        self,
        *,
        session_id: UUID,
        event_id: UUID,
        choice: InterpretationChoice,
        amended_value: str | None,
        actor: str,
        resolved_at: datetime | None = None,
        runtime_model_identifier: str | None = None,
        runtime_model_version: str | None = None,
        session_operation_context: SessionOperationContext,
    ) -> tuple[InterpretationEventRecord, CompositionStateRecord]:
        """Commit a resolution and update the affected interpretation surface.

        F-14: ``accepted_value`` is computed internally — implementations
        read ``llm_draft`` from the pending row when ``choice`` is
        ``ACCEPTED_AS_DRAFTED``, and use ``amended_value`` when ``choice``
        is ``AMENDED``.

        Single transaction. Raises ``ValueError`` for no pending event
        (TOCTOU / IDOR), missing composition state or affected node,
        or any prompt-template patch failure.
        """
        ...

    async def list_interpretation_events(
        self,
        session_id: UUID,
        *,
        status: Literal["pending", "all"] = "all",
        composition_state_id: UUID | None = None,
        sources: Sequence[InterpretationSource] | None = None,
    ) -> list[InterpretationEventRecord]:
        """Read-back of interpretation events for the session.

        Returns rows ordered by ``created_at, id``. ``status='pending'``
        filters to ``choice='pending'`` rows; ``status='all'`` returns
        every row.

        ``sources``: when set, filters to rows whose
        ``interpretation_source`` is in the supplied sequence. Used by
        the opt-out audit-summary surface
        (``GET /interpretations/opt_out_summary``) to retrieve only
        ``auto_interpreted_opt_out`` and ``auto_interpreted_no_surfaces``
        rows. ``None`` (default) imposes no source filter.
        """
        ...

    async def record_session_interpretation_opt_out(
        self,
        *,
        session_id: UUID,
        actor: str,
        session_operation_context: SessionOperationContext,
        opted_out_at: datetime | None = None,
    ) -> InterpretationEventRecord:
        """Mark the session as 'don't surface interpretations any more'.

        F-29: idempotent. If an opted-out row already exists for this
        session, returns the existing record without inserting a duplicate;
        the sessions boolean stays true. Atomic single transaction
        (interpretation_events INSERT + sessions UPDATE inside one
        write lock).
        """
        ...

    async def upsert_skill_markdown_history(
        self,
        *,
        skill_hash: str,
        filename: str,
        content: str,
    ) -> bool:
        """Best-effort INSERT-OR-IGNORE into ``skill_markdown_history`` (F-5c).

        Captures the exact composer-skill markdown text the LLM was
        prompted with so a forensic auditor can reconstruct it from the
        ``composer_skill_hash`` recorded on later interpretation event
        rows. Hash is the primary key; subsequent calls with the same
        hash are no-ops.

        Returns ``True`` when a row was inserted, ``False`` when it
        already existed. Best-effort only — NOT transactional with the
        interpretation-event row write.
        """
        ...

    async def record_auto_interpreted_no_surfaces_event(
        self,
        *,
        session_id: UUID,
        actor: str,
        kind: InterpretationKind,
        model_identifier: str,
        model_version: str,
        provider: str,
        composer_skill_hash: str,
        session_operation_context: SessionOperationContext,
        created_at: datetime | None = None,
    ) -> InterpretationEventRecord:
        """Write an AUTO_INTERPRETED_NO_SURFACES row (Phase 5b Task 5, F-6).

        Called by the compose loop when the per-term or per-day rate cap
        is hit for ``request_interpretation_review``: the LLM is expected
        to fall back to baking the interpretation directly into the prompt
        template without surfacing it for review. This writer records the
        fact in the audit trail so an auditor can distinguish "user opted
        out" from "rate cap exhausted" via ``interpretation_source``.

        Row shape (see ``ck_interpretation_events_no_surfaces_shape``):
        * ``interpretation_source = 'auto_interpreted_no_surfaces'``
        * ``choice = 'opted_out'`` (semantics: resolved-at-write — there
          is no pending surface to acknowledge)
        * Interpretation-surface fields are NULL: ``composition_state_id``,
          ``affected_node_id``, ``tool_call_id``, ``user_term``,
          ``llm_draft`` (the rejected request never produced a surface).
        * ``kind`` and LLM provenance fields MUST be populated — the composer LLM
          that triggered the rate cap is fully identifiable from the
          compose-loop snapshot.
        * ``arguments_hash`` is NULL because no user-visible surface was
          created to resolve.
        * ``resolved_at`` equals ``created_at`` (the rate-cap event is
          itself a resolution).
        """
        ...

    async def add_message(
        self,
        session_id: UUID,
        role: ChatMessageRole,
        content: str,
        *,
        writer_principal: ChatMessageWriterPrincipal,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
        composition_state_id: UUID | None = None,
        raw_content: str | None = None,
        tool_call_id: str | None = None,
        parent_assistant_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        session_operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
    ) -> ChatMessageRecord: ...

    async def complete_composer_async_operation(
        self,
        running: ComposerOperationRunning,
        *,
        assistant: ComposerOperationAssistantWrite | None,
        assistant_record: ChatMessageRecord | None,
        audit_cohort: tuple[AuditMessageDraft, ...],
        audit_composition_state_id: UUID | None,
        build_response: Callable[
            [ChatMessageRecord, tuple[CompositionProposalRecord, ...], CompositionStateRecord | None], MessageWithStateResponse
        ],
        required_work: RequiredWorkCoordinator | None = None,
    ) -> ComposerOperationRecord: ...

    async def fail_composer_async_operation(
        self,
        running: ComposerOperationRunning,
        *,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
        required_work: RequiredWorkCoordinator | None = None,
        failure_projection_work: RequiredWorkTicket | None = None,
    ) -> ComposerOperationRecord: ...

    async def add_messages_atomic(
        self,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        *,
        writer_principal: ChatMessageWriterPrincipal,
        composition_state_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        session_operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
        audit_only: bool = False,
        required_work: RequiredWorkTicket | None = None,
    ) -> None:
        """Persist one audit cohort all-or-nothing (elspeth-90231248dc).

        Implementations MUST commit every draft in a single transaction
        under the session write lock with a contiguous sequence block —
        a mid-cohort failure must leave zero rows durable, never a
        prefix. A draft's ``composition_state_id`` overrides the
        cohort-level value for that row (``None`` falls back to it), and
        every distinct effective state id MUST be verified against the
        session before any insert. An empty ``drafts`` sequence is a
        no-op.
        """
        ...

    async def add_run_diagnostics_audit_message(
        self,
        authority: RunDiagnosticsAuditAuthority,
        content: str,
        *,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatMessageRecord:
        """Append one ``role=audit`` row under run-diagnostics authority.

        The row's ``writer_principal`` (``run_diagnostics``) and
        ``composition_state_id`` (``authority.state_id``) are derived from
        the authority, never caller-supplied. Implementations MUST
        re-prove the authority durably inside the same locked
        transaction as the insert — run row present with exactly the
        authority's session/state binding, session present and not
        archived — and MUST raise
        :class:`RunDiagnosticsAuthorityLostError` without consuming a
        chat sequence number when the proof fails.
        """
        ...

    async def add_run_diagnostics_audit_messages_atomic(
        self,
        authority: RunDiagnosticsAuditAuthority,
        drafts: Sequence[RunDiagnosticsAuditDraft],
    ) -> tuple[ChatMessageRecord, ...]:
        """Append one run-diagnostics audit cohort all-or-nothing.

        Cohort sibling of :meth:`add_run_diagnostics_audit_message`
        (elspeth-90231248dc): implementations MUST prove the authority
        once and commit every draft in the same locked transaction with
        a contiguous sequence block — a mid-cohort failure or lost
        authority must leave zero rows durable, never a prefix. An empty
        ``drafts`` sequence is a no-op.
        """
        ...

    async def get_messages(
        self,
        session_id: UUID,
        limit: int | None = 100,
        offset: int = 0,
        *,
        required_work: RequiredWorkTicket | None = None,
    ) -> list[ChatMessageRecord]: ...

    async def add_message_with_transcript(
        self,
        session_id: UUID,
        role: ChatMessageRole,
        content: str,
        *,
        operation_id: UUID,
        requested_state_id: UUID | None,
        writer_principal: ChatMessageWriterPrincipal,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
        composition_state_id: UUID | None = None,
        raw_content: str | None = None,
        tool_call_id: str | None = None,
        parent_assistant_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        running: ComposerOperationRunning,
        required_work: RequiredWorkTicket | None = None,
    ) -> MessageIngressFresh:
        """Accept a user message once and return a nominal admission result.

        The insert and the transcript read MUST happen inside one
        write-locked transaction on one connection, so the returned
        transcript ends at the inserted row by construction. Callers that
        need "the transcript this write belongs to" (the freeform
        send_message snapshot guard) MUST use this method instead of an
        ``add_message`` + ``get_messages`` pair — the split pair reads on
        a different pooled connection and a stale reader turns the Tier-1
        snapshot guard into a false 500. Exact receipt replays
        do not read a transcript or imply composition completion.
        """
        ...

    async def count_tool_responses_for_assistant_async(
        self,
        *,
        session_id: str,
        assistant_message_id: str | None,
    ) -> int:
        """Count persisted tool rows linked to an assistant message."""
        ...

    async def record_audit_grade_view_async(
        self,
        *,
        session_id: str,
        requesting_principal: str,
        auth_provider_type: AuthProviderType,
        request_path: str,
        query_args: Mapping[str, str],
        ip_address: str | None,
    ) -> None:
        """Append one audit_access_log row before exposing tool rows."""
        ...

    async def save_composition_state(
        self,
        session_id: UUID,
        state: CompositionStateData,
        *,
        provenance: CompositionStateProvenance,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> CompositionStateRecord:
        """Save a new immutable composition state snapshot.

        A live, exact COMPOSE ``SessionOperationContext`` is required and
        must be validated transactionally before any state write.

        ``provenance`` MUST be one of the values enumerated by the
        ``ck_composition_states_provenance`` CHECK constraint and the
        :data:`CompositionStateProvenance` Literal. It records WHY this row
        was written and is the load-bearing discriminator for the
        backward-direction INV-AUDIT-AHEAD invariant (§4.1.2). Implementations
        MUST persist the value verbatim — no defaulting, no coercion: a
        confident wrong attribution is evidence-tampering-class harm under
        the auditability standard.
        """
        ...

    async def save_composition_state_with_interpretations(
        self,
        session_id: UUID,
        state: CompositionStateData,
        *,
        provenance: CompositionStateProvenance,
        interpretations: tuple[PreparedInterpretationEventDraft, ...],
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord:
        """Atomically save one state and all review events it requires.

        Implementations must return the final session head because an opted-
        out session may auto-resolve a prepared event and derive a newer state
        inside the settlement transaction.
        """
        ...

    async def commit_composition_response(
        self,
        *,
        session_id: UUID,
        expected_current_state_id: UUID | None,
        state: CompositionStateData,
        assistant_content: str,
        raw_content: str | None,
        session_operation_context: SessionOperationContext,
    ) -> TransitionResponseSettlement:
        """Atomically persist one post-compose state and its response.

        The exact live COMPOSE authority is validated in the same database
        transaction as both rows, so takeover commits neither half.
        """
        ...

    async def get_current_state(
        self,
        session_id: UUID,
        *,
        required_work: RequiredWorkTicket | None = None,
    ) -> CompositionStateRecord | None: ...

    async def get_state(self, state_id: UUID, *, required_work: RequiredWorkTicket | None = None) -> CompositionStateRecord: ...

    async def get_state_in_session(
        self,
        state_id: UUID,
        session_id: UUID,
    ) -> CompositionStateRecord:
        """Fetch a composition state with a session-scope invariant check.

        Migration 007 added a composite FK ``(state_id, session_id)`` on
        tables that reference ``composition_states``, which prevents
        *future* cross-session state references at the schema layer. This
        method is the runtime defence-in-depth for pre-007 rows repaired
        with Variant-A (delete orphans) — and for any future code path
        that acquires a ``state_id`` indirectly (e.g. via a
        ``RunRecord.state_id`` carried through the fork lineage) and then
        resolves it inside a session-scoped handler.

        Implementations MUST raise ``AuditIntegrityError`` when the
        resolved state's ``session_id`` does not match the caller-supplied
        ``session_id``. That is a Tier 1 audit anomaly: the state was
        reachable from a run but does not belong to the session hosting
        the run. Silent coercion or a soft 404 would produce a confident
        wrong answer — exactly the pattern
        docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust
        Model forbids for our own data. Raises ``ValueError`` when the state
        does not exist at all, consistent with ``get_state``.
        """
        ...

    async def get_state_versions(
        self,
        session_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompositionStateRecord]: ...

    async def get_state_version_numbers(
        self,
        session_id: UUID,
    ) -> dict[str, int]:
        """Map composition-state id → version for one session.

        Lean projection (id/version columns only) for callers that need to
        resolve state ids to version numbers without hydrating full state
        rows — e.g. the messages route's per-tool-call outcome stamping
        (elspeth-f5e6723133).
        """
        ...

    async def list_composition_rejection_events(
        self,
        session_id: UUID,
    ) -> tuple[CompositionRejectionEventRecord, ...]:
        """Refused composer tool-call reasons for one session (elspeth-3e28029d2f).

        Read side of ``composition_rejection_events`` for the audit-grade
        messages view (``include_rejection_reasons``). Ordered by
        ``created_at`` then ``id``. Never selects ``planner_payload``.
        """
        ...

    async def create_run(
        self,
        session_id: UUID,
        state_id: UUID,
        pipeline_yaml: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
        execution_input: RunExecutionInput | None = None,
    ) -> RunRecord: ...

    async def check_approval_binding(
        self,
        session_id: UUID,
        state_id: UUID,
        *,
        approval: ApprovalGateInputs,
        session_operation_context: SessionOperationContext,
    ) -> AdmissionRefusalReason | None: ...

    async def issue_run_start_permit(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    async def assess_run_start_admission(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    async def observe_run_start_permit_for_cleanup(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext
    ) -> RunStartPermitRecord: ...

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision: ...

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        """Charge auto-title (COMPOSE) or run (EXECUTE) provider calls to the session owner's token ledger."""

    async def begin_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None = None,
        required_work: RequiredWorkTicket | None = None,
    ) -> ProviderAttempt:
        """Admit and persist pending provider evidence before dispatch."""

    def begin_run_provider_attempt_sync(self, *, session_operation_context: SessionOperationContext, run_id: UUID) -> ProviderAttempt:
        """Return a committed EXECUTE attempt before the pipeline enters its provider."""

    async def finish_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        call: ComposerLLMCall,
        required_work: RequiredWorkTicket | None = None,
    ) -> None:
        """Checkpoint terminal provider audit and settle its ledger atomically."""

    async def cancel_undispatched_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        attempt_id: str,
        requested_model: str,
        required_work: RequiredWorkTicket | None = None,
    ) -> None:
        """Close a proven undispatched COMPOSE intent under its original fence."""

    async def settle_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        """Settle non-Composer provider evidence under COMPOSE or EXECUTE authority."""

    def settle_run_provider_attempt_sync(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        """Commit an EXECUTE attempt's exact Landscape usage before return."""

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
    ) -> RunRecord: ...

    async def get_run_execution_input(self, run_id: UUID) -> RunExecutionInput | None: ...

    async def list_recoverable_run_records(self) -> tuple[RunRecord, ...]: ...

    async def get_run(self, run_id: UUID) -> RunRecord: ...

    async def list_runs_for_session(self, session_id: UUID) -> list[RunRecord]: ...

    async def update_run_status(
        self,
        run_id: UUID,
        status: SessionRunStatus,
        error: str | None = None,
        landscape_run_id: str | None = None,
        rows_processed: int | None = None,
        rows_succeeded: int | None = None,
        rows_failed: int | None = None,
        rows_routed_success: int | None = None,
        rows_routed_failure: int | None = None,
        rows_quarantined: int | None = None,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Update a run's status and metadata.

        Transitions MUST comply with LEGAL_RUN_TRANSITIONS.

        landscape_run_id is write-once: once set to a non-None value,
        subsequent calls MUST NOT overwrite it. Implementations MUST
        raise ValueError if landscape_run_id is provided but the run
        already has one set.
        """
        ...

    async def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: SessionRunEventType,
        data: Mapping[str, Any],
        session_operation_context: SessionOperationContext,
    ) -> RunEventRecord:
        """Append a structured execution event for replay/audit."""
        ...

    async def list_run_events(self, run_id: UUID) -> list[RunEventRecord]:
        """Return persisted execution events for a run in event order."""
        ...

    async def record_blob_inline_resolutions(
        self,
        *,
        run_id: UUID,
        resolutions: Sequence[ResolvedBlobContent],
        attempt: int = 1,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Write audit rows for inline-content blob refs before plugin construction."""
        ...

    async def get_active_run(
        self,
        session_id: UUID,
    ) -> RunRecord | None: ...

    async def fork_session(
        self,
        authority: SessionForkParentAuthority,
        *,
        fork_message_id: UUID,
        new_message_content: str,
    ) -> StagedForkSession:
        """Stage or resume the exact child bound to a fork operation.

        The child remains archived until ``settle_fork_operation_receipt``.
        """
        ...

    async def settle_fork_operation_receipt(
        self,
        command: SessionForkSettlementCommand,
    ) -> SessionRecord:
        """Atomically rewrite, activate, and settle one staged fork child."""
        ...

    async def fail_fork_operation_receipt(
        self,
        authority: SessionForkAuthority,
        *,
        failure_code: OperationReceiptFailureCode,
        actor: str,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> OperationReceiptFailed: ...

    async def cancel_all_orphaned_runs(
        self,
        max_age_seconds: int | None = None,
        exclude_run_ids: frozenset[str] = frozenset(),
        reason: str | None = None,
    ) -> int:
        """Force-cancel orphaned runs across all sessions.

        Called on startup to recover sessions blocked by runs orphaned
        during a previous server crash. Returns the count of cancelled runs.

        Args:
            max_age_seconds: Only cancel runs older than this. None cancels
                all non-terminal runs (correct for single-process servers
                where every non-terminal run is orphaned after restart).
            exclude_run_ids: Run IDs known to have active executor threads.
                These are skipped even if they exceed max_age_seconds.
            reason: Written to the error column so operators can distinguish
                orphan-cleanup cancellations from user cancellations.
        """
        ...

    async def cancel_all_orphaned_run_records(
        self,
        max_age_seconds: int | None = None,
        exclude_run_ids: frozenset[str] = frozenset(),
        reason: str | None = None,
    ) -> list[RunRecord]:
        """Force-cancel orphaned runs and return the cancelled records.

        Used by app-level startup reconciliation to terminalize matching
        Landscape audit rows via each record's ``landscape_run_id``.
        """
        ...

    async def list_pending_landscape_reconciliations(self) -> list[RunRecord]:
        """Return cancelled runs whose error ends in the exact pending marker."""
        ...

    async def mark_landscape_reconciliation_outcomes(
        self,
        *,
        complete_run_ids: frozenset[UUID],
        absent_run_ids: frozenset[UUID],
    ) -> None:
        """Atomically replace exact pending suffixes with closed outcomes."""
        ...

    async def persist_compose_turn_async(
        self,
        *,
        session_id: str,
        assistant_content: str,
        raw_content: str | None = None,
        redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...],
        redacted_tool_rows: tuple[Any, ...],
        rejection_records: tuple[Any, ...] = (),
        parent_composition_state_id: str | None,
        expected_current_state_id: str | None,
        writer_principal: ChatMessageWriterPrincipal,
        plugin_crash_pending: bool,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> Any:
        """Persist one compose turn (assistant + tool rows + per-tool
        composition states) atomically.

        Spec §5.2.2. The async dispatcher; the underlying sync work runs
        in a worker thread under ``asyncio.shield`` (commit-wins
        cancellation contract — see ``SessionServiceImpl
        .persist_compose_turn_async``).

        Raises :class:`StaleComposeStateError` when the session's current
        composition state changed between the LLM call and the persist
        attempt. Raises :class:`ToolCallIDMismatchError` when the
        assistant ``tool_calls`` IDs and the tool rows'
        ``tool_call_id`` values are not the same unique set.
        """
        ...
