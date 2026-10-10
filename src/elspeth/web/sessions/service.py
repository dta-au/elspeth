"""SessionService implementation -- CRUD, state versioning, active run enforcement.

Uses SQLAlchemy Core with a synchronous engine. Database calls run in a
thread pool executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
from uuid import UUID

import structlog
from opentelemetry import metrics
from pydantic import ValidationError
from sqlalchemy import ColumnElement, Connection, Engine, case, delete, desc, exists, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import BlobForkPlanEntry, BlobRecord, fork_blob_id
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    ChargeableAdmissionRefused,
    ChargeableOperation,
)
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
    InterpretationSurfaceOrigin,
)
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.async_workers import (
    run_required_sql_finish_once,
    run_required_sql_in_worker,
    run_stream_read_in_worker,
    run_sync_in_worker,
)
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PresentBase,
    composition_content_hash,
    is_owned_composition_state_authority,
    owned_composition_state_review_arguments,
)
from elspeth.web.composer.provider_telemetry import (
    record_settled_composer_audit_message,
)
from elspeth.web.composer.redaction import (
    redact_tool_call_arguments,
    semantic_redacted_pipeline_arguments_hash,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

# Phase 8 cohort-emit helper (Sub-task 7e — B3 cohort b1). The opt-out
# audit row is committed inside ``record_session_interpretation_opt_out``
# below, and the helper must fire only on the INSERT path (not the F-29
# idempotent re-fire). The helper module lives under ``web/composer``
# per project plan; the sessions→composer import direction follows the
# precedent set by ``_auto_title.py`` and
# ``converters.py``. The W5 try/except wrap inside the helper preserves
# the audit-primacy rule (a broken OTel exporter must not 500 a POST
# whose audit row already wrote).
from elspeth.web.composer.telemetry_phase8 import record_interpretation_opt_out
from elspeth.web.coordination.approval_authority import (
    ApprovalGateInputs,
    ApprovalSupersession,
    refuse_unrecorded_approval_supersession,
    supersede_open_approvals,
)
from elspeth.web.coordination.composer_operation_authority import (
    ComposerAsyncOperationAuthority,
    _read,
    _record_from_row,
    _select_failure_on_connection,
    bind_composer_operation_user_message_on_connection,
    derive_proposal_composer_binding_on_connection,
    prove_revocation_composer_binding_on_connection,
    require_composer_operation_mutation_on_connection,
    require_composer_settlement_actor_on_connection,
    settle_composer_operation_on_connection,
    verify_historical_proposal_composer_binding_on_connection,
)
from elspeth.web.coordination.contracts import (
    ArchiveManifestRelation,
    CancellationSource,
    FenceLossReason,
    RecoveryRequiredReason,
    RunSagaState,
    SessionOperationContext,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import (
    ProviderAttempt,
    QuotaExceeded,
    TokenUsageEntry,
    TokenUsageSource,
    begin_provider_attempt_on_connection,
    cancel_undispatched_provider_attempt_on_connection,
    llm_call_usage_entries,
    record_token_usage_on_connection,
    settle_provider_attempt_on_connection,
)
from elspeth.web.coordination.repository import (
    PostgresSessionOperationRepository,
    SessionDerivedCustodyError,
    _ForkCreationTransaction,
    _RepositoryInterpretationMutations,
    _RepositoryMutationState,
    _RepositorySessionMutations,
)
from elspeth.web.coordination.run_cancellation_authority import RepositoryRunCancellationAuthority
from elspeth.web.coordination.run_diagnostics_authority import RepositoryRunDiagnosticsAuditAuthority
from elspeth.web.coordination.run_recovery_authority import RepositoryGlobalRunRecoveryAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    PublicationProjectionDisposition,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    RequiredWorkTicket,
)
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions._persist_payload import AuditMessageDraft, AuditOutcome, RedactedToolRow, RejectionRecord, StatePayload
from elspeth.web.sessions.archive_quarantine import (
    ArchiveQuarantineIdentity,
    archive_quarantine_operation_ids,
    archive_quarantine_session_ids,
    canonical_archive_present,
    list_archive_quarantine_manifests,
    prepare_archive_quarantine,
    purge_archive_quarantine,
    restore_archive_quarantine,
    retire_archive_quarantine,
    stage_archive_quarantine,
)
from elspeth.web.sessions.audit_checkpoint import uncheckpointed_envelopes
from elspeth.web.sessions.composer_operations import (
    ComposerOperationAssistantWrite,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationRecord,
    ComposerOperationRunning,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.dead_site_supersession import supersede_dead_site_pending_interpretation_events
from elspeth.web.sessions.fork_custody import (
    _fork_blob_plan_content,
    _fork_blob_plan_from_content,
    _fork_blob_plan_identity_from_content,
    _refuse_unrewritable_fork_custody,
    _verify_fork_settlement_blob_custody,
)
from elspeth.web.sessions.inline_blob_preflight import InlinePreflightState, SessionInlineBlobSnapshot, prepare_session_inline_blob_snapshot
from elspeth.web.sessions.interpretation_validation import (
    SessionInterpretationValidationInputs,
    build_interpretation_validation_inputs,
    validate_composition_state_with_interpretation_inputs,
)
from elspeth.web.sessions.locking import (
    _blob_custody_session_lock,
    acquire_session_advisory_xact_lock,
    process_session_lock,
    sqlite_session_mutex,
    sqlite_transaction_session_lock,
    try_blob_custody_session_lock,
)
from elspeth.web.sessions.models import (
    audit_access_log_table,
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_rejection_events_table,
    composition_states_table,
    interpretation_events_table,
    message_ingress_receipts_table,
    proposal_events_table,
    run_events_table,
    run_execution_inputs_table,
    runs_table,
    session_operation_fences_table,
    session_operation_receipts_table,
    sessions_table,
)
from elspeth.web.sessions.mutation_capabilities import _SessionComposerMutationTransaction
from elspeth.web.sessions.operation_receipts import (
    bind_operation_receipt,
    read_operation_receipt,
    renew_operation_receipt,
    require_live_operation_receipt,
    reserve_operation_receipt,
    settle_operation_receipt,
)
from elspeth.web.sessions.pending_interpretation import (
    SessionRuntimePreflight,
    _interpretation_hash_domain_v2,
    _PreparedPendingInterpretation,
    _resolve_invented_source,
    _resolve_model_choice_review,
    _resolve_pipeline_decision_review,
    _resolve_prompt_template_review,
    _resolve_source_data_contract,
    _resolve_vague_term,
    _reviewed_content_identity,
    _SessionPendingInterpretationValidator,
    _source_data_contract_demand_from_state_record,
    _surfacing_prompt_structure_hash,
)
from elspeth.web.sessions.pending_interpretation import (
    _patch_llm_transform_prompt as _patch_llm_transform_prompt,
)
from elspeth.web.sessions.pipeline_finish_once import (
    ComposerPipelineBusinessReturned,
    ComposerPipelineFinishOnce,
    ComposerPipelineRaised,
    ComposerPipelineRevocationCompleted,
    ComposerRevocationExpected,
    ComposerRevocationSQLResult,
    PipelineFinishOnceWork,
    PipelinePublicationSQLResult,
    _ComposerRevocationRequired,
    decode_composer_revocation_result,
)
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected, PipelineRejectionSQLResult, PipelineRejectionWriteResult
from elspeth.web.sessions.pipeline_rejection_finish_once import (
    PipelineCreationFinishOnce,
    PipelineCreationRaised,
    PipelineCreationReturned,
    PipelineRejectionFinishOnce,
    PipelineRejectionRaised,
    PipelineRejectionReturned,
)
from elspeth.web.sessions.pipeline_review_evidence import (
    review_cohort_member,
    review_semantic_material,
    verify_opt_out_transformation,
    verify_review_event_material,
)
from elspeth.web.sessions.pipeline_settlement_payloads import (
    ComposerRevocationEvidence,
    PipelineAcceptedEvidence,
    PipelineDispatchEvidence,
    ReviewCohortMember,
    TransitionAssistantBinding,
)
from elspeth.web.sessions.proposal_authority import (
    _assert_assistant_row_has_audit_content,
    _assert_parent_assistant_message,
    _assert_state_in_session,
    _classify_authoritative_composition_proposal,
    _composition_state_data_content_hash,
    _normalize_proposal_composer_provenance,
    _persisted_pipeline_dispatch_content_hashes,
    _pipeline_accepted_payload,
    _pipeline_created_payload,
    _pipeline_dispatch_recovery_from_envelope,
    _pipeline_public_metadata,
    _pipeline_rejected_payload,
    _proposal_event_record_from_row,
    _proposal_record_from_row,
    _restore_authoritative_pipeline_proposal,
    _validate_tool_call_id_set_equality,
    _validated_pipeline_rejection_reason,
    _verify_pipeline_lifecycle_authority,
)
from elspeth.web.sessions.protocol import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    COMPOSER_TRUST_MODE_VALUES,
    SESSION_RUN_EVENT_TYPE_VALUES,
    SESSION_TERMINAL_RUN_STATUS_VALUES,
    AuditAccessLogAuthority,
    AuditAccessLogRecord,
    AuditAccessLogWriteError,
    AuthoritativeCompositionProposal,
    AuthoritativePipelineProposal,
    ChatMessageRecord,
    ChatMessageRole,
    ChatMessageWriterPrincipal,
    ComposerDensityDefault,
    ComposerSessionPreferencesRecord,
    ComposerSessionPreferencesTransition,
    ComposerTrustMode,
    CompositionProposalRecord,
    CompositionRejectionEventRecord,
    CompositionStateData,
    CompositionStateProvenance,
    CompositionStateRecord,
    CompositionValidationError,
    GlobalRunRecoveryAuthority,
    InterpretationEventAlreadyResolvedError,
    InterpretationEventNotFoundError,
    InterpretationPlaceholderConsumedError,
    InterpretationSourceDataContractDriftError,
    InterpretationUnsupportedChoiceError,
    MessageIngressFresh,
    OperationReceiptActive,
    OperationReceiptCompleted,
    OperationReceiptFailed,
    OperationReceiptFailureCode,
    OperationReceiptFence,
    OperationReceiptFenceLostError,
    OperationReceiptKind,
    OperationReceiptOutcome,
    OperationReceiptResult,
    OperationReceiptSettlementConflictError,
    PipelineDispatchRecovery,
    PipelineProposalRejectionReason,
    PipelineProposalSettlementResult,
    PreparedInterpretationEventDraft,
    ProposalEventRecord,
    ProposalLifecycleStatus,
    ProposalStateConflictError,
    RedactedPipelineArguments,
    RunDiagnosticsAuditAuthority,
    RunDiagnosticsAuditDraft,
    RunDiagnosticsAuditMutationAuthority,
    RunEventRecord,
    RunRecord,
    RunStartPermitRecord,
    SessionArchiveDisposition,
    SessionCompositionStateCreation,
    SessionForkAuthority,
    SessionForkChildCreation,
    SessionForkChildMessageCreation,
    SessionForkChildStateCreation,
    SessionForkCreationTransaction,
    SessionForkParentAuthority,
    SessionForkReceiptResult,
    SessionForkSettlementCommand,
    SessionNotFoundError,
    SessionOperationAuthority,
    SessionOperationMutationTransaction,
    SessionPendingInterpretationCommand,
    SessionRecord,
    SessionRunEventType,
    SessionRunStatus,
    StagedForkSession,
    StaleComposeStateError,
    StateRevertReceiptResult,
    TransitionAssistantDraft,
    TransitionResponseSettlement,
    TrustModeAutoCommitRevokedError,
    decode_stored_composition_validation_errors,
    serialize_composition_validation_errors,
)
from elspeth.web.sessions.protocol import (
    InterpretationResolveError as InterpretationResolveError,
)
from elspeth.web.sessions.schemas import MessageWithStateResponse
from elspeth.web.sessions.skill_markdown_history import (
    RepositorySkillMarkdownHistoryAuthority,
    SkillMarkdownHistoryAuthority,
)
from elspeth.web.sessions.state_envelope import envelope_state_column, unwrap_state_column
from elspeth.web.sessions.telemetry import _SessionsTelemetry
from elspeth.web.sessions.time_normalization import restore_utc
from elspeth.web.validation import _validate_accepted_value_content

if TYPE_CHECKING:
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState, ValidationSummary
    from elspeth.web.execution.envelope import RunExecutionInput
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry


# Process-wide SQLite session-write lock registry.
#
# These three globals back ``_session_write_lock`` and
# ``_assert_session_write_lock_held`` on ``SessionServiceImpl``. They
# are MODULE-LEVEL ON PURPOSE -- not instance-level -- because the
# correctness contract is process-wide, not service-instance-wide.
#
# Why process-wide is required (do not refactor to instance state):
#   * ``run_sync_in_worker`` dispatches DB writes to a thread pool, so
#     two coroutines holding two different ``SessionServiceImpl``
#     instances against the same SQLite file MUST serialise on the
#     same lock or they race on the ``SELECT MAX(...) + 1``
#     allocator. Instance-local locks would let two services for the
#     same DB skip past each other's allocator reads.
#   * Multiple ``SessionServiceImpl`` instances against the same
#     engine URL are legal (the web app constructs them per request
#     scope in some configurations). The (database_url, session_id)
#     key in ``_SQLITE_SESSION_LOCKS`` is what makes process-wide
#     locking honour multi-instance scope without serialising
#     UNRELATED databases inside the same process.
#   * ``ContextVar`` is the standard Python primitive for per-task /
#     per-thread scoped state. ``_assert_session_write_lock_held``
#     reads it to verify the calling helper is inside a
#     ``_session_write_lock`` block without threading the lock state
#     through every function signature.
#
# Module-level mutable state is normally a smell; here it is the
# correct shape because the resource being guarded (a SQLite file on
# disk) is itself process-scoped. Refactoring to instance state would
# break the multi-instance correctness invariant the comment above
# describes.
_SESSION_WRITE_LOCK_HELD: ContextVar[frozenset[tuple[int, str]]] = ContextVar(
    "_SESSION_WRITE_LOCK_HELD",
    default=frozenset(),
)
_PIPELINE_METER = metrics.get_meter(__name__)
_PIPELINE_PLANNER_COUNTER = _PIPELINE_METER.create_counter("composer.pipeline_proposal.created_total")
_PIPELINE_CUSTODY_COUNTER = _PIPELINE_METER.create_counter("composer.pipeline_proposal.custody_total")
_PIPELINE_SETTLEMENT_COUNTER = _PIPELINE_METER.create_counter("composer.pipeline_proposal.settled_total")


_INTERPRETATION_IMMUTABLE_TRIGGER_MSG: str = "interpretation_events: resolved rows are immutable"

_STRUCTURAL_DIRECTIVE_PREFIXES: tuple[str, ...] = (
    "system:",
    "role:",
    "instructions:",
)


def _enveloped_state_column(value: Any) -> Any:
    """Wrap one composition_states JSON column through the single envelope rule."""
    return envelope_state_column(value)


def _current_adr019_counter_subsets_hold(
    *,
    rows_succeeded: int,
    rows_failed: int,
    rows_routed_success: int,
    rows_routed_failure: int,
    rows_quarantined: int,
) -> bool:
    return rows_routed_success <= rows_succeeded and rows_routed_failure <= rows_failed and rows_quarantined <= rows_failed


def _legacy_disjoint_counter_shape_holds(
    *,
    status: SessionRunStatus,
    rows_processed: int,
    rows_succeeded: int,
    rows_failed: int,
    rows_routed_success: int,
    rows_routed_failure: int,
    rows_quarantined: int,
) -> bool:
    """Return true for the pre-ADR-019 disjoint session-counter contract."""
    if status not in SESSION_TERMINAL_RUN_STATUS_VALUES:
        return False
    legacy_total = rows_succeeded + rows_failed + rows_routed_success + rows_routed_failure + rows_quarantined
    if rows_processed < legacy_total:
        return False

    success_indicator = rows_succeeded > 0 or rows_routed_success > 0
    failure_indicator = rows_failed > 0 or rows_routed_failure > 0 or rows_quarantined > 0
    if status == "completed":
        return success_indicator and not failure_indicator
    if status == "completed_with_failures":
        return success_indicator and failure_indicator
    if status == "failed":
        return True
    if status == "empty":
        return rows_processed == 0 and not success_indicator and not failure_indicator
    return status == "cancelled"


def _normalize_pre_adr019_session_counters(
    *,
    status: SessionRunStatus,
    rows_processed: int,
    rows_succeeded: int,
    rows_failed: int,
    rows_routed_success: int,
    rows_routed_failure: int,
    rows_quarantined: int,
) -> tuple[int, int]:
    """Fold unambiguous legacy disjoint counters into ADR-019 base counters.

    ADR-019 did not change the session DB schema, so historical rows have no
    version marker. Only shapes that fail the current subset invariant but pass
    the previous disjoint predicate are normalized; current rows and ambiguous
    data are left untouched for the response/schema guards to validate.
    """
    if _current_adr019_counter_subsets_hold(
        rows_succeeded=rows_succeeded,
        rows_failed=rows_failed,
        rows_routed_success=rows_routed_success,
        rows_routed_failure=rows_routed_failure,
        rows_quarantined=rows_quarantined,
    ):
        return rows_succeeded, rows_failed
    if not _legacy_disjoint_counter_shape_holds(
        status=status,
        rows_processed=rows_processed,
        rows_succeeded=rows_succeeded,
        rows_failed=rows_failed,
        rows_routed_success=rows_routed_success,
        rows_routed_failure=rows_routed_failure,
        rows_quarantined=rows_quarantined,
    ):
        return rows_succeeded, rows_failed
    return rows_succeeded + rows_routed_success, rows_failed + rows_routed_failure + rows_quarantined


def _interpretation_event_record_from_row(row: Any) -> InterpretationEventRecord:
    """Convert a SQLAlchemy row to an InterpretationEventRecord.

    Per the Tier-1 audit-trust contract
    (docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust Model),
    this conversion crashes
    loudly on any anomaly — the enum constructors raise ValueError on an
    unrecognised string, and the UUID/datetime constructors raise on
    malformed values. The schema CHECK constraints guarantee the closed-
    enum values and source-conditional nullability invariants; this helper
    relies on that guarantee.
    """
    return InterpretationEventRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        composition_state_id=UUID(row.composition_state_id) if row.composition_state_id is not None else None,
        affected_node_id=row.affected_node_id,
        tool_call_id=row.tool_call_id,
        user_term=row.user_term,
        kind=InterpretationKind(row.kind) if row.kind is not None else None,
        llm_draft=row.llm_draft,
        accepted_value=row.accepted_value,
        choice=InterpretationChoice(row.choice),
        created_at=restore_utc(row.created_at),
        resolved_at=restore_utc(row.resolved_at) if row.resolved_at is not None else None,
        actor=row.actor,
        model_identifier=row.model_identifier,
        model_version=row.model_version,
        provider=row.provider,
        composer_skill_hash=row.composer_skill_hash,
        arguments_hash=row.arguments_hash,
        hash_domain_version=row.hash_domain_version,
        interpretation_source=InterpretationSource(row.interpretation_source),
        surface_origin=InterpretationSurfaceOrigin(row.surface_origin) if row.surface_origin is not None else None,
        runtime_model_identifier_at_resolve=row.runtime_model_identifier_at_resolve,
        runtime_model_version_at_resolve=row.runtime_model_version_at_resolve,
        approved_prompt_artifact_hash=row.approved_prompt_artifact_hash,
    )


class QuarantineCleanupError(AuditIntegrityError):
    """Session archive committed, but staged blob cleanup failed."""


def _record_auto_commit_revocation_on_connection(
    conn: Connection,
    *,
    session_id: str,
    proposal_id: str,
    required_trust_mode: str,
    current_trust_mode: str,
    actor: str,
    created_at: datetime,
) -> ProposalEventRecord:
    """Insert or reuse the one exact non-terminal revocation outcome."""
    for name, value in (("required_trust_mode", required_trust_mode), ("current_trust_mode", current_trust_mode)):
        if value not in COMPOSER_TRUST_MODE_VALUES:
            raise ValueError(f"{name} must be one of {sorted(COMPOSER_TRUST_MODE_VALUES)!r}; got {value!r}")
    expected_payload = {
        "required_trust_mode": required_trust_mode,
        "current_trust_mode": current_trust_mode,
    }
    existing_rows = conn.execute(
        select(proposal_events_table)
        .where(proposal_events_table.c.session_id == session_id)
        .where(proposal_events_table.c.proposal_id == proposal_id)
        .where(proposal_events_table.c.event_type == "auto_commit.revoked")
    ).fetchall()
    if len(existing_rows) > 1:
        raise AuditIntegrityError("pipeline proposal has duplicate auto-commit revocation events")
    if existing_rows:
        existing = _proposal_event_record_from_row(existing_rows[0])
        if existing.actor != actor or deep_thaw(existing.payload) != expected_payload:
            raise AuditIntegrityError("pipeline proposal auto-commit revocation binding is malformed")
        return existing

    event_id = str(uuid.uuid4())
    conn.execute(
        insert(proposal_events_table).values(
            id=event_id,
            session_id=session_id,
            proposal_id=proposal_id,
            event_type="auto_commit.revoked",
            actor=actor,
            payload=expected_payload,
            created_at=created_at,
        )
    )
    row = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == event_id)).one()
    return _proposal_event_record_from_row(row)


def _refuse_unrecorded_quota_exceeded(outcome: QuotaExceeded) -> None:
    """The recorder a service gets when none is wired: a quota refusal nobody audits fails closed (R4)."""
    raise AuditIntegrityError(f"quota_exceeded refusal for identity {outcome.identity_id} has no auth audit writer wired")


class ComposerTerminalSQLCompletionUnknown(AuditIntegrityError):
    """The SQL worker completion witness is unavailable; settlement must wait."""


class ComposerRequiredAuditPersistenceError(AuditIntegrityError):
    """Required atomic audit SQL failed; preserves the canonical public envelope."""

    def __init__(self, message: str, *, helper: Literal["llm_calls", "turn_audit_cohort"]) -> None:
        if helper not in ("llm_calls", "turn_audit_cohort"):
            raise ValueError("required audit helper must name a closed producer seam")
        self.helper = helper
        super().__init__(message)


class _ComposerTerminalRetryStateChanged(RuntimeError):
    """Locked retry refuses an ordinary change to terminal-driving state."""


def _refuse_required_sql(ticket: RequiredWorkTicket | None, error: BaseException) -> NoReturn:
    if ticket is not None:
        ticket.complete_without_submission(error)
    raise error


@contextlib.contextmanager
def _required_sql_preflight(ticket: RequiredWorkTicket | None) -> Iterator[None]:
    """Observe only preparation that has not submitted its SQL callable."""
    try:
        yield
    except BaseException as error:
        _refuse_required_sql(ticket, error)


def _validate_required_sql_ticket(
    ticket: RequiredWorkTicket | None,
    *,
    sources: tuple[RequiredWorkSource, ...],
    session_id: str,
    context: SessionOperationContext | None = None,
    running: ComposerOperationRunning | None = None,
    proposal_id: str | None = None,
    tool_call_id: str | None = None,
) -> None:
    """Refuse a mismatched preallocated receipt before executor submission."""
    if ticket is None:
        return
    if type(ticket) is not RequiredWorkTicket:
        raise TypeError("required SQL receipt must be an exact RequiredWorkTicket")
    scope = ticket.key.authority
    try:
        if ticket.key.source not in sources or ticket.key.session_id != session_id:
            raise AuditIntegrityError("required SQL receipt source/session mismatch")
        if context is not None and scope.context != context:
            raise AuditIntegrityError("required SQL receipt fence mismatch")
        if running is not None and (
            scope.durable_operation_id != running.claim.operation_id
            or scope.claim_attempt != running.claim.attempt
            or scope.context != running.session_operation_context
        ):
            raise AuditIntegrityError("required SQL receipt claim mismatch")
        if proposal_id is not None and scope.proposal_id != proposal_id:
            raise AuditIntegrityError("required SQL receipt proposal mismatch")
        if tool_call_id is not None and scope.tool_call_id != tool_call_id:
            raise AuditIntegrityError("required SQL receipt tool identity mismatch")
    except BaseException as error:
        ticket.complete_without_submission(error)
        raise


@dataclass(frozen=True, slots=True)
class _ProviderAdmissionRefused:
    refusal: ChargeableAdmissionRefused


def _validate_principal_snapshot(value: tuple[str | None, PluginAvailabilitySnapshot | None]) -> None:
    if type(value) is not tuple or len(value) != 2:
        raise AuditIntegrityError("preparation principal read returned an invalid owned tuple")
    user_id, snapshot = value
    if (user_id is not None and type(user_id) is not str) or (snapshot is not None and type(snapshot) is not PluginAvailabilitySnapshot):
        raise AuditIntegrityError("preparation principal read returned invalid owned values")


def _validate_inline_snapshot(value: SessionInlineBlobSnapshot | None) -> None:
    if value is not None and type(value) is not SessionInlineBlobSnapshot:
        raise AuditIntegrityError("preparation inline read returned an invalid owned snapshot")


class SessionServiceImpl:
    """Concrete async session service backed by worker-dispatched SQLAlchemy Core."""

    def __init__(
        self,
        engine: Engine,
        data_dir: Path | None = None,
        *,
        telemetry: _SessionsTelemetry,
        log: structlog.stdlib.BoundLogger,
        plugin_snapshot_factory: Callable[[str], PluginAvailabilitySnapshot] | None = None,
        operator_profile_registry: OperatorProfileRegistry | None = None,
        catalog: CatalogService | None = None,
        session_operation_authority: SessionOperationAuthority | None = None,
        global_run_recovery_authority: GlobalRunRecoveryAuthority | None = None,
        audit_access_log_authority: AuditAccessLogAuthority | None = None,
        run_diagnostics_audit_authority: RunDiagnosticsAuditMutationAuthority | None = None,
        skill_markdown_history_authority: SkillMarkdownHistoryAuthority | None = None,
        owner_instance_id: str | None = None,
        session_operation_lease_seconds: int = 30,
        runtime_preflight: SessionRuntimePreflight | None = None,
        inline_blob_read: Callable[[SessionOperationContext, UUID], tuple[BlobRecord, bytes]] | None = None,
        chargeable_admission_policy: ChargeableAdmissionPolicy | None = None,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = _refuse_unrecorded_quota_exceeded,
        approval_supersession_recorder: Callable[[ApprovalSupersession], None] = refuse_unrecorded_approval_supersession,
    ) -> None:
        from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority

        if (plugin_snapshot_factory is None) != (operator_profile_registry is None):
            raise ValueError("plugin_snapshot_factory and operator_profile_registry must be configured together")
        if plugin_snapshot_factory is not None and catalog is None:
            raise ValueError("profile-aware session validation requires the authoritative catalog")
        self._engine = engine
        self._data_dir = data_dir
        self._telemetry = telemetry
        self._log = log
        self._plugin_snapshot_factory = plugin_snapshot_factory
        self._operator_profile_registry = operator_profile_registry
        self._catalog = catalog
        self._runtime_preflight = runtime_preflight
        self._inline_blob_read = inline_blob_read
        self._chargeable_admission_policy = chargeable_admission_policy or ChargeableAdmissionPolicy(
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
        )
        self._quota_exceeded_recorder = quota_exceeded_recorder
        self._approval_supersession_recorder = approval_supersession_recorder
        if owner_instance_id is not None and (type(owner_instance_id) is not str or not owner_instance_id.strip()):
            raise ValueError("owner_instance_id must be a nonblank exact string")
        if type(session_operation_lease_seconds) is not int or not 1 <= session_operation_lease_seconds <= 3600:
            raise ValueError("session_operation_lease_seconds must be an exact integer from 1 through 3600")
        self._owner_instance_id = owner_instance_id if owner_instance_id is not None else f"{engine.dialect.name}-{uuid.uuid4()}"
        self._session_operation_lease_seconds = session_operation_lease_seconds
        if session_operation_authority is None:
            if engine.dialect.name == "sqlite":
                session_operation_authority = SQLiteLocalSessionOperationAuthority(
                    engine, approval_supersession_recorder=approval_supersession_recorder
                )
            elif engine.dialect.name == "postgresql":
                session_operation_authority = PostgresSessionOperationRepository(
                    engine, approval_supersession_recorder=approval_supersession_recorder
                )
            else:
                raise NotImplementedError(f"session operation authority not implemented for dialect {engine.dialect.name}")
        self._session_operation_authority = session_operation_authority
        self._global_run_recovery_authority = global_run_recovery_authority or RepositoryGlobalRunRecoveryAuthority(engine)
        self._audit_access_log_authority = audit_access_log_authority or RepositoryAuditAccessLogAuthority(engine)
        self._run_diagnostics_audit_authority = run_diagnostics_audit_authority or RepositoryRunDiagnosticsAuditAuthority(engine)
        self._skill_markdown_history_authority = skill_markdown_history_authority or RepositorySkillMarkdownHistoryAuthority(engine)

    @property
    def session_operation_authority(self) -> SessionOperationAuthority:
        """Return the handle-free authority shared by session service callers."""
        return self._session_operation_authority

    @property
    def session_operation_owner_instance_id(self) -> str:
        return self._owner_instance_id

    @property
    def session_operation_lease_seconds(self) -> int:
        return self._session_operation_lease_seconds

    @staticmethod
    def _inline_preflight_config(state: CompositionStateRecord | CompositionStateData) -> InlinePreflightState:
        """Inspect stored option fields without constructing unrelated state shapes."""
        return InlinePreflightState.from_stored_state(state)

    async def _prepare_inline_blob_snapshot(
        self,
        config: InlinePreflightState,
        *,
        session_id: UUID,
        session_operation_context: SessionOperationContext,
        preparation_work: RequiredWorkBinding | None = None,
    ) -> SessionInlineBlobSnapshot | None:
        """Read custody-verified bytes before entering any SESSIONS lock."""
        reader = self._inline_blob_read
        if reader is None:
            return None

        def _metadata_hint(blob_id: UUID) -> tuple[str, str | None, int] | None:
            # This short read is only a size/status hint. The custody read
            # below still verifies the exact fence and returns one row/byte
            # version. Close this connection before taking BLOB_CUSTODY.
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(blobs_table.c.status, blobs_table.c.content_hash, blobs_table.c.size_bytes)
                    .where(blobs_table.c.id == str(blob_id))
                    .where(blobs_table.c.session_id == str(session_id))
                ).one_or_none()
                return (row.status, row.content_hash, row.size_bytes) if row is not None else None

        if preparation_work is not None:
            preparation_work.validate_context(session_operation_context)
            return await self._run_required_preparation_read(
                preparation_work,
                lambda: prepare_session_inline_blob_snapshot(
                    config,
                    session_id=session_id,
                    read_blob=lambda blob_id: reader(session_operation_context, blob_id),
                    metadata_hint=_metadata_hint,
                ),
                project=_validate_inline_snapshot,
            )
        return cast(
            SessionInlineBlobSnapshot | None,
            await self._run_sync(
                prepare_session_inline_blob_snapshot,
                config,
                session_id=session_id,
                read_blob=lambda blob_id: reader(session_operation_context, blob_id),
                metadata_hint=_metadata_hint,
            ),
        )

    async def _preflight_state_pair(
        self,
        *,
        session_id: UUID,
        anchor_id: UUID,
    ) -> tuple[CompositionStateRecord | None, CompositionStateRecord | None]:
        """Capture a short read-only state view before blob custody acquisition."""
        sid = str(session_id)

        def _read() -> tuple[CompositionStateRecord | None, CompositionStateRecord | None]:
            with self._engine.connect() as conn:
                anchor_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.id == str(anchor_id))
                    .where(composition_states_table.c.session_id == sid)
                ).one_or_none()
                live_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                return (
                    self._row_to_state_record(anchor_row) if anchor_row is not None else None,
                    self._row_to_state_record(live_row) if live_row is not None else None,
                )

        return cast(tuple[CompositionStateRecord | None, CompositionStateRecord | None], await self._run_sync(_read))

    def _validate_patched_composition_state(
        self,
        state: CompositionState,
        *,
        validation_inputs: SessionInterpretationValidationInputs,
        session_id: str,
        user_id: str | None,
        inline_blob_snapshot: SessionInlineBlobSnapshot | None = None,
    ) -> ValidationSummary:
        """Validate a post-review state through its executable profile view.

        The persisted ``is_valid`` contract is the full runtime-preflight
        verdict — the compose path persists ``validate_pipeline``'s outcome
        (``_composer_persisted_validation``). When the authoring gate passes
        and a runtime preflight is wired, the runtime verdict is merged in so
        an interpretation-resolution row never claims validity over engine
        stages (graph_structure, route resolution, schema compatibility) the
        authoring validator cannot see (elspeth-155947ca47). The preflight is
        side-effect-free (see ``web/execution/runtime_preflight.py``) and the
        session locks it may run under are per-session, so calling it inside
        the resolution transaction serialises only this session's writes.
        """
        summary = validate_composition_state_with_interpretation_inputs(state, validation_inputs).validation
        plugin_snapshot = validation_inputs.plugin_snapshot
        if not summary.is_valid or self._runtime_preflight is None:
            return summary

        if inline_blob_snapshot is not None:
            if not inline_blob_snapshot.assert_covers(InlinePreflightState.from_composition_state(state)):
                raise AuditIntegrityError("interpretation resolution introduced an unprepared inline blob marker")
            runtime = self._runtime_preflight(state, user_id, session_id, plugin_snapshot, inline_blob_snapshot.content)
        else:
            runtime = self._runtime_preflight(state, user_id, session_id, plugin_snapshot)
        if runtime.is_valid:
            return summary

        from elspeth.web.composer.state import ValidationEntry

        def _component(component_id: str | None, component_type: str | None) -> str:
            if component_id is None:
                return "pipeline"
            if component_type == "transform":
                return f"node:{component_id}"
            if component_type == "sink":
                return f"output:{component_id}"
            return component_id

        runtime_entries = tuple(
            ValidationEntry(
                component=_component(error.component_id, error.component_type),
                message=error.message,
                severity="high",
                error_code=error.error_code,
            )
            for error in runtime.errors
        )
        return replace(summary, is_valid=False, errors=(*summary.errors, *runtime_entries))

    async def _plugin_snapshot_for_session(self, session_id: str) -> PluginAvailabilitySnapshot | None:
        """Build a principal snapshot before a session write transaction starts."""
        if self._plugin_snapshot_factory is None:
            return None
        _user_id, snapshot = await self._session_principal_context(session_id)
        return snapshot

    async def _run_sync(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous callable in the thread pool executor."""
        return await run_sync_in_worker(func, *args, **kwargs)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _acquire_session_advisory_lock(self, conn: Connection, session_id: str) -> None:
        """Acquire a session write lock for the duration of the
        current transaction. Released automatically on COMMIT or ROLLBACK.

        SQLite: no-op. SQLite serialization is owned by
        ``_session_write_lock`` below; this helper exists only for the
        PostgreSQL advisory-lock SQL and remains no-op on SQLite so callers
        can test the dialect-specific SQL separately.

        PostgreSQL: pg_advisory_xact_lock(ELSPETH_SESSIONS_LOCK_CLASSID,
        hashtext(session_id)) -- the **two-argument** form
        (B3 from the advisory-lock review synthesis). The classid namespace
        is reserved in src/elspeth/contracts/advisory_locks.py and is
        on-the-wire ABI under change control; do not open-code the literal
        here, always import the constant.

        Hash-function notes:

        * ``pg_advisory_xact_lock(int, int)`` requires two signed int4
          arguments. ``hashtext(text)`` returns int4 directly. Do not use
          ``hashtextextended(... )::int``: PostgreSQL integer casts are
          range-checked and may fail before the lock is acquired.
        * Birthday collisions become probable around ~65k *concurrent*
          sessions hashing to the same classid slot. This
          is benign -- the unique index ix_chat_messages_session_sequence
          is the correctness guarantee; the advisory lock is a
          contention-reducer ahead of it. Collisions cause spurious
          serialisation between two unrelated sessions, never duplicate
          rows or lost writes.
        * The classid value is **NOT** a deployment knob. Two ELSPETH
          instances on the same Postgres cluster (including different
          versions during a rolling deploy) MUST share the same value
          or they will not mutually exclude each other. See
          src/elspeth/contracts/advisory_locks.py for the ABI commitment.
        """
        dialect = self._engine.dialect.name
        if dialect == "sqlite":
            return  # SQLite serialization is owned by _session_write_lock
        if dialect == "postgresql":
            acquire_session_advisory_xact_lock(conn, session_id)
            return
        raise NotImplementedError(f"_acquire_session_advisory_lock not implemented for dialect {dialect}")

    def _sqlite_lock_for_session(self, session_id: str) -> threading.RLock:
        """Return the process-wide SQLite write lock for one DB/session pair."""
        return sqlite_session_mutex(self._engine, session_id)

    @contextlib.contextmanager
    def _session_process_locked_begin(self, session_id: str) -> Iterator[Connection]:
        """Acquire SQLite process exclusion before opening the DB transaction."""
        with process_session_lock(self._engine, session_id), self._engine.begin() as conn:
            yield conn

    @contextlib.contextmanager
    def _session_pair_locked_begin(self, first_session_id: str, second_session_id: str) -> Iterator[Connection]:
        """Acquire two session locks in global UUID order before DB work."""
        if first_session_id == second_session_id:
            raise AuditIntegrityError("paired session transaction requires two distinct session ids")
        ordered = tuple(sorted((first_session_id, second_session_id)))
        with contextlib.ExitStack() as process_stack:
            for session_id in ordered:
                process_stack.enter_context(process_session_lock(self._engine, session_id))
            with self._engine.begin() as conn, contextlib.ExitStack() as transaction_stack:
                for session_id in ordered:
                    transaction_stack.enter_context(self._session_write_lock(conn, session_id))
                yield conn

    def _assert_session_write_lock_held(
        self,
        conn: Connection,
        session_id: str,
        *,
        caller: str,
    ) -> None:
        """Mechanical precondition guard for session-scoped allocators.

        The docstring precondition on _reserve_sequence_range and
        _insert_composition_state is not enough: future callers can forget the
        lock and still pass type checks. _session_write_lock sets a per-thread
        ContextVar token keyed by (id(conn), session_id); lock-requiring helpers
        crash immediately if called without that token in the same transaction.
        """
        if (id(conn), session_id) not in _SESSION_WRITE_LOCK_HELD.get():
            raise RuntimeError(
                f"{caller}: _session_write_lock(conn, {session_id!r}) must be "
                "held in the same transaction before allocating session-scoped "
                "sequence/version values"
            )

    @contextlib.contextmanager
    def _session_write_lock(self, conn: Connection, session_id: str) -> Iterator[None]:
        """Serialize same-session sequence/version allocators.

        PostgreSQL uses the transaction-scoped advisory lock. SQLite uses a
        process-wide per-session RLock held until the surrounding transaction
        commits or rolls back. Every caller that performs ``SELECT MAX(...) +
        1`` for ``chat_messages.sequence_no`` or ``composition_states.version``
        MUST wrap that read and every dependent INSERT in this context.
        """
        key = (id(conn), session_id)
        held = _SESSION_WRITE_LOCK_HELD.get()
        token = _SESSION_WRITE_LOCK_HELD.set(held | {key})
        dialect = self._engine.dialect.name
        try:
            if dialect == "sqlite":
                with sqlite_transaction_session_lock(conn, self._engine, session_id):
                    yield
                return
            if dialect == "postgresql":
                self._acquire_session_advisory_lock(conn, session_id)
                yield
                return
            raise NotImplementedError(f"_session_write_lock not implemented for dialect {dialect}")
        finally:
            _SESSION_WRITE_LOCK_HELD.reset(token)

    def _require_session_fork_session_fences_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> datetime:
        """Validate the parent and adopted-child session-operation fences only."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("fork authority must be exact")
        now = database_now(conn)
        for context in (
            authority.parent.parent_context,
            authority.child_context,
        ):
            fence = context.fence
            exact = conn.execute(
                select(session_operation_fences_table.c.session_id).where(
                    session_operation_fences_table.c.session_id == fence.session_id,
                    session_operation_fences_table.c.operation_id == fence.operation_id,
                    session_operation_fences_table.c.lease_token == fence.lease_token,
                    session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
                    session_operation_fences_table.c.operation_kind == context.operation_kind.value,
                    session_operation_fences_table.c.released_at.is_(None),
                    session_operation_fences_table.c.lease_expires_at > now,
                )
            ).one_or_none()
            if exact is None:
                raise OperationReceiptFenceLostError(authority.parent.receipt_fence)
        return now

    def _require_session_fork_authority_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> tuple[Any, datetime]:
        """Validate parent, child, and live receipt authority under one pair lock."""
        now = self._require_session_fork_session_fences_on_connection(conn, authority)
        receipt = require_live_operation_receipt(conn, authority.parent.receipt_fence, now=now)
        if receipt["kind"] != "session_fork":
            raise AuditIntegrityError("fork authority is bound to a non-fork receipt")
        return receipt, now

    def _require_settled_session_fork_authority_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> datetime:
        """Validate fork authority after this transaction terminalised the parent operation.

        Settlement completes the parent receipt first so a later failure
        rolls the terminal row back atomically. Writes that follow in the same
        transaction therefore cannot demand a live receipt lease; they require
        the still-live session-operation fences plus a parent row that is
        ``completed`` and bound to exactly this operation and adopted child.
        """
        now = self._require_session_fork_session_fences_on_connection(conn, authority)
        receipt_fence = authority.parent.receipt_fence
        sid = str(receipt_fence.session_id)
        self._assert_session_write_lock_held(conn, sid, caller="_require_settled_session_fork_authority_on_connection")
        row = read_operation_receipt(conn, session_id=receipt_fence.session_id, operation_id=receipt_fence.operation_id)
        if (
            row is None
            or row["kind"] != "session_fork"
            or row["status"] != "completed"
            or row["attempt"] != receipt_fence.attempt
            or row["result_session_id"] != authority.child_context.fence.session_id
        ):
            raise OperationReceiptFenceLostError(receipt_fence)
        return now

    def _require_session_operation_context_on_connection(
        self,
        conn: Connection,
        context: SessionOperationContext,
        *,
        session_id: str,
        expected_kind: SessionOperationKind,
        now: datetime,
        audit_only: bool = False,
    ) -> None:
        """Validate one exact current session authority in the caller transaction."""
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind is not expected_kind
            or context.fence.session_id != session_id
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        fence = context.fence
        exact = conn.execute(
            select(session_operation_fences_table.c.session_id).where(
                session_operation_fences_table.c.session_id == fence.session_id,
                session_operation_fences_table.c.operation_id == fence.operation_id,
                session_operation_fences_table.c.lease_token == fence.lease_token,
                session_operation_fences_table.c.operation_epoch == fence.operation_epoch,
                session_operation_fences_table.c.operation_kind == expected_kind.value,
                session_operation_fences_table.c.released_at.is_(None),
                session_operation_fences_table.c.lease_expires_at > now,
            )
        ).one_or_none()
        if exact is None:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

        if not audit_only:
            require_composer_operation_mutation_on_connection(conn, context)

    @contextlib.contextmanager
    def _session_composer_mutation_transaction(
        self,
        conn: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
        expected_kind: SessionOperationKind,
        audit_only: bool = False,
    ) -> Iterator[_SessionComposerMutationTransaction]:
        """Yield a lifetime-checked ordinary Composer mutation capability."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        now = database_now(conn)
        self._require_session_operation_context_on_connection(
            conn,
            session_operation_context,
            session_id=session_id,
            expected_kind=expected_kind,
            now=now,
            audit_only=audit_only,
        )
        transaction = _SessionComposerMutationTransaction(
            self,
            conn,
            session_id=session_id,
            session_operation_context=session_operation_context,
            expected_kind=expected_kind,
            audit_only=audit_only,
        )
        try:
            yield transaction
        finally:
            transaction._close()

    @staticmethod
    def _pipeline_settlement_operation_kind(
        session_operation_context: SessionOperationContext,
    ) -> SessionOperationKind:
        """Accept only the two authorities that may settle a proposal."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        operation_kind = session_operation_context.operation_kind
        if operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}:
            raise ValueError("pipeline settlement requires COMPOSE or PROPOSAL authority")
        return operation_kind

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
    ) -> OperationReceiptOutcome:
        """Claim or join one ordinary mutation under its live session fence."""
        sid = str(session_id)

        def _sync() -> OperationReceiptOutcome:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                now = database_now(conn)
                expected = SessionOperationKind.SESSION_FORK if kind == "session_fork" else SessionOperationKind.COMPOSE
                self._require_session_operation_context_on_connection(
                    conn, session_operation_context, session_id=sid, expected_kind=expected, now=now
                )
                return reserve_operation_receipt(
                    conn,
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=kind,
                    request_hash=request_hash,
                    actor=actor,
                    lease_seconds=lease_seconds,
                    now=now,
                )

        return cast("OperationReceiptOutcome", await self._run_sync(_sync))

    async def get_operation_receipt(
        self, *, session_id: UUID, operation_id: str, kind: OperationReceiptKind, request_hash: str
    ) -> OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed | None:
        """Read a strict replay descriptor without claiming writer authority."""
        from elspeth.web.sessions.operation_receipts import _outcome, _validate_identity
        from elspeth.web.sessions.protocol import OperationReceiptConflictError

        _validate_identity(operation_id=operation_id, kind=kind, request_hash=request_hash)

        def _sync() -> OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed | None:
            with self._engine.connect() as conn:
                # The receipt row and its event chain are two SELECTs. Under
                # PostgreSQL READ COMMITTED, a settlement between them would
                # combine incompatible snapshots and falsely report corruption.
                if conn.dialect.name == "postgresql":
                    conn.execution_options(isolation_level="REPEATABLE READ")
                with conn.begin():
                    now = database_now(conn)
                    row = read_operation_receipt(conn, session_id=session_id, operation_id=operation_id)
            if row is None:
                return None
            if row["kind"] != kind or row["request_hash"] != request_hash:
                raise OperationReceiptConflictError(session_id=session_id, operation_id=operation_id)
            return _outcome(row, now=now)

        return cast(
            "OperationReceiptActive | OperationReceiptCompleted | OperationReceiptFailed | None",
            await self._run_sync(_sync),
        )

    def require_operation_receipt_authority_on_connection(
        self, conn: Connection, fence: OperationReceiptFence, session_operation_context: SessionOperationContext
    ) -> tuple[RowMapping, datetime]:
        sid = str(fence.session_id)
        self._assert_session_write_lock_held(conn, sid, caller="require_operation_receipt_authority_on_connection")
        now = database_now(conn)
        row = require_live_operation_receipt(conn, fence, now=now)
        expected = SessionOperationKind.SESSION_FORK if row["kind"] == "session_fork" else SessionOperationKind.COMPOSE
        self._require_session_operation_context_on_connection(
            conn, session_operation_context, session_id=sid, expected_kind=expected, now=now
        )
        return row, now

    async def renew_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> OperationReceiptFence:
        sid = str(fence.session_id)

        def _sync() -> OperationReceiptFence:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                _row, now = self.require_operation_receipt_authority_on_connection(conn, fence, session_operation_context)
                return renew_operation_receipt(conn, fence, actor=actor, lease_seconds=lease_seconds, now=now)

        return cast("OperationReceiptFence", await self._run_sync(_sync))

    async def bind_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        originating_message_id: UUID | None = None,
        result_session_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
    ) -> None:
        sid = str(fence.session_id)

        def _sync() -> None:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                _row, now = self.require_operation_receipt_authority_on_connection(conn, fence, session_operation_context)
                bind_operation_receipt(
                    conn, fence, now=now, originating_message_id=originating_message_id, result_session_id=result_session_id
                )

        await self._run_sync(_sync)

    async def complete_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        result: OperationReceiptResult,
        response_hash: str,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> OperationReceiptCompleted:
        sid = str(fence.session_id)

        def _sync() -> OperationReceiptCompleted:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                _row, now = self.require_operation_receipt_authority_on_connection(conn, fence, session_operation_context)
                settled = settle_operation_receipt(conn, fence, now=now, actor=actor, result=result, response_hash=response_hash)
                if type(settled) is not OperationReceiptCompleted:
                    raise AuditIntegrityError("Operation receipt completion returned a failure")
                return settled

        return cast("OperationReceiptCompleted", await self._run_sync(_sync))

    async def fail_operation_receipt(
        self,
        fence: OperationReceiptFence,
        *,
        failure_code: OperationReceiptFailureCode,
        actor: str,
        session_operation_context: SessionOperationContext,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> OperationReceiptFailed:
        sid = str(fence.session_id)

        def _sync() -> OperationReceiptFailed:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                _row, now = self.require_operation_receipt_authority_on_connection(conn, fence, session_operation_context)
                settled = settle_operation_receipt(
                    conn,
                    fence,
                    now=now,
                    actor=actor,
                    failure_code=failure_code,
                    failure_diagnostics=failure_diagnostics,
                )
                if type(settled) is not OperationReceiptFailed:
                    raise AuditIntegrityError("Operation receipt failure returned a completion")
                return settled

        return cast("OperationReceiptFailed", await self._run_sync(_sync))

    async def fail_fork_operation_receipt(
        self,
        authority: SessionForkAuthority,
        *,
        failure_code: OperationReceiptFailureCode,
        actor: str,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> OperationReceiptFailed:
        """Settle a failed fork while its parent, child, and receipt fences live."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("authority must be an exact SessionForkAuthority")
        parent_id = authority.parent.parent_context.fence.session_id
        child_id = authority.child_context.fence.session_id

        def _sync() -> OperationReceiptFailed:
            with self._session_pair_locked_begin(parent_id, child_id) as conn:
                _row, now = self._require_session_fork_authority_on_connection(conn, authority)
                settled = settle_operation_receipt(
                    conn,
                    authority.parent.receipt_fence,
                    now=now,
                    actor=actor,
                    failure_code=failure_code,
                    failure_diagnostics=failure_diagnostics,
                )
                if type(settled) is not OperationReceiptFailed:
                    raise AuditIntegrityError("Fork receipt failure returned a completion")
                return settled

        return cast("OperationReceiptFailed", await self._run_sync(_sync))

    def _reserve_sequence_range(self, conn: Connection, session_id: str, *, count: int) -> int:
        """Reserve ``count`` consecutive sequence numbers for ``session_id``.

        PRECONDITION: caller MUST be inside
        ``with self._session_write_lock(conn, session_id):`` in the same
        transaction. That context acquires the PostgreSQL advisory lock or
        the SQLite process-local session lock, making the unilateral
        ``SELECT MAX + 1`` allocation race-free under concurrent writers.

        Inside the same transaction, performs:
            SELECT COALESCE(MAX(sequence_no), 0) FROM chat_messages WHERE session_id = ?
        and returns max+1. The caller writes rows at max+1, max+2, ... max+count.

        The session write lock prevents same-session allocator collisions
        on both PostgreSQL and SQLite. Do not call this helper outside
        the context, even in tests.

        Note: gaps in sequence_no are permitted (transaction rollback after
        reservation leaves the next caller's MAX+1 higher than the first
        successful row's sequence_no). Sequence_no is an ordering key, not a count.

        *Design seam acknowledgement (synthesised review A-F9 / M11).*
        The three-helper protocol (``_acquire_session_advisory_lock`` →
        ``_reserve_sequence_range`` → ``_insert_chat_message``) requires
        callers to invoke them in order. Schedule 1A has the current
        add-message path and ``fork_session`` copy path; Schedule 1B adds
        ``persist_compose_turn``. Every caller must enter
        ``_session_write_lock`` before reserving a sequence range. If a
        later phase adds another invocation site, consider consolidating
        into a single ``_write_chat_messages_atomic(conn, session_id, rows)``
        entry point that hides the protocol. Until that third site appears,
        the consolidation is not justified.
        """
        self._assert_session_write_lock_held(
            conn,
            session_id,
            caller="_reserve_sequence_range",
        )
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")
        # SQLAlchemy 2.x ``select(func.max(...))`` is the project-standard
        # idiom (see existing ``save_composition_state`` at
        # ``service.py:398``); using it here keeps the pattern uniform
        # and gives mypy a typed ``int | None`` instead of the ``Any``
        # that ``text(...)`` plus ``.first()`` produces. Closes
        # synthesised review finding P-L-1 / L15.
        current_max = conn.execute(
            select(func.coalesce(func.max(chat_messages_table.c.sequence_no), 0)).where(chat_messages_table.c.session_id == session_id)
        ).scalar_one()
        return int(current_max) + 1

    def _insert_chat_message(
        self,
        conn: Connection,
        /,
        *,
        session_id: str,
        role: str,
        content: str,
        raw_content: str | None,
        tool_calls: Any,
        sequence_no: int,
        writer_principal: ChatMessageWriterPrincipal,
        composition_state_id: str | None,
        tool_call_id: str | None,
        parent_assistant_id: str | None,
        created_at: datetime,
        session_operation_context: SessionOperationContext,
        message_id: str | None = None,
        audit_only: bool = False,
    ) -> str:
        """Single-row insert into ``chat_messages`` with the supplied fields.

        SessionMutationAuthority boundary (P4-D6 family A2b, elspeth-99949c96ca):
        ``session_operation_context`` is the operation the caller holds over
        this session (COMPOSE, PROPOSAL or SESSION_FORK) and is proved live on
        ``conn`` immediately before the INSERT -- there is no unfenced path.

        PRECONDITIONS (mechanically enforced — see body):

        1. Caller MUST be inside ``self._session_write_lock(conn, session_id)``
           in the same transaction. The session write lock is what makes
           the ``_reserve_sequence_range`` allocation safe against
           same-session concurrent writers; a writer that bypasses the
           lock could persist a sequence_no that another transaction is
           about to allocate.
        2. Caller MUST have already obtained ``sequence_no`` from
           ``_reserve_sequence_range``. This helper does NOT allocate
           sequence numbers — it persists what the caller supplies.

        ``created_at`` is supplied by the caller so multi-row inserts
        in the same transaction (``persist_compose_turn``) and
        same-transaction ``sessions.updated_at`` writes
        (``add_message``) can share a single timestamp. Generating a
        new ``datetime.now(UTC)`` inside this helper would produce
        per-row drift visible in the audit trail.

        ``raw_content`` is the audit-attribution column for assistant
        messages whose visible ``content`` was rewritten by runtime
        preflight redaction. It MUST be persisted as supplied —
        silently discarding it would regress the pre-rev-4
        ``add_message`` behaviour and create audit-data loss (silent wrong
        results are worse than a crash — see
        docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust
        Model).

        If ``role == "tool"``, this helper additionally verifies that
        ``parent_assistant_id`` references an assistant row in the
        same session. The DB FK on
        ``(parent_assistant_id, session_id)`` proves same-session
        existence; SQL cannot portably enforce that the referenced
        row's role is ``assistant``. The guard at the service-writer
        boundary closes that gap.

        Returns the newly-allocated UUID-shaped message id (so the
        caller can persist downstream references — e.g.
        ``tool_call_id`` parents — without a follow-up SELECT).
        """
        self._assert_session_write_lock_held(
            conn,
            session_id,
            caller="_insert_chat_message",
        )
        if audit_only and role not in ("audit", "tool"):
            raise ValueError("audit_only writes carry only audit or tool rows")
        self._require_session_write_authority_on_connection(conn, session_operation_context, session_id=session_id, audit_only=audit_only)
        if role == "tool":
            if parent_assistant_id is None:
                raise RuntimeError(f"_insert_chat_message: tool row requires parent_assistant_id (session={session_id!r})")
            _assert_parent_assistant_message(
                conn,
                parent_assistant_id=parent_assistant_id,
                session_id=session_id,
                caller="_insert_chat_message",
            )
        elif role == "assistant":
            _assert_assistant_row_has_audit_content(
                content=content,
                raw_content=raw_content,
                tool_calls=tool_calls,
                caller="_insert_chat_message",
            )
        msg_id = message_id or str(uuid.uuid4())
        conn.execute(
            insert(chat_messages_table).values(
                id=msg_id,
                session_id=session_id,
                role=role,
                content=content,
                raw_content=raw_content,
                tool_calls=tool_calls,
                sequence_no=sequence_no,
                writer_principal=writer_principal,
                composition_state_id=composition_state_id,
                tool_call_id=tool_call_id,
                parent_assistant_id=parent_assistant_id,
                created_at=created_at,
            )
        )
        return msg_id

    def _insert_message_ingress_receipt(
        self,
        conn: Connection,
        /,
        *,
        session_id: str,
        operation_id: str,
        user_message_id: str,
        requested_state_id: str | None,
        created_at: datetime,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Bind one accepted route user row to its request key under COMPOSE authority."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        self._assert_session_write_lock_held(conn, session_id, caller="_insert_message_ingress_receipt")
        self._require_session_write_authority_on_connection(conn, session_operation_context, session_id=session_id)
        conn.execute(
            insert(message_ingress_receipts_table).values(
                session_id=session_id,
                operation_id=operation_id,
                user_message_id=user_message_id,
                requested_state_id=requested_state_id,
                created_at=created_at,
            )
        )

    def _insert_transition_assistant(
        self,
        conn: Connection,
        *,
        session_id: str,
        state_id: str,
        content: str,
        raw_content: str | None,
        created_at: datetime,
        session_operation_context: SessionOperationContext,
    ) -> ChatMessageRecord:
        """Insert one transition response under the caller's fenced transaction."""
        self._assert_session_write_lock_held(
            conn,
            session_id,
            caller="_insert_transition_assistant",
        )
        sequence_no = self._reserve_sequence_range(conn, session_id, count=1)
        message_id = self._insert_chat_message(
            conn,
            session_id=session_id,
            role="assistant",
            content=content,
            raw_content=raw_content,
            tool_calls=None,
            sequence_no=sequence_no,
            writer_principal="compose_loop",
            composition_state_id=state_id,
            tool_call_id=None,
            parent_assistant_id=None,
            created_at=created_at,
            session_operation_context=session_operation_context,
        )
        with self._session_mutations(conn, session_id=session_id, session_operation_context=session_operation_context) as session_mutations:
            session_mutations.mark_session_updated(updated_at=created_at)
        message_row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == message_id)).one()
        return self._row_to_chat_message_record(message_row)

    def _insert_composition_state(
        self,
        conn: Connection,
        *,
        session_id: str,
        payload: StatePayload,
        provenance: str,
        session_operation_context: SessionOperationContext,
        created_at: datetime | None = None,
        state_id: str | None = None,
    ) -> str:
        """Single-row insert into composition_states with per-session
        version allocation under _session_write_lock.

        SessionMutationAuthority boundary (P4-D6 family A2b, elspeth-99949c96ca):
        ``session_operation_context`` is the operation the caller holds over
        this session (COMPOSE, PROPOSAL, or SESSION_FORK for the fork child's
        settlement) and is proved live on ``conn`` immediately before the
        INSERT -- there is no unfenced path.

        Takes a single :class:`StatePayload` carrying
        ``data`` (a :class:`CompositionStateData`) and
        ``derived_from_state_id`` rather than two separate keyword
        arguments. Bundling the two coheres with B1 — the payload object
        is the unit of state-advance, so a helper that takes it as a
        unit prevents future callers from passing inconsistent
        ``(state, derived_from_state_id)`` pairs. Critically,
        ``StatePayload`` does NOT carry a caller-supplied ``version``;
        version allocation remains inside this helper, under the held
        lock (see B1 below).

        PRECONDITION: caller MUST be inside
        ``with self._session_write_lock(conn, session_id):`` in the same
        transaction. The context is what makes the
        ``SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states
        WHERE session_id = :sid`` allocation race-free under concurrent
        writers — without it, two callers could both observe MAX = N,
        both pick N+1, and the loser's INSERT would hit
        ``uq_composition_state_version``. The locked-path
        ``IntegrityError`` handler classifies that as a Tier-1
        audit-integrity violation, fabricating a Tier-1 alert from a
        benign contention loss. **Under ELSPETH's auditability standard
        fabricated Tier-1 violations are evidence-tampering-class harm:
        the audit trail asserts a violation that did not occur.** The
        session write lock makes the SELECT-MAX-then-INSERT sequence
        atomic against every other writer for this ``session_id`` on
        both PostgreSQL and SQLite, so the fabrication path is
        structurally unreachable. Closes B1/B3 from the state-persistence
        review synthesis. Also mirrors the precondition contract on
        ``_reserve_sequence_range``.

        Version allocation is per-session: the COALESCE query filters by
        ``session_id`` because ``uq_composition_state_version`` is a
        per-session constraint. A global MAX
        would silently break the per-session monotonic-version contract
        every read path assumes.

        This helper does NOT contain a retry loop. The lock + atomic
        SELECT-INSERT makes one a defensive-programming anti-pattern.

        Writes the real per-column schema (source/nodes/edges/outputs/
        metadata_/is_valid/validation_errors/composer_meta/
        derived_from_state_id), using the shared
        ``_enveloped_state_column(...)`` and ``deep_thaw(...)`` patterns
        the existing inline inserts use today.

        The ``provenance`` argument must satisfy the
        ``ck_composition_states_provenance`` CHECK constraint; passing an
        unknown value raises ``IntegrityError``.

        The ``created_at`` argument is optional. When ``None`` (the
        default), the helper stamps ``datetime.now(UTC)`` at insert
        time. Callers that need cross-table timestamp consistency within
        a single transaction (e.g. ``fork_session`` pre-computes ``now``
        at the top of its sync block and reuses it across the
        ``sessions``, ``composition_states``, and ``chat_messages``
        inserts so all rows share one wall-clock instant) MUST pass an
        explicit ``created_at``. Earlier B1 framing (helper hardcoding
        ``now()`` silently changed fork timestamp semantics) is preserved.

        The optional ``state_id`` exists for ``fork_session``, which
        already precomputes ``copied_state_id_str`` and uses that same
        id for chat-row ``composition_state_id`` FKs and returned
        records. Other callers leave it ``None`` and let the helper
        allocate a fresh UUID.
        """
        self._assert_session_write_lock_held(
            conn,
            session_id,
            caller="_insert_composition_state",
        )
        self._require_session_write_authority_on_connection(conn, session_operation_context, session_id=session_id)
        # Unpack the bundled payload once so the rest of the body refers
        # to ``state`` and ``derived_from_state_id`` exactly as it did
        # pre-1B-refactor. This avoids touching the version-allocation
        # arithmetic, the per-column INSERT, or the IntegrityError
        # handler — all of which Schedule 1A reviewed and merged.
        state = payload.data
        derived_from_state_id = payload.derived_from_state_id
        # B1: allocate version under _session_write_lock. The
        # SELECT-MAX-then-INSERT sequence is atomic against every other
        # writer for this session because the caller is required to be
        # inside ``_session_write_lock(conn, session_id)`` for the full
        # transaction (see PRECONDITION above). The COALESCE pins the
        # first state to version 1; the WHERE clause makes the
        # allocation per-session, matching ``uq_composition_state_version``'s
        # scope.
        next_version = conn.execute(
            select(func.coalesce(func.max(composition_states_table.c.version), 0) + 1).where(
                composition_states_table.c.session_id == session_id
            )
        ).scalar_one()
        allocated_state_id = state_id or str(uuid.uuid4())
        conn.execute(
            insert(composition_states_table).values(
                id=allocated_state_id,
                session_id=session_id,
                version=int(next_version),
                source=None,
                sources=_enveloped_state_column(state.sources),
                nodes=_enveloped_state_column(state.nodes),
                edges=_enveloped_state_column(state.edges),
                outputs=_enveloped_state_column(state.outputs),
                metadata_=_enveloped_state_column(state.metadata_),
                is_valid=state.is_valid,
                validation_errors=serialize_composition_validation_errors(state.validation_errors),
                composer_meta=_enveloped_state_column(state.composer_meta),
                derived_from_state_id=derived_from_state_id,
                provenance=provenance,
                created_at=created_at if created_at is not None else datetime.now(UTC),
            )
        )
        self._supersede_dead_site_pending_interpretation_events(
            conn,
            session_id=session_id,
            state_id=allocated_state_id,
        )
        supersede_open_approvals(
            conn,
            session_id=session_id,
            now=database_now(conn),
            record=self._approval_supersession_recorder,
        )
        return allocated_state_id

    def _supersede_dead_site_pending_interpretation_events(
        self,
        conn: Connection,
        *,
        session_id: str,
        state_id: str,
    ) -> None:
        """Retire pending reviews the just-inserted head extinguished, then log them.

        The sweep itself lives in ``sessions/dead_site_supersession.py`` so the
        session-operation authority's own composition-state writers
        (``_RepositoryCompositionStateMutations.append_state`` and
        ``_RepositoryInterpretationMutations.create_or_reconcile_pending``) run
        the SAME retirement inside their own transactions. One rule for every
        writer; see that module for why the predicate is "derivation raises".

        The head is handed over as a BUILDER, not a value: the sweep short-
        circuits when nothing is pending, which is the common case, and this
        caller's derivation costs a row re-read plus a full record parse. Passing
        a materialised record here would put both on every composition-state
        insert.
        """

        def _build_state_record() -> CompositionStateRecord:
            state_row = conn.execute(
                select(composition_states_table)
                .where(composition_states_table.c.id == state_id)
                .where(composition_states_table.c.session_id == session_id)
            ).one_or_none()
            if state_row is None:  # pragma: no cover - caller inserted this row in this transaction
                raise AuditIntegrityError(
                    f"_supersede_dead_site_pending_interpretation_events: state {state_id!r} missing in session {session_id!r}"
                )
            return self._row_to_state_record(state_row)

        for retired in supersede_dead_site_pending_interpretation_events(
            conn,
            session_id=session_id,
            build_state_record=_build_state_record,
            now=self._now(),
        ):
            self._log.info(
                "interpretation_event_superseded_dead_site",
                session_id=session_id,
                event_id=retired.event_id,
                kind=retired.kind,
                affected_node_id=retired.affected_node_id,
                reason=retired.reason,
            )

    def persist_compose_turn(
        self,
        *,
        session_id: str,
        assistant_content: str,
        raw_content: str | None = None,
        redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...],
        redacted_tool_rows: tuple[RedactedToolRow, ...],
        rejection_records: tuple[RejectionRecord, ...] = (),
        parent_composition_state_id: str | None,
        expected_current_state_id: str | None,
        writer_principal: ChatMessageWriterPrincipal,
        plugin_crash_pending: bool,
        session_operation_context: SessionOperationContext,
    ) -> AuditOutcome:
        """Synchronous, single-transaction persistence of one compose turn.

        Spec §5.2.2. Concrete sync primitive. Production async callers MUST
        invoke ``await self.persist_compose_turn_async(...)`` through
        :class:`SessionServiceProtocol`; that dispatcher uses ``_run_sync``
        under the hood. Calling this sync primitive directly from async land
        would block the event loop because the body opens a synchronous
        SQLAlchemy transaction.

        The async-loop guard below uses ``asyncio.get_running_loop()`` to
        detect misuse: if there is a running loop in the calling thread,
        we are in async land and MUST refuse. ``RuntimeError`` is the
        canonical "you called the wrong API" signal — the call site is
        a bug, not a recoverable user error. Closes synthesised review
        finding SA-7 / M1.

        Order of work (load-bearing):

        1. Pre-DB transcript validation (``_validate_tool_call_id_set_equality``).
           Pure function of caller args; runs BEFORE ``_engine.begin()`` so
           a contract violation cannot leave a half-written audit trail.
        2. Open transaction; acquire session write lock.
        3. Cross-session guard on ``parent_composition_state_id`` (B5).
        4. Stale-state guard on ``expected_current_state_id``.
        5. Reserve sequence range for assistant + N tool rows.
        6. Insert assistant row (with optional ``raw_content``,
           B2 audit-attribution).
        7. For each tool row: optionally insert composition state under
           the held lock, then insert tool chat row referencing it.

        ``raw_content`` is the audit-attribution column for assistant
        messages whose visible ``content`` was rewritten by runtime
        preflight redaction. Routes already pass
        ``raw_content=result.raw_assistant_content`` to ``add_message``;
        compose-loop call sites use ``persist_compose_turn``, so the
        primitive must accept and persist the column today (B2).
        """
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop in this thread -- we are in a worker thread
            # or pure sync test context. Proceed.
            pass
        else:
            raise RuntimeError(
                "persist_compose_turn must be dispatched via "
                "await self.persist_compose_turn_async(...) -- "
                "calling it directly from a coroutine blocks the event "
                "loop on synchronous DB I/O."
            )

        # Q-F1 (Step 3c): transcript validation BEFORE _engine.begin().
        # Pre-lock, pre-transaction; pure function of caller args.
        _validate_tool_call_id_set_equality(
            redacted_assistant_tool_calls=redacted_assistant_tool_calls,
            redacted_tool_rows=redacted_tool_rows,
        )

        # Rejection records are caller-supplied context for tool rows in THIS
        # turn: an id naming no persisted tool row would orphan the reason.
        # Same pre-transaction posture as the transcript validation above.
        tool_row_ids = {row.tool_call_id for row in redacted_tool_rows}
        for record in rejection_records:
            if record.tool_call_id not in tool_row_ids:
                raise ValueError(
                    "persist_compose_turn: rejection record names tool_call_id "
                    f"{record.tool_call_id!r} which is not among this turn's "
                    f"tool rows {sorted(tool_row_ids)!r}"
                )
        now = self._now()
        # IntegrityError disposition (spec §4.5): the catch is
        # OUTSIDE ``with self._engine.begin()`` deliberately. Order is
        # load-bearing — the ``with`` context's ``__exit__`` runs first
        # (rolling back the transaction so no partial audit row survives),
        # THEN we increment the operational counter, THEN the exception
        # re-raises so the caller observes the actual constraint
        # violation. Putting the catch INSIDE the ``with`` would fire
        # before rollback completes and could mask the original error.
        #
        # Audit primacy: telemetry signals operational rate; the
        # exception itself is the authoritative signal to the caller.
        # Both channels fire; neither is suppressed. Catch is exactly
        # ``IntegrityError`` -- not ``Exception``, not ``SQLAlchemyError``
        # -- because only constraint violations belong on this counter.
        try:
            with self._session_process_locked_begin(session_id) as conn:
                with (
                    self._session_write_lock(conn, session_id),
                    self._session_composer_mutation_transaction(
                        conn,
                        session_id=session_id,
                        session_operation_context=session_operation_context,
                        expected_kind=SessionOperationKind.COMPOSE,
                    ),
                ):
                    # B5: if a parent
                    # composition state is supplied, it MUST belong to this
                    # session.
                    if parent_composition_state_id is not None:
                        _assert_state_in_session(
                            conn,
                            state_id=parent_composition_state_id,
                            expected_session_id=session_id,
                            caller="persist_compose_turn",
                        )

                    current_state_id = conn.execute(
                        select(composition_states_table.c.id)
                        .where(composition_states_table.c.session_id == session_id)
                        .order_by(composition_states_table.c.version.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if current_state_id != expected_current_state_id:
                        raise StaleComposeStateError(
                            "persist_compose_turn: current composition state changed "
                            f"for session_id={session_id!r}; "
                            f"expected={expected_current_state_id!r}, "
                            f"actual={current_state_id!r}. Refusing to persist a "
                            "compose result based on a stale state."
                        )

                    base_seq = self._reserve_sequence_range(conn, session_id, count=1 + len(redacted_tool_rows))

                    assistant_id = self._insert_chat_message(
                        conn,
                        session_id=session_id,
                        role="assistant",
                        content=assistant_content,
                        # B2: raw_content captures pre-redaction LLM output.
                        raw_content=raw_content,
                        # ``deep_thaw`` recursively converts MappingProxyType /
                        # tuple to JSON-serialisable dict / list (closes
                        # P-L-4 / L18). Mirrors ``add_message``'s pattern.
                        tool_calls=deep_thaw(redacted_assistant_tool_calls) if redacted_assistant_tool_calls else None,
                        sequence_no=base_seq,
                        writer_principal=writer_principal,
                        composition_state_id=parent_composition_state_id,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )

                    for offset, tool_row in enumerate(redacted_tool_rows, start=1):
                        state_id: str | None = None
                        if tool_row.composition_state_payload is not None:
                            payload = tool_row.composition_state_payload
                            if payload.derived_from_state_id is None:
                                # Re-derive lineage under the held session
                                # write lock (spec §5.7.1): the async loop
                                # cannot know predecessor ids for rows not
                                # yet allocated, so it leaves lineage None
                                # and this primitive fills in the verified
                                # session head — the pre-turn state for the
                                # first insert, then each previously
                                # inserted revision of this turn
                                # (elspeth-7536e5d919).
                                payload = replace(payload, derived_from_state_id=current_state_id)
                            state_id = self._insert_composition_checkpoint(
                                conn,
                                session_id=session_id,
                                state=payload.data,
                                derived_from_state_id=payload.derived_from_state_id,
                                provenance="tool_call",
                                created_at=now,
                                session_operation_context=session_operation_context,
                            )
                            current_state_id = state_id
                        self._insert_chat_message(
                            conn,
                            session_id=session_id,
                            role="tool",
                            content=tool_row.content,
                            raw_content=None,
                            tool_calls=None,
                            sequence_no=base_seq + offset,
                            writer_principal=writer_principal,
                            composition_state_id=state_id,
                            tool_call_id=tool_row.tool_call_id,
                            parent_assistant_id=assistant_id,
                            created_at=now,
                            session_operation_context=session_operation_context,
                        )
                        # elspeth-3e28029d2f: a refused mutation's reason
                        # persists unredacted alongside its (redacted) tool
                        # row, linked to the state CURRENT at rejection —
                        # ``current_state_id`` at this iteration, since a
                        # rejection commits no state of its own.
                        for record in rejection_records:
                            if record.tool_call_id != tool_row.tool_call_id:
                                continue
                            with self._session_mutations(
                                conn,
                                session_id=session_id,
                                session_operation_context=session_operation_context,
                            ) as session_mutations:
                                session_mutations.record_composition_rejection(
                                    tool_call_id=record.tool_call_id,
                                    tool_name=record.tool_name,
                                    error_code=record.error_code,
                                    message=record.message,
                                    planner_payload=record.planner_payload,
                                    composition_state_id=current_state_id,
                                    created_at=now,
                                )

                return AuditOutcome(
                    assistant_id=assistant_id,
                    unwind_audit_failed=False,
                    current_state_id=current_state_id,
                )
        except StaleComposeStateError as stale_exc:
            # StaleComposeStateError disposition (elspeth-45f72a949c):
            # the step-4 stale-state guard fired — a concurrent writer
            # advanced the session's composition state while this turn
            # was in flight. Nothing was inserted (the guard runs before
            # the sequence reservation) and the transaction rolled back.
            #
            # Disposition is asymmetric on ``plugin_crash_pending``,
            # mirroring the ``OperationalError`` arm below:
            #
            # 1. ``plugin_crash_pending=True`` — the caller is unwinding
            #    a captured plugin crash with possible partial effects.
            #    Letting the stale-state conflict propagate would replace
            #    that non-retryable primary failure with a misleading
            #    retryable "session changed" response, telling the user
            #    to retry a turn whose plugin already crashed mid-effect.
            #    Record the stale audit outcome (counter + slog — the
            #    secondary reconciliation context) and return
            #    ``AuditOutcome(unwind_audit_failed=True)`` so the caller
            #    re-raises the captured plugin crash as primary.
            #
            # 2. ``plugin_crash_pending=False`` — no crash in flight;
            #    the stale-state conflict IS the primary outcome and
            #    stays a retryable conflict for the route layer.
            if plugin_crash_pending:
                self._telemetry.tool_row_persist_failed_during_unwind_total.add(1)
                self._log.warning(
                    "stale_state_during_tool_failure_unwind",
                    session_id=session_id,
                    audit_exc_class=type(stale_exc).__name__,
                )
                return AuditOutcome(
                    assistant_id=None,
                    unwind_audit_failed=True,
                )
            raise
        except IntegrityError:
            self._telemetry.tool_row_integrity_violation_total.add(1)
            raise
        except OperationalError as audit_exc:
            # OperationalError disposition (spec §5.2.2 / §5.5
            # rows 9-10): the audit insert itself failed (commit-time
            # disk full, fsync failure, network partition, etc.). The
            # ``with self._engine.begin()`` context has already rolled
            # back by the time we enter this handler, so no partial
            # audit row survives.
            #
            # Disposition is asymmetric on ``plugin_crash_pending``:
            #
            # 1. ``plugin_crash_pending=True`` — the tool plugin
            #    already crashed and the caller is on the unwind path
            #    with a captured plugin exception in hand. Surfacing
            #    a separate ``AuditIntegrityError`` here would mask
            #    the original tool failure (which is what the operator
            #    needs to see). Record the audit failure via counter
            #    + slog (the slog call is permitted under the
            #    logging-telemetry-policy skill §Logging Policy
            #    because the audit system itself failed —
            #    telemetry has nowhere to write the structured event)
            #    and return ``AuditOutcome(unwind_audit_failed=True)``
            #    so the caller can raise the captured plugin
            #    exception while still surfacing that the unwind
            #    audit row could not be persisted.
            #
            # 2. ``plugin_crash_pending=False`` — the tool succeeded
            #    but the audit insert failed. This is a Tier-1 audit
            #    corruption per the trust model
            #    (docs/guides/data-trust-and-error-handling.md §The
            #    Three-Tier Trust Model): the system did
            #    work that it cannot prove it did. Returning a flag
            #    would let the caller proceed with corrupted audit
            #    state (synthesised review finding H1).
            #    ``AuditIntegrityError`` is registered in
            #    ``TIER_1_ERRORS`` via ``@tier_1_error`` on its
            #    declaration in ``contracts/errors.py``, so
            #    ``except Exception:`` blocks elsewhere cannot
            #    silently swallow it. The original ``OperationalError``
            #    is preserved as the ``__cause__`` via ``from
            #    audit_exc`` so the underlying DB error remains
            #    visible to the operator.
            if plugin_crash_pending:
                self._telemetry.tool_row_persist_failed_during_unwind_total.add(1)
                self._log.warning(
                    "audit_insert_failed_during_tool_failure_unwind",
                    session_id=session_id,
                    audit_exc_class=type(audit_exc).__name__,
                )
                return AuditOutcome(
                    assistant_id=None,
                    unwind_audit_failed=True,
                )
            self._telemetry.tool_row_tier1_violation_total.add(1)
            raise AuditIntegrityError(
                f"persist_compose_turn: audit insert failed for "
                f"session_id={session_id!r} with tool succeeded — "
                f"Tier-1 audit corruption (no recovery)"
            ) from audit_exc
        except SQLAlchemyError as audit_exc:
            # Spec §5.2.2 / §5.5 row 9: any non-Integrity, non-Operational
            # SQLAlchemyError (DataError, DatabaseError, ProgrammingError,
            # InterfaceError, DBAPIError siblings) on the audit insert
            # path is a Tier-1 audit corruption — the audit system
            # itself failed in an unforeseen way that the IntegrityError
            # / OperationalError dispositions above were not designed to
            # handle. The previous narrow catch let these subclasses
            # propagate uncaught past the disposition logic, leaving the
            # tool_row_tier1_violation_total counter dark on the
            # SLO=0 dashboard while the audit row was lost.
            #
            # Disposition matches the OperationalError success-path arm
            # (plugin_crash_pending=False): increment the Tier-1 counter
            # and raise AuditIntegrityError chained through the
            # SQLAlchemyError. Unlike OperationalError, this branch is
            # NOT asymmetric on plugin_crash_pending — there is no
            # established recovery shape for arbitrary SQLAlchemyError
            # subclasses. If a future caller's audit row fails with
            # DataError on the unwind path, masking the audit failure
            # to "preserve" a primary plugin error would be silently
            # losing a Tier-1 corruption signal; the Tier-1 raise is
            # what spec §5.5 row 9 prescribes.
            self._telemetry.tool_row_tier1_violation_total.add(1)
            raise AuditIntegrityError(
                f"persist_compose_turn: audit insert failed for "
                f"session_id={session_id!r} with non-Integrity, "
                f"non-Operational SQLAlchemyError "
                f"({type(audit_exc).__name__}) — Tier-1 audit "
                f"corruption (no recovery)"
            ) from audit_exc

    async def persist_compose_turn_async(
        self,
        *,
        session_id: str,
        assistant_content: str,
        raw_content: str | None = None,
        redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...],
        redacted_tool_rows: tuple[RedactedToolRow, ...],
        rejection_records: tuple[RejectionRecord, ...] = (),
        parent_composition_state_id: str | None,
        expected_current_state_id: str | None,
        writer_principal: ChatMessageWriterPrincipal,
        plugin_crash_pending: bool,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> AuditOutcome:
        """Async dispatcher for :meth:`persist_compose_turn`.

        Bridges to the sync primitive via ``_run_composer_sql``. Caller
        cancellation joins the actual executor future before propagating
        ``CancelledError``, retaining the lease owner until the transaction
        has completed (see ``run_stream_read_in_worker``). A submission the
        pool has not yet started when the awaiter is cancelled is dropped
        and never begins — the "rolled back" arm below, reached without a
        transaction (elspeth-5269b43bca).

        **Commit-wins cancellation contract (Q-F2).** When the caller is
        cancelled mid-flight, an already-started worker continues to run to
        completion. Either:

        1. The transaction commits — the assistant + tool rows are durably
           persisted; the caller observes ``CancelledError`` and never
           sees the ``AuditOutcome``. **Callers MUST NOT retry on
           CancelledError** — retrying risks a duplicate tool-call-ID
           INSERT that fires a fabricated Tier-1 counter increment.
        2. The transaction rolls back atomically — DB-level errors
           (``IntegrityError``, ``OperationalError``,
           ``ToolCallIDMismatchError`` raised pre-DB) cause the
           ``engine.begin()`` block to roll back. No rows persisted.
           Retry-on-CancelledError still forbidden.

        The compose loop is the only caller of this method.

        Pinned by ``test_persist_compose_turn_async_caller_cancellation_commits_anyway``.
        """

        _validate_required_sql_ticket(
            required_work, sources=(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,), session_id=session_id, context=session_operation_context
        )

        def _sync() -> AuditOutcome:
            return self.persist_compose_turn(
                session_id=session_id,
                assistant_content=assistant_content,
                raw_content=raw_content,
                redacted_assistant_tool_calls=redacted_assistant_tool_calls,
                redacted_tool_rows=redacted_tool_rows,
                rejection_records=rejection_records,
                parent_composition_state_id=parent_composition_state_id,
                expected_current_state_id=expected_current_state_id,
                writer_principal=writer_principal,
                plugin_crash_pending=plugin_crash_pending,
                session_operation_context=session_operation_context,
            )

        return await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync)

    async def create_session(
        self,
        user_id: str,
        title: str,
        auth_provider_type: AuthProviderType,
    ) -> SessionRecord:
        """Create one session plus an already-closed epoch-1 fence."""
        return cast(
            "SessionRecord",
            await self._run_sync(
                self._session_operation_authority.create_session_with_initial_fence,
                user_id=user_id,
                title=title,
                auth_provider_type=auth_provider_type,
                owner_instance_id=self._owner_instance_id,
                lease_seconds=self._session_operation_lease_seconds,
            ),
        )

    def _row_to_session_record(self, row: Any) -> SessionRecord:
        return SessionRecord(
            id=UUID(row.id),
            user_id=row.user_id,
            auth_provider_type=row.auth_provider_type,
            title=row.title,
            created_at=restore_utc(row.created_at),
            updated_at=restore_utc(row.updated_at),
            archived_at=restore_utc(row.archived_at) if row.archived_at else None,
            forked_from_session_id=UUID(row.forked_from_session_id) if row.forked_from_session_id else None,
            forked_from_message_id=UUID(row.forked_from_message_id) if row.forked_from_message_id else None,
        )

    def get_session_for_stream(self, session_id: UUID) -> SessionRecord:
        """Read owned session scope synchronously for joined observer SQL custody."""
        with self._engine.begin() as conn:
            row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one_or_none()
            if row is None:
                raise SessionNotFoundError(session_id)
            return self._row_to_session_record(row)

    async def get_session(self, session_id: UUID) -> SessionRecord:
        """Fetch a session by ID. Raises SessionNotFoundError if not found."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).fetchone()

        row = await self._run_sync(_sync)

        if row is None:
            raise SessionNotFoundError(session_id)

        return self._row_to_session_record(row)

    async def update_session_title(
        self,
        session_id: UUID,
        title: str,
        *,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkTicket | None = None,
    ) -> SessionRecord:
        """Update a session title under the caller's COMPOSE operation and return the refreshed record."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        sid = str(session_id)
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.TITLE_ACCOUNTING_SQL,),
            session_id=sid,
            context=session_operation_context,
        )
        now = self._now()

        def _write(conn: Connection) -> Any:
            if conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == sid)).one_or_none() is None:
                raise SessionNotFoundError(session_id)
            with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
                session_mutations.set_title(title=title, updated_at=now)
            return conn.execute(select(sessions_table).where(sessions_table.c.id == sid)).one()

        def _sync() -> Any:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ),
            ):
                return _write(conn)

        row = await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync)
        return SessionRecord(
            id=UUID(row.id),
            user_id=row.user_id,
            auth_provider_type=row.auth_provider_type,
            title=row.title,
            created_at=restore_utc(row.created_at),
            updated_at=restore_utc(row.updated_at),
            archived_at=restore_utc(row.archived_at) if row.archived_at else None,
            forked_from_session_id=UUID(row.forked_from_session_id) if row.forked_from_session_id else None,
            forked_from_message_id=UUID(row.forked_from_message_id) if row.forked_from_message_id else None,
        )

    async def list_sessions(
        self,
        user_id: str,
        auth_provider_type: AuthProviderType,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[SessionRecord]:
        """List sessions for a user, ordered by updated_at descending."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                conditions: list[ColumnElement[bool]] = [
                    sessions_table.c.user_id == user_id,
                    sessions_table.c.auth_provider_type == auth_provider_type,
                    or_(
                        sessions_table.c.forked_from_session_id.is_(None),
                        exists(
                            select(session_operation_receipts_table.c.operation_id).where(
                                session_operation_receipts_table.c.kind == "session_fork",
                                session_operation_receipts_table.c.status == "completed",
                                session_operation_receipts_table.c.result_session_id == sessions_table.c.id,
                            )
                        ),
                    ),
                ]
                if not include_archived:
                    conditions.append(sessions_table.c.archived_at.is_(None))
                return conn.execute(
                    select(sessions_table).where(*conditions).order_by(desc(sessions_table.c.updated_at)).limit(limit).offset(offset)
                ).fetchall()

        rows = await self._run_sync(_sync)

        return [
            SessionRecord(
                id=UUID(row.id),
                user_id=row.user_id,
                auth_provider_type=row.auth_provider_type,
                title=row.title,
                created_at=restore_utc(row.created_at),
                updated_at=restore_utc(row.updated_at),
                archived_at=restore_utc(row.archived_at) if row.archived_at else None,
                forked_from_session_id=UUID(row.forked_from_session_id) if row.forked_from_session_id else None,
                forked_from_message_id=UUID(row.forked_from_message_id) if row.forked_from_message_id else None,
            )
            for row in rows
        ]

    async def reconcile_consumed_archives(self) -> int:
        """Discharge exact obligations only after terminal DB absence is proven.

        Startup and periodic callers use the same path. Custody drains late
        blob writers and serializes replica cleanup; the DB proof releases its
        transaction before any blocking filesystem work begins.
        """
        if self._data_dir is None:
            return 0
        data_dir = self._data_dir

        def _sync() -> int:
            retired = 0
            for session_id in archive_quarantine_session_ids(data_dir):
                with try_blob_custody_session_lock(self._engine, str(session_id)) as acquired:
                    if not acquired:
                        continue
                    if not self._session_operation_authority.archive_cleanup_is_consumed(session_id):
                        continue
                    # Validate all obligations before deleting any one of them.
                    list_archive_quarantine_manifests(data_dir, session_id)
                    # A discovery snapshot may precede a peer's retirement.
                    # Re-list under custody, never act on that stale snapshot.
                    for identity in archive_quarantine_operation_ids(data_dir, session_id):
                        canonical = data_dir / "blobs" / str(session_id)
                        purge_archive_quarantine(data_dir, identity, canonical)
                        retire_archive_quarantine(data_dir, identity)
                        retired += 1
            return retired

        return cast(int, await self._run_sync(_sync))

    async def archive_session(self, session_id: UUID) -> None:
        """Archive through one exact DB fence and durable filesystem obligation."""
        sid = str(session_id)

        def decide_and_soft_archive(transaction: SessionOperationMutationTransaction) -> SessionArchiveDisposition:
            return transaction.session.decide_and_soft_archive(archived_at=self._now())

        try:
            lease = await SessionOperationLease.acquire(
                self._session_operation_authority,
                session_id=session_id,
                operation_kind=SessionOperationKind.ARCHIVE,
                owner_instance_id=self._owner_instance_id,
                lease_seconds=self._session_operation_lease_seconds,
            )
        except SessionOperationFenceLost as error:
            if error.reason is FenceLossReason.MISSING:
                raise SessionNotFoundError(session_id) from error
            raise

        data_dir = self._data_dir
        identity = ArchiveQuarantineIdentity(
            session_id=session_id,
            operation_id=UUID(lease.context.fence.operation_id),
            operation_epoch=lease.context.fence.operation_epoch,
        )
        canonical = data_dir / "blobs" / sid if data_dir is not None else None
        current_obligation_may_exist = False
        current_stage_attempted = False
        authority_uncertain = False

        async def run_owned_phase[T](
            coroutine_factory: Callable[[], Coroutine[Any, Any, T]],
            *,
            name: str,
        ) -> T:
            """Shield and join one lease-owned phase before cancellation resumes."""

            async def protect_inner_phase() -> T:
                inner_task = asyncio.create_task(coroutine_factory(), name=f"{name}-inner")
                try:
                    return cast("T", await asyncio.shield(inner_task))
                except asyncio.CancelledError:
                    while not inner_task.done():
                        try:
                            await asyncio.shield(inner_task)
                        except asyncio.CancelledError:
                            continue
                    try:
                        result = inner_task.result()
                    except BaseException:
                        lease.raise_if_lost()
                        raise
                    lease.raise_if_lost()
                    return result

            task = lease.create_task(protect_inner_phase(), name=name)
            try:
                return cast("T", await asyncio.shield(task))
            except asyncio.CancelledError as cancellation:
                current_task = asyncio.current_task()
                if current_task is None or current_task.cancelling() == 0:
                    lease.raise_if_lost()
                    raise
                try:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                    task.result()
                except BaseException as phase_error:
                    # Cancellation cannot hide a failure from the joined phase.
                    raise phase_error from cancellation
                raise

        async def checkpoint() -> None:
            nonlocal authority_uncertain
            try:
                lease.raise_if_lost()
                await run_sync_in_worker(
                    self._session_operation_authority.compare_and_swap,
                    lease.context,
                )
            except BaseException:
                authority_uncertain = True
                raise

        def current_in_place_guard(action: Callable[[], None]) -> None:
            with _blob_custody_session_lock(self._engine, sid):
                self._session_operation_authority.compare_and_swap(lease.context)
                action()
                self._session_operation_authority.compare_and_swap(lease.context)

        def consumed_cleanup_guard(action: Callable[[], None]) -> None:
            with _blob_custody_session_lock(self._engine, sid):
                if not self._session_operation_authority.archive_cleanup_is_consumed(session_id):
                    raise AuditIntegrityError("archive cleanup refused without confirmed session consumption")
                action()

        async def reconcile_prior_manifests() -> None:
            assert data_dir is not None
            assert canonical is not None
            manifests = await run_sync_in_worker(
                list_archive_quarantine_manifests,
                data_dir,
                session_id,
            )
            for manifest in manifests:
                relation = await run_sync_in_worker(
                    self._session_operation_authority.classify_archive_manifest,
                    lease.context,
                    manifest_operation_id=manifest.identity.operation_id,
                    manifest_operation_epoch=manifest.identity.operation_epoch,
                )
                if type(relation) is not ArchiveManifestRelation:
                    raise AuditIntegrityError("archive manifest classifier returned an invalid relation")
                await checkpoint()
                await run_sync_in_worker(
                    restore_archive_quarantine,
                    data_dir,
                    manifest.identity,
                    canonical,
                    in_place_guard=current_in_place_guard,
                )
                await checkpoint()
                await run_sync_in_worker(
                    retire_archive_quarantine,
                    data_dir,
                    manifest.identity,
                )

        async def prepare_current_manifest() -> None:
            nonlocal current_obligation_may_exist
            assert data_dir is not None
            assert canonical is not None
            source_present = await run_sync_in_worker(
                canonical_archive_present,
                data_dir,
                identity,
                canonical,
            )
            await checkpoint()
            current_obligation_may_exist = True
            await run_sync_in_worker(
                prepare_archive_quarantine,
                data_dir,
                identity,
                source_present=source_present,
            )
            await checkpoint()

        async def stage_current_payload() -> None:
            nonlocal current_stage_attempted
            assert data_dir is not None
            assert canonical is not None
            await checkpoint()
            current_stage_attempted = True
            await run_sync_in_worker(
                stage_archive_quarantine,
                data_dir,
                identity,
                canonical,
                in_place_guard=current_in_place_guard,
            )
            await checkpoint()

        async def restore_current() -> None:
            assert data_dir is not None
            assert canonical is not None
            await checkpoint()
            await run_sync_in_worker(
                restore_archive_quarantine,
                data_dir,
                identity,
                canonical,
                in_place_guard=current_in_place_guard,
            )
            await checkpoint()
            await run_sync_in_worker(
                retire_archive_quarantine,
                data_dir,
                identity,
            )

        def record_cleanup_failure(cleanup_exc: BaseException, error_number: int | None) -> None:
            # The archive is committed; only the staged-directory purge
            # stalled. The exception that reaches the route (and any traceback
            # rendered from it) must not carry the cleanup error's text: on the
            # shared mount that text is a filesystem path (elspeth-ada35955b6;
            # platform pin fec6a4f32 restored over the 7b402f716 transplant of
            # ``from cleanup_exc``). The causal fact is recorded here as
            # structured, non-textual fields: the quarantine obligation is
            # located from the identity, and the OS reason from the errno —
            # never from the exception's own strings.
            self._log.error(
                "session_archive_quarantine_cleanup_failed",
                session_id=sid,
                operation_id=str(identity.operation_id),
                operation_epoch=identity.operation_epoch,
                error_type=type(cleanup_exc).__name__,
                errno=error_number,
                strerror=None if error_number is None else os.strerror(error_number),
            )

        async def finalize_consumed() -> None:
            assert data_dir is not None
            assert canonical is not None
            try:
                await run_sync_in_worker(
                    purge_archive_quarantine,
                    data_dir,
                    identity,
                    canonical,
                    cleanup_guard=consumed_cleanup_guard,
                )
                await run_sync_in_worker(
                    retire_archive_quarantine,
                    data_dir,
                    identity,
                    cleanup_guard=consumed_cleanup_guard,
                )
            except contract_errors.TIER_1_ERRORS:
                raise
            except OSError as cleanup_exc:
                record_cleanup_failure(cleanup_exc, cleanup_exc.errno)
                raise QuarantineCleanupError("Session archive committed, but quarantine cleanup remains pending.") from None
            except BaseException as cleanup_exc:
                record_cleanup_failure(cleanup_exc, None)
                raise QuarantineCleanupError("Session archive committed, but quarantine cleanup remains pending.") from None

        async def compensate_precommit() -> None:
            """Restore only while exact authority remains provably current."""
            nonlocal authority_uncertain
            assert data_dir is not None
            assert canonical is not None
            try:
                await checkpoint()
                if current_stage_attempted:
                    await run_sync_in_worker(
                        restore_archive_quarantine,
                        data_dir,
                        identity,
                        canonical,
                        in_place_guard=current_in_place_guard,
                    )
                    await checkpoint()
                await run_sync_in_worker(
                    retire_archive_quarantine,
                    data_dir,
                    identity,
                )
            except BaseException:
                authority_uncertain = True
                raise

        try:
            archive_disposition = await run_owned_phase(
                lambda: run_sync_in_worker(
                    self._session_operation_authority.mutate,
                    lease.context,
                    decide_and_soft_archive,
                ),
                name="session-archive-decision",
            )
            if archive_disposition is SessionArchiveDisposition.SOFT_ARCHIVED:
                await lease.close()
                return

            if data_dir is None:
                await lease.consume_archive()
                return

            await run_owned_phase(
                reconcile_prior_manifests,
                name="session-archive-quarantine-reconcile-prior",
            )
            await run_owned_phase(
                prepare_current_manifest,
                name="session-archive-quarantine-prepare",
            )
            await run_owned_phase(
                stage_current_payload,
                name="session-archive-quarantine-stage",
            )
            await run_owned_phase(
                checkpoint,
                name="session-archive-quarantine-pre-delete-checkpoint",
            )
            await lease.consume_archive(
                restore_current=restore_current,
                finalize_consumed=finalize_consumed,
            )
        except BaseException as primary_error:
            failures = [primary_error]
            if not lease.closed:
                try:
                    if data_dir is not None and current_obligation_may_exist and not authority_uncertain:
                        try:
                            await run_owned_phase(
                                compensate_precommit,
                                name="session-archive-quarantine-precommit-compensation",
                            )
                        except BaseException as compensation_error:
                            if not any(compensation_error is failure for failure in failures):
                                failures.append(compensation_error)
                finally:
                    try:
                        await lease.close()
                    except BaseException as close_error:
                        if not any(close_error is failure for failure in failures):
                            failures.append(close_error)
            if len(failures) > 1:
                raise BaseExceptionGroup("Session archive and recovery failed", failures) from None
            raise

    async def get_composer_preferences(self, session_id: UUID) -> ComposerSessionPreferencesRecord:
        """Fetch composer trust/scaffolding preferences for a session."""

        def _sync() -> ComposerSessionPreferencesRecord:
            with self._engine.connect() as conn:
                row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()
                return ComposerSessionPreferencesRecord(
                    session_id=UUID(row.id),
                    trust_mode=row.trust_mode,
                    density_default=row.density_default,
                    interpretation_review_disabled=bool(row.interpretation_review_disabled),
                    updated_at=restore_utc(row.updated_at),
                )

        return cast(ComposerSessionPreferencesRecord, await self._run_sync(_sync))

    async def update_composer_preferences(
        self,
        session_id: UUID,
        *,
        trust_mode: ComposerTrustMode,
        density_default: ComposerDensityDefault,
        actor: str,
    ) -> ComposerSessionPreferencesTransition:
        """Update composer preferences and append the audit event first.

        Returns both the prior and current ``ComposerSessionPreferencesRecord``
        wrapped in a ``ComposerSessionPreferencesTransition``. The prior
        record is loaded **inside the same write transaction** as the
        audit + state writes — no TOCTOU window between read and write.
        Phase 8 plan §"Service signature precondition (B2 — load-bearing)"
        explicitly rejects the route-handler read-before-write
        alternative on these atomicity grounds.

        Audit-primacy ordering: the ``trust_mode.changed`` row is
        inserted before the ``sessions`` UPDATE. The audit payload now
        carries ``prior_trust_mode`` (B1 — see the docstring on
        ``proposal_events_table`` in ``sessions/models.py`` for the
        full payload contract). Telemetry consumers reading
        ``prior.trust_mode`` from this return value are guaranteed to
        find the same value in the audit row, satisfying the
        audit-primacy superset rule.

        Deliberately NOT serialised on the route-level compose lock: that
        lock is held across entire compose turns (unbounded provider
        calls), so a PATCH taking it would block until the turn finished
        and a mid-plan downgrade could never beat the auto-commit. The
        ordering authority against auto-commit is instead the per-session
        write lock this transaction already holds:
        ``settle_pipeline_composition_proposal`` re-reads ``trust_mode``
        under the same lock at the commit boundary
        (``required_trust_mode``, elspeth-01d4c6e683), so a downgrade is
        either durable before settlement (and blocks it) or lands after
        the commit.
        """
        now = self._now()
        sid = str(session_id)

        def _sync() -> ComposerSessionPreferencesTransition:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                # B2 (load-bearing): load the prior record inside the same
                # transaction as the audit insert and the state update.
                # A concurrent PATCH cannot interpose because the per-
                # session write lock above serialises writes for this
                # ``sid`` and the SELECT runs inside the same connection
                # as the UPDATE.
                prior_row = conn.execute(select(sessions_table).where(sessions_table.c.id == sid)).one()
                prior_record = ComposerSessionPreferencesRecord(
                    session_id=UUID(prior_row.id),
                    trust_mode=prior_row.trust_mode,
                    density_default=prior_row.density_default,
                    interpretation_review_disabled=bool(prior_row.interpretation_review_disabled),
                    updated_at=restore_utc(prior_row.updated_at),
                )
                # Audit fires before state mutation per the
                # logging-telemetry-policy skill §The Primacy Test.
                # B1 (load-bearing):
                # the payload now carries ``prior_trust_mode`` so a
                # downstream telemetry counter emitting
                # ``{from_mode, to_mode}`` attributes remains a strict
                # subset of audit-recorded reality.
                conn.execute(
                    insert(proposal_events_table).values(
                        id=str(uuid.uuid4()),
                        session_id=sid,
                        proposal_id=None,
                        event_type="trust_mode.changed",
                        actor=actor,
                        payload={
                            "trust_mode": trust_mode,
                            "prior_trust_mode": prior_record.trust_mode,
                            "density_default": density_default,
                        },
                        created_at=now,
                    )
                )
                conn.execute(
                    update(sessions_table)
                    .where(sessions_table.c.id == sid)
                    .values(
                        trust_mode=trust_mode,
                        density_default=density_default,
                        updated_at=now,
                    )
                )
                row = conn.execute(select(sessions_table).where(sessions_table.c.id == sid)).one()
                current_record = ComposerSessionPreferencesRecord(
                    session_id=UUID(row.id),
                    trust_mode=row.trust_mode,
                    density_default=row.density_default,
                    interpretation_review_disabled=bool(row.interpretation_review_disabled),
                    updated_at=restore_utc(row.updated_at),
                )
                return ComposerSessionPreferencesTransition(prior=prior_record, current=current_record)

        return cast(ComposerSessionPreferencesTransition, await self._run_sync(_sync))

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
    ) -> CompositionProposalRecord:
        """Create a pending composer proposal and its forward audit event."""
        composer_provenance = _normalize_proposal_composer_provenance(
            composer_model_identifier=composer_model_identifier,
            composer_model_version=composer_model_version,
            composer_provider=composer_provider,
            composer_skill_hash=composer_skill_hash,
            tool_arguments_hash=tool_arguments_hash,
        )
        sid = str(session_id)
        proposal_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        def _sync() -> CompositionProposalRecord:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ) as transaction,
            ):
                return transaction.composer.create_composition_proposal(
                    proposal_id=proposal_id,
                    event_id=event_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    summary=summary,
                    rationale=rationale,
                    affects=affects,
                    arguments_json=arguments_json,
                    arguments_redacted_json=arguments_redacted_json,
                    base_state_id=base_state_id,
                    actor=actor,
                    user_message_id=user_message_id,
                    composer_provenance=composer_provenance,
                )

        return cast(CompositionProposalRecord, await self._run_sync(_sync))

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
    ) -> CompositionProposalRecord:
        with _required_sql_preflight(required_work):
            redacted_pipeline_arguments = RedactedPipelineArguments(arguments_redacted_json)
        result = await self._create_pipeline_composition_proposal(
            session_id=session_id,
            plan=plan,
            summary=summary,
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=redacted_pipeline_arguments,
            actor=actor,
            composer_model_identifier=composer_model_identifier,
            composer_model_version=composer_model_version,
            composer_provider=composer_provider,
            user_message_id=user_message_id,
            session_operation_context=session_operation_context,
            required_work=required_work,
        )
        if type(result) is not CompositionProposalRecord:
            raise AuditIntegrityError("legacy creation returned a required handoff")
        return result

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
    ) -> PipelineCreationFinishOnce:
        result = await self._create_pipeline_composition_proposal(
            session_id=session_id,
            plan=plan,
            summary=summary,
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=arguments_redacted_json,
            actor=actor,
            composer_model_identifier=composer_model_identifier,
            composer_model_version=composer_model_version,
            composer_provider=composer_provider,
            user_message_id=user_message_id,
            session_operation_context=session_operation_context,
            required_work=required_work,
            finish_once=True,
            running=running,
        )
        if isinstance(result, (PipelineCreationReturned, PipelineCreationRaised)):
            return result
        raise AuditIntegrityError("required creation returned a legacy row")

    async def _create_pipeline_composition_proposal(
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
        required_work: RequiredWorkTicket | None = None,
        finish_once: bool = False,
        running: ComposerOperationRunning | None = None,
    ) -> CompositionProposalRecord | PipelineCreationFinishOnce:
        """Atomically create one canonical pipeline row + bound event."""
        try:
            _validate_required_sql_ticket(
                required_work,
                sources=(RequiredWorkSource.PROPOSAL_CREATION_SQL,),
                session_id=str(session_id),
                context=session_operation_context,
                running=running,
            )
            with _required_sql_preflight(required_work):
                if finish_once and required_work is not None:
                    if required_work.key.authority.durable_operation_id is not None and running is None:
                        raise AuditIntegrityError("durable creation handoff omitted actual running claim")
                    if running is not None and type(running) is not ComposerOperationRunning:
                        raise AuditIntegrityError("creation handoff supplied foreign running claim")
                if type(plan) is not PipelinePlanResult:
                    raise TypeError("plan must be an exact PipelinePlanResult")
                if type(arguments_redacted_json) is not RedactedPipelineArguments:
                    raise TypeError("arguments_redacted_json must be exact RedactedPipelineArguments")
                private_pipeline_arguments = deep_thaw(plan.proposal.pipeline)
                review_arguments = (
                    owned_composition_state_review_arguments(private_pipeline_arguments)
                    if is_owned_composition_state_authority(private_pipeline_arguments)
                    else private_pipeline_arguments
                )
                expected_redacted_arguments = redact_tool_call_arguments(
                    "set_pipeline",
                    cast(dict[str, Any], review_arguments),
                    telemetry=NoopRedactionTelemetry(),
                )
                supplied_redacted_arguments = deep_thaw(arguments_redacted_json.value)
                if supplied_redacted_arguments != expected_redacted_arguments:
                    raise AuditIntegrityError("pipeline proposal redacted arguments do not match the manifest projection")
                proposal = plan.proposal
                normalized = _normalize_proposal_composer_provenance(
                    composer_model_identifier=composer_model_identifier,
                    composer_model_version=composer_model_version,
                    composer_provider=composer_provider,
                    composer_skill_hash=proposal.skill_hash,
                    tool_arguments_hash=composer_authority_hash(proposal.pipeline),
                )
                assert all(value is not None for value in normalized.values())
                payload = _pipeline_created_payload(
                    plan=plan,
                    user_message_id=user_message_id,
                    composer_model_identifier=cast(str, normalized["composer_model_identifier"]),
                    composer_model_version=cast(str, normalized["composer_model_version"]),
                    composer_provider=cast(str, normalized["composer_provider"]),
                    summary=summary,
                    rationale=rationale,
                    affects=affects,
                    arguments_redacted_json=supplied_redacted_arguments,
                )
        except BaseException as error:
            if not finish_once or required_work is None or not required_work.complete:
                raise
            refused = RequiredSQLRaised(error, ())
            required_work.observe_finish_once_handoff(refused)
            return PipelineCreationRaised(refused)
        sid = str(session_id)
        proposal_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        def _sync() -> CompositionProposalRecord:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ) as transaction,
            ):
                binding = derive_proposal_composer_binding_on_connection(
                    conn,
                    session_operation_context,
                    user_message_id=user_message_id,
                    actor=actor,
                    running=running,
                )
                if required_work is not None:
                    scope = required_work.key.authority
                    if binding is None:
                        if scope.durable_operation_id is not None or scope.claim_attempt is not None:
                            raise AuditIntegrityError("proposal creation receipt invents durable claim custody")
                    elif scope.durable_operation_id != binding.operation_id or scope.claim_attempt != binding.attempt:
                        raise AuditIntegrityError("proposal creation receipt claim differs from locked authority")
                payload["composer_operation"] = (
                    {
                        "operation_id": binding.operation_id,
                        "session_operation_id": binding.session_operation_id,
                        "session_operation_epoch": binding.session_operation_epoch,
                        "attempt": binding.attempt,
                    }
                    if binding is not None
                    else None
                )
                return transaction.composer.create_pipeline_composition_proposal(
                    proposal_id=proposal_id,
                    event_id=event_id,
                    plan=plan,
                    summary=summary,
                    rationale=rationale,
                    affects=affects,
                    arguments_redacted_json=supplied_redacted_arguments,
                    actor=actor,
                    normalized_provenance=normalized,
                    payload=payload,
                    user_message_id=user_message_id,
                )

        if finish_once:
            if required_work is None:
                raise AuditIntegrityError("creation finish-once requires its registered SQL ticket")
            outcome = await run_required_sql_finish_once(required_work, _sync)
            if type(outcome) is RequiredSQLRaised:
                return PipelineCreationRaised(outcome)
            post_sql_failures: tuple[BaseException, ...] = ()
            try:
                _PIPELINE_PLANNER_COUNTER.add(1, {"surface": "freeform", "result": "proposal_created"})
                _PIPELINE_CUSTODY_COUNTER.add(1, {"surface": "freeform", "result": plan.custody_result})
            except BaseException as error:
                post_sql_failures = (error,)
            return PipelineCreationReturned(cast(RequiredSQLReturned[CompositionProposalRecord], outcome), post_sql_failures)
        record = (
            await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync)
        )
        _PIPELINE_PLANNER_COUNTER.add(1, {"surface": "freeform", "result": "proposal_created"})
        _PIPELINE_CUSTODY_COUNTER.add(1, {"surface": "freeform", "result": plan.custody_result})
        return record

    async def get_authoritative_pipeline_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        required_work: RequiredWorkTicket | None = None,
    ) -> AuthoritativePipelineProposal:
        """Load one canonical pipeline authority, rejecting current tool proposals."""
        authority = await self.get_authoritative_composition_proposal(
            session_id=session_id,
            proposal_id=proposal_id,
            required_work=required_work,
        )
        if authority.pipeline is None:
            raise ValueError("proposal uses the current tool-proposal lifecycle contract")
        return authority.pipeline

    async def get_authoritative_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        required_work: RequiredWorkTicket | None = None,
    ) -> AuthoritativeCompositionProposal:
        """Load exactly one row and one event, then classify without fallback."""
        sid = str(session_id)
        pid = str(proposal_id)
        _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.PREPARATION_READ_SQL,), session_id=sid)
        if required_work is not None and required_work.key.authority.proposal_id is not None:
            _validate_required_sql_ticket(
                required_work, sources=(RequiredWorkSource.PREPARATION_READ_SQL,), session_id=sid, proposal_id=pid
            )

        def _sync() -> AuthoritativeCompositionProposal:
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if row is None:
                    raise KeyError(pid)
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("pipeline proposal must have exactly one authoritative creation event")
                authority = _classify_authoritative_composition_proposal(
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                )
                if authority.pipeline is not None:
                    _verify_pipeline_lifecycle_authority(
                        conn,
                        service=self,
                        authority=authority.pipeline,
                    )
                return authority

        return cast(
            AuthoritativeCompositionProposal,
            await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync),
        )

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
        session_operation_context: SessionOperationContext,
        transition_assistant: TransitionAssistantDraft | None = None,
        required_trust_mode: ComposerTrustMode | None = None,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...] = (),
        running: ComposerOperationRunning | None = None,
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineProposalSettlementResult:
        result = await self._settle_pipeline_composition_proposal(
            session_id=session_id,
            proposal_id=proposal_id,
            draft_hash=draft_hash,
            state=state,
            candidate_content_hash=candidate_content_hash,
            executor_content_hash=executor_content_hash,
            final_composer_metadata=final_composer_metadata,
            dispatch=dispatch,
            actor=actor,
            session_operation_context=session_operation_context,
            transition_assistant=transition_assistant,
            required_trust_mode=required_trust_mode,
            prepared_interpretations=prepared_interpretations,
            running=running,
            required_work=required_work,
        )
        if type(result) is not PipelineProposalSettlementResult:
            raise AuditIntegrityError("legacy pipeline settlement returned a required handoff")
        return result

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
    ) -> ComposerPipelineFinishOnce:
        if type(coordinator) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("required pipeline handoff needs its registered coordinator")
        coordinator.validate_pipeline_publication_work(
            publication_ticket=required_work,
            projection_ticket=publication_projection_work,
            revocation_ticket=revocation_required_work,
            revocation_projection_ticket=revocation_projection_work,
        )
        work = PipelineFinishOnceWork(
            coordinator, required_work, publication_projection_work, revocation_required_work, revocation_projection_work
        )
        for ticket, source in (
            (publication_projection_work, RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION),
            (revocation_required_work, RequiredWorkSource.TRUST_REVOCATION_SQL),
            (revocation_projection_work, RequiredWorkSource.TRUST_REVOCATION_PROJECTION),
        ):
            _validate_required_sql_ticket(
                ticket,
                sources=(source,),
                session_id=str(session_id),
                context=session_operation_context,
                running=running,
                proposal_id=str(proposal_id),
                tool_call_id=dispatch.tool_call_id,
            )
        try:
            result = await self._settle_pipeline_composition_proposal(
                session_id=session_id,
                proposal_id=proposal_id,
                draft_hash=draft_hash,
                state=state,
                candidate_content_hash=candidate_content_hash,
                executor_content_hash=executor_content_hash,
                final_composer_metadata=final_composer_metadata,
                dispatch=dispatch,
                actor=actor,
                session_operation_context=session_operation_context,
                transition_assistant=transition_assistant,
                required_trust_mode=required_trust_mode,
                prepared_interpretations=prepared_interpretations,
                running=running,
                required_work=required_work,
                finish_once_work=work,
            )
        except BaseException as error:
            if not required_work.complete:
                raise
            outcome = RequiredSQLRaised(error, ())
            try:
                metadata = coordinator.issue_publication_projection_unused(
                    publication_ticket=required_work, projection_ticket=publication_projection_work, actual_outcome=outcome
                )
            except BaseException:
                # A downstream unknown outcome is not a publication preflight
                # failure. Preserve its actual custody signal and issue no arm.
                raise error from error.__cause__
            if metadata is None:
                raise AuditIntegrityError("pipeline preflight failure lacks unused projection evidence") from error
            for target in (revocation_required_work, revocation_projection_work):
                target.complete_unused(
                    coordinator.issue_revocation_work_unused(
                        target_ticket=target, publication_ticket=required_work, publication_outcome=outcome
                    )
                )
            return ComposerPipelineRaised(error, (), metadata.disposition, metadata)
        if isinstance(result, (ComposerPipelineBusinessReturned, ComposerPipelineRevocationCompleted, ComposerPipelineRaised)):
            return result
        raise AuditIntegrityError("required pipeline settlement returned a legacy result")

    async def _settle_pipeline_composition_proposal(
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
        finish_once_work: PipelineFinishOnceWork | None = None,
    ) -> PipelineProposalSettlementResult | ComposerPipelineFinishOnce:
        """Atomically publish state and settle a verified pipeline proposal.

        ``required_trust_mode`` is the commit-boundary trust check
        (elspeth-01d4c6e683): auto-commit callers pass ``"auto_commit"``
        and the session's trust mode is re-read inside this write
        transaction — under the same per-session write lock
        ``update_composer_preferences`` serialises on — recording one exact
        non-terminal revocation event on mismatch. The transaction commits
        before ``TrustModeAutoCommitRevokedError`` is raised. Only the
        pending->committed transition is guarded; the committed-replay
        branch returns the already-durable result regardless, so exact
        retries are never stranded by a later downgrade. Manual review
        approval passes ``None``.
        """
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.PIPELINE_PUBLICATION_SQL,),
            session_id=str(session_id),
            context=session_operation_context,
            running=running,
            proposal_id=str(proposal_id),
            tool_call_id=dispatch.tool_call_id if type(dispatch) is PipelineDispatchAuditBinding else None,
        )
        if type(dispatch) is not PipelineDispatchAuditBinding:
            _refuse_required_sql(required_work, TypeError("dispatch must be an exact PipelineDispatchAuditBinding"))
        try:
            state_content_hash = _composition_state_data_content_hash(state)
            settled_state = replace(state, composer_meta=final_composer_metadata)
            operation_kind = self._pipeline_settlement_operation_kind(session_operation_context)
        except BaseException as error:
            _refuse_required_sql(required_work, error)
        if candidate_content_hash != executor_content_hash or candidate_content_hash != state_content_hash:
            _refuse_required_sql(required_work, AuditIntegrityError("pipeline candidate/executor/state content hash mismatch"))
        if transition_assistant is not None and type(transition_assistant) is not TransitionAssistantDraft:
            _refuse_required_sql(required_work, TypeError("transition_assistant must be an exact TransitionAssistantDraft"))
        sid = str(session_id)
        pid = str(proposal_id)
        if type(prepared_interpretations) is not tuple or any(
            type(item) is not PreparedInterpretationEventDraft for item in prepared_interpretations
        ):
            _refuse_required_sql(required_work, TypeError("pipeline interpretation cohort must be an exact prepared tuple"))
        if len({item.event_id for item in prepared_interpretations}) != len(prepared_interpretations) or len(
            {item.tool_call_id for item in prepared_interpretations}
        ) != len(prepared_interpretations):
            _refuse_required_sql(required_work, AuditIntegrityError("pipeline prepared cohort identities are duplicated"))
        candidate_state_id = str(uuid.uuid4())
        prepared_events: list[_PreparedPendingInterpretation] = []
        for draft in prepared_interpretations:
            try:
                prepared = await self._prepare_or_create_pending_interpretation_event(
                    session_id=session_id,
                    composition_state_id=UUID(candidate_state_id),
                    affected_node_id=draft.affected_node_id,
                    tool_call_id=draft.tool_call_id,
                    user_term=draft.user_term,
                    kind=draft.kind,
                    llm_draft=draft.llm_draft,
                    model_identifier=draft.model_identifier,
                    model_version=draft.model_version,
                    provider=draft.provider,
                    composer_skill_hash=draft.composer_skill_hash,
                    surface_origin=draft.surface_origin,
                    session_operation_context=session_operation_context,
                    _event_id=draft.event_id,
                    _prepare_only=True,
                    proposed_state=settled_state,
                    preparation_work=(
                        RequiredWorkBinding(
                            finish_once_work.coordinator,
                            finish_once_work.publication.key.transition_ordinal,
                            finish_once_work.publication.key.semantic_ordinal,
                            RequiredWorkRole.TURN,
                        )
                        if finish_once_work is not None
                        else None
                    ),
                )
            except BaseException as error:
                _refuse_required_sql(required_work, error)
            if type(prepared) is not _PreparedPendingInterpretation:
                _refuse_required_sql(required_work, AuditIntegrityError("pipeline review preparation returned an invalid package"))
            prepared_events.append(prepared)

        def _sync() -> tuple[PipelinePublicationSQLResult | TrustModeAutoCommitRevokedError, bool]:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                contextlib.ExitStack() as publication_scope,
            ):
                # Stamped under the lock, not at request entry: a settlement
                # that waited on the session lock would otherwise record an
                # application time from before the transaction it commits in.
                # Stays on the Python clock like every sibling chat/session
                # row (_now); reading the database clock here would make these
                # rows incomparable with them and, on SQLite, truncate to
                # whole seconds.
                now = self._now()
                row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if row is None:
                    raise KeyError(pid)
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("pipeline proposal must have exactly one authoritative creation event")
                authority = _restore_authoritative_pipeline_proposal(
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                if authority.proposal.draft_hash != draft_hash:
                    raise StaleComposeStateError("pipeline proposal draft hash echo is stale or mismatched")
                if dispatch.tool_call_id != authority.row.tool_call_id:
                    raise AuditIntegrityError("pipeline dispatch tool call does not match proposal authority")
                if dispatch.arguments_hash != semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json):
                    raise AuditIntegrityError("pipeline dispatch arguments do not match persisted redacted proposal")
                if _persisted_pipeline_dispatch_content_hashes(conn, session_id=sid, dispatch=dispatch) != (state_content_hash,):
                    raise AuditIntegrityError("pipeline settlement requires one durable dispatch audit bound to the exact state content")

                terminal_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
                ).fetchall()
                if authority.row.status == "committed":
                    if len(terminal_rows) != 1 or terminal_rows[0].event_type != "proposal.accepted":
                        raise AuditIntegrityError("committed pipeline proposal terminal event authority is malformed")
                    if authority.row.audit_event_id != UUID(terminal_rows[0].id):
                        raise AuditIntegrityError("committed pipeline proposal terminal event pointer is malformed")
                    if authority.row.committed_state_id is None:
                        raise AuditIntegrityError("committed pipeline proposal is missing committed state")
                    committed_row = conn.execute(
                        select(composition_states_table)
                        .where(composition_states_table.c.session_id == sid)
                        .where(composition_states_table.c.id == str(authority.row.committed_state_id))
                    ).one_or_none()
                    if committed_row is None:
                        raise AuditIntegrityError("committed pipeline proposal state is missing")
                    committed_record = self._row_to_state_record(committed_row)
                    committed_hash = composition_content_hash(state_from_record(committed_record))
                    if committed_hash != state_content_hash:
                        raise AuditIntegrityError("pipeline exact retry state content mismatch")
                    if deep_thaw(committed_record.composer_meta) != deep_thaw(final_composer_metadata):
                        raise AuditIntegrityError("pipeline exact retry final metadata mismatch")
                    return (
                        self._read_pipeline_settlement_replay(
                            conn,
                            authority=authority,
                            accepted_payload=terminal_rows[0].payload,
                            candidate=committed_record,
                            prepared_interpretations=prepared_interpretations,
                            transition_assistant=transition_assistant,
                        ),
                        False,
                    )
                if authority.row.status != "pending":
                    raise ValueError(f"Proposal {pid} must be pending to commit; got {authority.row.status!r}")
                if terminal_rows:
                    raise AuditIntegrityError("pending pipeline proposal already has a terminal event")
                if required_trust_mode is not None:
                    # Commit-boundary trust re-verification: this SELECT runs
                    # inside the same locked transaction as the settlement
                    # writes, so a preference PATCH is either durable before
                    # it (mismatch -> abort to the review path) or lands only
                    # after this transaction commits.
                    current_trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == sid)).scalar_one()
                    if current_trust_mode != required_trust_mode:
                        if finish_once_work is not None:
                            if running is None or current_trust_mode != "explicit_approve" or required_trust_mode != "auto_commit":
                                raise AuditIntegrityError("required revocation lacks exact durable trust eligibility")
                            binding = prove_revocation_composer_binding_on_connection(
                                conn,
                                running,
                                user_message_id=authority.row.user_message_id,
                                actor=authority.creation_actor if authority.creation_actor is not None else "",
                            )
                            require_composer_settlement_actor_on_connection(conn, running, actor=actor)
                            if authority.creation_schema != "pipeline_proposal_created.v3" or authority.composer_operation != binding:
                                raise AuditIntegrityError("required revocation creation binding mismatch")
                            return _ComposerRevocationRequired(running, authority, dispatch, actor, state_content_hash), False
                        if running is not None:
                            self._record_composer_revocation_on_connection(
                                conn,
                                running=running,
                                authority=authority,
                                dispatch=dispatch,
                                actor=actor,
                                current_trust_mode=current_trust_mode,
                                expected_content_hash=state_content_hash,
                            )
                        else:
                            # Retained synchronous authority still obtains the complete
                            # normal guard; only the exact durable revocation proof may
                            # cross Stop/deadline, and it exposes no business mutation.
                            publication_scope.enter_context(
                                self._session_composer_mutation_transaction(
                                    conn,
                                    session_id=sid,
                                    session_operation_context=session_operation_context,
                                    expected_kind=operation_kind,
                                )
                            )
                            _record_auto_commit_revocation_on_connection(
                                conn,
                                session_id=sid,
                                proposal_id=pid,
                                required_trust_mode=required_trust_mode,
                                current_trust_mode=current_trust_mode,
                                actor=actor,
                                created_at=now,
                            )
                        return (
                            TrustModeAutoCommitRevokedError(
                                sid,
                                required=required_trust_mode,
                                current=current_trust_mode,
                            ),
                            False,
                        )

                # ALL normal business DML below retains the existing exact live
                # operation authority, including durable Stop and DB deadline.
                # The committed branch above has no DML. The preceding sealed
                # branch only emits exact operation-bound revocation evidence.
                publication_scope.enter_context(
                    self._session_composer_mutation_transaction(
                        conn,
                        session_id=sid,
                        session_operation_context=session_operation_context,
                        expected_kind=operation_kind,
                    )
                )
                settlement_binding = derive_proposal_composer_binding_on_connection(
                    conn,
                    session_operation_context,
                    user_message_id=authority.row.user_message_id,
                    actor=authority.creation_actor if authority.creation_actor is not None else actor,
                    running=running,
                )
                if settlement_binding is not None:
                    if (
                        running is None
                        or authority.creation_schema != "pipeline_proposal_created.v3"
                        or authority.composer_operation != settlement_binding
                    ):
                        raise AuditIntegrityError("durable auto-settlement requires this operation's v3 creation")
                    require_composer_settlement_actor_on_connection(conn, running, actor=actor)
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                if type(authority.proposal.base) is AbsentBase:
                    if current_row is not None:
                        raise StaleComposeStateError("pipeline proposal absent base conflicts with current state")
                    derived_from_state_id = None
                elif type(authority.proposal.base) is PresentBase:
                    if current_row is None or current_row.id != str(authority.proposal.base.state_id):
                        raise StaleComposeStateError("pipeline proposal present base state changed before settlement")
                    current_record = self._row_to_state_record(current_row)
                    current_hash = composition_content_hash(state_from_record(current_record))
                    if current_hash != authority.proposal.base.composition_content_hash:
                        raise StaleComposeStateError("pipeline proposal present base content changed before settlement")
                    derived_from_state_id = str(authority.proposal.base.state_id)
                else:
                    raise AuditIntegrityError("pipeline proposal base is malformed")

                state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=settled_state, derived_from_state_id=derived_from_state_id),
                    provenance="tool_call",
                    created_at=now,
                    session_operation_context=session_operation_context,
                    state_id=candidate_state_id,
                )
                candidate_row = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.session_id == sid,
                        composition_states_table.c.id == state_id,
                    )
                ).one()
                accepted_state = self._row_to_state_record(candidate_row)
                final_state = accepted_state
                cohort: list[ReviewCohortMember] = []
                interpretation_events: list[InterpretationEventRecord] = []
                mutation_state = _RepositoryMutationState(
                    conn, session_id=sid, database_now=database_now(conn), operation_context=session_operation_context
                )
                try:
                    mutations = _RepositoryInterpretationMutations(mutation_state)
                    for ordinal, (draft, prepared) in enumerate(zip(prepared_interpretations, prepared_events, strict=True)):
                        inserted = mutations.create_pipeline_candidate_pending(prepared.command, prepared.validator)
                        if inserted.event.id != draft.event_id or inserted.event.composition_state_id != UUID(state_id):
                            raise AuditIntegrityError("pipeline review result is not candidate-bound")
                        interpretation_events.append(inserted.event)
                        if inserted.produced_state is not None:
                            final_state = inserted.produced_state
                        cohort.append(
                            review_cohort_member(
                                draft,
                                candidate_id=state_id,
                                ordinal=ordinal,
                                event=inserted.event,
                                produced_state=inserted.produced_state,
                                produced_hash=composition_content_hash(state_from_record(inserted.produced_state))
                                if inserted.produced_state is not None
                                else None,
                            )
                        )
                finally:
                    mutation_state._close()
                transition_message = None
                assistant_binding = None
                if transition_assistant is not None:
                    transition_message = self._insert_transition_assistant(
                        conn,
                        session_id=sid,
                        state_id=str(final_state.id),
                        content=transition_assistant.content,
                        raw_content=transition_assistant.raw_content,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                    assistant_binding = TransitionAssistantBinding(
                        message_id=str(transition_message.id),
                        final_state_id=str(final_state.id),
                        content_hash=hashlib.sha256(transition_message.content.encode("utf-8")).hexdigest(),
                        raw_content_hash=hashlib.sha256(transition_message.raw_content.encode("utf-8")).hexdigest()
                        if transition_message.raw_content is not None
                        else None,
                    )
                event_id = str(uuid.uuid4())
                legacy_payload = _pipeline_accepted_payload(
                    authority=authority,
                    state_id=state_id,
                    state_content_hash=state_content_hash,
                    final_composer_metadata=final_composer_metadata,
                    dispatch=dispatch,
                )
                terminal_payload = PipelineAcceptedEvidence(
                    schema="pipeline_proposal_accepted.v2",
                    tool_call_id=authority.row.tool_call_id,
                    tool_name="set_pipeline",
                    status="committed",
                    outcome="accepted",
                    draft_hash=authority.proposal.draft_hash,
                    committed_state_id=state_id,
                    committed_state_content_hash=state_content_hash,
                    final_composer_metadata_hash=legacy_payload["final_composer_metadata_hash"],
                    dispatch=PipelineDispatchEvidence.model_validate(dispatch.to_dict()),
                    creation_composer_operation=authority.composer_operation,
                    settlement_composer_operation=settlement_binding,
                    review_cohort=tuple(cohort),
                    final_state_id=str(final_state.id),
                    final_state_content_hash=composition_content_hash(state_from_record(final_state)),
                    transition_assistant=assistant_binding,
                ).model_dump(mode="json", by_alias=True)
                conn.execute(
                    insert(proposal_events_table).values(
                        id=event_id,
                        session_id=sid,
                        proposal_id=pid,
                        event_type="proposal.accepted",
                        actor=actor,
                        payload=terminal_payload,
                        created_at=now,
                    )
                )
                settled = conn.execute(
                    update(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                    .where(composition_proposals_table.c.status == "pending")
                    .values(
                        status="committed",
                        committed_state_id=state_id,
                        audit_event_id=event_id,
                        updated_at=now,
                    )
                )
                if settled.rowcount != 1:
                    # The status guard above reads the authority snapshot; this
                    # predicate is what actually closes the window. A zero-row
                    # CAS means the proposal left ``pending`` under us, so the
                    # accepted event and state must roll back with it rather
                    # than reporting a terminalization that never happened.
                    raise AuditIntegrityError("pipeline proposal left pending before settlement committed")
                settled_row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one()
                return (
                    PipelineProposalSettlementResult(
                        proposal=replace(
                            _proposal_record_from_row(settled_row),
                            pipeline_metadata=_pipeline_public_metadata(authority),
                        ),
                        state=final_state,
                        accepted_state=accepted_state,
                        interpretation_events=tuple(interpretation_events),
                        transition_message=transition_message,
                    ),
                    True,
                )

        if finish_once_work is not None:

            def _publication() -> PipelinePublicationSQLResult:
                actual, transitioned = _sync()
                if isinstance(actual, TrustModeAutoCommitRevokedError):
                    raise AuditIntegrityError("required publication returned legacy revocation")
                if transitioned:
                    _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "freeform", "result": "accepted"})
                return actual

            return await self._run_pipeline_publication_finish_once(finish_once_work, _publication)

        result, transitioned = cast(
            tuple[PipelineProposalSettlementResult | TrustModeAutoCommitRevokedError, bool],
            await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync),
        )
        if type(result) is TrustModeAutoCommitRevokedError:
            raise result
        settled_result = cast(PipelineProposalSettlementResult, result)
        if transitioned:
            assert settled_result.proposal.pipeline_metadata is not None
            _PIPELINE_SETTLEMENT_COUNTER.add(
                1,
                {"surface": "freeform", "result": "accepted"},
            )
        return settled_result

    async def _run_pipeline_publication_finish_once(
        self,
        work: PipelineFinishOnceWork,
        publication: Callable[[], PipelinePublicationSQLResult],
    ) -> ComposerPipelineFinishOnce:
        outcome = await run_required_sql_finish_once(work.publication, publication)
        metadata = work.coordinator.issue_publication_projection_unused(
            publication_ticket=work.publication,
            projection_ticket=work.publication_projection,
            actual_outcome=outcome,
        )
        if metadata is None or isinstance(outcome, RequiredSQLRaised):
            for target in (work.revocation, work.revocation_projection):
                target.complete_unused(
                    work.coordinator.issue_revocation_work_unused(
                        target_ticket=target,
                        publication_ticket=work.publication,
                        publication_outcome=outcome,
                    )
                )
            if isinstance(outcome, RequiredSQLRaised):
                if metadata is None:
                    raise AuditIntegrityError("failed publication lacks exact projection disposition")
                return ComposerPipelineRaised(outcome.error, outcome.deferred_cancellations, metadata.disposition, metadata)
            if not isinstance(outcome.value, PipelineProposalSettlementResult):
                raise AuditIntegrityError("business publication lacks an owned settlement result")
            return ComposerPipelineBusinessReturned(outcome.value, outcome.deferred_cancellations)
        eligibility = outcome.value
        if type(eligibility) is not _ComposerRevocationRequired:
            raise AuditIntegrityError("required publication eligibility is not owned")
        revocation = await run_required_sql_finish_once(
            work.revocation,
            self._record_required_composer_revocation_sync,
            eligibility,
        )
        cancellations = list(outcome.deferred_cancellations)
        for cancellation in revocation.deferred_cancellations:
            if not any(cancellation is retained for retained in cancellations):
                cancellations.append(cancellation)
        if isinstance(revocation, RequiredSQLRaised):
            work.revocation_projection.complete_unused(
                work.coordinator.issue_revocation_work_unused(
                    target_ticket=work.revocation_projection,
                    publication_ticket=work.publication,
                    publication_outcome=outcome,
                    revocation_ticket=work.revocation,
                    revocation_outcome=revocation,
                )
            )
            return ComposerPipelineRaised(revocation.error, tuple(cancellations), metadata.disposition, metadata)
        work.revocation_projection.begin_projection()
        try:
            event = decode_composer_revocation_result(revocation.value)
        except BaseException as error:
            work.revocation_projection.complete_owned(error)
            return ComposerPipelineRaised(error, tuple(cancellations), metadata.disposition, metadata)
        work.revocation_projection.complete_owned()
        if metadata.disposition is not PublicationProjectionDisposition.ELIGIBILITY_SELECTED:
            raise AuditIntegrityError("revocation projection disposition lost actual eligibility")
        return ComposerPipelineRevocationCompleted(
            event, tuple(cancellations), PublicationProjectionDisposition.ELIGIBILITY_SELECTED, metadata
        )

    def _record_required_composer_revocation_sync(
        self,
        eligibility: _ComposerRevocationRequired,
    ) -> ComposerRevocationSQLResult:
        """Re-prove sealed evidence under a distinct locked transaction."""
        if type(eligibility) is not _ComposerRevocationRequired:
            raise AuditIntegrityError("required revocation needs owned publication eligibility")
        sid = str(eligibility.authority.row.session_id)
        pid = str(eligibility.authority.row.id)
        with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
            row = conn.execute(
                select(composition_proposals_table).where(
                    composition_proposals_table.c.session_id == sid, composition_proposals_table.c.id == pid
                )
            ).one()
            creations = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.session_id == sid,
                    proposal_events_table.c.proposal_id == pid,
                    proposal_events_table.c.event_type == "proposal.created",
                )
            ).all()
            if len(creations) != 1:
                raise AuditIntegrityError("required revocation creation authority changed")
            current = _restore_authoritative_pipeline_proposal(
                row=_proposal_record_from_row(row), creation_event=_proposal_event_record_from_row(creations[0])
            )
            _verify_pipeline_lifecycle_authority(conn, service=self, authority=current)
            if current != eligibility.authority:
                raise AuditIntegrityError("required revocation immutable proposal authority changed")
            record = self._record_composer_revocation_on_connection(
                conn,
                running=eligibility.running,
                authority=current,
                dispatch=eligibility.dispatch,
                actor=eligibility.actor,
                current_trust_mode=eligibility.current_trust_mode,
                expected_content_hash=eligibility.expected_content_hash,
            )
            if current.composer_operation is None or current.row.user_message_id is None:
                raise AuditIntegrityError("required revocation lacks immutable operation/user binding")
            expected = ComposerRevocationEvidence(
                schema="auto_commit.revoked.v2",
                required_trust_mode=eligibility.required_trust_mode,
                current_trust_mode=eligibility.current_trust_mode,
                composer_operation=current.composer_operation,
                proposal_id=pid,
                user_message_id=str(current.row.user_message_id),
                tool_call_id=current.row.tool_call_id,
                dispatch=PipelineDispatchEvidence.model_validate(eligibility.dispatch.to_dict()),
            )
            return ComposerRevocationSQLResult(record, ComposerRevocationExpected(sid, pid, eligibility.actor, expected))

    def _record_composer_revocation_on_connection(
        self,
        conn: Connection,
        *,
        running: ComposerOperationRunning,
        authority: AuthoritativePipelineProposal,
        dispatch: PipelineDispatchAuditBinding,
        actor: str,
        current_trust_mode: str,
        expected_content_hash: str,
    ) -> ProposalEventRecord:
        """Sealed operation-bound evidence; never grants business mutation authority."""
        binding = prove_revocation_composer_binding_on_connection(
            conn,
            running,
            user_message_id=authority.row.user_message_id,
            actor=authority.creation_actor if authority.creation_actor is not None else "",
        )
        require_composer_settlement_actor_on_connection(conn, running, actor=actor)
        if (
            authority.creation_schema != "pipeline_proposal_created.v3"
            or authority.composer_operation != binding
            or authority.row.session_id != running.claim.session_id
            or authority.row.status != "pending"
            or authority.row.user_message_id is None
            or current_trust_mode != "explicit_approve"
        ):
            raise AuditIntegrityError("sealed revocation proposal/operation/trust mismatch")
        sid, pid = str(authority.row.session_id), str(authority.row.id)
        terminals = conn.execute(
            select(proposal_events_table.c.id)
            .where(
                proposal_events_table.c.session_id == sid,
                proposal_events_table.c.proposal_id == pid,
                proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")),
            )
            .limit(1)
        ).scalar_one_or_none()
        current = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == sid)).scalar_one()
        if terminals is not None or current != current_trust_mode:
            raise AuditIntegrityError("sealed revocation pending/trust authority changed")
        if (
            dispatch.tool_call_id != authority.row.tool_call_id
            or dispatch.arguments_hash != semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json)
            or _persisted_pipeline_dispatch_content_hashes(conn, session_id=sid, dispatch=dispatch) != (expected_content_hash,)
        ):
            raise AuditIntegrityError("sealed revocation lacks exact successful dispatch authority")
        payload = ComposerRevocationEvidence(
            schema="auto_commit.revoked.v2",
            required_trust_mode="auto_commit",
            current_trust_mode="explicit_approve",
            composer_operation=binding,
            proposal_id=pid,
            user_message_id=str(authority.row.user_message_id),
            tool_call_id=authority.row.tool_call_id,
            dispatch=PipelineDispatchEvidence.model_validate(dispatch.to_dict()),
        ).model_dump(mode="json", by_alias=True)
        existing = conn.execute(
            select(proposal_events_table).where(
                proposal_events_table.c.session_id == sid,
                proposal_events_table.c.proposal_id == pid,
                proposal_events_table.c.event_type == "auto_commit.revoked",
            )
        ).all()
        if len(existing) > 1:
            raise AuditIntegrityError("sealed revocation authority is duplicated")
        if existing:
            record = _proposal_event_record_from_row(existing[0])
            if record.actor != actor or deep_thaw(record.payload) != payload:
                raise AuditIntegrityError("sealed revocation immutable replay mismatch")
            return record
        event_id = str(uuid.uuid4())
        conn.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=sid,
                proposal_id=pid,
                event_type="auto_commit.revoked",
                actor=actor,
                payload=payload,
                created_at=self._now(),
            )
        )
        return _proposal_event_record_from_row(
            conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == event_id)).one()
        )

    def _read_pipeline_settlement_replay(
        self,
        conn: Connection,
        *,
        authority: AuthoritativePipelineProposal,
        accepted_payload: object,
        candidate: CompositionStateRecord,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...],
        transition_assistant: TransitionAssistantDraft | None,
        verify_requested_assistant: bool = True,
    ) -> PipelineProposalSettlementResult:
        try:
            accepted = PipelineAcceptedEvidence.model_validate_json(canonical_json(accepted_payload))
        except (ValidationError, TypeError, ValueError) as exc:
            raise AuditIntegrityError("pipeline replay requires complete closed accepted-v2 evidence") from exc
        if accepted.creation_composer_operation != authority.composer_operation or accepted.committed_state_id != str(candidate.id):
            raise AuditIntegrityError("pipeline replay candidate/creation binding mismatch")
        for binding in (accepted.creation_composer_operation, accepted.settlement_composer_operation):
            if binding is not None:
                verify_historical_proposal_composer_binding_on_connection(
                    conn,
                    binding=binding,
                    session_id=authority.row.session_id,
                    user_message_id=authority.row.user_message_id,
                    actor=authority.creation_actor,
                )
        if len(prepared_interpretations) != len(accepted.review_cohort):
            raise AuditIntegrityError("pipeline replay semantic cohort length mismatch")
        sid = str(authority.row.session_id)
        previous = candidate
        events: list[InterpretationEventRecord] = []
        for ordinal, (draft, member) in enumerate(zip(prepared_interpretations, accepted.review_cohort, strict=True)):
            if review_semantic_material(draft, candidate_id=str(candidate.id), ordinal=ordinal) != member.semantic_material:
                raise AuditIntegrityError("pipeline replay ordered semantic cohort mismatch")
            event_row = conn.execute(
                select(interpretation_events_table).where(
                    interpretation_events_table.c.session_id == sid,
                    interpretation_events_table.c.id == member.event_id,
                )
            ).one_or_none()
            if event_row is None:
                raise AuditIntegrityError("pipeline replay bound review event missing")
            event = _interpretation_event_record_from_row(event_row)
            verify_review_event_material(member, event)
            events.append(event)
            if member.produced_state_id is not None:
                produced_row = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.session_id == sid,
                        composition_states_table.c.id == member.produced_state_id,
                    )
                ).one_or_none()
                if produced_row is None or produced_row.provenance != "interpretation_resolve":
                    raise AuditIntegrityError("pipeline replay bound derived state missing or misattributed")
                produced = self._row_to_state_record(produced_row)
                if composition_content_hash(state_from_record(produced)) != member.produced_state_content_hash:
                    raise AuditIntegrityError("pipeline replay derived content mismatch")
                verify_opt_out_transformation(member, previous=previous, produced=produced)
                previous = produced
        if (
            str(previous.id) != accepted.final_state_id
            or composition_content_hash(state_from_record(previous)) != accepted.final_state_content_hash
        ):
            raise AuditIntegrityError("pipeline replay explicit final head mismatch")
        assistant = accepted.transition_assistant
        transition_message = None
        if assistant is None:
            if verify_requested_assistant and transition_assistant is not None:
                raise AuditIntegrityError("pipeline replay cannot add an omitted assistant")
        else:
            message_row = conn.execute(
                select(chat_messages_table).where(
                    chat_messages_table.c.session_id == sid,
                    chat_messages_table.c.id == assistant.message_id,
                )
            ).one_or_none()
            if message_row is None:
                raise AuditIntegrityError("pipeline replay bound assistant missing")
            transition_message = self._row_to_chat_message_record(message_row)
            if (
                message_row.role != "assistant"
                or message_row.writer_principal != "compose_loop"
                or message_row.composition_state_id != accepted.final_state_id
                or hashlib.sha256(message_row.content.encode("utf-8")).hexdigest() != assistant.content_hash
                or (hashlib.sha256(message_row.raw_content.encode("utf-8")).hexdigest() if message_row.raw_content is not None else None)
                != assistant.raw_content_hash
                or (
                    verify_requested_assistant
                    and (
                        transition_assistant is None
                        or message_row.content != transition_assistant.content
                        or message_row.raw_content != transition_assistant.raw_content
                    )
                )
            ):
                raise AuditIntegrityError("pipeline replay assistant immutable binding mismatch")
        return PipelineProposalSettlementResult(
            proposal=replace(authority.row, pipeline_metadata=_pipeline_public_metadata(authority)),
            state=previous,
            accepted_state=candidate,
            interpretation_events=tuple(events),
            transition_message=transition_message,
        )

    async def replay_pipeline_composition_proposal(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...],
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineProposalSettlementResult:
        """Read only the complete committed cohort and explicit final head."""
        if type(authority) is not AuthoritativePipelineProposal:
            _refuse_required_sql(required_work, TypeError("pipeline replay requires exact proposal authority"))
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.POSTCOMMIT_REVIEW_READ_SQL,),
            session_id=str(authority.row.session_id),
            proposal_id=str(authority.row.id),
            tool_call_id=authority.row.tool_call_id,
        )
        if type(prepared_interpretations) is not tuple or any(
            type(item) is not PreparedInterpretationEventDraft for item in prepared_interpretations
        ):
            _refuse_required_sql(required_work, TypeError("pipeline replay requires an exact prepared tuple"))
        sid, pid = str(authority.row.session_id), str(authority.row.id)

        def _sync() -> PipelineProposalSettlementResult:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(composition_proposals_table).where(
                        composition_proposals_table.c.session_id == sid,
                        composition_proposals_table.c.id == pid,
                    )
                ).one_or_none()
                creations = conn.execute(
                    select(proposal_events_table).where(
                        proposal_events_table.c.session_id == sid,
                        proposal_events_table.c.proposal_id == pid,
                        proposal_events_table.c.event_type == "proposal.created",
                    )
                ).all()
                if row is None or len(creations) != 1:
                    raise AuditIntegrityError("pipeline replay creation authority missing or ambiguous")
                current = _restore_authoritative_pipeline_proposal(
                    row=_proposal_record_from_row(row), creation_event=_proposal_event_record_from_row(creations[0])
                )
                if current != authority or current.row.status != "committed" or current.row.committed_state_id is None:
                    raise AuditIntegrityError("pipeline replay proposal authority changed")
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=current)
                candidate_row = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.session_id == sid,
                        composition_states_table.c.id == str(current.row.committed_state_id),
                    )
                ).one_or_none()
                terminals = conn.execute(
                    select(proposal_events_table).where(
                        proposal_events_table.c.session_id == sid,
                        proposal_events_table.c.proposal_id == pid,
                        proposal_events_table.c.event_type == "proposal.accepted",
                    )
                ).all()
                if candidate_row is None or len(terminals) != 1:
                    raise AuditIntegrityError("pipeline replay committed authority missing or ambiguous")
                return self._read_pipeline_settlement_replay(
                    conn,
                    authority=current,
                    accepted_payload=terminals[0].payload,
                    candidate=self._row_to_state_record(candidate_row),
                    prepared_interpretations=prepared_interpretations,
                    transition_assistant=None,
                    verify_requested_assistant=False,
                )

        return await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync)

    async def get_pipeline_dispatch_recovery(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        required_work: RequiredWorkTicket | None = None,
    ) -> PipelineDispatchRecovery | None:
        """Return one content-bound durable dispatch for pending recovery."""
        if type(authority) is not AuthoritativePipelineProposal:
            _refuse_required_sql(required_work, TypeError("authority must be an exact AuthoritativePipelineProposal"))
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.PREPARATION_READ_SQL,),
            session_id=str(authority.row.session_id),
            proposal_id=str(authority.row.id),
            tool_call_id=authority.row.tool_call_id,
        )

        def _sync() -> PipelineDispatchRecovery | None:
            with self._engine.begin() as conn:
                return self._pipeline_dispatch_recovery_on_connection(conn, authority=authority)

        return cast(
            PipelineDispatchRecovery | None,
            await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync),
        )

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
    ) -> CompositionProposalRecord:
        result = await self._reject_pipeline_composition_proposal(
            session_id=session_id,
            proposal_id=proposal_id,
            draft_hash=draft_hash,
            reason=reason,
            dispatch=dispatch,
            actor=actor,
            session_operation_context=session_operation_context,
        )
        if type(result) is not CompositionProposalRecord:
            raise AuditIntegrityError("legacy rejection returned required custody outcome")
        return result

    async def reject_pipeline_composition_proposal_finish_once(
        self,
        *,
        expected: PipelineRejectionExpected,
        coordinator: RequiredWorkCoordinator,
        rejection_work: RequiredWorkTicket,
        rejection_projection_work: RequiredWorkTicket,
        transition_ordinal: int,
        semantic_ordinal: int,
    ) -> PipelineRejectionFinishOnce:
        if type(coordinator) is not RequiredWorkCoordinator or type(expected) is not PipelineRejectionExpected:
            raise AuditIntegrityError("rejection requires nominal independent custody")
        # Pair ownership is proved before recording refusal; foreign work is never completed.
        coordinator.validate_proposal_rejection_work(
            rejection_ticket=rejection_work,
            projection_ticket=rejection_projection_work,
            transition_ordinal=transition_ordinal,
            semantic_ordinal=semantic_ordinal,
        )
        outcome: CompositionProposalRecord | RequiredSQLFinishOnce[PipelineRejectionSQLResult]
        try:
            with _required_sql_preflight(rejection_work):
                if type(expected.authority) is not AuthoritativePipelineProposal:
                    raise AuditIntegrityError("rejection requires independently restored authority")
                row = expected.authority.row
                if type(row) is not CompositionProposalRecord or type(expected.context) is not SessionOperationContext:
                    raise AuditIntegrityError("rejection requires exact proposal and operation context")
                if type(expected.actor_user_id) is not str or not expected.actor_user_id:
                    raise AuditIntegrityError("rejection requires independent authenticated principal")
                allowed_actors = (
                    f"user:{expected.actor_user_id}",
                    f"system:pipeline_commit:user:{expected.actor_user_id}",
                    "system:auto_reject_request_cancelled",
                )
                if expected.actor not in allowed_actors:
                    raise AuditIntegrityError("rejection actor is outside its authenticated caller scope")
                scope = rejection_work.key.authority
                if (
                    scope.context != expected.context
                    or rejection_work.key.session_id != str(row.session_id)
                    or scope.proposal_id != str(row.id)
                    or scope.tool_call_id != row.tool_call_id
                ):
                    raise AuditIntegrityError("rejection receipt differs from independent proposal/fence scope")
                if expected.running is None and (scope.durable_operation_id is not None or scope.claim_attempt is not None):
                    raise AuditIntegrityError("durable rejection omitted actual running claim")
                if expected.running is not None:
                    if type(expected.running) is not ComposerOperationRunning:
                        raise AuditIntegrityError("rejection supplied foreign running claim")
                    if (
                        expected.running.session_operation_context != expected.context
                        or scope.durable_operation_id != expected.running.claim.operation_id
                        or scope.claim_attempt != expected.running.claim.attempt
                        or expected.running.claim.session_id != row.session_id
                    ):
                        raise AuditIntegrityError("rejection receipt differs from actual running claim")
                self._pipeline_settlement_operation_kind(expected.context)
                reason = _validated_pipeline_rejection_reason(expected.reason)
                if reason == "candidate_executor_mismatch" and expected.dispatch is None:
                    raise AuditIntegrityError("mismatch rejection omitted dispatch")
                if expected.dispatch is not None and type(expected.dispatch) is not PipelineDispatchAuditBinding:
                    raise AuditIntegrityError("rejection supplied foreign dispatch")
        except BaseException as error:
            # No known arm if actual SQL custody remains unresolved.
            if not rejection_work.complete:
                raise
            outcome = RequiredSQLRaised(error, ())
            rejection_work.observe_finish_once_handoff(outcome)
        else:
            outcome = await self._reject_pipeline_composition_proposal(
                session_id=row.session_id,
                proposal_id=row.id,
                draft_hash=expected.authority.proposal.draft_hash,
                reason=reason,
                dispatch=expected.dispatch,
                actor=expected.actor,
                session_operation_context=expected.context,
                expected=expected,
                rejection_work=rejection_work,
            )
        if type(outcome) is RequiredSQLRaised:
            unused = coordinator.issue_rejection_projection_unused(
                rejection_ticket=rejection_work,
                projection_ticket=rejection_projection_work,
                actual_outcome=outcome,
            )
            if unused is None:
                raise AuditIntegrityError("rejection failure lacks issued unused projection evidence")
            return PipelineRejectionRaised(outcome, unused)
        if type(outcome) is not RequiredSQLReturned:
            raise AuditIntegrityError("required rejection returned legacy material")
        returned = outcome
        coordinator.verify_rejection_returned(rejection_ticket=rejection_work, actual_outcome=returned)
        post_sql_failures: tuple[BaseException, ...] = ()
        if returned.value.transitioned:
            try:
                _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "freeform", "result": expected.reason})
            except BaseException as error:
                post_sql_failures = (error,)
        return PipelineRejectionReturned(returned, post_sql_failures)

    async def _reject_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        reason: PipelineProposalRejectionReason,
        dispatch: PipelineDispatchAuditBinding | None,
        actor: str,
        session_operation_context: SessionOperationContext,
        expected: PipelineRejectionExpected | None = None,
        rejection_work: RequiredWorkTicket | None = None,
    ) -> CompositionProposalRecord | RequiredSQLFinishOnce[PipelineRejectionSQLResult]:
        """Atomically terminalise a pipeline proposal with a closed reason."""
        reason = _validated_pipeline_rejection_reason(reason)
        if reason == "candidate_executor_mismatch" and dispatch is None:
            raise AuditIntegrityError("candidate/executor mismatch rejection requires dispatch evidence")
        if dispatch is not None and type(dispatch) is not PipelineDispatchAuditBinding:
            raise TypeError("dispatch must be an exact PipelineDispatchAuditBinding or None")
        sid = str(session_id)
        pid = str(proposal_id)
        operation_kind = self._pipeline_settlement_operation_kind(session_operation_context)

        def _sync() -> PipelineRejectionWriteResult:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=operation_kind,
                ),
            ):
                # Stamped under the lock for the same reason as the settling
                # path above: a terminalization that waited on the session lock
                # must not record a time from before its own transaction.
                now = self._now()
                row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if row is None:
                    raise KeyError(pid)
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("pipeline proposal must have exactly one authoritative creation event")
                authority = _restore_authoritative_pipeline_proposal(
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                if expected is not None:
                    enriched = replace(authority.row, pipeline_metadata=_pipeline_public_metadata(authority))
                    if replace(authority, row=enriched) != expected.authority:
                        raise AuditIntegrityError("rejection locked immutable authority changed")
                    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
                    if principal != expected.actor_user_id:
                        raise AuditIntegrityError("rejection actor is not this session principal")
                    binding = derive_proposal_composer_binding_on_connection(
                        conn,
                        session_operation_context,
                        user_message_id=authority.row.user_message_id,
                        actor=authority.creation_actor if authority.creation_actor is not None else actor,
                        running=expected.running,
                    )
                    if binding is not None:
                        if expected.running is None or authority.composer_operation != binding:
                            raise AuditIntegrityError("rejection requires actual current durable creation binding")
                        require_composer_settlement_actor_on_connection(conn, expected.running, actor=f"user:{expected.actor_user_id}")
                if authority.proposal.draft_hash != draft_hash:
                    raise StaleComposeStateError("pipeline proposal draft hash echo is stale or mismatched")
                if dispatch is not None:
                    if dispatch.tool_call_id != authority.row.tool_call_id:
                        raise AuditIntegrityError("pipeline rejection dispatch tool call does not match proposal authority")
                    if dispatch.arguments_hash != semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json):
                        raise AuditIntegrityError("pipeline rejection dispatch arguments do not match persisted redacted proposal")
                    if len(_persisted_pipeline_dispatch_content_hashes(conn, session_id=sid, dispatch=dispatch)) != 1:
                        raise AuditIntegrityError("pipeline rejection requires exactly one matching durable dispatch audit")
                expected_payload = _pipeline_rejected_payload(authority=authority, reason=reason, dispatch=dispatch)
                terminal_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
                ).fetchall()
                if authority.row.status == "rejected":
                    if (
                        len(terminal_rows) != 1
                        or terminal_rows[0].event_type != "proposal.rejected"
                        or authority.row.audit_event_id != UUID(terminal_rows[0].id)
                        or terminal_rows[0].payload != expected_payload
                        or (expected is not None and terminal_rows[0].actor != actor)
                    ):
                        raise AuditIntegrityError("rejected pipeline proposal terminal binding mismatch")
                    record = replace(authority.row, pipeline_metadata=_pipeline_public_metadata(authority))
                    return PipelineRejectionWriteResult(record, _proposal_event_record_from_row(terminal_rows[0]), False)
                if authority.row.status != "pending":
                    raise ValueError(f"Proposal {pid} must be pending to reject; got {authority.row.status!r}")
                if terminal_rows:
                    raise AuditIntegrityError("pending pipeline proposal already has a terminal event")
                event_id = str(uuid.uuid4())
                conn.execute(
                    insert(proposal_events_table).values(
                        id=event_id,
                        session_id=sid,
                        proposal_id=pid,
                        event_type="proposal.rejected",
                        actor=actor,
                        payload=expected_payload,
                        created_at=now,
                    )
                )
                rejected = conn.execute(
                    update(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                    .where(composition_proposals_table.c.status == "pending")
                    .values(status="rejected", audit_event_id=event_id, updated_at=now)
                )
                if rejected.rowcount != 1:
                    # Same CAS contract as the settling path: a zero-row update
                    # means another writer terminalized the proposal, so the
                    # rejection event must roll back with it.
                    raise AuditIntegrityError("pipeline proposal left pending before rejection committed")
                updated_row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one()
                event_row = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == event_id)).one()
                record = replace(_proposal_record_from_row(updated_row), pipeline_metadata=_pipeline_public_metadata(authority))
                return PipelineRejectionWriteResult(record, _proposal_event_record_from_row(event_row), True)

        if rejection_work is not None:
            if expected is None:
                raise AuditIntegrityError("required rejection omitted independent immutable expectation")
            retained_expected = expected

            def _required_sync() -> PipelineRejectionSQLResult:
                written = _sync()
                return PipelineRejectionSQLResult(written.record, written.event, retained_expected, written.transitioned)

            return await run_required_sql_finish_once(rejection_work, _required_sync)
        result: PipelineRejectionWriteResult = await self._run_sync(_sync)
        if result.transitioned:
            _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "freeform", "result": reason})
        return result.record

    def _list_composition_proposals_on_connection(
        self,
        conn: Connection,
        *,
        session_id: UUID,
        status: ProposalLifecycleStatus | None = None,
    ) -> tuple[CompositionProposalRecord, ...]:
        sid = str(session_id)
        stmt = select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)
        if status is not None:
            stmt = stmt.where(composition_proposals_table.c.status == status)
        stmt = stmt.order_by(composition_proposals_table.c.created_at)
        rows = conn.execute(stmt).fetchall()
        records = [_proposal_record_from_row(row) for row in rows]
        if not records:
            return tuple(records)
        creation_rows = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.session_id == sid)
            .where(proposal_events_table.c.event_type == "proposal.created")
            .where(proposal_events_table.c.proposal_id.in_([str(record.id) for record in records]))
        ).fetchall()
        by_proposal: dict[str, list[Any]] = {str(record.id): [] for record in records}
        for event_row in creation_rows:
            if event_row.proposal_id not in by_proposal:
                raise AuditIntegrityError("proposal creation event escaped the constrained proposal query")
            by_proposal[event_row.proposal_id].append(event_row)
        enriched: list[CompositionProposalRecord] = []
        for record in records:
            events = by_proposal[str(record.id)]
            if len(events) != 1:
                raise AuditIntegrityError("composition proposal must have exactly one creation event")
            authority = _classify_authoritative_composition_proposal(
                row=record,
                creation_event=_proposal_event_record_from_row(events[0]),
            )
            if authority.pipeline is not None:
                _verify_pipeline_lifecycle_authority(
                    conn,
                    service=self,
                    authority=authority.pipeline,
                )
            metadata = _pipeline_public_metadata(authority.pipeline) if authority.pipeline is not None else None
            enriched.append(replace(record, pipeline_metadata=metadata))
        return tuple(enriched)

    async def list_composition_proposals(
        self,
        session_id: UUID,
        *,
        status: ProposalLifecycleStatus | None = None,
    ) -> list[CompositionProposalRecord]:
        """List strict authoritative proposals in creation order."""

        def _sync() -> list[CompositionProposalRecord]:
            with self._engine.connect() as conn:
                return list(self._list_composition_proposals_on_connection(conn, session_id=session_id, status=status))

        return cast(list[CompositionProposalRecord], await self._run_sync(_sync))

    async def reject_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord:
        """Reject a pending proposal, or return an exact prior rejection.

        Raise contract, in order: ``KeyError`` when no proposal row exists for
        ``proposal_id`` in this session, and ``ProposalStateConflictError``
        when another decision or actor made it terminal. The exact prior
        rejection must still have one well-formed, bound terminal event.
        """
        sid = str(session_id)
        pid = str(proposal_id)
        event_id = str(uuid.uuid4())

        def _sync() -> CompositionProposalRecord:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.PROPOSAL,
                ) as transaction,
            ):
                row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.id == pid)
                    .where(composition_proposals_table.c.session_id == sid)
                ).one_or_none()
                if row is None:
                    raise KeyError(pid)
                if row.status == "rejected":
                    terminal_rows = conn.execute(
                        select(proposal_events_table)
                        .where(proposal_events_table.c.session_id == sid)
                        .where(proposal_events_table.c.proposal_id == pid)
                        .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
                    ).fetchall()
                    if (
                        len(terminal_rows) != 1
                        or terminal_rows[0].event_type != "proposal.rejected"
                        or row.audit_event_id != terminal_rows[0].id
                        or row.committed_state_id is not None
                        or terminal_rows[0].payload != {"status": "rejected"}
                    ):
                        raise AuditIntegrityError("rejected proposal terminal binding mismatch")
                    if terminal_rows[0].actor == actor:
                        return _proposal_record_from_row(row)
                if row.status != "pending":
                    raise ProposalStateConflictError(f"Proposal {pid} must be pending to reject; got {row.status!r}")

                transaction.composer.reject_pending_proposal(
                    proposal_id=pid,
                    event_id=event_id,
                    actor=actor,
                )
                updated_row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == pid)).one()
                return _proposal_record_from_row(updated_row)

        return cast(CompositionProposalRecord, await self._run_sync(_sync))

    async def accept_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        expected_current_state_id: UUID | None,
        state: CompositionStateData | None,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord:
        """Accept one ordinary proposal and its optional new state atomically."""
        if expected_current_state_id is not None and type(expected_current_state_id) is not UUID:
            raise TypeError("expected_current_state_id must be an exact UUID or None")
        if state is not None and type(state) is not CompositionStateData:
            raise TypeError("state must be an exact CompositionStateData or None")
        sid = str(session_id)
        pid = str(proposal_id)
        event_id = str(uuid.uuid4())

        def _sync() -> CompositionProposalRecord:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.PROPOSAL,
                ) as transaction,
            ):
                return transaction.composer.accept_pending_ordinary_proposal(
                    proposal_id=pid,
                    event_id=event_id,
                    expected_current_state_id=expected_current_state_id,
                    state=state,
                    actor=actor,
                )

        return cast(CompositionProposalRecord, await self._run_sync(_sync))

    async def has_applied_blob_proposal_effect(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        session_operation_context: SessionOperationContext,
    ) -> bool:
        """Verify whether the exact proposal already committed its blob effect."""
        sid = str(session_id)
        pid = str(proposal_id)

        def _sync() -> bool:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.PROPOSAL,
                ) as transaction,
            ):
                return transaction.composer.has_applied_blob_effect(proposal_id=pid)

        return cast(bool, await self._run_sync(_sync))

    async def list_proposal_events(
        self,
        session_id: UUID,
    ) -> list[ProposalEventRecord]:
        """List immutable composer proposal lifecycle events for a session."""
        sid = str(session_id)

        def _sync() -> list[ProposalEventRecord]:
            lifecycle_group = func.coalesce(proposal_events_table.c.proposal_id, proposal_events_table.c.id)
            terminal_phase = case(
                (proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")), 1),
                else_=0,
            )
            with self._engine.begin() as conn:
                rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .order_by(
                        proposal_events_table.c.created_at,
                        lifecycle_group,
                        terminal_phase,
                        proposal_events_table.c.id,
                    )
                ).fetchall()
                return [_proposal_event_record_from_row(row) for row in rows]

        return cast(list[ProposalEventRecord], await self._run_sync(_sync))

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
        """Insert one checked pending event in its own locked transaction."""

        result = await self._prepare_or_create_pending_interpretation_event(
            session_id=session_id,
            composition_state_id=composition_state_id,
            affected_node_id=affected_node_id,
            tool_call_id=tool_call_id,
            user_term=user_term,
            kind=kind,
            llm_draft=llm_draft,
            model_identifier=model_identifier,
            model_version=model_version,
            provider=provider,
            composer_skill_hash=composer_skill_hash,
            session_operation_context=session_operation_context,
            created_at=created_at,
            surface_origin=surface_origin,
        )
        return cast(InterpretationEventRecord, result)

    async def _prepare_or_create_pending_interpretation_event(
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
        surface_origin: InterpretationSurfaceOrigin,
        created_at: datetime | None = None,
        _event_id: UUID | None = None,
        _prepare_only: bool = False,
        proposed_state: CompositionStateData | None = None,
        preparation_work: RequiredWorkBinding | None = None,
    ) -> InterpretationEventRecord | _PreparedPendingInterpretation:
        """Insert a PENDING interpretation event.

        Called from the compose-loop tool handler for
        ``request_interpretation_review``. Acquires the session write lock
        for the duration of the insert. Validates ``affected_node_id``
        exists in ``composition_states.nodes`` BEFORE committing the row.
        Expected under-lock re-check failures — a consumed/absent pending
        requirement or a staged draft that no longer matches ``llm_draft`` —
        raise :class:`InterpretationResolveError` subclasses
        (:class:`InterpretationPlaceholderConsumedError`,
        :class:`InterpretationDraftMismatchError`), which the tool handler
        converts to ARG_ERROR. Genuine audit anomalies (missing state row,
        malformed structures) raise bare :class:`ValueError` and crash.

        Per the engine-patterns-reference skill §Offensive Programming
        Examples, the writer-boundary
        validation reads the parent composition_states row inside the
        locked transaction and inspects its ``nodes`` JSON before INSERT —
        a malformed reference is a Tier-1 audit anomaly we crash on rather
        than fabricating a binding.

        The ``actor`` column on the pending row is set to the sentinel
        ``"composer-llm"`` — the row was created by the composer LLM, not
        a user. ``resolve_interpretation_event`` overwrites this with the
        user identity passed by the route at resolution time, which is
        what :data:`InterpretationEventRecord` ``actor`` documents as
        "user identity at resolution". The closed CHECK on choice and the
        immutability trigger together prevent any other writer from
        appearing on a resolved row.

        Telemetry: NONE — composition-time user decisions are audit-primary;
        no ephemeral operational signal required.
        """
        if type(kind) is not InterpretationKind:
            raise ValueError(f"kind must be InterpretationKind, got {type(kind).__name__}: {kind!r}")
        now = restore_utc(created_at) if created_at is not None else self._now()
        sid = str(session_id)
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if (
            session_operation_context.operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}
            or session_operation_context.fence.session_id != sid
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        event_id = _event_id if _event_id is not None else uuid.uuid4()
        principal_user_id, plugin_snapshot = await self._session_principal_context(sid, preparation_work=preparation_work)
        expected_anchor: CompositionStateRecord | None = None
        expected_live: CompositionStateRecord | None = None
        if proposed_state is None:
            expected_anchor, expected_live = await self._preflight_state_pair(session_id=session_id, anchor_id=composition_state_id)
            snapshot_config = self._inline_preflight_config(expected_live) if expected_live is not None else None
        else:
            snapshot_config = self._inline_preflight_config(proposed_state)
        inline_blob_snapshot = (
            await self._prepare_inline_blob_snapshot(
                snapshot_config,
                session_id=session_id,
                session_operation_context=session_operation_context,
                preparation_work=preparation_work,
            )
            if snapshot_config is not None
            else None
        )
        validation_inputs = build_interpretation_validation_inputs(
            profile_aware=self._plugin_snapshot_factory is not None,
            plugin_snapshot=plugin_snapshot,
            profile_registry=self._operator_profile_registry,
            catalog=self._catalog,
        )
        validator = _SessionPendingInterpretationValidator(
            validation_inputs=validation_inputs,
            runtime_preflight=self._runtime_preflight,
            inline_blob_snapshot=inline_blob_snapshot,
            expected_anchor=expected_anchor if inline_blob_snapshot is not None else None,
            expected_live=expected_live if inline_blob_snapshot is not None else None,
            session_id=sid,
            user_id=principal_user_id,
        )

        command = SessionPendingInterpretationCommand(
            event_id=event_id,
            opt_out_marker_event_id=uuid.uuid4(),
            composition_state_id=composition_state_id,
            affected_node_id=affected_node_id,
            tool_call_id=tool_call_id,
            user_term=user_term,
            kind=kind,
            llm_draft=llm_draft,
            surface_origin=surface_origin,
            model_identifier=model_identifier,
            model_version=model_version,
            provider=provider,
            composer_skill_hash=composer_skill_hash,
            created_at=now,
        )
        prepared = _PreparedPendingInterpretation(command=command, validator=validator)

        if _prepare_only:
            return prepared
        return cast(
            InterpretationEventRecord,
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.interpretations.create_or_reconcile_pending(command, validator),
            ),
        )

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
        """Commit a resolution AND patch the affected LLM transform's prompt template.

        F-14 (business-rule split): ``accepted_value`` is computed
        internally. When ``choice == ACCEPTED_AS_DRAFTED``, the service
        reads the pending event's ``llm_draft`` from the DB and uses that
        as ``accepted_value``. When ``choice == AMENDED``,
        ``amended_value`` is used directly. The route passes only
        ``choice`` and ``amended_value`` — the computation lives here to
        avoid duplicating the branch across callers.

        Single transaction (F-25: ``_session_write_lock`` acquires the
        per-session write lock; the SQLAlchemy ``begin()`` is implicitly
        BEGIN IMMEDIATE on SQLite when a write occurs inside, but the
        process-wide RLock is the actual serialiser):

            1. SELECT the pending event by id AND session_id AND choice='pending'
               (F-7: session_id in WHERE prevents cross-session IDOR;
               choice='pending' is the TOCTOU guard against double-resolve).
               Raise ValueError if no matching row.
            2. Compute ``accepted_value`` per F-14.
            3. Validate ``accepted_value`` via ``_validate_accepted_value_content``
               (defence-in-depth against future callers that bypass the route).
            4. Call :func:`_patch_llm_transform_prompt` to produce the
               resolved prompt-template string. Keep a local reference; do
               NOT call again in step 5a.
            4a. Compute ``approved_prompt_artifact_hash`` via
                the versioned system-prompt and effective-query artifact.
                NOT part of
                ``INTERPRETATION_HASH_DOMAIN_V2`` — covers a different
                input.
            5. UPDATE interpretation_events with the settled fields.
            5a. Write the new composition_states row with provenance =
                'interpretation_resolve', version += 1, carrying the
                patched ``prompt_template`` and ``approved_prompt_artifact_hash``
                on the affected node JSON.
            6. Return the resolved event + the new state.

        Trigger error note (F-28): if the immutability trigger
        ``trg_interpretation_events_immutable_resolved`` fires, SQLAlchemy
        raises :class:`IntegrityError` carrying the trigger's RAISE(ABORT,
        ...) message. We match that specific substring explicitly so it is
        mapped to a 409/400 by the route, NOT conflated with a generic
        integrity violation. Normal use never reaches the trigger because
        the SELECT-then-UPDATE pattern with ``WHERE choice='pending'``
        short-circuits to a ValueError first.

        Telemetry: NONE — composition-time user decisions are audit-primary;
        no ephemeral operational signal required.
        """
        now = restore_utc(resolved_at) if resolved_at is not None else self._now()
        sid = str(session_id)
        eid = str(event_id)
        principal_user_id, plugin_snapshot = await self._session_principal_context(sid)
        validation_inputs = build_interpretation_validation_inputs(
            profile_aware=self._plugin_snapshot_factory is not None,
            plugin_snapshot=plugin_snapshot,
            profile_registry=self._operator_profile_registry,
            catalog=self._catalog,
        )

        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")

        def _read_pending_event() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(
                    select(interpretation_events_table)
                    .where(interpretation_events_table.c.id == eid)
                    .where(interpretation_events_table.c.session_id == sid)
                    .where(interpretation_events_table.c.choice == InterpretationChoice.PENDING.value)
                ).one_or_none()

        preflight_event = await self._run_sync(_read_pending_event)
        expected_anchor: CompositionStateRecord | None = None
        expected_live: CompositionStateRecord | None = None
        inline_blob_snapshot: SessionInlineBlobSnapshot | None = None
        if preflight_event is not None and preflight_event.composition_state_id is not None:
            expected_anchor, expected_live = await self._preflight_state_pair(
                session_id=session_id,
                anchor_id=UUID(preflight_event.composition_state_id),
            )
            if expected_live is not None:
                inline_blob_snapshot = await self._prepare_inline_blob_snapshot(
                    self._inline_preflight_config(expected_live),
                    session_id=session_id,
                    session_operation_context=session_operation_context,
                )

        def _sync() -> tuple[InterpretationEventRecord, CompositionStateRecord]:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ),
            ):
                # Step 1: SELECT pending event, scoped to session.
                event_row = conn.execute(
                    select(interpretation_events_table)
                    .where(interpretation_events_table.c.id == eid)
                    .where(interpretation_events_table.c.session_id == sid)
                    .where(interpretation_events_table.c.choice == InterpretationChoice.PENDING.value)
                ).one_or_none()
                if event_row is None:
                    existing_event_row = conn.execute(
                        select(interpretation_events_table)
                        .where(interpretation_events_table.c.id == eid)
                        .where(interpretation_events_table.c.session_id == sid)
                    ).one_or_none()
                    if existing_event_row is not None:
                        raise InterpretationEventAlreadyResolvedError(
                            f"resolve_interpretation_event: interpretation event {eid!r} in session {sid!r} is already resolved"
                        )
                    raise InterpretationEventNotFoundError(
                        f"resolve_interpretation_event: interpretation event {eid!r} not found in session {sid!r}"
                    )
                if inline_blob_snapshot is not None and event_row != preflight_event:
                    raise AuditIntegrityError("interpretation event changed after inline blob snapshot")

                # Step 2: compute accepted_value per F-14.
                if choice is InterpretationChoice.ACCEPTED_AS_DRAFTED:
                    accepted_value = event_row.llm_draft
                    if accepted_value is None:
                        raise AuditIntegrityError(
                            f"resolve_interpretation_event: event {eid!r} has no llm_draft to accept; row shape is malformed"
                        )
                elif choice is InterpretationChoice.AMENDED:
                    if amended_value is None:
                        raise ValueError("resolve_interpretation_event: choice=AMENDED requires amended_value to be set")
                    accepted_value = amended_value
                else:
                    raise ValueError(
                        f"resolve_interpretation_event: choice {choice!r} is not "
                        f"a resolution choice; only ACCEPTED_AS_DRAFTED and AMENDED "
                        f"are valid here"
                    )

                kind = InterpretationKind(event_row.kind)
                if choice is InterpretationChoice.AMENDED and kind in {
                    InterpretationKind.INVENTED_SOURCE,
                    InterpretationKind.LLM_PROMPT_TEMPLATE,
                    InterpretationKind.PIPELINE_DECISION,
                    # A data contract is acknowledged or declined, never edited:
                    # the field set is the graph's own demand, not user prose.
                    InterpretationKind.SOURCE_DATA_CONTRACT,
                }:
                    raise InterpretationUnsupportedChoiceError(
                        f"resolve_interpretation_event: {kind.value} does not support inline amendment in this release"
                    )
                if kind in {
                    InterpretationKind.VAGUE_TERM,
                    InterpretationKind.INVENTED_SOURCE,
                    InterpretationKind.PIPELINE_DECISION,
                    InterpretationKind.LLM_MODEL_CHOICE,
                }:
                    # Step 3: defence-in-depth validation of user/LLM-supplied
                    # content. Prompt-template review carries real Jinja and
                    # deliberately skips this accepted-value validator.
                    _validate_accepted_value_content(accepted_value)

                # Step 4: produce kind-specific patched state. Helpers raise
                # typed interpretation errors on structural anomalies; the
                # raise short-circuits the transaction before any UPDATE/INSERT.
                #
                # Vague-term patches still land on the CURRENT composition
                # state (highest version for the session), not the surfacing
                # state. Prompt-template and invented-source reviews follow
                # the same current-state rule but update only review metadata:
                # prompt-template review stamps the existing LLM prompt hash,
                # and invented-source review stamps source authoring metadata.
                live_state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                if live_state_row is None:
                    raise AuditIntegrityError(f"resolve_interpretation_event: session {sid!r} has no composition state to patch")
                state_record = self._row_to_state_record(live_state_row)
                surfacing_state_record: CompositionStateRecord | None = None
                if event_row.composition_state_id is not None:
                    surfacing_state_row = conn.execute(
                        select(composition_states_table)
                        .where(composition_states_table.c.id == event_row.composition_state_id)
                        .where(composition_states_table.c.session_id == sid)
                    ).one_or_none()
                    if surfacing_state_row is not None:
                        surfacing_state_record = self._row_to_state_record(surfacing_state_row)
                if surfacing_state_record is None:
                    raise AuditIntegrityError(f"resolve_interpretation_event: event {eid!r} has no same-session surfacing state")
                if inline_blob_snapshot is not None:
                    if state_record != expected_live or surfacing_state_record != expected_anchor:
                        raise AuditIntegrityError("interpretation state changed after inline blob snapshot")
                    inline_blob_snapshot.assert_current_rows(conn, session_id=session_id)
                if kind is InterpretationKind.SOURCE_DATA_CONTRACT:
                    source_name, reviewed_fields = _source_data_contract_demand_from_state_record(
                        surfacing_state_record,
                        affected_node_id=event_row.affected_node_id,
                        context="resolve_interpretation_event",
                    )
                    try:
                        _live_source_name, current_fields = _source_data_contract_demand_from_state_record(
                            state_record,
                            affected_node_id=event_row.affected_node_id,
                            context="resolve_interpretation_event",
                        )
                    except InterpretationResolveError as exc:
                        raise InterpretationSourceDataContractDriftError(
                            source_name=source_name,
                            reviewed_fields=reviewed_fields,
                            current_fields=None,
                        ) from exc
                    if current_fields != reviewed_fields:
                        raise InterpretationSourceDataContractDriftError(
                            source_name=source_name,
                            reviewed_fields=reviewed_fields,
                            current_fields=current_fields,
                        )
                else:
                    surfacing_review_identity = _reviewed_content_identity(
                        surfacing_state_record,
                        kind=kind,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        context="resolve_interpretation_event",
                    )
                    live_review_identity = _reviewed_content_identity(
                        state_record,
                        kind=kind,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        context="resolve_interpretation_event",
                    )
                    if live_review_identity != surfacing_review_identity:
                        if kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
                            raise InterpretationPlaceholderConsumedError(
                                "resolve_interpretation_event: llm_prompt_template prompt skeleton no longer matches "
                                "the structure the review approved"
                            )
                        raise InterpretationPlaceholderConsumedError(
                            "resolve_interpretation_event: reviewed content no longer matches the event's surfacing state"
                        )
                final_sources: Mapping[str, Mapping[str, Any]] | None
                final_nodes: list[Mapping[str, Any]]
                approved_prompt_artifact_hash: str | None
                if kind is InterpretationKind.VAGUE_TERM:
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_vague_term(
                        state_record,
                        surfacing_state_record=surfacing_state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        llm_draft=event_row.llm_draft,
                        accepted_value=accepted_value,
                    )
                elif kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
                    # Skeleton the user actually reviewed (the surfacing state's
                    # node). The acceptance gate compares this to the live
                    # skeleton so a sibling vague_term baked between surfacing and
                    # resolve does not invalidate this review (elspeth-e51216d305).
                    surfacing_structure_hash = _surfacing_prompt_structure_hash(
                        surfacing_state_record,
                        affected_node_id=event_row.affected_node_id,
                    )
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_prompt_template_review(
                        state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        accepted_value=accepted_value,
                        surfacing_structure_hash=surfacing_structure_hash,
                    )
                elif kind is InterpretationKind.INVENTED_SOURCE:
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_invented_source(
                        state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        llm_draft=event_row.llm_draft,
                        accepted_value=accepted_value,
                    )
                elif kind is InterpretationKind.SOURCE_DATA_CONTRACT:
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_source_data_contract(
                        state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        llm_draft=event_row.llm_draft,
                        accepted_value=accepted_value,
                    )
                elif kind is InterpretationKind.PIPELINE_DECISION:
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_pipeline_decision_review(
                        state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        llm_draft=event_row.llm_draft,
                        accepted_value=accepted_value,
                    )
                elif kind is InterpretationKind.LLM_MODEL_CHOICE:
                    final_sources, final_nodes, approved_prompt_artifact_hash = _resolve_model_choice_review(
                        state_record,
                        event_id=eid,
                        affected_node_id=event_row.affected_node_id,
                        user_term=event_row.user_term,
                        llm_draft=event_row.llm_draft,
                        accepted_value=accepted_value,
                    )
                else:
                    raise AssertionError(f"unhandled InterpretationKind {kind!r}")

                # The live state can be invalid solely because an earlier
                # finalisation/runtime-preflight pass saw the unresolved
                # ``{{interpretation:<term>}}`` placeholder. Resolving the
                # event consumes that placeholder, so the new state row must
                # carry validation for the patched state, not stale failure
                # metadata copied from the pre-resolve live row.
                from elspeth.web.sessions.converters import state_from_record

                patched_state_record = replace(
                    state_record,
                    source=None,
                    sources=final_sources,
                    nodes=final_nodes,
                    is_valid=False,
                    validation_errors=None,
                )
                if inline_blob_snapshot is None:
                    patched_validation = self._validate_patched_composition_state(
                        state_from_record(patched_state_record),
                        validation_inputs=validation_inputs,
                        session_id=sid,
                        user_id=principal_user_id,
                    )
                else:
                    patched_validation = self._validate_patched_composition_state(
                        state_from_record(patched_state_record),
                        validation_inputs=validation_inputs,
                        session_id=sid,
                        user_id=principal_user_id,
                        inline_blob_snapshot=inline_blob_snapshot,
                    )
                raw_validation_errors = [
                    CompositionValidationError(message=error.message, error_code=error.error_code, component=error.component)
                    for error in patched_validation.errors
                ] or None
                patched_validation_errors = raw_validation_errors

                # Compute arguments_hash over the INTERPRETATION_HASH_DOMAIN_V2
                # field set. The closed domain is the source of truth — read
                # from the constant, do not duplicate the field list inline.
                # The composition_state_id in the hash domain is the surfacing
                # anchor (the pending row's composition_state_id), not the
                # live state we patched — the hash identifies the discrete
                # surface-and-decide event, not the state mutation it
                # produced.
                surfacing_state_id_str = event_row.composition_state_id
                domain_dict = _interpretation_hash_domain_v2(
                    session_id=sid,
                    composition_state_id=surfacing_state_id_str,
                    affected_node_id=event_row.affected_node_id,
                    tool_call_id=event_row.tool_call_id,
                    user_term=event_row.user_term,
                    kind=event_row.kind,
                    llm_draft=event_row.llm_draft,
                    accepted_value=accepted_value,
                    actor=actor,
                    model_identifier=event_row.model_identifier,
                    model_version=event_row.model_version,
                    provider=event_row.provider,
                    composer_skill_hash=event_row.composer_skill_hash,
                    context="resolve_interpretation_event",
                )
                arguments_hash = stable_hash(domain_dict)

                # Step 5: UPDATE the interpretation event. The trigger
                # short-circuit (F-28) is unreachable on this branch because
                # the SELECT already filtered to choice='pending'; we catch
                # IntegrityError defensively in case a future refactor
                # reorders the writes.
                try:
                    with self._interpretation_mutations(
                        conn,
                        session_id=sid,
                        session_operation_context=session_operation_context,
                    ) as interpretation_mutations:
                        interpretation_mutations.resolve_pending_event(
                            event_id=event_id,
                            choice=choice,
                            accepted_value=accepted_value,
                            resolved_at=now,
                            actor=actor,
                            arguments_hash=arguments_hash,
                            hash_domain_version="v2",
                            runtime_model_identifier=runtime_model_identifier,
                            runtime_model_version=runtime_model_version,
                            approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                        )
                except IntegrityError as exc:
                    # F-28: classify the trigger immutability message
                    # specifically. Any other IntegrityError reraises as-is.
                    if _INTERPRETATION_IMMUTABLE_TRIGGER_MSG in str(exc):
                        raise InterpretationEventAlreadyResolvedError(
                            f"resolve_interpretation_event: event {eid!r} is already resolved (immutability trigger fired)"
                        ) from exc
                    raise

                # Step 5a: write the new composition state row carrying the
                # patched prompt template + hash sibling. The
                # ``interpretation_resolve`` provenance is the load-bearing
                # discriminator that lets backward-direction audit walks
                # identify this row as resulting from an interpretation
                # decision.
                new_state_id_str = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(
                        data=CompositionStateData(
                            sources=final_sources,
                            nodes=final_nodes,
                            edges=state_record.edges,
                            outputs=state_record.outputs,
                            metadata_=state_record.metadata_,
                            is_valid=patched_validation.is_valid,
                            validation_errors=patched_validation_errors,
                            composer_meta=state_record.composer_meta,
                        ),
                        derived_from_state_id=str(state_record.id),
                    ),
                    provenance="interpretation_resolve",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )

                resolved_event_row = conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.id == eid)).one()
                new_state_row = conn.execute(
                    select(composition_states_table).where(composition_states_table.c.id == new_state_id_str)
                ).one()
                return (
                    _interpretation_event_record_from_row(resolved_event_row),
                    self._row_to_state_record(new_state_row),
                )

        return cast(
            tuple[InterpretationEventRecord, CompositionStateRecord],
            await self._run_sync(_sync),
        )

    async def list_interpretation_events(
        self,
        session_id: UUID,
        *,
        status: Literal["pending", "all"] = "all",
        composition_state_id: UUID | None = None,
        sources: Sequence[InterpretationSource] | None = None,
    ) -> list[InterpretationEventRecord]:
        """Read-back of interpretation events for the session.

        Used by the audit-readiness panel (counts), by the frontend on
        reload (rehydrate pending review affordances), and by the
        opt-out audit-summary surface (``sources`` filter — F-22).

        Telemetry: NONE — composition-time user decisions are audit-primary;
        no ephemeral operational signal required.
        """
        sid = str(session_id)
        cs_id = str(composition_state_id) if composition_state_id is not None else None
        # Materialise the source-value list once so the inner _sync closure
        # uses primitive string values, not enum instances captured by
        # closure (cheap; defensive against the iterable being a generator).
        source_values: list[str] | None = [s.value for s in sources] if sources is not None else None

        def _sync() -> list[InterpretationEventRecord]:
            stmt = select(interpretation_events_table).where(interpretation_events_table.c.session_id == sid)
            if status == "pending":
                stmt = stmt.where(interpretation_events_table.c.choice == InterpretationChoice.PENDING.value)
            if cs_id is not None:
                stmt = stmt.where(interpretation_events_table.c.composition_state_id == cs_id)
            if source_values is not None:
                stmt = stmt.where(interpretation_events_table.c.interpretation_source.in_(source_values))
            stmt = stmt.order_by(
                interpretation_events_table.c.created_at,
                interpretation_events_table.c.id,
            )
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
                return [_interpretation_event_record_from_row(row) for row in rows]

        return cast(list[InterpretationEventRecord], await self._run_sync(_sync))

    async def record_session_interpretation_opt_out(
        self,
        *,
        session_id: UUID,
        actor: str,
        session_operation_context: SessionOperationContext,
        opted_out_at: datetime | None = None,
    ) -> InterpretationEventRecord:
        """Mark the session as 'don't surface interpretations any more'.

        F-27 (write-lock annotation): acquires the session write lock for
        the ENTIRE duration of the transaction — both the
        interpretation_events INSERT and the sessions boolean UPDATE are
        inside one ``_session_write_lock`` block to ensure atomicity.

        Idempotency (F-29): if an opted_out row already exists for this
        session, return the existing record without inserting a duplicate.
        The sessions boolean remains true. First opt-out timestamp is
        authoritative.

        Writes a row to ``interpretation_events_table`` with
        ``choice='opted_out'``, ``interpretation_source='auto_interpreted_opt_out'``,
        all nullable interpretation fields NULL, and ``resolved_at`` set
        to ``opted_out_at``. Also sets
        ``sessions.interpretation_review_disabled = true``. Single
        transaction.

        Does NOT write to ``proposal_events_table``. The
        ``interpretation_events`` table is the single source of truth for
        all interpretation-related decisions.

        Telemetry: ``composer.interpretation.opt_out_total`` fires on the
        INSERT path only (B3 cohort b1 — Sub-task 7e in Phase 8 plan). The
        F-29 idempotent re-fire returns the existing row without writing a
        new audit row, so emitting there would over-count and break the
        superset rule (the counter must aggregate over audit rows, not over
        route hits). Emission happens AFTER the transaction commits, in
        line with the ``record_audit_grade_view`` pattern elsewhere in
        this module.
        """
        now = restore_utc(opted_out_at) if opted_out_at is not None else self._now()
        sid = str(session_id)
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if (
            session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
            or session_operation_context.fence.session_id != sid
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        event_id = uuid.uuid4()

        record, was_inserted = cast(
            "tuple[InterpretationEventRecord, bool]",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.interpretations.record_session_opt_out(
                    event_id=event_id,
                    actor=actor,
                    opted_out_at=now,
                ),
            ),
        )
        # B3 cohort b1 — Phase 5b interpretation opt-out (Sub-task 7e).
        # Helper-based emit. The helper applies W5 wrapping so a broken
        # OTel exporter cannot 500 a POST whose audit row already wrote.
        # Fires only on the INSERT path; idempotent re-fires do not emit
        # (no new audit row → no aggregate increment, per superset rule).
        if was_inserted:
            record_interpretation_opt_out(self._telemetry)
        return record

    async def upsert_skill_markdown_history(
        self,
        *,
        skill_hash: str,
        filename: str,
        content: str,
    ) -> bool:
        """Best-effort INSERT-OR-IGNORE into ``skill_markdown_history`` (F-5c).

        Called once per ``(skill_hash, compose_loop_init)`` so a forensic
        auditor can reconstruct the exact composer skill text that was in
        memory when any ``interpretation_events.composer_skill_hash``
        column was populated. The hash is the primary key, so subsequent
        upserts with the same hash collapse via ``INSERT OR IGNORE`` —
        no row is duplicated.

        **Best-effort, not transactional.** This writer is intentionally
        decoupled from the interpretation-event row write (per the spec
        at docs/composer/ux-redesign-2026-05/18a-phase-5b-backend.md
        §"skill_markdown_history upsert (F-5c)"). Storage cost is
        bounded — one row per distinct deploy of the skill markdown.

        Returns ``True`` when a row was inserted, ``False`` when the row
        already existed (the upsert was a no-op). Callers MAY use the
        return value for telemetry, but they MUST NOT branch on it for
        correctness: the table's contents are an audit-archive, not a
        coordination surface.

        Trust tier: the ``skill_hash`` / ``filename`` / ``content`` triple
        is operator-supplied (drawn from the on-disk skill file the
        operator deployed). It is Tier-1 data on insert — we crash on any
        DB-side anomaly. The caller's discipline (passing values from
        ``load_skill_with_hash``) ensures atomic consistency.
        """
        return cast(
            bool,
            await self._run_sync(
                self._skill_markdown_history_authority.upsert_exact,
                skill_hash=skill_hash,
                filename=filename,
                content=content,
            ),
        )

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
        """Write an AUTO_INTERPRETED_NO_SURFACES row (F-6).

        Triggered by the compose loop when the per-term or per-day
        ``request_interpretation_review`` rate cap is hit and the LLM
        is expected to bake the interpretation directly into the prompt
        template without surfacing it for review. The row records that
        the LLM *was* consulted (provenance fields populated) but no
        surface was produced (interpretation surface fields NULL).

        Validates against ``ck_interpretation_events_no_surfaces_shape``:
        the five interpretation-surface fields (composition_state_id,
        affected_node_id, tool_call_id, user_term, llm_draft) MUST be
        NULL; kind and the four LLM provenance fields (model_identifier,
        model_version, provider, composer_skill_hash) MUST be NOT NULL.

        ``choice`` is set to ``OPTED_OUT`` because the resolve semantics
        are "no further user action required" — the rate cap is the
        resolution. ``resolved_at`` equals ``created_at`` because the
        row is born resolved. ``arguments_hash`` is NULL because no
        user-visible surface was created to resolve.

        Telemetry: NONE — composition-time user decisions are
        audit-primary; no ephemeral operational signal required.
        """
        if type(kind) is not InterpretationKind:
            raise ValueError(f"kind must be InterpretationKind, got {type(kind).__name__}: {kind!r}")
        now = restore_utc(created_at) if created_at is not None else self._now()
        sid = str(session_id)
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if (
            session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
            or session_operation_context.fence.session_id != sid
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        event_id = uuid.uuid4()
        return cast(
            InterpretationEventRecord,
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.interpretations.record_auto_interpreted_no_surfaces_event(
                    event_id=event_id,
                    actor=actor,
                    kind=kind,
                    model_identifier=model_identifier,
                    model_version=model_version,
                    provider=provider,
                    composer_skill_hash=composer_skill_hash,
                    created_at=now,
                ),
            ),
        )

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
    ) -> ChatMessageRecord:
        """Add a chat message and update the session's ``updated_at``.

        BREAKING CHANGE in rev 4: ``writer_principal`` is now a required
        keyword-only argument (must be one of the values listed in the
        ``ck_chat_messages_writer_principal`` CHECK constraint). All
        callers were updated atomically with this signature change per
        the no-legacy single-cut policy.

        Preserved behaviours from pre-rev-4:
        - ``_assert_state_in_session`` cross-session guard fires when
          ``composition_state_id`` is not None.
        - ``sessions_table.updated_at`` is bumped to ``now``.
        - ``raw_content`` is persisted verbatim when supplied.
        - Returns ``ChatMessageRecord``, not just an id string.

        New in rev 4:
        - ``sequence_no`` is allocated under ``_session_write_lock``
          (PostgreSQL advisory lock or SQLite per-session process lock),
          replacing the implicit "last write wins" ordering.
        - ``tool_call_id`` and ``parent_assistant_id`` MUST be set when
          ``role='tool'`` and MUST be ``None`` otherwise; the
          ``ck_chat_messages_tool_call_id_role`` and
          ``ck_chat_messages_parent_role`` CHECK constraints enforce
          this at write time.
        """
        if type(session_operation_kind) is not SessionOperationKind:
            raise TypeError("session_operation_kind must be an exact SessionOperationKind")
        if session_operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}:
            raise ValueError("add_message fenced writes require COMPOSE or PROPOSAL authority")
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        now = self._now()
        sid = str(session_id)
        csid = str(composition_state_id) if composition_state_id else None
        pid = str(parent_assistant_id) if parent_assistant_id else None
        msg_id_holder: dict[str, str] = {}
        sequence_holder: dict[str, int] = {}

        def _write(conn: Connection) -> None:
            self._assert_session_write_lock_held(
                conn,
                sid,
                caller="add_message._write",
            )
            if csid is not None:
                _assert_state_in_session(
                    conn,
                    state_id=csid,
                    expected_session_id=sid,
                    caller="add_message",
                )
            seq = self._reserve_sequence_range(conn, sid, count=1)
            sequence_holder["sequence_no"] = seq
            msg_id_holder["id"] = self._insert_chat_message(
                conn,
                session_id=sid,
                role=role,
                content=content,
                raw_content=raw_content,
                # ``deep_thaw`` matches the persist_compose_turn site:
                # SQLAlchemy JSON serialisation handles raw dicts/lists,
                # but tool_calls may be a ``MappingProxyType`` / ``tuple``
                # after frozen-dataclass round-trips, which the JSON encoder
                # rejects.
                tool_calls=deep_thaw(tool_calls) if tool_calls else None,
                sequence_no=seq,
                writer_principal=writer_principal,
                composition_state_id=csid,
                tool_call_id=tool_call_id,
                parent_assistant_id=pid,
                created_at=now,
                session_operation_context=session_operation_context,
            )
            with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
                session_mutations.mark_session_updated(updated_at=now)

        def _sync() -> None:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=session_operation_kind,
                ),
            ):
                _write(conn)

        await self._run_sync_with_post_commit_projection(
            _sync,
            project=lambda _result: record_settled_composer_audit_message(
                role=role,
                writer_principal=writer_principal,
                tool_calls=tool_calls,
            ),
        )

        return ChatMessageRecord(
            id=UUID(msg_id_holder["id"]),
            session_id=session_id,
            role=role,
            content=content,
            raw_content=raw_content,
            tool_calls=tool_calls,
            created_at=now,
            sequence_no=sequence_holder["sequence_no"],
            composition_state_id=composition_state_id,
            writer_principal=writer_principal,
            tool_call_id=tool_call_id,
            parent_assistant_id=parent_assistant_id,
        )

    async def add_run_diagnostics_audit_message(
        self,
        authority: RunDiagnosticsAuditAuthority,
        content: str,
        *,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatMessageRecord:
        """Append one run-diagnostics ``role=audit`` row under proven authority.

        The canonical same-session lock is acquired before the custody
        proof, and the proof runs before sequence allocation, so a
        refused write aborts without consuming a chat sequence number.
        ``writer_principal`` and ``composition_state_id`` are derived
        from the authority. The injected repository authority is the only
        production writer of ``writer_principal='run_diagnostics'`` rows
        (elspeth-0fcf68d50f).
        """
        return cast(
            ChatMessageRecord,
            await self._run_sync(
                self._run_diagnostics_audit_authority.append_audit_message,
                authority=authority,
                content=content,
                tool_calls=tool_calls,
            ),
        )

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
        """Accept one freeform user message and read its transcript in one transaction.

        Write-then-read split across two pooled connections is how the
        freeform send path produced false Tier-1 ``AuditIntegrityError``
        500s: ``add_message`` committed on one connection while the
        follow-up ``get_messages`` snapshot ran on another, so a stale
        reader (read/write-splitting proxy, pinned snapshot) could return
        a transcript that did not yet contain the committed insert. This
        method performs the insert and the transcript SELECT on the SAME
        connection inside a single ``_session_process_locked_begin``
        transaction holding ``_session_write_lock``, so the returned
        transcript contains the inserted row as its maximum
        ``sequence_no`` BY CONSTRUCTION — no retry or tolerance semantics
        are involved, and none may be added.

        Behaviour is otherwise identical to :meth:`add_message` (state
        cross-session guard, sequence allocation under the write lock,
        and ``sessions.updated_at`` bump).

        Durable admission owns replay. This transaction binds the one
        running action, new user row and same-session ingress receipt.
        """
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.INGRESS_SQL,),
            session_id=str(session_id),
            context=session_operation_context,
            running=running,
        )
        if role != "user" or writer_principal != "route_user_message":
            _refuse_required_sql(required_work, ValueError("message ingress requires a route-owned user message"))
        if raw_content is not None or tool_calls is not None or tool_call_id is not None or parent_assistant_id is not None:
            _refuse_required_sql(required_work, ValueError("message ingress user row cannot carry provider or tool metadata"))
        now = self._now()
        sid = str(session_id)
        csid = str(composition_state_id) if composition_state_id else None
        requested_sid = str(requested_state_id) if requested_state_id else None
        request_id = str(operation_id)
        pid = str(parent_assistant_id) if parent_assistant_id else None
        msg_id_holder: dict[str, str] = {}

        def _sync() -> Sequence[Any]:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ),
            ):
                if requested_sid is not None:
                    _assert_state_in_session(
                        conn,
                        state_id=requested_sid,
                        expected_session_id=sid,
                        caller="add_message_with_transcript",
                    )
                if csid is not None:
                    _assert_state_in_session(
                        conn,
                        state_id=csid,
                        expected_session_id=sid,
                        caller="add_message_with_transcript",
                    )
                seq = self._reserve_sequence_range(conn, sid, count=1)
                msg_id_holder["id"] = self._insert_chat_message(
                    conn,
                    session_id=sid,
                    role=role,
                    content=content,
                    raw_content=raw_content,
                    # Same deep_thaw rationale as ``add_message``:
                    # tool_calls may be a frozen mapping/tuple shape the
                    # JSON encoder rejects.
                    tool_calls=deep_thaw(tool_calls) if tool_calls else None,
                    sequence_no=seq,
                    writer_principal=writer_principal,
                    composition_state_id=csid,
                    tool_call_id=tool_call_id,
                    parent_assistant_id=pid,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                if (
                    running.claim.session_id != session_id
                    or running.claim.operation_id != request_id
                    or running.session_operation_context != session_operation_context
                ):
                    raise AuditIntegrityError("composer ingress running authority mismatch")
                bind_composer_operation_user_message_on_connection(conn, running, user_message_id=UUID(msg_id_holder["id"]))
                self._insert_message_ingress_receipt(
                    conn,
                    session_id=sid,
                    operation_id=request_id,
                    user_message_id=msg_id_holder["id"],
                    requested_state_id=requested_sid,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._session_mutations(
                    conn, session_id=sid, session_operation_context=session_operation_context
                ) as session_mutations:
                    session_mutations.mark_session_updated(updated_at=now)
                # Transcript snapshot on the SAME connection, inside the
                # SAME transaction as the insert — this read sees its own
                # write on every dialect, which is the entire point.
                message_rows = conn.execute(
                    select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
                ).fetchall()
                return message_rows

        sync_result = (
            await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync)
        )
        transcript = [self._row_to_chat_message_record(row) for row in sync_result]
        if not transcript or str(transcript[-1].id) != msg_id_holder["id"]:
            # By construction (single transaction, session write lock held
            # for the sequence allocation) the inserted row IS the maximum
            # sequence_no in the snapshot just read. If this fires, the
            # store violated read-your-own-write inside one transaction —
            # genuine engine/store corruption, not a timing artifact.
            raise AuditIntegrityError(
                "Tier 1 audit anomaly: add_message_with_transcript same-"
                f"transaction snapshot for session {sid} does not end at "
                f"inserted message {msg_id_holder['id']}."
            )
        message = replace(transcript[-1], operation_id=operation_id)
        transcript[-1] = message
        return MessageIngressFresh(operation_id=operation_id, message=message, transcript=tuple(transcript))

    def _row_to_chat_message_record(self, row: Any, *, operation_id: str | None = None) -> ChatMessageRecord:
        if operation_id is not None and (row.role != "user" or row.writer_principal != "route_user_message"):
            raise AuditIntegrityError("Tier 1: message ingress receipt is attached to a non-user message")
        return ChatMessageRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            role=row.role,
            content=row.content,
            raw_content=row.raw_content,
            tool_calls=row.tool_calls,
            created_at=restore_utc(row.created_at),
            sequence_no=row.sequence_no,
            composition_state_id=UUID(row.composition_state_id) if row.composition_state_id else None,
            writer_principal=row.writer_principal,
            tool_call_id=row.tool_call_id,
            parent_assistant_id=UUID(row.parent_assistant_id) if row.parent_assistant_id else None,
            operation_id=UUID(operation_id) if operation_id is not None else None,
        )

    async def get_messages(
        self,
        session_id: UUID,
        limit: int | None = 100,
        offset: int = 0,
        *,
        required_work: RequiredWorkTicket | None = None,
    ) -> list[ChatMessageRecord]:
        """Get messages for a session, ordered by ``sequence_no`` ascending.

        Rev-4 (B2): the canonical ordering key is ``sequence_no``, allocated
        under the per-session advisory/process write lock. ``created_at`` is
        informational; on fast SQLite paths multiple rows in one
        ``persist_compose_turn`` share a single timestamp, so ordering by
        ``created_at`` produced arbitrary intra-turn ordering. The
        per-session unique index ``ix_chat_messages_session_sequence``
        makes the new key total within a session.
        """

        _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.PREPARATION_READ_SQL,), session_id=str(session_id))

        def _sync() -> Sequence[Any]:
            with self._engine.connect() as conn:
                message_rows = conn.execute(
                    select(chat_messages_table, message_ingress_receipts_table.c.operation_id)
                    .select_from(
                        chat_messages_table.outerjoin(
                            message_ingress_receipts_table,
                            (message_ingress_receipts_table.c.user_message_id == chat_messages_table.c.id)
                            & (message_ingress_receipts_table.c.session_id == chat_messages_table.c.session_id),
                        )
                    )
                    .where(chat_messages_table.c.session_id == str(session_id))
                    .order_by(chat_messages_table.c.sequence_no)
                    .limit(limit)
                    .offset(offset)
                ).fetchall()
                return message_rows

        rows = await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync)

        return [self._row_to_chat_message_record(row, operation_id=row.operation_id) for row in rows]

    def count_tool_responses_for_assistant(
        self,
        *,
        session_id: str,
        assistant_message_id: str | None,
    ) -> int:
        """Count role='tool' rows linked to the given assistant message."""

        if assistant_message_id is None:
            return 0
        with self._engine.connect() as conn:
            result = conn.execute(
                select(func.count())
                .select_from(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id)
                .where(chat_messages_table.c.parent_assistant_id == assistant_message_id)
                .where(chat_messages_table.c.role == "tool")
            ).scalar_one()
        return int(result)

    async def count_tool_responses_for_assistant_async(
        self,
        *,
        session_id: str,
        assistant_message_id: str | None,
    ) -> int:
        """Async dispatcher for :meth:`count_tool_responses_for_assistant`."""

        return cast(
            int,
            await self._run_sync(
                self.count_tool_responses_for_assistant,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
            ),
        )

    @staticmethod
    def _validate_audit_grade_query_args(query_args: Mapping[str, str]) -> dict[str, str]:
        """Return an owned dict after enforcing the privacy allowlist."""

        unexpected = frozenset(query_args) - AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST
        if unexpected:
            raise ValueError(f"unallowlisted audit-grade query args: {sorted(unexpected)}")
        return dict(query_args)

    def record_audit_grade_view(
        self,
        *,
        session_id: str,
        requesting_principal: str,
        auth_provider_type: AuthProviderType,
        request_path: str,
        query_args: Mapping[str, str],
        ip_address: str | None,
    ) -> None:
        """Append one row to ``audit_access_log`` before returning tool rows.

        The writer principal is pinned to ``audit_grade_view``; admin-tool
        writes use a separate path. The privacy posture is mechanical:
        ``query_args`` must already be reduced to the closed allowlist in
        ``AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST``, and ``ip_address`` is
        either stored literally or omitted as ``None``.
        """

        allowed_query_args = self._validate_audit_grade_query_args(query_args)
        try:
            self._audit_access_log_authority.record_audit_grade_view(
                session_id=session_id,
                requesting_principal=requesting_principal,
                auth_provider_type=auth_provider_type,
                request_path=request_path,
                query_args=allowed_query_args,
                ip_address=ip_address,
            )
        except AuditAccessLogWriteError:
            self._telemetry.audit_access_log_write_failed_total.add(1)
            raise
        except SQLAlchemyError as exc:
            self._telemetry.audit_access_log_write_failed_total.add(1)
            raise AuditAccessLogWriteError("audit_access_log write failed for audit-grade messages view") from exc
        self._telemetry.audit_grade_view_total.add(1)

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
        """Async dispatcher for :meth:`record_audit_grade_view`."""

        await self._run_sync(
            self.record_audit_grade_view,
            session_id=session_id,
            requesting_principal=requesting_principal,
            auth_provider_type=auth_provider_type,
            request_path=request_path,
            query_args=query_args,
            ip_address=ip_address,
        )

    def list_audit_access_log(self, *, session_id: str) -> list[AuditAccessLogRecord]:
        """Return audit-access log rows for tests and operator diagnostics."""

        with self._engine.connect() as conn:
            rows = conn.execute(
                select(audit_access_log_table)
                .where(audit_access_log_table.c.session_id == session_id)
                .order_by(audit_access_log_table.c.timestamp)
            ).fetchall()
        return [
            AuditAccessLogRecord(
                id=row.id,
                timestamp=restore_utc(row.timestamp),
                session_id=row.session_id,
                requesting_principal=row.requesting_principal,
                request_path=row.request_path,
                query_args=row.query_args,
                ip_address=row.ip_address,
                writer_principal=row.writer_principal,
            )
            for row in rows
        ]

    def _insert_composition_checkpoint(
        self,
        conn: Connection,
        *,
        session_id: str,
        state: CompositionStateData,
        provenance: CompositionStateProvenance,
        created_at: datetime,
        session_operation_context: SessionOperationContext,
        state_id: UUID | None = None,
        derived_from_state_id: str | None = None,
        operation_kind: Literal[SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL] = SessionOperationKind.COMPOSE,
    ) -> str:
        """Insert a checkpoint under the exact live session operation authority."""
        self._assert_session_write_lock_held(conn, session_id, caller="_insert_composition_checkpoint")
        self._require_session_operation_context_on_connection(
            conn,
            session_operation_context,
            session_id=session_id,
            expected_kind=operation_kind,
            now=database_now(conn),
        )
        return self._insert_composition_state(
            conn,
            session_id=session_id,
            payload=StatePayload(data=state, derived_from_state_id=derived_from_state_id),
            provenance=provenance,
            created_at=created_at,
            state_id=str(state_id or uuid.uuid4()),
            session_operation_context=session_operation_context,
        )

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

        The session-operation authority owns the locked transaction, exact
        COMPOSE fence validation, version allocation, and insert.

        Version is max(existing versions for session) + 1, starting at 1.

        ``provenance`` is required (no default). Earlier revisions hardcoded
        ``"session_seed"`` here, which silently conflated four distinct
        writer paths (session create / branch reseed plus the three
        ``_handle_*`` partial-state captures from
        ``web/sessions/routes.py``). Threading the discriminator through
        the public API restores the audit attribution promised by
        §4.1.2 and the ``ck_composition_states_provenance`` CHECK
        constraint.
        """
        state_id = uuid.uuid4()
        now = self._now()
        sid = str(session_id)
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.RECOVERY_PARTIAL_STATE_SQL),
            session_id=sid,
            context=session_operation_context,
        )
        if type(session_operation_context) is not SessionOperationContext:
            _refuse_required_sql(required_work, TypeError("session_operation_context must be an exact SessionOperationContext"))
        if (
            session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
            or session_operation_context.fence.session_id != sid
        ):
            _refuse_required_sql(required_work, SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH))
        with _required_sql_preflight(required_work):
            creation = SessionCompositionStateCreation(
                id=state_id,
                data=state,
                provenance=provenance,
                created_at=now,
                derived_from_state_id=None,
            )
        if required_work is not None:
            return await run_required_sql_in_worker(
                required_work,
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.composition_states.append_state(creation),
            )
        return cast(
            "CompositionStateRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.composition_states.append_state(creation),
            ),
        )

    async def save_composition_state_with_interpretations(
        self,
        session_id: UUID,
        state: CompositionStateData,
        *,
        provenance: CompositionStateProvenance,
        interpretations: tuple[PreparedInterpretationEventDraft, ...],
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord:
        """Commit one imported state and its review-event cohort atomically.

        The prepared writer closures reuse the exact standalone event-writer
        boundary, but execute under the state insert's transaction.  Any
        writer rejection or storage failure therefore rolls back the state and
        every earlier event in the cohort.  Opt-out auto-resolution may append
        derived versions in the same transaction; return that final head.
        """
        if type(interpretations) is not tuple or any(type(draft) is not PreparedInterpretationEventDraft for draft in interpretations):
            raise TypeError("interpretations must be an exact tuple of PreparedInterpretationEventDraft")
        sid = str(session_id)
        state_id = uuid.uuid4()
        now = self._now()
        prepared_events: list[_PreparedPendingInterpretation] = []
        for index, draft in enumerate(interpretations):
            prepared = await self._prepare_or_create_pending_interpretation_event(
                session_id=session_id,
                composition_state_id=state_id,
                affected_node_id=draft.affected_node_id,
                tool_call_id=draft.tool_call_id,
                user_term=draft.user_term,
                kind=draft.kind,
                llm_draft=draft.llm_draft,
                model_identifier=draft.model_identifier,
                model_version=draft.model_version,
                provider=draft.provider,
                composer_skill_hash=draft.composer_skill_hash,
                surface_origin=draft.surface_origin,
                session_operation_context=session_operation_context,
                # list_interpretation_events orders by created_at then id.
                # Preserve the generic surfacer's deterministic call order
                # without depending on random event UUID ordering.
                created_at=now + timedelta(microseconds=index),
                _event_id=draft.event_id,
                _prepare_only=True,
                proposed_state=state,
            )
            if type(prepared) is not _PreparedPendingInterpretation:
                raise AuditIntegrityError("imported interpretation preparation did not return an exact package")
            prepared_events.append(prepared)

        def _sync() -> CompositionStateRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                # This method writes ``composition_states`` AND
                # ``interpretation_events``, and both of the platform writers it
                # fuses (``save_composition_state``,
                # ``create_pending_interpretation_event``) settle under an
                # operation context. Without this check the whole cohort would
                # commit outside ``session_operation_fences``.
                db_now = database_now(conn)
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=SessionOperationKind.COMPOSE,
                    now=db_now,
                )
                self._insert_composition_checkpoint(
                    conn,
                    session_id=sid,
                    state=state,
                    provenance=provenance,
                    created_at=now,
                    state_id=state_id,
                    session_operation_context=session_operation_context,
                )
                # Prepared interpretation packages execute under the same
                # COMPOSE authority as the state insert.
                interpretation_state = _RepositoryMutationState(
                    conn,
                    session_id=sid,
                    database_now=db_now,
                    operation_context=session_operation_context,
                )
                try:
                    interpretation_mutations = _RepositoryInterpretationMutations(interpretation_state)
                    for prepared_event in prepared_events:
                        interpretation_mutations.create_or_reconcile_pending(prepared_event.command, prepared_event.validator)
                finally:
                    interpretation_state._close()
                result_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one()
                return self._row_to_state_record(result_row)

        return cast(CompositionStateRecord, await self._run_sync(_sync))

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
        """Persist a post-compose state and assistant under exact authority."""

        sid = str(session_id)
        expected_state_id = str(expected_current_state_id) if expected_current_state_id is not None else None
        now = self._now()

        def _sync() -> tuple[CompositionStateRecord, ChatMessageRecord]:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ),
            ):
                current_state_id = conn.execute(
                    select(composition_states_table.c.id)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).scalar_one_or_none()
                if current_state_id != expected_state_id:
                    raise StaleComposeStateError(
                        "commit_composition_response: current composition state changed "
                        f"for session_id={sid!r}; expected={expected_state_id!r}, "
                        f"actual={current_state_id!r}"
                    )

                state_id = self._insert_composition_checkpoint(
                    conn,
                    session_id=sid,
                    state=state,
                    provenance="post_compose",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                message_record = self._insert_transition_assistant(
                    conn,
                    session_id=sid,
                    state_id=state_id,
                    content=assistant_content,
                    raw_content=raw_content,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .where(composition_states_table.c.id == state_id)
                ).one()
                return self._row_to_state_record(state_row), message_record

        state_record, message_record = cast(
            tuple[CompositionStateRecord, ChatMessageRecord],
            await self._run_sync(_sync),
        )
        return TransitionResponseSettlement(state=state_record, message=message_record)

    async def get_current_state(
        self,
        session_id: UUID,
        *,
        required_work: RequiredWorkTicket | None = None,
    ) -> CompositionStateRecord | None:
        """Return the highest-version state for a session, or None."""

        _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.PREPARATION_READ_SQL,), session_id=str(session_id))

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == str(session_id))
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).fetchone()

        row = await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync)

        if row is None:
            return None

        return self._row_to_state_record(row)

    async def get_state_versions(
        self,
        session_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompositionStateRecord]:
        """Return state versions for a session, ascending order."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == str(session_id))
                    .order_by(composition_states_table.c.version)
                    .limit(limit)
                    .offset(offset)
                ).fetchall()

        rows = await self._run_sync(_sync)

        return [self._row_to_state_record(row) for row in rows]

    @staticmethod
    def _unwrap_envelope(val: Any) -> Any:
        """Unwrap one composition_states JSON column through the single envelope rule."""
        return unwrap_state_column(val)

    def _row_to_state_record(self, row: Any) -> CompositionStateRecord:
        """Convert a SQLAlchemy row to a CompositionStateRecord.

        Seam contract A: metadata_ maps DB column metadata_ back to the
        dataclass field. JSON columns are unwrapped from their _version envelope.
        """
        row_mapping = row._mapping
        return CompositionStateRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            version=row.version,
            source=self._unwrap_envelope(row.source),
            sources=self._unwrap_envelope(row_mapping["sources"] if "sources" in row_mapping else None),
            nodes=self._unwrap_envelope(row.nodes),
            edges=self._unwrap_envelope(row.edges),
            outputs=self._unwrap_envelope(row.outputs),
            metadata_=self._unwrap_envelope(row.metadata_),
            is_valid=row.is_valid,
            validation_errors=decode_stored_composition_validation_errors(row.validation_errors),
            created_at=restore_utc(row.created_at),
            derived_from_state_id=(UUID(row.derived_from_state_id) if row.derived_from_state_id is not None else None),
            composer_meta=self._unwrap_envelope(row.composer_meta),
        )

    async def create_run(
        self,
        session_id: UUID,
        state_id: UUID,
        pipeline_yaml: str | None = None,
        *,
        session_operation_context: SessionOperationContext,
        execution_input: RunExecutionInput | None = None,
    ) -> RunRecord:
        """Create a new pending run, enforcing one active run per session (B6).

        Enforced by partial unique index uq_runs_one_active_per_session
        (at most one row with status IN ('pending','running') per session_id).
        The SELECT is an early-out optimization; the index is the real guard.
        Raises RunAlreadyActiveError if a pending or running run exists.

        Run admission is part of the same-session custody lock domain: the
        blob update/delete active-run guards evaluate inside
        ``locked_session_transaction`` and are only sound if this INSERT is
        mutually exclusive with that lock. A bare ``engine.begin()`` here
        would let PostgreSQL admit a run concurrently with an in-flight blob
        mutation (both sides passing their guards) — SQLite masks the hole
        only because its ``engine.begin()`` issues ``BEGIN IMMEDIATE``
        (elspeth-3d1d1fcb6c).
        """
        run_id = uuid.uuid4()
        now = self._now()
        return cast(
            "RunRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.create_pending_run(
                    run_id=run_id,
                    state_id=state_id,
                    pipeline_yaml=pipeline_yaml,
                    started_at=now,
                    execution_input=execution_input,
                ),
            ),
        )

    async def check_approval_binding(
        self,
        session_id: UUID,
        state_id: UUID,
        *,
        approval: ApprovalGateInputs,
        session_operation_context: SessionOperationContext,
    ) -> AdmissionRefusalReason | None:
        if session_operation_context.fence.session_id != str(session_id):
            raise AuditIntegrityError("Approval preflight session custody mismatch")
        return cast(
            "AdmissionRefusalReason | None",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.check_approval_binding(state_id=state_id, approval=approval),
            ),
        )

    async def assess_run_start_admission(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return await self._audited_start_admission(
            run_id,
            session_operation_context=session_operation_context,
            admit=lambda transaction: transaction.runs.assess_start_admission(
                run_id=run_id, policy=self._chargeable_admission_policy, approval=approval
            ),
        )

    async def issue_run_start_permit(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return await self._audited_start_admission(
            run_id,
            session_operation_context=session_operation_context,
            admit=lambda transaction: transaction.runs.issue_start_permit(
                run_id=run_id, policy=self._chargeable_admission_policy, approval=approval
            ),
        )

    async def _audited_start_admission(
        self,
        run_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
        admit: Callable[[SessionOperationMutationTransaction], RunStartPermitRecord],
    ) -> RunStartPermitRecord:
        """Decide one permit; write ``quota_exceeded`` before commit only when THIS call refused on quota.

        The run facet appends the run's terminal ``failed`` event exactly once,
        when a refusal is first recorded (``_RepositoryRunMutations._record_admission_refusal``).
        A terminal event that already exists before ``admit`` runs therefore marks
        a replayed refusal, which is not audited a second time. The EXECUTE fence
        admits one holder, so no concurrent decision can race this read.
        """
        session = await self.get_session(UUID(session_operation_context.fence.session_id))
        already_terminal = any(event.event_type in SESSION_TERMINAL_RUN_STATUS_VALUES for event in await self.list_run_events(run_id))

        def _decide(transaction: SessionOperationMutationTransaction) -> RunStartPermitRecord:
            permit = admit(transaction)
            refusal = permit.execution_refusal or permit.admission_decision
            if not already_terminal and refusal is not None and refusal.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED:
                self._quota_exceeded_recorder(self._quota_exceeded_outcome(session, refusal, operation=ChargeableOperation.RUN))
            return permit

        return cast(
            "RunStartPermitRecord",
            await self._run_sync(self._session_operation_authority.mutate, session_operation_context, _decide),
        )

    @staticmethod
    def _quota_exceeded_outcome(
        session: SessionRecord, decision: ChargeableAdmissionDecision, *, operation: ChargeableOperation
    ) -> QuotaExceeded:
        evidence = decision.evidence
        if decision.refusal_reason is not AdmissionRefusalReason.QUOTA_EXCEEDED or evidence.dimension is None or evidence.usage is None:
            raise AuditIntegrityError("Only a measured quota_exceeded decision has a quota_exceeded audit row")
        return QuotaExceeded(
            identity_id=session.user_id,
            provider=session.auth_provider_type,
            operation=operation.value,
            dimension=evidence.dimension,
            cap=evidence.cap,
            ceiling=evidence.ceiling,
            usage=evidence.usage,
            identity_policy_id=evidence.identity_policy_id,
            container_policy_id=evidence.container_policy_id,
        )

    async def observe_run_start_permit_for_cleanup(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext
    ) -> RunStartPermitRecord:
        return cast(
            "RunStartPermitRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.observe_start_permit_for_cleanup(run_id=run_id),
            ),
        )

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision:
        decision = cast(
            "ChargeableAdmissionDecision",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.session.assess_chargeable_operation(
                    policy=self._chargeable_admission_policy, operation=operation
                ),
            ),
        )
        if decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED:
            # A Composer or auto-title refusal writes no sessions row, so its
            # audit row follows the read-only admission transaction and still
            # precedes the caller learning of the refusal (R4).
            session = await self.get_session(UUID(session_operation_context.fence.session_id))
            await self._run_sync(self._quota_exceeded_recorder, self._quota_exceeded_outcome(session, decision, operation=operation))
        return decision

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        """Charge provider calls whose evidence is not a Composer audit cohort (Task I1).

        Auto-title spends under COMPOSE authority; a run's LLM calls are charged
        under the run's EXECUTE authority. Composer cohorts are charged inside
        their own audit transaction and never come through here.
        """
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        expected_kind = SessionOperationKind.EXECUTE if source == "run" else SessionOperationKind.COMPOSE
        if session_operation_context.operation_kind is not expected_kind:
            raise ValueError(f"source={source!r} token usage requires {expected_kind.value} authority")
        if not entries:
            return ()
        sid = session_operation_context.fence.session_id

        def _sync() -> tuple[str, ...]:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=expected_kind,
                    now=database_now(conn),
                )
                if (
                    run_id is not None
                    and conn.execute(select(runs_table.c.session_id).where(runs_table.c.id == str(run_id))).scalar_one_or_none() != sid
                ):
                    raise AuditIntegrityError("Run token usage names a run outside the operation's session")
                return record_token_usage_on_connection(
                    conn,
                    session_id=sid,
                    source=source,
                    run_id=None if run_id is None else str(run_id),
                    entries=entries,
                    recorded_at=database_now(conn),
                )

        return cast("tuple[str, ...]", await self._run_sync(_sync))

    async def begin_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None = None,
        required_work: RequiredWorkTicket | None = None,
    ) -> ProviderAttempt:
        """Admit a provider dispatch and retain pending evidence before sending it."""
        if type(session_operation_context) is not SessionOperationContext:
            _refuse_required_sql(required_work, TypeError("provider admission requires an owned operation context"))
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.PROVIDER_ADMISSION_SQL, RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL),
            session_id=session_operation_context.fence.session_id,
            context=session_operation_context,
        )
        if required_work is not None:
            if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
                _refuse_required_sql(required_work, ValueError("required provider admission needs COMPOSE authority"))
            expected_source = "auto_title" if required_work.key.source is RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL else "composer"
            if source != expected_source or run_id is not None:
                _refuse_required_sql(required_work, AuditIntegrityError("required provider admission role/source mismatch"))

            def _required_admission() -> ProviderAttempt | _ProviderAdmissionRefused:
                try:
                    return self._begin_provider_attempt_sync(
                        session_operation_context=session_operation_context, source=source, run_id=run_id
                    )
                except ChargeableAdmissionRefused as refusal:
                    try:
                        self._record_provider_attempt_quota_refusal_sync(session_operation_context, source, refusal)
                    except BaseException as recorder_error:
                        raise BaseExceptionGroup("provider admission refusal recording failed", [refusal, recorder_error]) from None
                    return _ProviderAdmissionRefused(refusal)

            outcome = await run_required_sql_in_worker(required_work, _required_admission)
            if isinstance(outcome, _ProviderAdmissionRefused):
                raise outcome.refusal
            return outcome

        def _sync() -> ProviderAttempt:
            return self._begin_provider_attempt_sync(session_operation_context=session_operation_context, source=source, run_id=run_id)

        compose_custody = (
            type(session_operation_context) is SessionOperationContext
            and session_operation_context.operation_kind is SessionOperationKind.COMPOSE
        )
        try:
            return await self._run_composer_sql(_sync) if compose_custody else await self._run_sync(_sync)
        except ChargeableAdmissionRefused as exc:

            def _record_refusal(refusal: ChargeableAdmissionRefused = exc) -> None:
                self._record_provider_attempt_quota_refusal_sync(session_operation_context, source, refusal)

            if compose_custody:
                await self._run_composer_sql(_record_refusal)
            else:
                await self._run_sync(_record_refusal)
            raise

    def begin_run_provider_attempt_sync(self, *, session_operation_context: SessionOperationContext, run_id: UUID) -> ProviderAttempt:
        """Return the committed EXECUTE attempt identity to the pipeline worker."""
        try:
            return self._begin_provider_attempt_sync(session_operation_context=session_operation_context, source="run", run_id=run_id)
        except ChargeableAdmissionRefused as exc:
            self._record_provider_attempt_quota_refusal_sync(session_operation_context, "run", exc)
            raise

    def _begin_provider_attempt_sync(
        self, *, session_operation_context: SessionOperationContext, source: TokenUsageSource, run_id: UUID | None
    ) -> ProviderAttempt:
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        expected_kind = SessionOperationKind.EXECUTE if source == "run" else SessionOperationKind.COMPOSE
        if session_operation_context.operation_kind is not expected_kind:
            raise ValueError(f"source={source!r} provider attempts require {expected_kind.value} authority")
        sid = session_operation_context.fence.session_id
        attempt: ProviderAttempt | None = None
        try:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn, session_operation_context, session_id=sid, expected_kind=expected_kind, now=database_now(conn)
                )
                attempt = begin_provider_attempt_on_connection(
                    conn,
                    session_operation_context=session_operation_context,
                    source=source,
                    policy=self._chargeable_admission_policy,
                    run_id=None if run_id is None else str(run_id),
                )
        except Exception as exc:
            if source == "run" and attempt is not None:
                # The body wrote an attempt, but the transaction exit failed.
                # A commit may have landed even if its receipt did not. Never
                # dispatch or manufacture a zero-use outcome from this state.
                raise AuditIntegrityError("run provider admission commit outcome is uncertain; reconcile the pending attempt") from exc
            raise
        if attempt is None:
            raise AuditIntegrityError("Provider admission returned without an attempt")
        return attempt

    def _record_provider_attempt_quota_refusal_sync(
        self, session_operation_context: SessionOperationContext, source: TokenUsageSource, exc: ChargeableAdmissionRefused
    ) -> None:
        if exc.decision.refusal_reason is not AdmissionRefusalReason.QUOTA_EXCEEDED:
            return
        sid = session_operation_context.fence.session_id
        with self._engine.connect() as conn:
            row = conn.execute(select(sessions_table).where(sessions_table.c.id == sid)).fetchone()
        if row is None:
            raise SessionNotFoundError(UUID(sid))
        session = self._row_to_session_record(row)
        self._quota_exceeded_recorder(self._quota_exceeded_outcome(session, exc.decision, operation=ChargeableOperation(source)))

    async def finish_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        call: ComposerLLMCall,
        required_work: RequiredWorkTicket | None = None,
    ) -> None:
        """Persist actual terminal call evidence and settle its ledger atomically."""
        from elspeth.web.composer.audit import llm_call_audit_envelope

        if type(session_operation_context) is not SessionOperationContext:
            _refuse_required_sql(required_work, TypeError("provider settlement requires an owned operation context"))

        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL),
            session_id=session_operation_context.fence.session_id,
            context=session_operation_context,
        )

        with _required_sql_preflight(required_work):
            if type(call) is not ComposerLLMCall or call.call_id is None:
                raise AuditIntegrityError("A provider checkpoint requires its pending attempt identity")
            drafts = (
                AuditMessageDraft(role="audit", content="Provider call result recorded.", tool_calls=(llm_call_audit_envelope(call),)),
            )
        try:
            await self.add_messages_atomic(
                UUID(session_operation_context.fence.session_id),
                drafts,
                writer_principal="compose_loop",
                session_operation_context=session_operation_context,
                audit_only=True,
                required_work=required_work,
            )
        except SQLAlchemyError as exc:
            raise ComposerRequiredAuditPersistenceError(
                "composer_llm_call_persist_failed: required provider checkpoint failed", helper="llm_calls"
            ) from exc

    async def cancel_undispatched_provider_attempt(
        self,
        *,
        session_operation_context: SessionOperationContext,
        attempt_id: str,
        requested_model: str,
        required_work: RequiredWorkTicket | None = None,
    ) -> None:
        """Close a COMPOSE intent only when its owner proved SDK entry never occurred.

        A distinct audit message, zero-use ledger entry, and settlement commit
        together under the original live fence. This cannot reconcile a past
        pending attempt or a provider call whose outcome is unknown.
        """
        if type(session_operation_context) is not SessionOperationContext:
            _refuse_required_sql(required_work, TypeError("undispatched cancellation requires an owned operation context"))
        _validate_required_sql_ticket(
            required_work,
            sources=(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,),
            session_id=session_operation_context.fence.session_id,
            context=session_operation_context,
        )
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise ValueError("Undispatched cancellation requires COMPOSE authority")
        sid = session_operation_context.fence.session_id

        def _sync() -> None:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=SessionOperationKind.COMPOSE,
                    now=database_now(conn),
                    audit_only=True,
                )

                def _append_audit_event(event_id: str, content: str, created_at: datetime) -> None:
                    self._assert_session_write_lock_held(conn, sid, caller="cancel_undispatched_provider_attempt")
                    sequence_no = self._reserve_sequence_range(conn, sid, count=1)
                    self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="audit",
                        content=content,
                        raw_content=None,
                        tool_calls=None,
                        sequence_no=sequence_no,
                        writer_principal="compose_loop",
                        composition_state_id=None,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=created_at,
                        session_operation_context=session_operation_context,
                        message_id=event_id,
                        audit_only=True,
                    )
                    with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as mutations:
                        mutations.mark_session_updated(updated_at=created_at)

                cancel_undispatched_provider_attempt_on_connection(
                    conn,
                    session_operation_context=session_operation_context,
                    attempt_id=attempt_id,
                    requested_model=requested_model,
                    append_audit_event=_append_audit_event,
                )

        if required_work is not None:
            await run_required_sql_in_worker(required_work, _sync)
        else:
            await self._run_composer_sql(_sync)

    async def settle_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        """Settle non-Composer provider evidence under its original session lease."""

        def _sync() -> None:
            self._settle_provider_attempt_sync(session_operation_context=session_operation_context, attempt_id=attempt_id, entry=entry)

        if (
            type(session_operation_context) is SessionOperationContext
            and session_operation_context.operation_kind is SessionOperationKind.COMPOSE
        ):
            await self._run_composer_sql(_sync)
        else:
            await self._run_sync(_sync)

    def settle_run_provider_attempt_sync(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        """Commit exact Landscape usage before returning to the pipeline worker."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.EXECUTE:
            raise ValueError("Run provider settlement requires EXECUTE authority")
        self._settle_provider_attempt_sync(session_operation_context=session_operation_context, attempt_id=attempt_id, entry=entry)

    def _settle_provider_attempt_sync(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        expected_kind = session_operation_context.operation_kind
        if expected_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.EXECUTE}:
            raise ValueError("Provider settlement requires COMPOSE or EXECUTE authority")
        sid = session_operation_context.fence.session_id
        body_completed = False
        try:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn, session_operation_context, session_id=sid, expected_kind=expected_kind, now=database_now(conn), audit_only=True
                )
                settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=attempt_id, entry=entry)
                body_completed = True
        except Exception as exc:
            if expected_kind is SessionOperationKind.EXECUTE and body_completed:
                # The exact call evidence is in Landscape. Keep it and the
                # attempt row for an authoritative replay of this settlement.
                raise AuditIntegrityError("run provider settlement commit outcome is uncertain; replay exact Landscape evidence") from exc
            raise

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
    ) -> RunRecord:
        return cast(
            "RunRecord",
            await self._run_sync(
                RepositoryRunCancellationAuthority(self._engine).request,
                run_id,
                session_id=session_id,
                user_id=user_id,
                auth_provider_type=auth_provider_type,
            ),
        )

    async def list_recoverable_run_records(self) -> tuple[RunRecord, ...]:
        return cast(
            "tuple[RunRecord, ...]",
            await self._run_sync(
                RepositoryGlobalRunRecoveryAuthority(self._engine).list_recoverable_run_records,
            ),
        )

    async def get_run_execution_input(self, run_id: UUID) -> RunExecutionInput | None:
        from elspeth.web.execution.envelope import RunExecutionInput

        def _sync() -> RunExecutionInput | None:
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(run_execution_inputs_table).where(run_execution_inputs_table.c.run_id == str(run_id))
                ).one_or_none()
                if row is None:
                    return None
                return RunExecutionInput(
                    schema_version=row.schema_version,
                    envelope_json=json.dumps(row.envelope, sort_keys=True, separators=(",", ":")),
                    canonical_input_digest=row.canonical_input_digest,
                    topology_digest=row.topology_digest,
                    source_manifest_digest=row.source_manifest_digest,
                    application_fingerprint=row.application_fingerprint,
                    plugin_registry_fingerprint=row.plugin_registry_fingerprint,
                    configuration_fingerprint=row.configuration_fingerprint,
                    graph_fingerprint=row.graph_fingerprint,
                    runtime_fingerprint=row.runtime_fingerprint,
                    implementation_fingerprint=row.implementation_fingerprint,
                    deployment_generation=row.deployment_generation,
                    session_epoch=row.session_epoch,
                    landscape_epoch=row.landscape_epoch,
                    coordination_protocol=row.coordination_protocol,
                    automatic_recovery_eligible=row.automatic_recovery_eligible,
                )

        return cast("RunExecutionInput | None", await self._run_sync(_sync))

    async def get_run(self, run_id: UUID) -> RunRecord:
        """Fetch a run by ID. Raises ValueError if not found."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(select(runs_table).where(runs_table.c.id == str(run_id))).fetchone()

        row = await self._run_sync(_sync)

        if row is None:
            raise ValueError(f"Run not found: {run_id}")

        return self._row_to_run_record(row)

    async def list_runs_for_session(self, session_id: UUID) -> list[RunRecord]:
        """List all runs for a session, newest first."""
        sid = str(session_id)

        def _sync() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(
                    select(runs_table).where(runs_table.c.session_id == sid).order_by(runs_table.c.started_at.desc())
                ).fetchall()

        rows = await self._run_sync(_sync)
        return [self._row_to_run_record(row) for row in rows]

    async def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: SessionRunEventType,
        data: Mapping[str, Any],
        session_operation_context: SessionOperationContext,
    ) -> RunEventRecord:
        """Append a structured run event for websocket replay and audit inspection."""
        if type(run_id) is not UUID:
            raise TypeError("run_id must be an exact UUID")
        if type(timestamp) is not datetime:
            raise TypeError("timestamp must be an exact datetime")
        if timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        event_type_value: object = event_type
        if type(event_type_value) is not str or event_type_value not in SESSION_RUN_EVENT_TYPE_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: run_events.event_type is {event_type!r}, expected one of {sorted(SESSION_RUN_EVENT_TYPE_VALUES)}"
            )
        return cast(
            "RunEventRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.append_run_event(
                    run_id=run_id,
                    timestamp=timestamp,
                    event_type=event_type,
                    data=data,
                ),
            ),
        )

    async def list_run_events(self, run_id: UUID) -> list[RunEventRecord]:
        """List persisted run events in durable insertion order."""
        rid = str(run_id)

        def _sync() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(
                    select(run_events_table).where(run_events_table.c.run_id == rid).order_by(run_events_table.c.sequence)
                ).fetchall()

        rows = await self._run_sync(_sync)
        return [self._row_to_run_event_record(row) for row in rows]

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
        """Update a run's status and optional fields.

        Enforces LEGAL_RUN_TRANSITIONS (D3). Enforces landscape_run_id
        write-once semantics (D4). Sets finished_at for terminal states
        (completed, failed, cancelled). Optional parameters only update
        the column when not None. Raises ValueError if run not found or
        transition is illegal.
        """
        try:
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.transition_run_status(
                    run_id=run_id,
                    status=status,
                    error=error,
                    landscape_run_id=landscape_run_id,
                    rows_processed=rows_processed,
                    rows_succeeded=rows_succeeded,
                    rows_failed=rows_failed,
                    rows_routed_success=rows_routed_success,
                    rows_routed_failure=rows_routed_failure,
                    rows_quarantined=rows_quarantined,
                ),
            )
        except SessionDerivedCustodyError:
            raise ValueError(f"Run not found: {run_id}") from None

    async def record_blob_inline_resolutions(
        self,
        *,
        run_id: UUID,
        resolutions: Sequence[ResolvedBlobContent],
        attempt: int = 1,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Write audit rows for runtime-resolved inline blob content.

        This is an audit-primary write site: callers must invoke it before
        resolved bytes can reach plugin construction. A DB failure is a
        Tier-1 anomaly and propagates as ``AuditIntegrityError``.
        """
        if not resolutions:
            try:
                await self._run_sync(
                    self._session_operation_authority.compare_and_swap,
                    session_operation_context,
                )
            except SQLAlchemyError as exc:
                raise AuditIntegrityError(f"failed to record blob_inline_resolutions for run {run_id}") from exc
            return
        try:
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.blobs.insert_blob_inline_resolutions(
                    run_id=run_id,
                    attempt=attempt,
                    resolutions=resolutions,
                    resolved_at=self._now(),
                ),
            )
        except SQLAlchemyError as exc:
            raise AuditIntegrityError(f"failed to record blob_inline_resolutions for run {run_id}") from exc

    async def get_active_run(
        self,
        session_id: UUID,
    ) -> RunRecord | None:
        """Return the pending/running run for a session, or None."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(
                    select(runs_table).where(
                        runs_table.c.session_id == str(session_id),
                        runs_table.c.status.in_(["pending", "running"]),
                    )
                ).fetchone()

        row = await self._run_sync(_sync)

        if row is None:
            return None

        return self._row_to_run_record(row)

    async def get_state(self, state_id: UUID, *, required_work: RequiredWorkTicket | None = None) -> CompositionStateRecord:
        """Fetch a composition state by its primary key. Raises ValueError if not found."""

        if required_work is not None:
            _validate_required_sql_ticket(
                required_work, sources=(RequiredWorkSource.PREPARATION_READ_SQL,), session_id=required_work.key.session_id
            )

        def _sync() -> Any:
            with self._engine.begin() as conn:
                query = select(composition_states_table).where(composition_states_table.c.id == str(state_id))
                if required_work is not None:
                    query = query.where(composition_states_table.c.session_id == required_work.key.session_id)
                return conn.execute(query).fetchone()

        row = await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_sync(_sync)

        if row is None:
            raise ValueError(f"State not found: {state_id}")

        return self._row_to_state_record(row)

    async def get_state_in_session(
        self,
        state_id: UUID,
        session_id: UUID,
    ) -> CompositionStateRecord:
        """Scoped read: fetch state and verify it belongs to ``session_id``.

        Runtime defence-in-depth complementing the current-schema
        composite foreign key: persisted data cannot create cross-session
        state references at the schema layer, and this method raises
        ``AuditIntegrityError`` on any mismatch it
        encounters. The exception class is chosen deliberately to match
        the cross-session blob-ref rejection in the
        ``fork_from_message`` route handler (web/sessions/routes.py):
        a Tier 1 anomaly in our own data must surface as corruption,
        not as a soft 404. ``ValueError`` on "state does not exist at
        all" is preserved from ``get_state`` for callers that still
        need to distinguish absence from mismatch.

        The ``AuditIntegrityError`` class is the same signal used by
        the ``fork_from_message`` route handler in
        ``sessions/routes.py`` for the cross-session blob-reference
        rejection — the two guards are the read-side and write-side of
        the same Tier 1 invariant.
        """
        record = await self.get_state(state_id)
        if record.session_id != session_id:
            raise AuditIntegrityError(
                f"Tier 1 audit anomaly: composition_state {state_id} "
                f"belongs to session {record.session_id}, not {session_id}. "
                f"Migration 007 composite FK prevents this for post-007 "
                f"data; pre-007 orphans should have been deleted by "
                f"Variant-A repair. Cross-session state reference rejected."
            )
        return record

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
    ) -> CompositionStateRecord:
        """Copy one checkpoint and settle its receipt in the same fenced transaction."""
        sid = str(fence.session_id)
        target_state_id = str(state_id)
        now = self._now()

        def _sync() -> CompositionStateRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                receipt, _database_now = self.require_operation_receipt_authority_on_connection(conn, fence, session_operation_context)
                if receipt["kind"] != "state_revert":
                    raise AuditIntegrityError("state revert requires a state_revert receipt")
                self._require_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=expected_current_state_id,
                    expected_state_version=expected_current_state_version,
                )
                prior_row = conn.execute(
                    select(composition_states_table).where(composition_states_table.c.id == target_state_id)
                ).one_or_none()
                if prior_row is None or prior_row.session_id != sid:
                    raise ValueError("State not found")
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                if current_row is None:
                    raise AuditIntegrityError("state revert session unexpectedly has no current checkpoint")

                # Verify every pending pipeline authority before writing a new head.
                pending_rows = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.tool_name == "set_pipeline")
                    .where(composition_proposals_table.c.status == "pending")
                    .order_by(composition_proposals_table.c.created_at, composition_proposals_table.c.id)
                ).fetchall()
                pending_authorities: list[AuthoritativePipelineProposal] = []
                for proposal_row in pending_rows:
                    creation_rows = conn.execute(
                        select(proposal_events_table)
                        .where(proposal_events_table.c.session_id == sid)
                        .where(proposal_events_table.c.proposal_id == proposal_row.id)
                        .where(proposal_events_table.c.event_type == "proposal.created")
                    ).fetchall()
                    if len(creation_rows) != 1:
                        raise AuditIntegrityError("state revert pipeline proposal must have exactly one creation event")
                    authority = _restore_authoritative_pipeline_proposal(
                        row=_proposal_record_from_row(proposal_row),
                        creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    )
                    _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                    pending_authorities.append(authority)

                for authority in pending_authorities:
                    proposal_id = str(authority.row.id)
                    event_id = str(uuid.uuid4())
                    conn.execute(
                        insert(proposal_events_table).values(
                            id=event_id,
                            session_id=sid,
                            proposal_id=proposal_id,
                            event_type="proposal.rejected",
                            actor=actor,
                            payload=_pipeline_rejected_payload(authority=authority, reason="superseded", dispatch=None),
                            created_at=now,
                        )
                    )
                    rejected = conn.execute(
                        update(composition_proposals_table)
                        .where(
                            composition_proposals_table.c.session_id == sid,
                            composition_proposals_table.c.id == proposal_id,
                            composition_proposals_table.c.status == "pending",
                        )
                        .values(status="rejected", audit_event_id=event_id, updated_at=now)
                    )
                    if rejected.rowcount != 1:
                        raise OperationReceiptSettlementConflictError()

                new_state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(
                        data=CompositionStateData(
                            sources=self._unwrap_envelope(prior_row.sources),
                            nodes=self._unwrap_envelope(prior_row.nodes),
                            edges=self._unwrap_envelope(prior_row.edges),
                            outputs=self._unwrap_envelope(prior_row.outputs),
                            metadata_=self._unwrap_envelope(prior_row.metadata_),
                            is_valid=prior_row.is_valid,
                            validation_errors=decode_stored_composition_validation_errors(prior_row.validation_errors),
                            composer_meta=self._unwrap_envelope(prior_row.composer_meta),
                        ),
                        derived_from_state_id=target_state_id,
                    ),
                    provenance="session_seed",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                state_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == new_state_id)).one()
                record = self._row_to_state_record(state_row)
                sequence_no = self._reserve_sequence_range(conn, sid, count=1)
                self._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="system",
                    content=f"Pipeline reverted to version {prior_row.version}.",
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no,
                    writer_principal="route_system_message",
                    composition_state_id=None,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._session_mutations(
                    conn, session_id=sid, session_operation_context=session_operation_context
                ) as session_mutations:
                    session_mutations.mark_session_updated(updated_at=now)
                settle_operation_receipt(
                    conn,
                    fence,
                    now=database_now(conn),
                    actor=actor,
                    result=StateRevertReceiptResult(state_id=record.id),
                    response_hash=response_hash_factory(record),
                )
                return record

        return cast("CompositionStateRecord", await self._run_sync(_sync))

    @staticmethod
    def _require_expected_current_state_on_connection(
        conn: Connection,
        *,
        session_id: str,
        expected_state_id: UUID | None,
        expected_state_version: int | None,
    ) -> None:
        """Fence a state-producing operation to the route-observed head.

        The comparison runs under the same session write lock and transaction
        as the state/message/settlement writes. An empty observation is encoded
        as ``(None, None)``; a present observation requires both exact UUID and
        positive version. No stale worker may attach a breadcrumb or locator to
        a different session head.
        """

        if (expected_state_id is None) != (expected_state_version is None):
            raise ValueError("expected current state id and version must be both present or both absent")
        if expected_state_id is not None and type(expected_state_id) is not UUID:
            raise ValueError("expected current state id must be a UUID")
        if expected_state_version is not None and (type(expected_state_version) is not int or expected_state_version < 1):
            raise ValueError("expected current state version must be a positive integer")
        current = conn.execute(
            select(composition_states_table.c.id, composition_states_table.c.version)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(desc(composition_states_table.c.version))
            .limit(1)
        ).one_or_none()
        if expected_state_id is None:
            if current is not None:
                raise OperationReceiptSettlementConflictError()
            return
        if current is None or current.id != str(expected_state_id) or current.version != expected_state_version:
            raise OperationReceiptSettlementConflictError()

    @staticmethod
    def _pipeline_dispatch_recovery_on_connection(
        conn: Connection,
        *,
        authority: AuthoritativePipelineProposal,
    ) -> PipelineDispatchRecovery | None:
        sid = str(authority.row.session_id)
        expected_arguments_hash = semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json)
        rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.session_id == sid)).fetchall()
        matches: list[PipelineDispatchRecovery] = []
        for row in rows:
            if type(row.tool_calls) is not list:
                continue
            for envelope in row.tool_calls:
                recovery = _pipeline_dispatch_recovery_from_envelope(
                    envelope,
                    expected_tool_call_id=authority.row.tool_call_id,
                )
                if recovery is None:
                    continue
                if recovery.binding.arguments_hash != expected_arguments_hash:
                    raise AuditIntegrityError("pipeline recovery dispatch arguments do not match authority")
                matches.append(recovery)
        if len(matches) > 1:
            raise AuditIntegrityError("pipeline recovery has duplicate successful content-bound dispatches")
        return matches[0] if matches else None

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
        cancelled = await self.cancel_all_orphaned_run_records(
            max_age_seconds=max_age_seconds,
            exclude_run_ids=exclude_run_ids,
            reason=reason,
        )
        return len(cancelled)

    async def cancel_all_orphaned_run_records(
        self,
        max_age_seconds: int | None = None,
        exclude_run_ids: frozenset[str] = frozenset(),
        reason: str | None = None,
    ) -> list[RunRecord]:
        """Force-cancel orphaned runs and return the cancelled records.

        Startup reconciliation needs the cancelled run ids and their
        ``landscape_run_id`` anchors so it can terminalize the matching
        Landscape audit rows. ``cancel_all_orphaned_runs`` keeps the older
        integer API by delegating here.
        """
        records = await self._run_sync(
            self._global_run_recovery_authority.cancel_orphaned_run_records,
            max_age_seconds=max_age_seconds,
            exclude_run_ids=exclude_run_ids,
            reason=reason,
        )
        return list(cast("tuple[RunRecord, ...]", records))

    async def list_pending_landscape_reconciliations(self) -> list[RunRecord]:
        """Return the durable, exact pending Landscape reconciliation set."""
        from elspeth.web.sessions.protocol import LANDSCAPE_RECONCILIATION_PENDING_SUFFIX

        def _sync() -> list[RunRecord]:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    select(runs_table)
                    .where(
                        runs_table.c.status == "cancelled",
                        runs_table.c.error.is_not(None),
                        runs_table.c.error.endswith(LANDSCAPE_RECONCILIATION_PENDING_SUFFIX, autoescape=True),
                    )
                    .order_by(runs_table.c.started_at, runs_table.c.id)
                ).fetchall()
            return [self._row_to_run_record(row) for row in rows]

        return cast(list[RunRecord], await self._run_sync(_sync))

    async def mark_landscape_reconciliation_outcomes(
        self,
        *,
        complete_run_ids: frozenset[UUID],
        absent_run_ids: frozenset[UUID],
    ) -> None:
        """Atomically close exact pending markers without rewriting reasons."""
        await self._run_sync(
            self._global_run_recovery_authority.mark_landscape_reconciliation_outcomes,
            complete_run_ids=complete_run_ids,
            absent_run_ids=absent_run_ids,
        )

    def _load_staged_fork_from_transaction(
        self,
        transaction: SessionForkCreationTransaction,
        *,
        parent_session_id: UUID,
        child_session_id: UUID,
        operation_id: str,
        fork_message_id: UUID,
        new_message_content: str,
        authority: SessionForkAuthority,
    ) -> StagedForkSession:
        if type(transaction) is not _ForkCreationTransaction:
            raise TypeError("transaction must be the repository-owned SessionForkCreationTransaction")
        parent_row = transaction.read_parent_session()
        child_row, rows, state_row = transaction.read_child_snapshot()
        if (
            child_row is None
            or child_row.archived_at is None
            or child_row.forked_from_session_id != str(parent_session_id)
            or child_row.forked_from_message_id != str(fork_message_id)
            or parent_row is None
            or child_row.user_id != parent_row.user_id
            or child_row.auth_provider_type != parent_row.auth_provider_type
        ):
            raise AuditIntegrityError("Fork bound child failed archived lineage, principal, or fork message validation")

        plan_candidates: list[tuple[BlobForkPlanEntry, ...]] = []
        public_messages: list[ChatMessageRecord] = []
        for row in rows:
            if row.role == "audit" and row.writer_principal == "session_fork":
                row_source_session_id, row_child_session_id, row_operation_id = _fork_blob_plan_identity_from_content(row.content)
                retained_plan = _fork_blob_plan_from_content(
                    row.content,
                    expected_source_session_id=row_source_session_id,
                    expected_child_session_id=row_child_session_id,
                    expected_operation_id=row_operation_id,
                )
                if row_child_session_id == child_session_id and row_operation_id == operation_id:
                    if row_source_session_id != parent_session_id:
                        raise AuditIntegrityError("staged fork blob plan has malformed custody binding")
                    plan_candidates.append(retained_plan)
            if row.role != "audit":
                public_messages.append(self._row_to_chat_message_record(row))
        if len(plan_candidates) != 1:
            raise AuditIntegrityError("Fork bound child must retain exactly one strict blob plan audit row")
        if not public_messages:
            raise AuditIntegrityError("Fork bound child has no public messages")
        edited_message = public_messages[-1]
        if (
            edited_message.role != "user"
            or edited_message.writer_principal != "session_fork"
            or edited_message.content != new_message_content
        ):
            raise AuditIntegrityError("Fork bound child edited message validation failed")

        state = self._row_to_state_record(state_row) if state_row is not None else None
        if edited_message.composition_state_id != (state.id if state is not None else None):
            raise AuditIntegrityError("Fork bound child edited message does not reference its staged current state")
        return StagedForkSession(
            session=self._row_to_session_record(child_row),
            messages=tuple(public_messages),
            state=state,
            blob_plan=plan_candidates[0],
            authority=authority,
        )

    async def fork_session(
        self,
        authority: SessionForkParentAuthority,
        *,
        fork_message_id: UUID,
        new_message_content: str,
    ) -> StagedForkSession:
        """Stage or resume the exact child bound to a fenced fork operation.

        Initial staging atomically creates and binds one archived child with a
        frozen blob plan. Repeated calls under the current fence, including an
        expired-lease takeover, reload that same hidden cohort. Blob realization
        and final activation happen later at the route/coordinator settlement
        boundary; a failed operation retains the archived child and plan as
        integrity evidence while compensating only authorized partial blobs.

        The staged child contains:
        1. Composition state copied from the fork message's pre-send state
        2. All messages BEFORE the fork message (with NULL state provenance)
        3. A synthetic system message noting the fork
        4. The new edited user message (provenance = copied state, not source)

        Returns a ``StagedForkSession`` with the child, public messages, copied
        state when present, and the immutable plan used by later blob custody.
        """
        from elspeth.web.sessions.protocol import InvalidForkTargetError

        # Load source data (read-only, outside the write transaction)
        if type(authority) is not SessionForkParentAuthority:
            raise TypeError("fork_session authority must be an exact SessionForkParentAuthority")
        fence = authority.receipt_fence
        source_session_id = fence.session_id
        source_session = await self.get_session(source_session_id)
        source_messages = await self.get_messages(source_session_id, limit=None)

        # Find the fork message — must be a user message
        fork_msg = None
        fork_idx = -1
        for i, msg in enumerate(source_messages):
            if msg.id == fork_message_id:
                fork_msg = msg
                fork_idx = i
                break

        if fork_msg is None:
            raise ValueError(f"Message {fork_message_id} not found in session {source_session_id}")
        if fork_msg.role != "user":
            raise InvalidForkTargetError(str(fork_message_id), fork_msg.role)

        messages_to_copy = source_messages[:fork_idx]
        pre_send_state_id = fork_msg.composition_state_id

        # Load source composition state if it exists (read-only)
        source_state_record: CompositionStateRecord | None = None
        if pre_send_state_id is not None:
            source_state_record = await self.get_state_in_session(
                pre_send_state_id,
                source_session_id,
            )

        # Prepare IDs and timestamps upfront
        now = self._now()
        # Prepare state copy if needed
        copied_state_id = uuid.uuid4() if source_state_record is not None else None
        copied_state_id_str = str(copied_state_id) if copied_state_id else None

        # Allocate every copied chat id before preparing inserts.
        # Tool-parent links use this one exact map.
        source_to_copied_message_id = {str(msg.id): str(uuid.uuid4()) for msg in messages_to_copy}
        source_assistant_ids = {str(msg.id) for msg in messages_to_copy if msg.role == "assistant"}

        # Prepare all message rows upfront — preserve original created_at
        # so get_messages() ordering is deterministic.  Stamping all rows
        # with `now` would make them indistinguishable by timestamp and
        # produce non-deterministic ordering on subsequent reads.
        child_messages: list[SessionForkChildMessageCreation] = []
        for msg in messages_to_copy:
            copied_msg_id = source_to_copied_message_id[str(msg.id)]

            copied_parent_assistant_id: str | None = None
            if msg.role == "tool":
                # CHECK constraint biconditional: tool rows must carry both
                # ``tool_call_id`` and ``parent_assistant_id``. The source row
                # already had them (the biconditional applies there too);
                # an absent value here means the source row predates the
                # cutover — that's a Tier-1 audit anomaly, crash with a named
                # error rather than letting the FK fire generically.
                if msg.parent_assistant_id is None:
                    raise RuntimeError(f"fork_session: tool message id={msg.id} has no parent assistant")
                parent_key = str(msg.parent_assistant_id)
                if parent_key not in source_to_copied_message_id or parent_key not in source_assistant_ids:
                    # Slice ``[:fork_idx]`` excluded the assistant message
                    # this tool row depends on. Detect it pre-batch with a
                    # named error per the offensive-programming policy.
                    raise RuntimeError(f"fork slice excludes parent assistant of tool message id={msg.id}")
                copied_parent_assistant_id = source_to_copied_message_id[parent_key]

            child_messages.append(
                SessionForkChildMessageCreation(
                    id=UUID(copied_msg_id),
                    role=msg.role,
                    content=msg.content,
                    raw_content=msg.raw_content,
                    tool_calls=msg.tool_calls,
                    tool_call_id=msg.tool_call_id,
                    parent_assistant_id=(UUID(copied_parent_assistant_id) if copied_parent_assistant_id is not None else None),
                    # Preserve the source row's stored writer; deriving from
                    # role would fabricate provenance for any source row whose
                    # writer differs from the role-keyed default.
                    writer_principal=msg.writer_principal,
                    created_at=msg.created_at,
                    composition_state_id=None,
                )
            )
        # System message — no raw_content (synthetic, not from the LLM).
        # writer_principal="session_fork" because this row is unambiguously
        # authored by the fork operation, not by any route handler.
        system_msg_id = str(uuid.uuid4())
        child_messages.append(
            SessionForkChildMessageCreation(
                id=UUID(system_msg_id),
                role="system",
                content="Conversation forked from an earlier point.",
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                parent_assistant_id=None,
                writer_principal="session_fork",
                created_at=now,
                composition_state_id=None,
            )
        )
        # New edited user message — provenance points to COPIED state, not source.
        # created_at = now is correct here: ordering is enforced by sequence_no
        # (allocated under the new-session write lock inside _sync), not by
        # created_at. The microsecond offset that earlier guarded against
        # SQLite same-microsecond ordering ambiguity is no longer needed.
        # raw_content is None: this is a new user-authored message, not an LLM turn.
        new_user_msg_id = str(uuid.uuid4())
        child_messages.append(
            SessionForkChildMessageCreation(
                id=UUID(new_user_msg_id),
                role="user",
                content=new_message_content,
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                parent_assistant_id=None,
                writer_principal="session_fork",
                created_at=now,
                composition_state_id=(UUID(copied_state_id_str) if copied_state_id_str is not None else None),
            )
        )

        def _sync(
            transaction: SessionForkCreationTransaction,
            fork_authority: SessionForkAuthority,
        ) -> StagedForkSession:
            """Create+bind or reload exactly one child under the parent fence."""
            new_session_id = UUID(fork_authority.child_context.fence.session_id)
            operation, _database_now = transaction.require_parent_fork_receipt(fence)
            if operation["kind"] != "session_fork":
                raise AuditIntegrityError("fork_session fence is not bound to session_fork")
            bound_child_id = operation["result_session_id"]
            if bound_child_id is not None:
                if operation["originating_message_id"] != str(fork_message_id):
                    raise AuditIntegrityError("Fork bound child has a different fork message binding")
                return self._load_staged_fork_from_transaction(
                    transaction,
                    parent_session_id=source_session_id,
                    child_session_id=UUID(bound_child_id),
                    operation_id=fence.operation_id,
                    fork_message_id=fork_message_id,
                    new_message_content=new_message_content,
                    authority=fork_authority,
                )

            parent_row = transaction.read_parent_session()
            fork_row = transaction.read_parent_message(fork_message_id)
            if parent_row is None or parent_row.archived_at is not None:
                raise AuditIntegrityError("Fork parent failed active custody validation")
            if (
                fork_row is None
                or fork_row.role != "user"
                or fork_row.composition_state_id != (str(source_state_record.id) if source_state_record is not None else None)
            ):
                raise AuditIntegrityError("Fork message changed before staging")

            locked_source_state: CompositionStateRecord | None = None
            forked_composer_meta: dict[str, Any] | None = None
            if source_state_record is not None:
                locked_source_row = transaction.read_parent_state(source_state_record.id)
                if locked_source_row is None:
                    raise AuditIntegrityError("Fork source checkpoint is missing or cross-session")
                locked_source_state = self._row_to_state_record(locked_source_row)
                forked_composer_meta = deep_thaw(locked_source_state.composer_meta)
                # Custody no rewriter can rebase is refused HERE, inside the
                # staging transaction and before any child row is written, so
                # the failure the route's backstop can only NAME never leaves
                # an archived child behind. The needle set is every parent blob
                # row at any status: an unplanned (non-``ready``) blob has no
                # child copy to rebase onto, so the plan's own reader cannot
                # see the references that matter here.
                parent_blob_rows = transaction.read_parent_blob_custody()
                _refuse_unrewritable_fork_custody(
                    composer_meta=forked_composer_meta,
                    validation_errors=locked_source_state.validation_errors,
                    metadata=locked_source_state.metadata_,
                    forbidden=frozenset(item for row in parent_blob_rows for item in (row.id, row.storage_path)),
                )

            plan = tuple(
                BlobForkPlanEntry(
                    source_blob_id=UUID(row.id),
                    target_blob_id=fork_blob_id(
                        target_session_id=new_session_id,
                        source_blob_id=UUID(row.id),
                    ),
                    content_hash=row.content_hash,
                    size_bytes=row.size_bytes,
                )
                for row in transaction.read_parent_ready_blobs()
            )
            plan_message = SessionForkChildMessageCreation(
                id=uuid.uuid4(),
                role="audit",
                content=_fork_blob_plan_content(
                    source_session_id=source_session_id,
                    child_session_id=new_session_id,
                    operation_id=fence.operation_id,
                    entries=plan,
                ),
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                parent_assistant_id=None,
                writer_principal="session_fork",
                created_at=now,
                composition_state_id=None,
            )

            if locked_source_state is not None and copied_state_id is not None:
                transaction.child_mutations.insert_child_state(
                    SessionForkChildStateCreation(
                        id=copied_state_id,
                        data=CompositionStateData(
                            sources=locked_source_state.sources,
                            nodes=locked_source_state.nodes,
                            edges=locked_source_state.edges,
                            outputs=locked_source_state.outputs,
                            metadata_=locked_source_state.metadata_,
                            is_valid=locked_source_state.is_valid,
                            validation_errors=locked_source_state.validation_errors,
                            composer_meta=forked_composer_meta,
                        ),
                        created_at=now,
                    )
                )
            transaction.child_mutations.append_child_messages((*child_messages, plan_message))
            transaction.parent_receipt_mutations.bind_fork_receipt(
                originating_message_id=fork_message_id,
            )
            return self._load_staged_fork_from_transaction(
                transaction,
                parent_session_id=source_session_id,
                child_session_id=new_session_id,
                operation_id=fence.operation_id,
                fork_message_id=fork_message_id,
                new_message_content=new_message_content,
                authority=fork_authority,
            )

        return cast(
            "StagedForkSession",
            await self._run_sync(
                self._session_operation_authority.mutate_fork_creation,
                authority,
                SessionForkChildCreation(
                    user_id=source_session.user_id,
                    auth_provider_type=source_session.auth_provider_type,
                    title=f"{source_session.title} (fork)",
                    created_at=now,
                    archived_at=now,
                    forked_from_message_id=fork_message_id,
                ),
                mutation=_sync,
            ),
        )

    async def settle_fork_operation_receipt(
        self,
        command: SessionForkSettlementCommand,
    ) -> SessionRecord:
        """Atomically rewrite, activate, and complete one staged fork."""
        if type(command) is not SessionForkSettlementCommand:
            raise TypeError("settle_fork_operation_receipt command must be exact")
        parent_session_id_str = str(command.fence.session_id)
        child_session_id_str = str(command.child_session_id)

        def _sync() -> SessionRecord:
            with self._session_pair_locked_begin(parent_session_id_str, child_session_id_str) as conn:
                operation, now = self._require_session_fork_authority_on_connection(
                    conn,
                    command.authority,
                )
                if operation["kind"] != "session_fork" or operation["result_session_id"] != child_session_id_str:
                    raise AuditIntegrityError("Fork settlement child is not bound to the exact operation fence")
                parent = conn.execute(
                    select(
                        sessions_table.c.user_id,
                        sessions_table.c.auth_provider_type,
                    ).where(sessions_table.c.id == parent_session_id_str)
                ).one_or_none()
                if parent is None:
                    raise AuditIntegrityError("Fork settlement parent is missing")

                child = conn.execute(
                    select(sessions_table).where(sessions_table.c.id == child_session_id_str).with_for_update()
                ).one_or_none()
                if (
                    child is None
                    or child.archived_at is None
                    or child.forked_from_session_id != parent_session_id_str
                    or child.forked_from_message_id != operation["originating_message_id"]
                    or child.user_id != parent.user_id
                    or child.auth_provider_type != parent.auth_provider_type
                ):
                    raise AuditIntegrityError("Fork settlement child failed staged custody validation")

                current_state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == child_session_id_str)
                    .order_by(composition_states_table.c.version.desc())
                    .limit(1)
                    .with_for_update()
                ).one_or_none()
                current_state_id = UUID(current_state_row.id) if current_state_row is not None else None
                if current_state_id != command.expected_current_state_id:
                    raise AuditIntegrityError("Fork settlement staged current state changed")
                edited_message = conn.execute(
                    select(chat_messages_table)
                    .where(
                        chat_messages_table.c.id == str(command.edited_message_id),
                        chat_messages_table.c.session_id == child_session_id_str,
                    )
                    .with_for_update()
                ).one_or_none()
                if (
                    edited_message is None
                    or edited_message.role != "user"
                    or edited_message.writer_principal != "session_fork"
                    or edited_message.composition_state_id
                    != (str(command.expected_current_state_id) if command.expected_current_state_id is not None else None)
                ):
                    raise AuditIntegrityError("Fork settlement edited message failed staged custody validation")

                if command.rewritten_state is not None:
                    settlement_state: Mapping[str, Any] = {
                        "sources": deep_thaw(command.rewritten_state.sources),
                        "nodes": deep_thaw(command.rewritten_state.nodes),
                        "edges": deep_thaw(command.rewritten_state.edges),
                        "outputs": deep_thaw(command.rewritten_state.outputs),
                        "metadata": deep_thaw(command.rewritten_state.metadata_),
                        "validation_errors": serialize_composition_validation_errors(command.rewritten_state.validation_errors),
                        "composer_meta": deep_thaw(command.rewritten_state.composer_meta),
                    }
                elif current_state_row is not None:
                    settlement_state = {
                        "source": current_state_row.source,
                        "sources": current_state_row.sources,
                        "nodes": current_state_row.nodes,
                        "edges": current_state_row.edges,
                        "outputs": current_state_row.outputs,
                        "metadata": current_state_row.metadata_,
                        "validation_errors": current_state_row.validation_errors,
                        "composer_meta": current_state_row.composer_meta,
                    }
                else:
                    settlement_state = {}
                _verify_fork_settlement_blob_custody(
                    conn,
                    parent_session_id=command.fence.session_id,
                    child_session_id=command.child_session_id,
                    operation_id=command.fence.operation_id,
                    state_payload=settlement_state,
                )

                # The receipt terminalizes first in SQL statement order.
                # All child rewrites and activation remain in this transaction,
                # so a later failure rolls the terminal row back atomically.
                settle_operation_receipt(
                    conn,
                    command.fence,
                    now=now,
                    actor=command.actor,
                    result=SessionForkReceiptResult(session_id=command.child_session_id),
                    response_hash=command.response_hash,
                )

                if command.rewritten_state is not None and command.rewritten_state_id is not None:
                    # Detach the edited message from the staged state and delete
                    # that staged state BEFORE inserting the rewritten replacement.
                    # ``_insert_composition_state`` allocates ``MAX(version)+1``
                    # per session, so the replacement's version depends on whether
                    # the staged row (version 1) is still present when it runs.
                    # Removing the staged row first lets the replacement reclaim
                    # version 1 — a fresh forked-and-edited child settles at its
                    # first version rather than leaking a version-2 gap whose only
                    # meaning is "settlement happened to rewrite blob custody"
                    # (a non-rewriting settlement already lands the child at
                    # version 1). The composite FK on
                    # ``chat_messages(composition_state_id, session_id)`` is
                    # RESTRICT, so the message must be detached to NULL before the
                    # staged state can be removed; the insert then repoints it.
                    # All four writes share this transaction under the
                    # parent+child pair lock, so the transient NULL provenance is
                    # never externally observable.
                    detached = conn.execute(
                        update(chat_messages_table)
                        .where(
                            chat_messages_table.c.id == str(command.edited_message_id),
                            chat_messages_table.c.session_id == child_session_id_str,
                            chat_messages_table.c.composition_state_id == str(command.expected_current_state_id),
                        )
                        .values(composition_state_id=None)
                    )
                    if detached.rowcount != 1:
                        raise AuditIntegrityError("Fork settlement lost edited-message compare-and-swap")
                    removed_staged_state = conn.execute(
                        delete(composition_states_table).where(
                            composition_states_table.c.id == str(command.expected_current_state_id),
                            composition_states_table.c.session_id == child_session_id_str,
                        )
                    )
                    if removed_staged_state.rowcount != 1:
                        raise AuditIntegrityError("Fork settlement could not remove superseded staged state")
                    self._insert_composition_state(
                        conn,
                        session_id=child_session_id_str,
                        payload=StatePayload(
                            data=command.rewritten_state,
                            derived_from_state_id=None,
                        ),
                        provenance="session_fork",
                        created_at=now,
                        state_id=str(command.rewritten_state_id),
                        session_operation_context=command.authority.child_context,
                    )
                    repointed = conn.execute(
                        update(chat_messages_table)
                        .where(
                            chat_messages_table.c.id == str(command.edited_message_id),
                            chat_messages_table.c.session_id == child_session_id_str,
                            chat_messages_table.c.composition_state_id.is_(None),
                        )
                        .values(composition_state_id=str(command.rewritten_state_id))
                    )
                    if repointed.rowcount != 1:
                        raise AuditIntegrityError("Fork settlement could not bind replacement state")

                activated = conn.execute(
                    update(sessions_table)
                    .where(
                        sessions_table.c.id == child_session_id_str,
                        sessions_table.c.archived_at.is_not(None),
                    )
                    .values(archived_at=None, updated_at=now)
                )
                if activated.rowcount != 1:
                    raise AuditIntegrityError("Fork settlement lost archived-to-active compare-and-swap")

                settled_row = conn.execute(select(sessions_table).where(sessions_table.c.id == child_session_id_str)).one()
                return self._row_to_session_record(settled_row)

        return cast("SessionRecord", await self._run_sync(_sync))

    def _row_to_run_record(self, row: Any) -> RunRecord:
        """Convert a SQLAlchemy row to a RunRecord."""
        rows_succeeded, rows_failed = _normalize_pre_adr019_session_counters(
            status=row.status,
            rows_processed=row.rows_processed,
            rows_succeeded=row.rows_succeeded,
            rows_failed=row.rows_failed,
            rows_routed_success=row.rows_routed_success,
            rows_routed_failure=row.rows_routed_failure,
            rows_quarantined=row.rows_quarantined,
        )
        return RunRecord(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            state_id=UUID(row.state_id),
            status=row.status,
            started_at=restore_utc(row.started_at),
            finished_at=restore_utc(row.finished_at) if row.finished_at is not None else None,
            rows_processed=row.rows_processed,
            rows_succeeded=rows_succeeded,
            rows_failed=rows_failed,
            rows_routed_success=row.rows_routed_success,
            rows_routed_failure=row.rows_routed_failure,
            rows_quarantined=row.rows_quarantined,
            error=row.error,
            landscape_run_id=row.landscape_run_id,
            pipeline_yaml=row.pipeline_yaml,
            cancel_requested_at=restore_utc(row.cancel_requested_at) if row.cancel_requested_at is not None else None,
            cancellation_source=CancellationSource(row.cancellation_source) if row.cancellation_source is not None else None,
            saga_state=RunSagaState(row.saga_state),
            recovery_required_reason=RecoveryRequiredReason(row.recovery_required_reason)
            if row.recovery_required_reason is not None
            else None,
        )

    def _row_to_run_event_record(self, row: Any) -> RunEventRecord:
        """Convert a SQLAlchemy row to a RunEventRecord."""
        if row.event_type not in SESSION_RUN_EVENT_TYPE_VALUES:
            raise AuditIntegrityError(
                f"Tier 1: run_events.event_type is {row.event_type!r}, expected one of {sorted(SESSION_RUN_EVENT_TYPE_VALUES)}"
            )
        if type(row.data) is not dict:
            raise AuditIntegrityError(f"Tier 1: run_events.data for event {row.id} is not a JSON object")
        return RunEventRecord(
            id=UUID(row.id),
            run_id=UUID(row.run_id),
            sequence=int(row.sequence),
            timestamp=restore_utc(row.timestamp),
            event_type=cast(SessionRunEventType, row.event_type),
            data=cast(Mapping[str, Any], row.data),
        )

    async def _session_principal_context(
        self, session_id: str, *, preparation_work: RequiredWorkBinding | None = None
    ) -> tuple[str | None, PluginAvailabilitySnapshot | None]:
        """Read the session principal and build its snapshot before a write transaction."""

        def _sync() -> tuple[str | None, PluginAvailabilitySnapshot | None]:
            with self._engine.connect() as conn:
                user_id = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id)).scalar_one_or_none()
            if user_id is None or self._plugin_snapshot_factory is None:
                return user_id, None
            return user_id, self._plugin_snapshot_factory(user_id)

        if preparation_work is not None:
            if preparation_work.coordinator.authority.context.fence.session_id != session_id:
                raise AuditIntegrityError("preparation read binding has a foreign session")
            return await self._run_required_preparation_read(preparation_work, _sync, project=_validate_principal_snapshot)
        return cast(
            "tuple[str | None, PluginAvailabilitySnapshot | None]",
            await self._run_sync(_sync),
        )

    async def _run_required_preparation_read[T](
        self,
        binding: RequiredWorkBinding,
        read: Callable[[], T],
        *,
        project: Callable[[T], None],
    ) -> T:
        """Join the actual preparation read, project its value, then retain cancellation."""
        sql, projection = binding.reserve_pair(RequiredWorkSource.PREPARATION_READ_SQL, RequiredWorkSource.PREPARATION_READ_PROJECTION)
        outcome = await run_required_sql_finish_once(sql, read)
        errors: list[BaseException] = []
        if isinstance(outcome, RequiredSQLRaised):
            projection.complete_without_submission()
            errors.append(outcome.error)
        else:
            projection.begin_projection()
            try:
                project(outcome.value)
            except BaseException as error:
                projection.complete_owned(error)
                errors.append(error)
            else:
                projection.complete_owned()
        errors.extend(outcome.deferred_cancellations)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Required preparation original outcomes", errors)
        if isinstance(outcome, RequiredSQLRaised):
            raise AuditIntegrityError("required preparation failure lost its original error")
        return outcome.value

    async def _run_sync_with_post_commit_projection[T](
        self,
        func: Callable[[], T],
        *,
        project: Callable[[T], None],
        composer_custody: bool = False,
        required_work: RequiredWorkTicket | None = None,
    ) -> T:
        """Drain one worker through cancellation, then project iff it committed."""

        worker = asyncio.create_task(
            run_required_sql_in_worker(required_work, func)
            if required_work is not None
            else self._run_composer_sql(func)
            if composer_custody
            else self._run_sync(func)
        )
        cancellation: asyncio.CancelledError | None = None

        def record_cancelled_failure(primary: asyncio.CancelledError, failure: BaseException, *, phase: str) -> None:
            # Request cancellation can hide a chained worker or projection
            # error from ASGI's exception reporting. Emit only its class and
            # the fixed phase; private payloads and exception text stay out.
            try:
                self._log.error(
                    "session_post_commit_secondary_failure",
                    phase=phase,
                    error_type=type(failure).__name__,
                )
            except contract_errors.TIER_1_ERRORS:
                raise
            except Exception as logging_error:
                primary.add_note(f"Session post-commit secondary-failure logging also failed with {type(logging_error).__name__}.")

        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except BaseException:
                break
        try:
            result = cast("T", worker.result())
        except BaseException as failure:
            if cancellation is not None:
                record_cancelled_failure(cancellation, failure, phase="worker")
                raise cancellation from failure
            raise
        try:
            project(result)
        except BaseException as projection_failure:
            # The captured cancellation must be re-raised on EVERY exit path
            # once observed — a projection bug must not silently discard it
            # (the task would complete "normally" after being cancelled).
            if cancellation is not None:
                record_cancelled_failure(cancellation, projection_failure, phase="projection")
                raise cancellation from projection_failure
            raise
        if cancellation is not None:
            raise cancellation
        return result

    async def _run_composer_sql[T](self, func: Callable[[], T]) -> T:
        """Keep actual executor-future custody through direct task cancellation."""
        return await run_stream_read_in_worker(func)

    async def _run_composer_terminal_sql(
        self,
        running: ComposerOperationRunning,
        func: Callable[[], ComposerOperationRecord],
        *,
        can_retry: Callable[[ComposerOperationRecord, datetime], bool] | None = None,
        required_work: RequiredWorkCoordinator | None = None,
        terminal_source: RequiredWorkSource = RequiredWorkSource.OPERATION_TERMINAL_SQL,
    ) -> ComposerOperationRecord:
        """Retain SQL custody; replay terminal or retry one immutable bundle.

        Every independent writer read follows actual worker completion. A
        failed/unknown read cannot establish rollback. Changed cancel/deadline
        state leaves exact retry; the worker may choose one separate failure
        through the sealed authority with the original exception evidence.
        """
        authority = ComposerAsyncOperationAuthority(
            self._engine, owner_instance_id=self.session_operation_owner_instance_id, claim_lease_seconds=30
        )
        if required_work is not None:
            if type(required_work) is not RequiredWorkCoordinator:
                raise TypeError("terminal SQL requires an exact RequiredWorkCoordinator")
            scope = required_work.authority
            if (
                scope.context != running.session_operation_context
                or scope.durable_operation_id != running.claim.operation_id
                or scope.claim_attempt != running.claim.attempt
                or scope.proposal_id is not None
            ):
                raise AuditIntegrityError("terminal SQL coordinator authority mismatch")
        first_failure: BaseException | None = None
        for attempt in range(2):
            sql_started = threading.Event()
            sql_completed = threading.Event()
            sql_outcome: list[ComposerOperationRecord | BaseException] = []

            def witnessed_sql(
                started: threading.Event = sql_started,
                completed: threading.Event = sql_completed,
                outcomes: list[ComposerOperationRecord | BaseException] = sql_outcome,
            ) -> ComposerOperationRecord:
                started.set()
                try:
                    result = func()
                except BaseException as exc:
                    outcomes.append(exc)
                    raise
                else:
                    outcomes.append(result)
                    return result
                finally:
                    completed.set()

            ticket = required_work.reserve(terminal_source, sql_attempt_ordinal=attempt) if required_work is not None else None
            child = asyncio.create_task(
                run_required_sql_in_worker(ticket, witnessed_sql) if ticket is not None else self._run_composer_sql(witnessed_sql)
            )
            cancelled: asyncio.CancelledError | None = None
            while not child.done():
                try:
                    await asyncio.shield(child)
                except asyncio.CancelledError as exc:
                    cancelled = exc
                except BaseException:
                    break
            if child.cancelled():
                if not sql_started.is_set():
                    raise ComposerTerminalSQLCompletionUnknown("cancelled SQL custody has no invocation completion witness")
                while not sql_completed.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
            try:
                if child.cancelled():
                    outcome = sql_outcome[0]
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome
                return child.result()
            except BaseException as original_failure:
                if first_failure is None:
                    first_failure = original_failure
                # New pooled connection, committed writer database state.
                read_ticket = (
                    required_work.reserve(RequiredWorkSource.TERMINAL_WRITER_READ_SQL, recurrence_ordinal=attempt)
                    if required_work is not None
                    else None
                )
                read_child = asyncio.create_task(
                    run_required_sql_in_worker(
                        read_ticket,
                        authority.get_with_database_now,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                    )
                    if read_ticket is not None
                    else run_stream_read_in_worker(
                        authority.get_with_database_now, session_id=running.claim.session_id, operation_id=running.claim.operation_id
                    )
                )
                while not read_child.done():
                    try:
                        await asyncio.shield(read_child)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
                    except BaseException:
                        break
                try:
                    current, now = read_child.result()
                except BaseException as read_failure:
                    raise ComposerTerminalSQLCompletionUnknown("terminal writer read cannot prove committed outcome") from read_failure
                if current is not None and current.status in ("completed", "failed"):
                    return current
                if (
                    attempt == 0
                    and cancelled is None
                    and current is not None
                    and current.status == "running"
                    and can_retry is not None
                    and can_retry(current, now)
                ):
                    continue
                if cancelled is not None:
                    raise cancelled from original_failure
                if attempt > 0 and (
                    isinstance(original_failure, _ComposerTerminalRetryStateChanged)
                    or (current is not None and (current.cancel_requested_at is not None or current.deadline_at <= now))
                ):
                    # The original immutable bundle remains the failure
                    # evidence; ordinary Stop/deadline changes cannot invent
                    # an integrity fault or erase an original accounting fault.
                    raise first_failure from original_failure
                if attempt > 0 and original_failure is not first_failure:
                    raise BaseExceptionGroup("terminal SQL attempts failed", [first_failure, original_failure]) from None
                raise
        raise AuditIntegrityError("terminal retry exceeded its closed bound")

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
    ) -> ComposerOperationRecord:
        """Publish final assistant/audit/snapshot/result in one exact COMPOSE transaction."""
        if (assistant is None) == (assistant_record is None):
            raise ValueError("terminal requires exactly one new or persisted assistant")
        frozen_created_at: datetime | None = None
        frozen_audit_ids = tuple(str(uuid.uuid4()) for _draft in audit_cohort)
        sid = str(running.claim.session_id)
        context = running.session_operation_context
        attempted_response: MessageWithStateResponse | None = None
        attempted_message: ChatMessageRecord | None = None
        attempted_audit_rows: tuple[ChatMessageRecord, ...] | None = None
        attempted_proposals: tuple[CompositionProposalRecord, ...] | None = None
        attempted_state: CompositionStateRecord | None = None
        attempted_deadline_expired: bool | None = None

        def _can_retry(current: ComposerOperationRecord, now: datetime) -> bool:
            return (
                attempted_response is not None
                and current.cancel_requested_at is None
                and attempted_deadline_expired is False
                and current.deadline_at > now
                and current.session_operation_id == context.fence.operation_id
                and current.session_operation_epoch == context.fence.operation_epoch
            )

        def _sync() -> ComposerOperationRecord:
            nonlocal attempted_response, attempted_deadline_expired, frozen_created_at
            nonlocal attempted_message, attempted_audit_rows, attempted_proposals, attempted_state
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn, session_id=sid, session_operation_context=context, expected_kind=SessionOperationKind.COMPOSE
                ),
            ):
                now = database_now(conn)
                if frozen_created_at is None:
                    frozen_created_at = now
                # The bound row itself is selected on this transaction below;
                # no cross-connection response/state read is used.
                job_row = _read(conn, running.claim.session_id, running.claim.operation_id)
                if job_row is None:
                    raise AuditIntegrityError("terminal operation missing")
                current_job = _record_from_row(job_row)
                if attempted_response is not None and not _can_retry(current_job, now):
                    raise _ComposerTerminalRetryStateChanged("immutable terminal retry driving state changed")
                attempted_deadline_expired = current_job.deadline_at <= now
                if assistant is not None:
                    csid = str(assistant.composition_state_id) if assistant.composition_state_id is not None else None
                    if csid is not None:
                        _assert_state_in_session(conn, state_id=csid, expected_session_id=sid, caller="complete_composer_async_operation")
                    message_id = self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="assistant",
                        content=assistant.content,
                        raw_content=assistant.raw_content,
                        tool_calls=None,
                        sequence_no=self._reserve_sequence_range(conn, sid, count=1),
                        writer_principal="compose_loop",
                        composition_state_id=csid,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=frozen_created_at,
                        session_operation_context=context,
                        message_id=str(assistant.message_id),
                    )
                    stored_row = conn.execute(
                        select(chat_messages_table).where(chat_messages_table.c.id == message_id, chat_messages_table.c.session_id == sid)
                    ).one()
                    message = self._row_to_chat_message_record(stored_row)
                else:
                    if assistant_record is None:
                        raise AuditIntegrityError("terminal assistant absent")
                    row = conn.execute(
                        select(chat_messages_table).where(
                            chat_messages_table.c.id == str(assistant_record.id), chat_messages_table.c.session_id == sid
                        )
                    ).one_or_none()
                    if row is None:
                        raise AuditIntegrityError("terminal reused assistant missing")
                    message = self._row_to_chat_message_record(row)
                    if message != assistant_record or message.role != "assistant":
                        raise AuditIntegrityError("terminal reused assistant differs from durable record")
                if attempted_message is not None and message != attempted_message:
                    raise _ComposerTerminalRetryStateChanged("immutable terminal assistant bundle changed")
                attempted_message = message
                try:
                    active_drafts = self._write_audit_cohort_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        drafts=audit_cohort,
                        writer_principal="compose_loop",
                        composition_state_id=audit_composition_state_id,
                        session_operation_context=context,
                        audit_only=False,
                        message_ids=frozen_audit_ids,
                        created_at=frozen_created_at,
                        recorded_at=frozen_created_at,
                    )
                except SQLAlchemyError as exc:
                    raise ComposerRequiredAuditPersistenceError(
                        f"composer_turn_audit_cohort_persist_failed: required atomic audit insert failed for session_id={sid!r}",
                        helper="turn_audit_cohort",
                    ) from exc
                audit_rows = tuple(
                    self._row_to_chat_message_record(row)
                    for row in conn.execute(
                        select(chat_messages_table)
                        .where(chat_messages_table.c.session_id == sid, chat_messages_table.c.id.in_(frozen_audit_ids))
                        .order_by(chat_messages_table.c.sequence_no)
                    )
                )
                if attempted_audit_rows is not None and audit_rows != attempted_audit_rows:
                    raise _ComposerTerminalRetryStateChanged("immutable terminal audit bundle changed")
                attempted_audit_rows = audit_rows
                proposals = self._list_composition_proposals_on_connection(conn, session_id=running.claim.session_id)
                state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(composition_states_table.c.version.desc())
                    .limit(1)
                ).one_or_none()
                state = self._row_to_state_record(state_row) if state_row is not None else None
                if attempted_response is not None and (proposals != attempted_proposals or state != attempted_state):
                    raise _ComposerTerminalRetryStateChanged("immutable terminal response snapshot changed")
                attempted_proposals, attempted_state = proposals, state
                response = attempted_response if attempted_response is not None else build_response(message, proposals, state)
                response = MessageWithStateResponse.model_validate(response.model_dump(mode="python"), strict=True)
                attempted_response = response
                result = settle_composer_operation_on_connection(conn, running, outcome=response, settled_at=frozen_created_at)
            # Telemetry is after commit and cannot change the stored terminal.
            for draft in active_drafts:
                record_settled_composer_audit_message(role=draft.role, writer_principal="compose_loop", tool_calls=draft.tool_calls)
            return result

        return await self._run_composer_terminal_sql(running, _sync, can_retry=_can_retry, required_work=required_work)

    async def fail_composer_async_operation(
        self,
        running: ComposerOperationRunning,
        *,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
        required_work: RequiredWorkCoordinator | None = None,
        failure_projection_work: RequiredWorkTicket | None = None,
    ) -> ComposerOperationRecord:
        """One sealed failure bundle with at most one exact immutable retry."""
        if required_work is None:
            if failure_projection_work is not None:
                raise AuditIntegrityError("Failure projection requires its exact coordinator")
        else:
            if type(required_work) is not RequiredWorkCoordinator or type(failure_projection_work) is not RequiredWorkTicket:
                raise AuditIntegrityError("Required failure publication needs its exact projection handoff")
            scope = required_work.authority
            if (
                scope.context != running.session_operation_context
                or scope.durable_operation_id != running.claim.operation_id
                or scope.claim_attempt != running.claim.attempt
                or scope.proposal_id is not None
            ):
                raise AuditIntegrityError("Failure projection coordinator authority mismatch")
            required_work.validate_terminal_failure_work(projection_ticket=failure_projection_work)
        sid = str(running.claim.session_id)
        attempted_failure: ComposerOperationError | None = None
        attempted_cancel: datetime | None = None
        attempted_deadline_expired: bool | None = None
        frozen_settled_at: datetime | None = None

        def _can_retry(current: ComposerOperationRecord, now: datetime) -> bool:
            return (
                attempted_failure is not None
                and current.cancel_requested_at == attempted_cancel
                and (current.deadline_at <= now) == attempted_deadline_expired
                and current.session_operation_id == running.session_operation_context.fence.operation_id
                and current.session_operation_epoch == running.session_operation_context.fence.operation_epoch
            )

        def _sync() -> ComposerOperationRecord:
            nonlocal attempted_failure, attempted_cancel, attempted_deadline_expired, frozen_settled_at
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                now = database_now(conn)
                self._require_session_operation_context_on_connection(
                    conn,
                    running.session_operation_context,
                    session_id=sid,
                    expected_kind=SessionOperationKind.COMPOSE,
                    now=now,
                    audit_only=True,
                )
                row = _read(conn, running.claim.session_id, running.claim.operation_id)
                if row is None:
                    raise AuditIntegrityError("failed terminal operation missing")
                current = _record_from_row(row)
                if attempted_failure is not None and not _can_retry(current, now):
                    raise _ComposerTerminalRetryStateChanged("immutable failed terminal driving state changed")
                selected = _select_failure_on_connection(row, failure, now=now, authoritative_failure=authoritative_failure)
                if attempted_failure is not None and selected != attempted_failure:
                    raise _ComposerTerminalRetryStateChanged("immutable failed terminal bundle changed")
                attempted_failure = selected
                attempted_cancel = current.cancel_requested_at
                attempted_deadline_expired = current.deadline_at <= now
                if frozen_settled_at is None:
                    frozen_settled_at = now
                return settle_composer_operation_on_connection(
                    conn, running, outcome=attempted_failure, authoritative_failure=True, settled_at=frozen_settled_at
                )

        try:
            return await self._run_composer_terminal_sql(
                running,
                _sync,
                can_retry=_can_retry,
                required_work=required_work,
                terminal_source=RequiredWorkSource.TERMINAL_FAILURE_SQL,
            )
        except ComposerTerminalSQLCompletionUnknown:
            raise
        except (SessionOperationFenceLost, ComposerOperationFenceLost) as exc:
            if attempted_failure is None:
                # Exact-fence preflight failed before selecting or attempting
                # a terminal write; the owner-lapsed recovery authority may
                # now handle this proved refusal after actual SQL completion.
                raise
            raise ComposerTerminalSQLCompletionUnknown("failed terminal authority changed after its write attempt") from exc
        except BaseException as exc:
            # A separately selected failed-terminal bundle owns one bounded
            # exact retry; it cannot spawn further changed-state settlements.
            raise ComposerTerminalSQLCompletionUnknown("failed terminal attempts finished without a committed terminal") from exc

    def _write_audit_cohort_on_connection(
        self,
        conn: Connection,
        *,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        writer_principal: ChatMessageWriterPrincipal,
        composition_state_id: UUID | None,
        session_operation_context: SessionOperationContext,
        audit_only: bool,
        message_ids: tuple[str, ...] | None = None,
        created_at: datetime | None = None,
        recorded_at: datetime | None = None,
    ) -> tuple[AuditMessageDraft, ...]:
        now = created_at if created_at is not None else self._now()
        if message_ids is not None and len(message_ids) != len(drafts):
            raise AuditIntegrityError("audit bundle identities do not match its immutable drafts")
        sid = str(session_id)
        csid = str(composition_state_id) if composition_state_id else None
        effective_state_ids = tuple(d.composition_state_id if d.composition_state_id is not None else csid for d in drafts)
        if not drafts:
            return ()
        self._assert_session_write_lock_held(
            conn,
            sid,
            caller="add_messages_atomic._write",
        )
        for state_id in dict.fromkeys(effective_state_ids):
            if state_id is not None:
                _assert_state_in_session(
                    conn,
                    state_id=state_id,
                    expected_session_id=sid,
                    caller="add_messages_atomic",
                )
        active_drafts: list[AuditMessageDraft] = []
        active_ids: list[str | None] = []
        for draft_index, draft in enumerate(drafts):
            if draft.tool_calls:
                envelopes = uncheckpointed_envelopes(conn, session_id=sid, envelopes=draft.tool_calls)
                if not envelopes and draft.role == "audit":
                    continue
                draft = replace(draft, tool_calls=envelopes)
            active_drafts.append(draft)
            active_ids.append(message_ids[draft_index] if message_ids is not None else None)
        if not active_drafts:
            return ()
        base_seq = self._reserve_sequence_range(conn, sid, count=len(active_drafts))
        for offset, draft in enumerate(active_drafts):
            self._insert_chat_message(
                conn,
                session_id=sid,
                role=draft.role,
                content=draft.content,
                raw_content=None,
                # Same deep_thaw rationale as add_message: the
                # envelopes may carry MappingProxyType / tuple
                # shapes from frozen-dataclass round-trips.
                tool_calls=deep_thaw(draft.tool_calls) if draft.tool_calls else None,
                sequence_no=base_seq + offset,
                writer_principal=writer_principal,
                composition_state_id=draft.composition_state_id if draft.composition_state_id is not None else csid,
                tool_call_id=draft.tool_call_id,
                parent_assistant_id=draft.parent_assistant_id,
                created_at=now,
                session_operation_context=session_operation_context,
                audit_only=audit_only,
                message_id=active_ids[offset],
            )
        entries = llm_call_usage_entries(
            tuple(envelope for draft in active_drafts if draft.tool_calls is not None for envelope in draft.tool_calls)
        )
        if entries:
            # Task I1 Composer adapter (compose loop, turn cohort, planner
            # evidence): charged in the transaction that makes the audit rows durable.
            record_token_usage_on_connection(
                conn,
                session_id=sid,
                source="composer",
                run_id=None,
                entries=entries,
                recorded_at=recorded_at if recorded_at is not None else database_now(conn),
            )
        with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
            session_mutations.mark_session_updated(updated_at=now)
        return tuple(active_drafts)

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
        """Persist one audit cohort in a single transaction (elspeth-90231248dc).

        Sibling of :meth:`add_message` for the composer's buffered audit
        cohorts (LLM-call sidecars, tool-invocation breadcrumbs, planner
        evidence). The per-row loop the route helpers previously ran —
        one ``add_message`` transaction per record — could fail after any
        prefix, leaving a partial sidecar set that reads as a complete
        record; with no stable per-record identity a retry can neither
        recognise nor complete the missing suffix. This method removes
        the partial state instead: the whole cohort commits in one
        transaction, under one held session write lock and one contiguous
        sequence-number block, or none of it does.

        Cancellation safety comes from
        ``_run_sync_with_post_commit_projection``: the sync worker is
        drained through a mid-flight ``CancelledError``, so the cohort is
        either fully durable or untouched — never a cancelled prefix.

        A draft's ``composition_state_id``, when set, overrides the
        cohort-level ``composition_state_id`` for that row (``None``
        falls back to it). One turn's tool rows and LLM sidecars carry
        different state ids — post-compose vs pre-send — and the
        override is what lets them settle as ONE cohort instead of two
        independently-committing transactions. Every distinct effective
        state id is verified against the session before any insert.

        An empty ``drafts`` sequence is a no-op (the compose loop drains
        conditionally; an empty cohort must not bump ``updated_at``).
        """
        _validate_required_sql_ticket(
            required_work,
            sources=(
                RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL,
                RequiredWorkSource.DISPATCH_AUDIT_SQL,
                RequiredWorkSource.PROVIDER_SETTLEMENT_SQL,
                RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
            ),
            session_id=str(session_id),
            context=session_operation_context,
        )
        if type(session_operation_kind) is not SessionOperationKind:
            _refuse_required_sql(required_work, TypeError("session_operation_kind must be an exact SessionOperationKind"))
        if session_operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}:
            _refuse_required_sql(required_work, ValueError("add_messages_atomic fenced writes require COMPOSE or PROPOSAL authority"))
        if type(session_operation_context) is not SessionOperationContext:
            _refuse_required_sql(required_work, TypeError("session_operation_context must be an exact SessionOperationContext"))
        if not drafts:
            if required_work is not None:
                required_work.complete_without_submission()
            return
        sid = str(session_id)

        def _write(conn: Connection) -> tuple[AuditMessageDraft, ...]:
            return self._write_audit_cohort_on_connection(
                conn,
                session_id=session_id,
                drafts=drafts,
                writer_principal=writer_principal,
                composition_state_id=composition_state_id,
                session_operation_context=session_operation_context,
                audit_only=audit_only,
            )

        def _sync() -> tuple[AuditMessageDraft, ...]:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=session_operation_kind,
                    audit_only=audit_only,
                ),
            ):
                return _write(conn)

        def _project(committed_drafts: tuple[AuditMessageDraft, ...]) -> None:
            for draft in committed_drafts:
                record_settled_composer_audit_message(
                    role=draft.role,
                    writer_principal=writer_principal,
                    tool_calls=draft.tool_calls,
                )

        await self._run_sync_with_post_commit_projection(_sync, project=_project, composer_custody=True, required_work=required_work)

    async def add_run_diagnostics_audit_messages_atomic(
        self,
        authority: RunDiagnosticsAuditAuthority,
        drafts: Sequence[RunDiagnosticsAuditDraft],
    ) -> tuple[ChatMessageRecord, ...]:
        """Append one run-diagnostics audit cohort all-or-nothing.

        Cohort sibling of :meth:`add_run_diagnostics_audit_message`
        (elspeth-90231248dc): the injected repository authority proves
        custody once and commits every draft in the same locked
        transaction with a contiguous sequence block, so a mid-cohort
        failure or lost authority leaves zero rows durable — never a
        prefix that reads as a complete diagnostics record.
        """
        if not drafts:
            return ()
        return cast(
            "tuple[ChatMessageRecord, ...]",
            await self._run_sync(
                self._run_diagnostics_audit_authority.append_audit_messages,
                authority=authority,
                rows=tuple(drafts),
            ),
        )

    async def get_state_version_numbers(
        self,
        session_id: UUID,
    ) -> dict[str, int]:
        """Map composition-state id → version for one session (lean projection)."""

        def _sync() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(
                    select(
                        composition_states_table.c.id,
                        composition_states_table.c.version,
                    ).where(composition_states_table.c.session_id == str(session_id))
                ).fetchall()

        rows = await self._run_sync(_sync)
        return {row.id: int(row.version) for row in rows}

    async def list_composition_rejection_events(
        self,
        session_id: UUID,
    ) -> tuple[CompositionRejectionEventRecord, ...]:
        """Read-only projection of ``composition_rejection_events`` (elspeth-3e28029d2f).

        Placed after ``get_state_version_numbers`` on purpose: the Sessions DB
        read inventory pins ``line`` and no reviewed identity sits below it.
        ``planner_payload`` is not selected.
        """

        def _sync() -> Any:
            with self._engine.connect() as conn:
                return conn.execute(
                    select(
                        composition_rejection_events_table.c.id,
                        composition_rejection_events_table.c.session_id,
                        composition_rejection_events_table.c.tool_call_id,
                        composition_rejection_events_table.c.tool_name,
                        composition_rejection_events_table.c.error_code,
                        composition_rejection_events_table.c.message,
                        composition_rejection_events_table.c.composition_state_id,
                        composition_rejection_events_table.c.created_at,
                    )
                    .where(composition_rejection_events_table.c.session_id == str(session_id))
                    .order_by(
                        composition_rejection_events_table.c.created_at,
                        composition_rejection_events_table.c.id,
                    )
                ).fetchall()

        rows = await self._run_sync(_sync)
        return tuple(
            CompositionRejectionEventRecord(
                id=row.id,
                session_id=row.session_id,
                tool_call_id=row.tool_call_id,
                tool_name=row.tool_name,
                error_code=row.error_code,
                message=row.message,
                composition_state_id=row.composition_state_id,
                # Same normalization as ``_row_to_chat_message_record``.
                created_at=restore_utc(row.created_at),
            )
            for row in rows
        )

    @contextlib.contextmanager
    def _session_mutations(
        self,
        conn: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
    ) -> Iterator[_RepositorySessionMutations]:
        """Yield the repository's session-row facet over the caller's fenced transaction.

        The caller sits inside a ``_session_composer_mutation_transaction``
        block, so the live fence row on ``conn`` is already proved; the facet
        re-checks the operation's exact shape before each write and dies with
        this block (P4-D6 family A2a).
        """
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        state = _RepositoryMutationState(
            conn,
            session_id=session_id,
            database_now=database_now(conn),
            operation_context=session_operation_context,
        )
        try:
            yield _RepositorySessionMutations(state)
        finally:
            state._close()

    @contextlib.contextmanager
    def _interpretation_mutations(
        self,
        conn: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
    ) -> Iterator[_RepositoryInterpretationMutations]:
        """Yield the repository's interpretation facet over the caller's fenced COMPOSE transaction (P4-D6 family A2b)."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        state = _RepositoryMutationState(
            conn,
            session_id=session_id,
            database_now=database_now(conn),
            operation_context=session_operation_context,
        )
        try:
            yield _RepositoryInterpretationMutations(state)
        finally:
            state._close()

    def _require_session_write_authority_on_connection(
        self,
        conn: Connection,
        context: SessionOperationContext,
        *,
        session_id: str,
        audit_only: bool = False,
    ) -> None:
        """Prove, on ``conn``, that ``context`` is a live operation over ``session_id`` that may write its rows.

        The SessionMutationAuthority proof of the two shared row writers
        (P4-D6 family A2b): an exact COMPOSE, PROPOSAL or SESSION_FORK context
        whose fence row is live and unreleased for exactly this session. Any
        other kind, a released or foreign fence, or a non-context raises before
        a row is written.
        """
        if type(context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if context.operation_kind not in {
            SessionOperationKind.COMPOSE,
            SessionOperationKind.PROPOSAL,
            SessionOperationKind.SESSION_FORK,
        }:
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        self._require_session_operation_context_on_connection(
            conn,
            context,
            session_id=session_id,
            expected_kind=context.operation_kind,
            now=database_now(conn),
            audit_only=audit_only,
        )
