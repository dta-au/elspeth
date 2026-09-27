"""SessionService implementation -- CRUD, state versioning, active run enforcement.

Uses SQLAlchemy Core with a synchronous engine. Database calls run in a
thread pool executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

import structlog
from opentelemetry import metrics
from sqlalchemy import ColumnElement, Connection, Engine, case, delete, desc, exists, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import BlobForkPlanEntry, BlobGuidedOperationWriteFence, BlobRecord, fork_blob_id
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
from elspeth.contracts.hashing import stable_hash
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_custody import (
    finalize_pipeline_custody_on_connection,
    staged_pipeline_custody,
)
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PlannerSurface,
    PresentBase,
    composition_content_hash,
    is_owned_composition_state_authority,
    owned_composition_state_review_arguments,
    reviewed_anchor_hash,
)
from elspeth.web.composer.provider_telemetry import (
    record_settled_composer_audit_message,
    record_settled_composer_provider_calls,
)
from elspeth.web.composer.redaction import (
    assert_guided_custody_persistable,
    redact_tool_call_arguments,
    semantic_redacted_pipeline_arguments_hash,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

# Phase 8 cohort-emit helper (Sub-task 7e — B3 cohort b1). The opt-out
# audit row is committed inside ``record_session_interpretation_opt_out``
# below, and the helper must fire only on the INSERT path (not the F-29
# idempotent re-fire). The helper module lives under ``web/composer``
# per project plan; the sessions→composer import direction follows the
# precedent set by ``_auto_title.py``, ``_guided_step_chat.py``, and
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
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions._persist_payload import AuditMessageDraft, AuditOutcome, RedactedToolRow, RejectionRecord, StatePayload
from elspeth.web.sessions.archive_quarantine import (
    ArchiveQuarantineIdentity,
    canonical_archive_present,
    list_archive_quarantine_manifests,
    prepare_archive_quarantine,
    purge_archive_quarantine,
    restore_archive_quarantine,
    retire_archive_quarantine,
    stage_archive_quarantine,
)
from elspeth.web.sessions.audit_checkpoint import uncheckpointed_envelopes
from elspeth.web.sessions.converters import pending_guided_checkpoint, state_from_record
from elspeth.web.sessions.dead_site_supersession import supersede_dead_site_pending_interpretation_events
from elspeth.web.sessions.fork_custody import (
    _fork_blob_plan_content,
    _fork_blob_plan_from_content,
    _fork_blob_plan_identity_from_content,
    _refuse_unrewritable_fork_custody,
    _strip_guided_profile_in_meta,
    _verify_fork_settlement_blob_custody,
)
from elspeth.web.sessions.guided_audit import (
    bind_guided_failure_audit_rows,
    prepare_guided_audit_rows,
    validate_guided_audit_payload_references,
)
from elspeth.web.sessions.guided_operation_rules import (
    _guided_in_progress_expiry,
    _guided_operation_event_values,
    _guided_terminal_outcome,
    _validate_guided_actor,
    _validate_guided_identity,
    _validate_guided_lease_seconds,
    _validate_guided_operation_admission_block_row,
    _validate_guided_operation_row,
)
from elspeth.web.sessions.guided_payloads import verify_guided_json_payloads
from elspeth.web.sessions.guided_proposal_authority import (
    _append_verified_guided_proposal_rebase,
    _carried_guided_checkpoint_session,
    _GuidedPendingProposalRebasePlan,
    _GuidedPendingProposalTransitionContext,
    _rebind_guided_pending_proposal,
    _require_pending_guided_checkpoint_proposal_authority,
    _verify_guided_pending_proposal_transition,
)
from elspeth.web.sessions.guided_replay import (
    guided_response_projection_hash,
    project_composition_proposal,
    project_guided_full_decline,
    project_guided_response,
    response_json,
    validation_errors_for_composer_surface,
    with_guided_response_descriptor,
)
from elspeth.web.sessions.inline_blob_preflight import InlinePreflightState, SessionInlineBlobSnapshot, prepare_session_inline_blob_snapshot
from elspeth.web.sessions.locking import (
    acquire_session_advisory_xact_lock,
    process_session_lock,
    sqlite_session_mutex,
    sqlite_transaction_session_lock,
)
from elspeth.web.sessions.models import (
    audit_access_log_table,
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_rejection_events_table,
    composition_states_table,
    guided_operation_admission_blocks_table,
    guided_operation_events_table,
    guided_operations_table,
    interpretation_events_table,
    message_ingress_receipts_table,
    proposal_events_table,
    run_events_table,
    run_execution_inputs_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.mutation_capabilities import (
    _GuidedSessionMutationTransaction,
    _SessionComposerMutationTransaction,
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
from elspeth.web.sessions.proposal_authority import (
    _PIPELINE_CREATED_SCHEMA,
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
    _valid_chat_ingress_inputs_metadata,
    _valid_compartment_ingress_metadata,
    _validate_tool_call_id_set_equality,
    _validated_pipeline_rejection_reason,
    _verified_guided_root_message_row,
    _verify_guided_correction_message_authority,
    _verify_guided_deferred_intent_mutation,
    _verify_guided_deferred_message_authority,
    _verify_guided_root_message_authority,
    _verify_pipeline_lifecycle_authority,
)
from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references
from elspeth.web.sessions.protocol import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    COMPOSER_TRUST_MODE_VALUES,
    GUIDED_FAILURE_AUDIT_LINEAGE_KEY,
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
    GuidedAuditEvidence,
    GuidedCompositionStateResult,
    GuidedDeclinedResult,
    GuidedFailureAuditCohort,
    GuidedFailureAuditLineage,
    GuidedForkSettlementCommand,
    GuidedFullPipelineDeclineCommand,
    GuidedFullPipelineDeclineSettlement,
    GuidedFullPipelineProposalStageCommand,
    GuidedFullPipelineProposalStageSettlement,
    GuidedOperationActive,
    GuidedOperationClaimed,
    GuidedOperationCompleted,
    GuidedOperationConflictError,
    GuidedOperationFailed,
    GuidedOperationFailureCode,
    GuidedOperationFailureCommand,
    GuidedOperationFence,
    GuidedOperationFenceLostError,
    GuidedOperationKind,
    GuidedOperationOutcome,
    GuidedOperationResult,
    GuidedOperationSettlementConflictError,
    GuidedOperationTakenOver,
    GuidedOriginatingUserMessageDraft,
    GuidedPendingProposalRebase,
    GuidedPipelineConfirmationAdmissionCommand,
    GuidedPipelineDispatchRecordCommand,
    GuidedPipelineProposalAcceptCommand,
    GuidedPipelineProposalBackEditCommand,
    GuidedPipelineProposalRejectCommand,
    GuidedPipelineProposalResult,
    GuidedPipelineProposalStageCommand,
    GuidedPipelineProposalStageSettlement,
    GuidedSessionResult,
    GuidedStartStateConverged,
    GuidedStartStateOutcome,
    GuidedStartStateSeeded,
    GuidedStateOperationCommand,
    GuidedStateOperationSettlement,
    InterpretationEventAlreadyResolvedError,
    InterpretationEventNotFoundError,
    InterpretationPlaceholderConsumedError,
    InterpretationSourceDataContractDriftError,
    InterpretationUnsupportedChoiceError,
    MessageIngressAccepted,
    MessageIngressConflict,
    MessageIngressFresh,
    PipelineDispatchRecovery,
    PipelineProposalRejectionReason,
    PipelineProposalSettlementResult,
    PreparedGuidedAuditRow,
    PreparedGuidedJsonPayload,
    PreparedInterpretationEventDraft,
    ProposalEventRecord,
    ProposalLifecycleStatus,
    ProposalStateConflictError,
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
    SessionNotFoundError,
    SessionOperationAuthority,
    SessionOperationMutationTransaction,
    SessionPendingInterpretationCommand,
    SessionRecord,
    SessionRunEventType,
    SessionRunStatus,
    StagedForkSession,
    StaleComposeStateError,
    TransitionAssistantDraft,
    TransitionResponseSettlement,
    TrustModeAutoCommitRevokedError,
    decode_stored_composition_validation_errors,
    serialize_composition_validation_errors,
)
from elspeth.web.sessions.protocol import (
    InterpretationResolveError as InterpretationResolveError,
)
from elspeth.web.sessions.skill_markdown_history import (
    RepositorySkillMarkdownHistoryAuthority,
    SkillMarkdownHistoryAuthority,
)
from elspeth.web.sessions.state_envelope import envelope_state_column, unwrap_state_column
from elspeth.web.sessions.telemetry import _SessionsTelemetry
from elspeth.web.sessions.time_normalization import restore_utc
from elspeth.web.validation import _validate_accepted_value_content

if TYPE_CHECKING:
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.guided.state_machine import GuidedSession
    from elspeth.web.composer.state import CompositionState, ValidationSummary
    from elspeth.web.execution.envelope import RunExecutionInput
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
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
        plugin_snapshot: PluginAvailabilitySnapshot | None,
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
        if self._plugin_snapshot_factory is None:
            summary = state.validate()
        else:
            if plugin_snapshot is None:
                raise AuditIntegrityError("Profile-aware composition validation has no principal snapshot")

            from elspeth.web.plugin_policy.validation import validate_authored_composition_state

            assert self._operator_profile_registry is not None
            assert self._catalog is not None
            result = validate_authored_composition_state(
                state,
                snapshot=plugin_snapshot,
                profile_registry=self._operator_profile_registry,
                catalog=self._catalog,
            )
            summary = result.validation
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

    @staticmethod
    def _guided_database_now(conn: Connection) -> datetime:
        """Read one fresh wall-clock timestamp from the operation database."""
        dialect = conn.dialect.name
        if dialect == "sqlite":
            value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
        elif dialect == "postgresql":
            # CURRENT_TIMESTAMP is transaction-start time on PostgreSQL.
            value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        else:
            raise NotImplementedError(f"guided operation database time not implemented for dialect {dialect}")
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise AuditIntegrityError("Guided operation database clock returned malformed datetime text") from exc
        if type(value) is not datetime:
            raise AuditIntegrityError("Guided operation database clock returned a non-datetime value")
        return restore_utc(value)

    def _require_session_fork_session_fences_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> datetime:
        """Validate the parent and adopted-child session-operation fences only."""
        if type(authority) is not SessionForkAuthority:
            raise TypeError("fork authority must be exact")
        now = self._guided_database_now(conn)
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
                raise GuidedOperationFenceLostError(authority.parent.guided_fence)
        return now

    def _require_session_fork_authority_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> tuple[Any, datetime]:
        """Validate parent, child, and live guided authority under one pair lock."""
        self._require_session_fork_session_fences_on_connection(conn, authority)
        return self.require_guided_operation_fence_on_connection(
            conn,
            authority.parent.guided_fence,
        )

    def _require_settled_session_fork_authority_on_connection(
        self,
        conn: Connection,
        authority: SessionForkAuthority,
    ) -> datetime:
        """Validate fork authority after this transaction terminalised the parent operation.

        Settlement completes the parent guided row first so a later failure
        rolls the terminal row back atomically. Writes that follow in the same
        transaction therefore cannot demand a live guided lease; they require
        the still-live session-operation fences plus a parent row that is
        ``completed`` and bound to exactly this operation and adopted child.
        """
        now = self._require_session_fork_session_fences_on_connection(conn, authority)
        guided_fence = authority.parent.guided_fence
        sid = str(guided_fence.session_id)
        self._assert_session_write_lock_held(conn, sid, caller="_require_settled_session_fork_authority_on_connection")
        row = (
            conn.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == sid,
                    guided_operations_table.c.operation_id == guided_fence.operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            _validate_guided_operation_row(
                row,
                expected_session_id=sid,
                expected_operation_id=guided_fence.operation_id,
            )
        if (
            row is None
            or row["kind"] != "session_fork"
            or row["status"] != "completed"
            or row["attempt"] != guided_fence.attempt
            or row["result_session_id"] != authority.child_context.fence.session_id
        ):
            raise GuidedOperationFenceLostError(guided_fence)
        return now

    def _require_session_operation_context_on_connection(
        self,
        conn: Connection,
        context: SessionOperationContext,
        *,
        session_id: str,
        expected_kind: SessionOperationKind,
        now: datetime,
        guided_fence: GuidedOperationFence | None = None,
    ) -> None:
        """Validate one exact current session authority in the caller transaction."""
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind is not expected_kind
            or context.fence.session_id != session_id
        ):
            if guided_fence is not None:
                raise GuidedOperationFenceLostError(guided_fence)
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
            if guided_fence is not None:
                raise GuidedOperationFenceLostError(guided_fence)
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

    def require_guided_operation_authority_on_connection(
        self,
        conn: Connection,
        fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
    ) -> tuple[RowMapping, datetime]:
        """Validate the exact session and guided pair under one session lock."""
        row, now = self.require_guided_operation_fence_on_connection(conn, fence)
        expected_kind = SessionOperationKind.SESSION_FORK if row["kind"] == "session_fork" else SessionOperationKind.COMPOSE
        self._require_session_operation_context_on_connection(
            conn,
            session_operation_context,
            session_id=str(fence.session_id),
            expected_kind=expected_kind,
            now=now,
            guided_fence=fence,
        )
        return row, now

    @contextlib.contextmanager
    def _session_composer_mutation_transaction(
        self,
        conn: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
        expected_kind: SessionOperationKind,
    ) -> Iterator[_SessionComposerMutationTransaction]:
        """Yield a lifetime-checked ordinary Composer mutation capability."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        now = self._guided_database_now(conn)
        self._require_session_operation_context_on_connection(
            conn,
            session_operation_context,
            session_id=session_id,
            expected_kind=expected_kind,
            now=now,
        )
        transaction = _SessionComposerMutationTransaction(
            self,
            conn,
            session_id=session_id,
            session_operation_context=session_operation_context,
            expected_kind=expected_kind,
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

    def _record_guided_fork_child_terminal_event(
        self,
        conn: Connection,
        *,
        authority: SessionForkAuthority,
        session_id: str,
        operation_id: str,
        event_kind: Literal["completed"],
        actor: str,
        attempt: int,
        prior_attempt: None,
        lease_expires_at: None,
        request_hash: str,
        failure_audit_cohort: None,
        occurred_at: datetime,
    ) -> None:
        """Append the staged child's synthetic terminal event under settled fork authority.

        Runs inside ``settle_guided_fork_operation`` after the parent fork row
        has been completed in the same transaction, so the check is the
        settled form: live session fences plus a completed parent bound to
        this child, never a live guided lease.
        """
        if type(authority) is not SessionForkAuthority:
            raise TypeError("authority must be an exact SessionForkAuthority")
        if session_id != authority.child_context.fence.session_id or event_kind != "completed":
            raise AuditIntegrityError("fork child guided event is not bound to the exact child authority")
        self._require_settled_session_fork_authority_on_connection(conn, authority)
        next_sequence = conn.execute(
            select(func.coalesce(func.max(guided_operation_events_table.c.sequence), 0) + 1).where(
                guided_operation_events_table.c.session_id == session_id,
                guided_operation_events_table.c.operation_id == operation_id,
            )
        ).scalar_one()
        self._require_settled_session_fork_authority_on_connection(conn, authority)
        conn.execute(
            insert(guided_operation_events_table).values(
                **_guided_operation_event_values(
                    session_id=session_id,
                    operation_id=operation_id,
                    sequence=int(next_sequence),
                    event_kind=event_kind,
                    actor=actor,
                    attempt=attempt,
                    prior_attempt=prior_attempt,
                    lease_expires_at=lease_expires_at,
                    request_hash=request_hash,
                    failure_audit_cohort=failure_audit_cohort,
                    occurred_at=occurred_at,
                )
            )
        )

    @contextlib.contextmanager
    def _guided_session_mutation_transaction(
        self,
        conn: Connection,
        *,
        guided_fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
    ) -> Iterator[_GuidedSessionMutationTransaction]:
        """Yield an exact dual-fenced capability inside the caller transaction."""
        if type(guided_fence) is not GuidedOperationFence:
            raise TypeError("guided_fence must be an exact GuidedOperationFence")
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        self.require_guided_operation_authority_on_connection(
            conn,
            guided_fence,
            session_operation_context,
        )
        transaction = _GuidedSessionMutationTransaction(
            self,
            conn,
            guided_fence=guided_fence,
            session_operation_context=session_operation_context,
        )
        try:
            yield transaction
        finally:
            transaction._close()

    @staticmethod
    def _read_guided_operation_authority(
        conn: Connection,
        *,
        session_id: str,
        operation_id: str,
    ) -> tuple[RowMapping | None, RowMapping | None]:
        """Read the mutually exclusive positive and negative authorities."""

        operation = (
            conn.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        block = (
            conn.execute(
                select(guided_operation_admission_blocks_table).where(
                    guided_operation_admission_blocks_table.c.session_id == session_id,
                    guided_operation_admission_blocks_table.c.operation_id == operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if operation is not None and block is not None:
            raise AuditIntegrityError("Tier 1: guided operation and admission block coexist")
        return operation, block

    @staticmethod
    def _guided_conflict(*, session_id: UUID, operation_id: str) -> GuidedOperationConflictError:
        return GuidedOperationConflictError(session_id=session_id, operation_id=operation_id)

    async def reserve_guided_operation(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: GuidedOperationKind,
        request_hash: str,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> GuidedOperationOutcome:
        """Claim, join, replay, or take over one normalized operation."""
        _validate_guided_identity(operation_id=operation_id, kind=kind, request_hash=request_hash)
        _validate_guided_actor(actor)
        _validate_guided_lease_seconds(lease_seconds)
        sid = str(session_id)

        def _sync() -> GuidedOperationOutcome:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                now = self._guided_database_now(conn)
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=(SessionOperationKind.SESSION_FORK if kind == "session_fork" else SessionOperationKind.COMPOSE),
                    now=now,
                )
                row, block = self._read_guided_operation_authority(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                )
                if block is not None:
                    _validate_guided_operation_admission_block_row(
                        block,
                        expected_session_id=sid,
                        expected_operation_id=operation_id,
                    )
                    if kind != "guided_start":
                        raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
                    return GuidedOperationFailed(failure_code="request_cancelled")
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                if row is None:
                    parent = conn.execute(
                        select(sessions_table.c.id, sessions_table.c.archived_at).where(sessions_table.c.id == sid)
                    ).one_or_none()
                    if parent is None or parent.archived_at is not None:
                        raise SessionNotFoundError(session_id)
                    lease_token = uuid.uuid4().hex
                    conn.execute(
                        insert(guided_operations_table).values(
                            session_id=sid,
                            operation_id=operation_id,
                            kind=kind,
                            status="in_progress",
                            request_hash=request_hash,
                            lease_token=lease_token,
                            lease_expires_at=lease_expires_at,
                            attempt=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    fence = GuidedOperationFence(session_id, operation_id, lease_token, 1)
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        mutation.guided.record_nonterminal_event(
                            event_kind="claimed",
                            actor=actor,
                            attempt=1,
                            prior_attempt=None,
                            lease_expires_at=lease_expires_at,
                            request_hash=request_hash,
                            occurred_at=now,
                        )
                    return GuidedOperationClaimed(
                        fence=fence,
                        lease_expires_at=lease_expires_at,
                    )
                _validate_guided_operation_row(
                    row,
                    expected_session_id=sid,
                    expected_operation_id=operation_id,
                )
                if row["kind"] != kind or row["request_hash"] != request_hash:
                    raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
                if row["status"] in {"completed", "failed"}:
                    return _guided_terminal_outcome(row)
                if row["status"] != "in_progress":
                    raise AuditIntegrityError("Tier 1: guided operation has an invalid status")
                parent = conn.execute(
                    select(sessions_table.c.id, sessions_table.c.archived_at).where(sessions_table.c.id == sid)
                ).one_or_none()
                if parent is None or parent.archived_at is not None:
                    raise AuditIntegrityError("Tier 1: in-progress guided operation lost its active parent custody")
                prior_expiry = _guided_in_progress_expiry(row)
                prior_attempt = row["attempt"]
                if type(prior_attempt) is not int or prior_attempt < 1:
                    raise AuditIntegrityError("Tier 1: guided operation has an invalid attempt")
                if prior_expiry > now:
                    return GuidedOperationActive(attempt=prior_attempt, lease_expires_at=prior_expiry)
                prior_token = row["lease_token"]
                assert type(prior_token) is str
                next_attempt = prior_attempt + 1
                lease_token = uuid.uuid4().hex
                changed = conn.execute(
                    update(guided_operations_table)
                    .where(
                        guided_operations_table.c.session_id == sid,
                        guided_operations_table.c.operation_id == operation_id,
                        guided_operations_table.c.status == "in_progress",
                        guided_operations_table.c.lease_token == prior_token,
                        guided_operations_table.c.attempt == prior_attempt,
                        guided_operations_table.c.lease_expires_at <= now,
                    )
                    .values(
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        attempt=next_attempt,
                        updated_at=now,
                    )
                ).rowcount
                if changed != 1:
                    raise AuditIntegrityError("Guided operation takeover lost its locked compare-and-swap")
                fence = GuidedOperationFence(session_id, operation_id, lease_token, next_attempt)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.record_nonterminal_event(
                        event_kind="taken_over",
                        actor=actor,
                        attempt=next_attempt,
                        prior_attempt=prior_attempt,
                        lease_expires_at=lease_expires_at,
                        request_hash=request_hash,
                        occurred_at=now,
                    )
                return GuidedOperationTakenOver(
                    fence=fence,
                    prior_attempt=prior_attempt,
                    lease_expires_at=lease_expires_at,
                )

        return cast("GuidedOperationOutcome", await self._run_sync(_sync))

    async def get_guided_operation(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: GuidedOperationKind,
        request_hash: str,
    ) -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
        """Read one matching replay descriptor without acquiring its lease."""
        _validate_guided_identity(operation_id=operation_id, kind=kind, request_hash=request_hash)
        sid = str(session_id)

        def _sync() -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
            with self._engine.connect() as conn:
                now = self._guided_database_now(conn)
                row, block = self._read_guided_operation_authority(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                )
            if block is not None:
                _validate_guided_operation_admission_block_row(
                    block,
                    expected_session_id=sid,
                    expected_operation_id=operation_id,
                )
                if kind != "guided_start":
                    raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
                return GuidedOperationFailed(failure_code="request_cancelled")
            if row is None:
                return None
            _validate_guided_operation_row(
                row,
                expected_session_id=sid,
                expected_operation_id=operation_id,
            )
            if row["kind"] != kind or row["request_hash"] != request_hash:
                raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
            if row["status"] in {"completed", "failed"}:
                return _guided_terminal_outcome(row)
            if row["status"] != "in_progress":
                raise AuditIntegrityError("Tier 1: guided operation has an invalid status")
            expiry = _guided_in_progress_expiry(row)
            attempt = row["attempt"]
            if type(attempt) is not int or attempt < 1:
                raise AuditIntegrityError("Tier 1: guided operation has an invalid attempt")
            return GuidedOperationActive(attempt=attempt, lease_expires_at=expiry, expired=expiry <= now)

        return cast(
            "GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None",
            await self._run_sync(_sync),
        )

    async def get_guided_start_reconciliation(
        self,
        *,
        session_id: UUID,
        operation_id: str,
    ) -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
        """Inspect cold-start custody without entering a write transaction."""
        if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
            raise ValueError("guided operation id must be a non-empty string of at most 128 characters")
        sid = str(session_id)

        def _sync() -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
            with self._engine.connect() as conn:
                now = self._guided_database_now(conn)
                row, block = self._read_guided_operation_authority(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                )
            if row is None:
                if block is None:
                    return None
                _validate_guided_operation_admission_block_row(
                    block,
                    expected_session_id=sid,
                    expected_operation_id=operation_id,
                )
                return GuidedOperationFailed(failure_code="request_cancelled")
            _validate_guided_operation_row(
                row,
                expected_session_id=sid,
                expected_operation_id=operation_id,
            )
            if row["kind"] != "guided_start":
                raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
            if row["status"] in {"completed", "failed"}:
                return _guided_terminal_outcome(row)
            if row["status"] != "in_progress":
                raise AuditIntegrityError("Tier 1: guided operation has an invalid status")
            expiry = _guided_in_progress_expiry(row)
            attempt = row["attempt"]
            if type(attempt) is not int or attempt < 1:
                raise AuditIntegrityError("Tier 1: guided operation has an invalid attempt")
            return GuidedOperationActive(
                attempt=attempt,
                lease_expires_at=expiry,
                expired=expiry <= now,
            )

        return cast(
            "GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None",
            await self._run_sync(_sync),
        )

    async def reconcile_guided_start_operation(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        observed_attempt: int | None,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
        """Seal one observed missing/expired guided start under both authorities."""
        if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
            raise ValueError("guided operation id must be a non-empty string of at most 128 characters")
        if observed_attempt is not None and (type(observed_attempt) is not int or observed_attempt < 1):
            raise ValueError("observed_attempt must be None or a positive exact integer")
        _validate_guided_actor(actor)
        _validate_guided_lease_seconds(lease_seconds)
        sid = str(session_id)

        def _sync() -> GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                now = self._guided_database_now(conn)
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=SessionOperationKind.COMPOSE,
                    now=now,
                )
                row, block = self._read_guided_operation_authority(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                )
                if row is None:
                    if block is not None:
                        _validate_guided_operation_admission_block_row(
                            block,
                            expected_session_id=sid,
                            expected_operation_id=operation_id,
                        )
                        return GuidedOperationFailed(failure_code="request_cancelled")
                    if observed_attempt is not None:
                        return None
                    parent = conn.execute(
                        select(sessions_table.c.id, sessions_table.c.archived_at).where(sessions_table.c.id == sid)
                    ).one_or_none()
                    if parent is None or parent.archived_at is not None:
                        raise SessionNotFoundError(session_id)
                    conn.execute(
                        insert(guided_operation_admission_blocks_table).values(
                            session_id=sid,
                            operation_id=operation_id,
                            kind="guided_start",
                            failure_code="request_cancelled",
                            actor=actor,
                            created_at=now,
                        )
                    )
                    return GuidedOperationFailed(failure_code="request_cancelled")
                _validate_guided_operation_row(
                    row,
                    expected_session_id=sid,
                    expected_operation_id=operation_id,
                )
                if row["kind"] != "guided_start":
                    raise self._guided_conflict(session_id=session_id, operation_id=operation_id)
                if row["status"] in {"completed", "failed"}:
                    return _guided_terminal_outcome(row)
                if row["status"] != "in_progress":
                    raise AuditIntegrityError("Tier 1: guided operation has an invalid status")
                expiry = _guided_in_progress_expiry(row)
                attempt = row["attempt"]
                if type(attempt) is not int or attempt < 1:
                    raise AuditIntegrityError("Tier 1: guided operation has an invalid attempt")
                if observed_attempt != attempt or expiry > now:
                    return GuidedOperationActive(
                        attempt=attempt,
                        lease_expires_at=expiry,
                        expired=expiry <= now,
                    )

                prior_token = row["lease_token"]
                assert type(prior_token) is str
                next_attempt = attempt + 1
                lease_token = uuid.uuid4().hex
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                changed = conn.execute(
                    update(guided_operations_table)
                    .where(
                        guided_operations_table.c.session_id == sid,
                        guided_operations_table.c.operation_id == operation_id,
                        guided_operations_table.c.status == "in_progress",
                        guided_operations_table.c.lease_token == prior_token,
                        guided_operations_table.c.attempt == attempt,
                        guided_operations_table.c.lease_expires_at <= now,
                    )
                    .values(
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        attempt=next_attempt,
                        updated_at=now,
                    )
                ).rowcount
                if changed != 1:
                    raise AuditIntegrityError("Guided operation reconciliation lost its locked compare-and-swap")
                fence = GuidedOperationFence(
                    session_id=session_id,
                    operation_id=operation_id,
                    lease_token=lease_token,
                    attempt=next_attempt,
                )
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.record_nonterminal_event(
                        event_kind="taken_over",
                        actor=actor,
                        attempt=next_attempt,
                        prior_attempt=attempt,
                        lease_expires_at=lease_expires_at,
                        request_hash=row["request_hash"],
                        occurred_at=now,
                    )
                    return mutation.guided.fail(
                        failure_code="request_cancelled",
                        actor=actor,
                        failure_audit_cohort=GuidedFailureAuditCohort.empty(),
                        unproducible_output_fields=(),
                    )

        return cast(
            "GuidedOperationActive | GuidedOperationCompleted | GuidedOperationFailed | None",
            await self._run_sync(_sync),
        )

    def require_guided_operation_fence_on_connection(
        self,
        conn: Connection,
        fence: GuidedOperationFence,
    ) -> tuple[RowMapping, datetime]:
        """Verify an exact live fence inside the caller's locked transaction."""
        sid = str(fence.session_id)
        self._assert_session_write_lock_held(conn, sid, caller="require_guided_operation_fence_on_connection")
        now = self._guided_database_now(conn)
        row = (
            conn.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == sid,
                    guided_operations_table.c.operation_id == fence.operation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            _validate_guided_operation_row(
                row,
                expected_session_id=sid,
                expected_operation_id=fence.operation_id,
            )
        if (
            row is None
            or row["status"] != "in_progress"
            or row["lease_token"] != fence.lease_token
            or row["attempt"] != fence.attempt
            or _guided_in_progress_expiry(row) <= now
        ):
            raise GuidedOperationFenceLostError(fence)
        return row, now

    async def renew_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        actor: str,
        lease_seconds: int,
        session_operation_context: SessionOperationContext,
    ) -> GuidedOperationFence:
        _validate_guided_actor(actor)
        _validate_guided_lease_seconds(lease_seconds)
        sid = str(fence.session_id)

        def _sync() -> GuidedOperationFence:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                row, now = self.require_guided_operation_authority_on_connection(
                    conn,
                    fence,
                    session_operation_context,
                )
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                changed = conn.execute(
                    update(guided_operations_table)
                    .where(
                        guided_operations_table.c.session_id == sid,
                        guided_operations_table.c.operation_id == fence.operation_id,
                        guided_operations_table.c.status == "in_progress",
                        guided_operations_table.c.lease_token == fence.lease_token,
                        guided_operations_table.c.attempt == fence.attempt,
                        guided_operations_table.c.lease_expires_at > now,
                    )
                    .values(lease_expires_at=lease_expires_at, updated_at=now)
                ).rowcount
                if changed != 1:
                    raise GuidedOperationFenceLostError(fence)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.record_nonterminal_event(
                        event_kind="renewed",
                        actor=actor,
                        attempt=fence.attempt,
                        prior_attempt=None,
                        lease_expires_at=lease_expires_at,
                        request_hash=row["request_hash"],
                        occurred_at=now,
                    )
                return fence

        return cast("GuidedOperationFence", await self._run_sync(_sync))

    async def bind_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        originating_message_id: UUID | None = None,
        proposal_id: UUID | None = None,
        result_state_id: UUID | None = None,
        result_session_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
    ) -> None:
        sid = str(fence.session_id)

        def _sync() -> None:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation,
            ):
                mutation.guided.bind(
                    originating_message_id=originating_message_id,
                    proposal_id=proposal_id,
                    result_state_id=result_state_id,
                    result_session_id=result_session_id,
                )

        await self._run_sync(_sync)

    async def complete_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        result: GuidedOperationResult,
        response_hash: str,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> GuidedOperationCompleted:
        sid = str(fence.session_id)

        def _sync() -> GuidedOperationCompleted:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation,
            ):
                return mutation.guided.complete(result=result, response_hash=response_hash, actor=actor)

        return cast("GuidedOperationCompleted", await self._run_sync(_sync))

    async def fail_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        failure_code: GuidedOperationFailureCode,
        actor: str,
        session_operation_context: SessionOperationContext,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> GuidedOperationFailed:
        """Settle under the live fence with caller-admitted, operator-safe facts.

        Diagnostics are internal evidence, never raw exception text or provider
        output. Fork routes admit static reasons and structured custody facts.
        """
        sid = str(fence.session_id)

        def _sync() -> GuidedOperationFailed:
            with (
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
                self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation,
            ):
                return mutation.guided.fail(
                    failure_code=failure_code,
                    actor=actor,
                    failure_audit_cohort=GuidedFailureAuditCohort.empty(),
                    unproducible_output_fields=(),
                    failure_diagnostics=failure_diagnostics,
                )

        return cast("GuidedOperationFailed", await self._run_sync(_sync))

    async def fail_guided_fork_operation(
        self,
        authority: SessionForkAuthority,
        *,
        failure_code: GuidedOperationFailureCode,
        actor: str,
        failure_diagnostics: tuple[str, ...] = (),
    ) -> GuidedOperationFailed:
        """Fail while parent, child, and guided fences live, retaining safe facts.

        As in ``fail_guided_operation``, callers must admit diagnostic content;
        this method validates the carrier shape, not arbitrary text provenance.
        """
        if type(authority) is not SessionForkAuthority:
            raise TypeError("authority must be an exact SessionForkAuthority")
        parent_id = authority.parent.parent_context.fence.session_id
        child_id = authority.child_context.fence.session_id

        def _sync() -> GuidedOperationFailed:
            with self._session_pair_locked_begin(parent_id, child_id) as conn:
                self._require_session_fork_authority_on_connection(conn, authority)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=authority.parent.guided_fence,
                    session_operation_context=authority.parent.parent_context,
                ) as mutation:
                    return mutation.guided.fail(
                        failure_code=failure_code,
                        actor=actor,
                        failure_audit_cohort=GuidedFailureAuditCohort.empty(),
                        unproducible_output_fields=(),
                        failure_diagnostics=failure_diagnostics,
                    )

        return cast("GuidedOperationFailed", await self._run_sync(_sync))

    async def fail_guided_operation_with_audit(
        self,
        command: GuidedOperationFailureCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> GuidedOperationFailed:
        """Atomically persist sanitized evidence and settle one closed failure."""

        if type(command) is not GuidedOperationFailureCommand:
            raise TypeError("command must be an exact GuidedOperationFailureCommand")
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=(),
            payload_store=None,
        )
        sid = str(command.fence.session_id)
        now = self._now()

        def _sync() -> GuidedOperationFailed:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                audit_records: tuple[ChatMessageRecord, ...] = ()
                if audit_rows:
                    lineage = GuidedFailureAuditLineage.from_authority(
                        session_id=command.fence.session_id,
                        operation_id=command.fence.operation_id,
                        attempt=command.fence.attempt,
                        request_hash=operation["request_hash"],
                    )
                    bound_audit_rows = bind_guided_failure_audit_rows(audit_rows, lineage=lineage)
                    current_state_id = conn.execute(
                        select(composition_states_table.c.id)
                        .where(composition_states_table.c.session_id == sid)
                        .order_by(desc(composition_states_table.c.version))
                        .limit(1)
                    ).scalar_one_or_none()
                    sequence_no = self._reserve_sequence_range(conn, sid, count=len(bound_audit_rows))
                    audit_records = self._insert_prepared_guided_audit_rows_on_connection(
                        conn,
                        session_id=sid,
                        composition_state_id=UUID(current_state_id) if current_state_id is not None else None,
                        audit_rows=bound_audit_rows,
                        sequence_no=sequence_no,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                    conn.execute(update(sessions_table).where(sessions_table.c.id == sid).values(updated_at=now))
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    return mutation.guided.fail(
                        failure_code=command.failure_code,
                        actor=command.actor,
                        failure_audit_cohort=GuidedFailureAuditCohort.from_records(audit_records),
                        unproducible_output_fields=command.unproducible_output_fields,
                    )

        return cast(
            "GuidedOperationFailed",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )

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
        self._require_session_write_authority_on_connection(conn, session_operation_context, session_id=session_id)
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
        client_request_id: str,
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
                client_request_id=client_request_id,
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
        # An active guided pair that cannot bind would re-raise on every read
        # of this row; refuse it here, under the lock, before it becomes the tip.
        assert_guided_custody_persistable(deep_thaw(state.sources), deep_thaw(state.composer_meta))
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
                            state_id = self._insert_checkpoint_preserving_guided_proposal(
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
    ) -> AuditOutcome:
        """Async dispatcher for :meth:`persist_compose_turn`.

        Bridges to the sync primitive via ``_run_sync``, which dispatches
        to a worker thread. A running worker is shielded from caller
        cancellation: a ``CancelledError`` raised in the awaiter does NOT
        interrupt the in-flight sync transaction (see
        ``elspeth.web.async_workers.run_sync_in_worker``). A submission the
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
        return cast(
            AuditOutcome,
            await self._run_sync(
                self.persist_compose_turn,
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
            ),
        )

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
    ) -> SessionRecord:
        """Update a session title under the caller's COMPOSE operation and return the refreshed record."""
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        sid = str(session_id)
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

        row = await self._run_sync(_sync)
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
                            select(guided_operations_table.c.operation_id).where(
                                guided_operations_table.c.kind == "session_fork",
                                guided_operations_table.c.status == "completed",
                                guided_operations_table.c.result_session_id == sessions_table.c.id,
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
                )
                await run_sync_in_worker(
                    retire_archive_quarantine,
                    data_dir,
                    identity,
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
        supersedes_proposal_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord:
        """Atomically create one canonical pipeline row + bound event."""
        if type(plan) is not PipelinePlanResult:
            raise TypeError("plan must be an exact PipelinePlanResult")

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
        supplied_redacted_arguments = deep_thaw(arguments_redacted_json)
        if supplied_redacted_arguments != expected_redacted_arguments:
            raise AuditIntegrityError("pipeline proposal redacted arguments do not match the manifest projection")
        arguments_redacted_json = supplied_redacted_arguments
        proposal = plan.proposal
        if proposal.surface in {PlannerSurface.FREEFORM, PlannerSurface.GUIDED_FULL} and proposal.reviewed_anchor_hash != stable_hash(
            {"schema": "guided.reviewed-anchors.v1", "facts": {}}
        ):
            raise AuditIntegrityError("freeform and guided-full pipeline proposals require empty reviewed facts")
        if (supersedes_proposal_id is None) != (proposal.supersedes_draft_hash is None):
            raise AuditIntegrityError("superseded proposal id and draft hash must be supplied together")
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
            arguments_redacted_json=arguments_redacted_json,
            supersedes_proposal_id=supersedes_proposal_id,
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
                return transaction.composer.create_pipeline_composition_proposal(
                    proposal_id=proposal_id,
                    event_id=event_id,
                    plan=plan,
                    summary=summary,
                    rationale=rationale,
                    affects=affects,
                    arguments_redacted_json=arguments_redacted_json,
                    actor=actor,
                    normalized_provenance=normalized,
                    payload=payload,
                    user_message_id=user_message_id,
                    supersedes_proposal_id=supersedes_proposal_id,
                )

        record = cast(CompositionProposalRecord, await self._run_sync(_sync))
        _PIPELINE_PLANNER_COUNTER.add(1, {"surface": proposal.surface.value, "result": "proposal_created"})
        _PIPELINE_CUSTODY_COUNTER.add(1, {"surface": proposal.surface.value, "result": plan.custody_result})
        return record

    async def get_authoritative_pipeline_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        reviewed_facts: Mapping[str, Any],
    ) -> AuthoritativePipelineProposal:
        """Load one canonical pipeline authority, rejecting current tool proposals."""
        authority = await self.get_authoritative_composition_proposal(
            session_id=session_id,
            proposal_id=proposal_id,
            reviewed_facts=reviewed_facts,
        )
        if authority.pipeline is None:
            raise ValueError("proposal uses the current tool-proposal lifecycle contract")
        return authority.pipeline

    async def get_authoritative_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        reviewed_facts: Mapping[str, Any] | None,
    ) -> AuthoritativeCompositionProposal:
        """Load exactly one row and one event, then classify without fallback."""
        sid = str(session_id)
        pid = str(proposal_id)

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
                    conn=conn,
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=reviewed_facts,
                )
                if authority.pipeline is not None:
                    _verify_pipeline_lifecycle_authority(
                        conn,
                        service=self,
                        authority=authority.pipeline,
                    )
                return authority

        return cast(AuthoritativeCompositionProposal, await self._run_sync(_sync))

    async def settle_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        reviewed_facts: Mapping[str, Any],
        state: CompositionStateData,
        candidate_content_hash: str,
        executor_content_hash: str,
        final_composer_metadata: Mapping[str, Any] | None,
        dispatch: PipelineDispatchAuditBinding,
        actor: str,
        transition_assistant: TransitionAssistantDraft | None = None,
        required_trust_mode: ComposerTrustMode | None = None,
        require_transition_consumed: bool = True,
        session_operation_context: SessionOperationContext,
    ) -> PipelineProposalSettlementResult:
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
        if type(dispatch) is not PipelineDispatchAuditBinding:
            raise TypeError("dispatch must be an exact PipelineDispatchAuditBinding")
        state_content_hash = _composition_state_data_content_hash(state)
        if candidate_content_hash != executor_content_hash or candidate_content_hash != state_content_hash:
            raise AuditIntegrityError("pipeline candidate/executor/state content hash mismatch")
        settled_state = replace(state, composer_meta=final_composer_metadata)
        if type(require_transition_consumed) is not bool:
            raise TypeError("require_transition_consumed must be an exact bool")
        if transition_assistant is not None:
            if type(transition_assistant) is not TransitionAssistantDraft:
                raise TypeError("transition_assistant must be an exact TransitionAssistantDraft")
            if require_transition_consumed:
                composer_meta = deep_thaw(settled_state.composer_meta)
                guided_session = (
                    composer_meta["guided_session"] if type(composer_meta) is dict and "guided_session" in composer_meta else None
                )
                if (
                    type(guided_session) is not dict
                    or "transition_consumed" not in guided_session
                    or guided_session["transition_consumed"] is not True
                ):
                    raise AuditIntegrityError("transition assistant requires guided_session.transition_consumed=true")
        sid = str(session_id)
        pid = str(proposal_id)
        operation_kind = self._pipeline_settlement_operation_kind(session_operation_context)

        def _sync() -> tuple[PipelineProposalSettlementResult | TrustModeAutoCommitRevokedError, bool]:
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
                    conn=conn,
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=reviewed_facts,
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
                    expected_terminal = _pipeline_accepted_payload(
                        authority=authority,
                        state_id=str(committed_record.id),
                        state_content_hash=committed_hash,
                        final_composer_metadata=final_composer_metadata,
                        dispatch=dispatch,
                    )
                    if terminal_rows[0].payload != expected_terminal:
                        raise AuditIntegrityError("pipeline exact retry terminal binding mismatch")
                    transition_message = None
                    if transition_assistant is not None:
                        message_rows = conn.execute(
                            select(chat_messages_table)
                            .where(chat_messages_table.c.session_id == sid)
                            .where(chat_messages_table.c.role == "assistant")
                            .where(chat_messages_table.c.composition_state_id == str(committed_record.id))
                            .where(chat_messages_table.c.writer_principal == "compose_loop")
                        ).fetchall()
                        matching_messages = [
                            self._row_to_chat_message_record(message_row)
                            for message_row in message_rows
                            if message_row.content == transition_assistant.content
                            and message_row.raw_content == transition_assistant.raw_content
                        ]
                        if len(matching_messages) > 1:
                            raise AuditIntegrityError("committed transition assistant exact retry is ambiguous")
                        if matching_messages:
                            transition_message = matching_messages[0]
                        else:
                            transition_message = self._insert_transition_assistant(
                                conn,
                                session_id=sid,
                                state_id=str(committed_record.id),
                                content=transition_assistant.content,
                                raw_content=transition_assistant.raw_content,
                                created_at=now,
                                session_operation_context=session_operation_context,
                            )
                    return (
                        PipelineProposalSettlementResult(
                            proposal=replace(authority.row, pipeline_metadata=_pipeline_public_metadata(authority)),
                            state=committed_record,
                            transition_message=transition_message,
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
                )
                event_id = str(uuid.uuid4())
                terminal_payload = _pipeline_accepted_payload(
                    authority=authority,
                    state_id=state_id,
                    state_content_hash=state_content_hash,
                    final_composer_metadata=final_composer_metadata,
                    dispatch=dispatch,
                )
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
                state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .where(composition_states_table.c.id == state_id)
                ).one()
                transition_message = None
                if transition_assistant is not None:
                    transition_message = self._insert_transition_assistant(
                        conn,
                        session_id=sid,
                        state_id=state_id,
                        content=transition_assistant.content,
                        raw_content=transition_assistant.raw_content,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                return (
                    PipelineProposalSettlementResult(
                        proposal=replace(
                            _proposal_record_from_row(settled_row),
                            pipeline_metadata=_pipeline_public_metadata(authority),
                        ),
                        state=self._row_to_state_record(state_row),
                        transition_message=transition_message,
                    ),
                    True,
                )

        result, transitioned = cast(
            tuple[PipelineProposalSettlementResult | TrustModeAutoCommitRevokedError, bool],
            await self._run_sync(_sync),
        )
        if type(result) is TrustModeAutoCommitRevokedError:
            raise result
        settled_result = cast(PipelineProposalSettlementResult, result)
        if transitioned:
            assert settled_result.proposal.pipeline_metadata is not None
            _PIPELINE_SETTLEMENT_COUNTER.add(
                1,
                {"surface": settled_result.proposal.pipeline_metadata.surface, "result": "accepted"},
            )
        return settled_result

    async def get_pipeline_dispatch_recovery(
        self,
        *,
        authority: AuthoritativePipelineProposal,
    ) -> PipelineDispatchRecovery | None:
        """Return one content-bound durable dispatch for pending recovery."""
        if type(authority) is not AuthoritativePipelineProposal:
            raise TypeError("authority must be an exact AuthoritativePipelineProposal")

        def _sync() -> PipelineDispatchRecovery | None:
            with self._engine.begin() as conn:
                return self._pipeline_dispatch_recovery_on_connection(conn, authority=authority)

        return cast(PipelineDispatchRecovery | None, await self._run_sync(_sync))

    async def reject_pipeline_composition_proposal(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        draft_hash: str,
        reviewed_facts: Mapping[str, Any] | None,
        reason: PipelineProposalRejectionReason,
        dispatch: PipelineDispatchAuditBinding | None,
        actor: str,
        session_operation_context: SessionOperationContext,
    ) -> CompositionProposalRecord:
        """Atomically terminalise a pipeline proposal with a closed reason."""
        reason = _validated_pipeline_rejection_reason(reason)
        if reason == "candidate_executor_mismatch" and dispatch is None:
            raise AuditIntegrityError("candidate/executor mismatch rejection requires dispatch evidence")
        if dispatch is not None and type(dispatch) is not PipelineDispatchAuditBinding:
            raise TypeError("dispatch must be an exact PipelineDispatchAuditBinding or None")
        sid = str(session_id)
        pid = str(proposal_id)
        operation_kind = self._pipeline_settlement_operation_kind(session_operation_context)

        def _sync() -> tuple[CompositionProposalRecord, bool]:
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
                    conn=conn,
                    row=_proposal_record_from_row(row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=reviewed_facts,
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
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
                    ):
                        raise AuditIntegrityError("rejected pipeline proposal terminal binding mismatch")
                    return replace(authority.row, pipeline_metadata=_pipeline_public_metadata(authority)), False
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
                return (
                    replace(
                        _proposal_record_from_row(updated_row),
                        pipeline_metadata=_pipeline_public_metadata(authority),
                    ),
                    True,
                )

        result, transitioned = cast(tuple[CompositionProposalRecord, bool], await self._run_sync(_sync))
        if transitioned:
            assert result.pipeline_metadata is not None
            _PIPELINE_SETTLEMENT_COUNTER.add(
                1,
                {"surface": result.pipeline_metadata.surface, "result": reason},
            )
        return result

    async def list_composition_proposals(
        self,
        session_id: UUID,
        *,
        status: ProposalLifecycleStatus | None = None,
    ) -> list[CompositionProposalRecord]:
        """List composer proposals for a session in creation order."""
        sid = str(session_id)

        def _sync() -> list[CompositionProposalRecord]:
            stmt = select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)
            if status is not None:
                stmt = stmt.where(composition_proposals_table.c.status == status)
            stmt = stmt.order_by(composition_proposals_table.c.created_at)
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
                records = [_proposal_record_from_row(row) for row in rows]
                if not records:
                    return records
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
                        conn=conn,
                        row=record,
                        creation_event=_proposal_event_record_from_row(events[0]),
                        reviewed_facts=None,
                    )
                    if authority.pipeline is not None:
                        _verify_pipeline_lifecycle_authority(
                            conn,
                            service=self,
                            authority=authority.pipeline,
                        )
                    metadata = _pipeline_public_metadata(authority.pipeline) if authority.pipeline is not None else None
                    enriched.append(replace(record, pipeline_metadata=metadata))
                return enriched

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
        principal_user_id, plugin_snapshot = await self._session_principal_context(sid)
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
            )
            if snapshot_config is not None
            else None
        )
        validator = _SessionPendingInterpretationValidator(
            profile_aware=self._plugin_snapshot_factory is not None,
            plugin_snapshot=plugin_snapshot,
            profile_registry=self._operator_profile_registry,
            catalog=self._catalog,
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
                        plugin_snapshot=plugin_snapshot,
                        session_id=sid,
                        user_id=principal_user_id,
                    )
                else:
                    patched_validation = self._validate_patched_composition_state(
                        state_from_record(patched_state_record),
                        plugin_snapshot=plugin_snapshot,
                        session_id=sid,
                        user_id=principal_user_id,
                        inline_blob_snapshot=inline_blob_snapshot,
                    )
                raw_validation_errors = [
                    CompositionValidationError(message=error.message, error_code=error.error_code, component=error.component)
                    for error in patched_validation.errors
                ] or None
                patched_validation_errors = validation_errors_for_composer_surface(
                    composer_meta=state_record.composer_meta,
                    is_valid=patched_validation.is_valid,
                    validation_errors=raw_validation_errors,
                )

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

    @staticmethod
    def _existing_message_ingress_result(
        conn: Connection,
        *,
        session_id: str,
        client_request_id: UUID,
        content: str,
        requested_state_id: str | None,
    ) -> MessageIngressAccepted | MessageIngressConflict | None:
        receipt = conn.execute(
            select(message_ingress_receipts_table).where(
                message_ingress_receipts_table.c.session_id == session_id,
                message_ingress_receipts_table.c.client_request_id == str(client_request_id),
            )
        ).one_or_none()
        if receipt is None:
            return None
        accepted_message = conn.execute(
            select(chat_messages_table).where(
                chat_messages_table.c.session_id == session_id,
                chat_messages_table.c.id == receipt.user_message_id,
            )
        ).one_or_none()
        if accepted_message is None or accepted_message.role != "user" or accepted_message.writer_principal != "route_user_message":
            raise AuditIntegrityError("Tier 1: message ingress receipt does not reference a route-owned user row")
        result_type = (
            MessageIngressAccepted
            if accepted_message.content == content and receipt.requested_state_id == requested_state_id
            else MessageIngressConflict
        )
        return result_type(client_request_id=client_request_id, user_message_id=UUID(receipt.user_message_id))

    async def lookup_message_ingress(
        self,
        session_id: UUID,
        *,
        client_request_id: UUID,
        content: str,
        requested_state_id: UUID | None,
        session_operation_context: SessionOperationContext,
    ) -> MessageIngressAccepted | MessageIngressConflict | None:
        """Read prior acceptance under the compose fence before state preflight.

        A missing receipt remains provisional; the atomic insert checks again.
        """
        sid = str(session_id)
        original_state_id = str(requested_state_id) if requested_state_id is not None else None

        def _sync() -> MessageIngressAccepted | MessageIngressConflict | None:
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
                return self._existing_message_ingress_result(
                    conn,
                    session_id=sid,
                    client_request_id=client_request_id,
                    content=content,
                    requested_state_id=original_state_id,
                )

        return cast(MessageIngressAccepted | MessageIngressConflict | None, await self._run_sync(_sync))

    async def add_message_with_transcript(
        self,
        session_id: UUID,
        role: ChatMessageRole,
        content: str,
        *,
        client_request_id: UUID,
        requested_state_id: UUID | None,
        writer_principal: ChatMessageWriterPrincipal,
        tool_calls: Sequence[Mapping[str, Any]] | None = None,
        composition_state_id: UUID | None = None,
        raw_content: str | None = None,
        tool_call_id: str | None = None,
        parent_assistant_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
    ) -> MessageIngressFresh | MessageIngressAccepted | MessageIngressConflict:
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
        ``sessions.updated_at`` bump) plus :meth:`get_messages`'s
        fail-closed guided-failure cohort verification, which runs over
        the SAME rows the transcript is built from (after commit — the
        insert stays durable even when verification fails, so "your
        message was saved" remains true for the caller).

        A same-session receipt binds the client key to the original content
        and nullable requested state before sequence allocation. An exact
        duplicate is an acceptance receipt, never a composition replay.
        """
        if role != "user" or writer_principal != "route_user_message":
            raise ValueError("message ingress requires a route-owned user message")
        if raw_content is not None or tool_calls is not None or tool_call_id is not None or parent_assistant_id is not None:
            raise ValueError("message ingress user row cannot carry provider or tool metadata")
        now = self._now()
        sid = str(session_id)
        csid = str(composition_state_id) if composition_state_id else None
        requested_sid = str(requested_state_id) if requested_state_id else None
        request_id = str(client_request_id)
        pid = str(parent_assistant_id) if parent_assistant_id else None
        msg_id_holder: dict[str, str] = {}

        def _sync() -> tuple[Sequence[Any], Sequence[Any]] | MessageIngressAccepted | MessageIngressConflict:
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
                existing = self._existing_message_ingress_result(
                    conn,
                    session_id=sid,
                    client_request_id=client_request_id,
                    content=content,
                    requested_state_id=requested_sid,
                )
                if existing is not None:
                    return existing
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
                self._insert_message_ingress_receipt(
                    conn,
                    session_id=sid,
                    client_request_id=request_id,
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
                failed_event_rows = conn.execute(
                    select(
                        guided_operation_events_table.c.session_id,
                        guided_operation_events_table.c.operation_id,
                        guided_operation_events_table.c.attempt,
                        guided_operation_events_table.c.request_hash,
                        guided_operation_events_table.c.failure_audit_cohort,
                    )
                    .where(guided_operation_events_table.c.session_id == sid)
                    .where(guided_operation_events_table.c.event_kind == "failed")
                ).fetchall()
                return message_rows, failed_event_rows

        sync_result = await self._run_sync(_sync)
        if isinstance(sync_result, (MessageIngressAccepted, MessageIngressConflict)):
            return sync_result
        message_rows, failed_event_rows = sync_result
        # Pure verifier over the same rows the transcript is built from —
        # parity with get_messages' fail-closed posture. Runs after commit,
        # so a poisoned cohort rejects the READ, not the durable write.
        self._verify_guided_failure_audit_cohort(message_rows, failed_event_rows)
        transcript = [self._row_to_chat_message_record(row) for row in message_rows]
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
        message = replace(transcript[-1], client_request_id=client_request_id)
        transcript[-1] = message
        return MessageIngressFresh(client_request_id=client_request_id, message=message, transcript=tuple(transcript))

    def _verify_guided_failure_audit_cohort(
        self,
        message_rows: Sequence[Any],
        failed_event_rows: Sequence[Any],
    ) -> None:
        """Fail closed unless every failed event commits its exact evidence cohort."""

        event_commitments: dict[GuidedFailureAuditLineage, list[GuidedFailureAuditCohort]] = {}
        for event in failed_event_rows:
            try:
                lineage = GuidedFailureAuditLineage.from_authority(
                    session_id=UUID(event.session_id),
                    operation_id=event.operation_id,
                    attempt=event.attempt,
                    request_hash=event.request_hash,
                )
            except (TypeError, ValueError, AuditIntegrityError) as exc:
                raise AuditIntegrityError("guided failure audit lineage has malformed terminal-event authority") from exc
            try:
                commitment = GuidedFailureAuditCohort.from_envelope(event.failure_audit_cohort)
            except (TypeError, ValueError, AuditIntegrityError) as exc:
                raise AuditIntegrityError("guided failure audit cohort has malformed terminal-event commitment") from exc
            if lineage not in event_commitments:
                event_commitments[lineage] = []
            event_commitments[lineage].append(commitment)

        records_by_lineage: dict[GuidedFailureAuditLineage, list[ChatMessageRecord]] = {lineage: [] for lineage in event_commitments}
        for row in message_rows:
            try:
                content = json.loads(row.content)
            except (TypeError, ValueError):
                content = None
            content_has_lineage = type(content) is dict and GUIDED_FAILURE_AUDIT_LINEAGE_KEY in content
            tool_calls = row.tool_calls
            envelope_has_lineage = type(tool_calls) is list and any(
                type(envelope) is dict and GUIDED_FAILURE_AUDIT_LINEAGE_KEY in envelope for envelope in tool_calls
            )
            if not content_has_lineage and not envelope_has_lineage:
                continue
            if (
                row.role != "audit"
                or row.writer_principal != "compose_loop"
                or type(content) is not dict
                or type(tool_calls) is not list
                or len(tool_calls) != 1
                or type(tool_calls[0]) is not dict
                or GUIDED_FAILURE_AUDIT_LINEAGE_KEY not in content
                or GUIDED_FAILURE_AUDIT_LINEAGE_KEY not in tool_calls[0]
            ):
                raise AuditIntegrityError("guided failure audit lineage is partial or attached to a malformed row")
            content_lineage = GuidedFailureAuditLineage.from_envelope(content[GUIDED_FAILURE_AUDIT_LINEAGE_KEY])
            envelope_lineage = GuidedFailureAuditLineage.from_envelope(tool_calls[0][GUIDED_FAILURE_AUDIT_LINEAGE_KEY])
            if content_lineage != envelope_lineage:
                raise AuditIntegrityError("guided failure audit lineage content and envelope disagree")
            if content_lineage.session_id != UUID(row.session_id):
                raise AuditIntegrityError("guided failure audit lineage names a different session")
            if content_lineage not in event_commitments or len(event_commitments[content_lineage]) != 1:
                raise AuditIntegrityError(
                    "guided failure audit lineage has absent or ambiguous terminal-event authority; "
                    "guided failure audit cohort authority is not exact"
                )
            records_by_lineage[content_lineage].append(self._row_to_chat_message_record(row))

        for lineage, commitments in event_commitments.items():
            if len(commitments) != 1:
                raise AuditIntegrityError(
                    "guided failure audit lineage has ambiguous terminal-event authority; "
                    "guided failure audit cohort authority is not exact"
                )
            records = tuple(records_by_lineage[lineage])
            actual = GuidedFailureAuditCohort.from_records(records)
            if actual != commitments[0]:
                raise AuditIntegrityError("guided failure audit cohort does not match the exact durable evidence rows")

    def _row_to_chat_message_record(self, row: Any, *, client_request_id: str | None = None) -> ChatMessageRecord:
        if client_request_id is not None and (row.role != "user" or row.writer_principal != "route_user_message"):
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
            client_request_id=UUID(client_request_id) if client_request_id is not None else None,
        )

    async def get_messages(
        self,
        session_id: UUID,
        limit: int | None = 100,
        offset: int = 0,
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

        def _sync() -> tuple[Sequence[Any], Sequence[Any], Sequence[Any]]:
            with self._engine.connect() as conn:
                message_rows = conn.execute(
                    select(chat_messages_table, message_ingress_receipts_table.c.client_request_id)
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
                verification_message_rows = conn.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == str(session_id))
                    .order_by(chat_messages_table.c.sequence_no)
                ).fetchall()
                failed_event_rows = conn.execute(
                    select(
                        guided_operation_events_table.c.session_id,
                        guided_operation_events_table.c.operation_id,
                        guided_operation_events_table.c.attempt,
                        guided_operation_events_table.c.request_hash,
                        guided_operation_events_table.c.failure_audit_cohort,
                    )
                    .where(guided_operation_events_table.c.session_id == str(session_id))
                    .where(guided_operation_events_table.c.event_kind == "failed")
                ).fetchall()
                return message_rows, verification_message_rows, failed_event_rows

        rows, verification_message_rows, failed_event_rows = await self._run_sync(_sync)
        self._verify_guided_failure_audit_cohort(verification_message_rows, failed_event_rows)

        return [self._row_to_chat_message_record(row, client_request_id=row.client_request_id) for row in rows]

    async def get_verified_guided_root_intent(
        self,
        *,
        session_id: UUID,
        root_message_id: UUID,
    ) -> ChatMessageRecord:
        """Re-derive a live start hash before returning its private root row.

        Shares one derivation with :func:`_verify_guided_root_message_authority`:
        the operation row (``guided_start`` or ``guided_convert``) is the
        authority, its result checkpoint supplies the profile discriminator,
        and the canonical request hash is re-derived from the live content.
        """

        sid = str(session_id)
        mid = str(root_message_id)

        def _sync() -> ChatMessageRecord:
            with self._engine.begin() as conn:
                message_row = _verified_guided_root_message_row(
                    conn,
                    service=self,
                    session_id=sid,
                    message_id=mid,
                )
                return self._row_to_chat_message_record(message_row)

        return cast("ChatMessageRecord", await self._run_sync(_sync))

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

    def _insert_checkpoint_preserving_guided_proposal(
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
        actor: str = "compose_loop",
    ) -> str:
        """Insert an ordinary checkpoint and carry its pending proposal atomically.

        A checkpoint-only save may move lifecycle currency, never the reviewed
        proposal identity or composition. The locked prior row supplies every
        rebase assertion; the ordinary transition verifier re-derives authority.
        """
        from elspeth.web.composer.guided.planning import guided_private_reviewed_facts

        self._assert_session_write_lock_held(conn, session_id, caller="_insert_checkpoint_preserving_guided_proposal")
        database_now = self._guided_database_now(conn)
        self._require_session_operation_context_on_connection(
            conn,
            session_operation_context,
            session_id=session_id,
            expected_kind=operation_kind,
            now=database_now,
        )
        current_row = conn.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(desc(composition_states_table.c.version))
            .limit(1)
        ).one_or_none()
        prior = pending_guided_checkpoint(unwrap_state_column(current_row.composer_meta)) if current_row is not None else None
        candidate = pending_guided_checkpoint(state.composer_meta)
        checkpoint_id = state_id if state_id is not None else uuid.uuid4()
        rebase_plan: _GuidedPendingProposalRebasePlan | None = None
        if prior is not None or candidate is not None:
            current_record = self._row_to_state_record(current_row) if current_row is not None else None
            if prior is None or candidate != prior or current_record is None:
                raise AuditIntegrityError("an ordinary checkpoint cannot change a pending guided review")
            active = prior.active_proposal
            if active is None:  # pragma: no cover - pending_guided_checkpoint owns this
                raise AuditIntegrityError("pending guided checkpoint lost its proposal")
            current_content_hash = composition_content_hash(state_from_record(current_record))
            verified = _verify_guided_pending_proposal_transition(
                conn,
                context=_GuidedPendingProposalTransitionContext(
                    service=self,
                    session_id=session_id,
                    current_record=current_record,
                    prior_guided=prior,
                    candidate_guided=candidate,
                    expected_current_content_hash=current_content_hash,
                    checkpoint_state_id=checkpoint_id,
                    candidate_content_hash=_composition_state_data_content_hash(state),
                    settlement_origin=f"{operation_kind.value} checkpoint",
                ),
                invalidation=None,
                rebase=GuidedPendingProposalRebase(
                    proposal_id=active.proposal_id,
                    draft_hash=active.draft_hash,
                    reviewed_facts=guided_private_reviewed_facts(prior),
                    from_state_id=current_record.id,
                    composition_content_hash=current_content_hash,
                    reason=("ordinary_proposal_checkpoint" if operation_kind is SessionOperationKind.PROPOSAL else "compose_checkpoint"),
                ),
            )
            rebase_plan = verified.rebased
            live_confirmation = conn.execute(
                select(guided_operations_table.c.operation_id)
                .where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.proposal_id == str(active.proposal_id),
                    guided_operations_table.c.status == "in_progress",
                    guided_operations_table.c.lease_expires_at > database_now,
                )
                .limit(1)
            ).scalar_one_or_none()
            if live_confirmation is not None:
                raise StaleComposeStateError("a live guided confirmation owns this proposal checkpoint")

        inserted_id = self._insert_composition_state(
            conn,
            session_id=session_id,
            payload=StatePayload(data=state, derived_from_state_id=derived_from_state_id),
            provenance=provenance,
            created_at=created_at,
            state_id=str(checkpoint_id),
            session_operation_context=session_operation_context,
        )
        if rebase_plan is not None:
            _append_verified_guided_proposal_rebase(
                conn,
                authority=rebase_plan.authority,
                reason=rebase_plan.reason,
                actor=actor,
                created_at=created_at,
                to_state_id=checkpoint_id,
            )
        return inserted_id

    async def save_composition_state(
        self,
        session_id: UUID,
        state: CompositionStateData,
        *,
        provenance: CompositionStateProvenance,
        session_operation_context: SessionOperationContext,
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
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if (
            session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
            or session_operation_context.fence.session_id != sid
        ):
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        # An active guided pair that cannot bind would re-raise on every read of
        # this row; refuse it before it becomes the tip, exactly as
        # ``_insert_composition_state`` does under the session write lock.
        assert_guided_custody_persistable(deep_thaw(state.sources), deep_thaw(state.composer_meta))
        if pending_guided_checkpoint(state.composer_meta) is not None:

            def _sync_guided_checkpoint() -> CompositionStateRecord:
                with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                    inserted_id = self._insert_checkpoint_preserving_guided_proposal(
                        conn,
                        session_id=sid,
                        state=state,
                        provenance=provenance,
                        created_at=now,
                        state_id=state_id,
                        session_operation_context=session_operation_context,
                    )
                    row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == inserted_id)).one()
                    return self._row_to_state_record(row)

            return cast(CompositionStateRecord, await self._run_sync(_sync_guided_checkpoint))
        creation = SessionCompositionStateCreation(
            id=state_id,
            data=state,
            provenance=provenance,
            created_at=now,
            derived_from_state_id=None,
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
                database_now = self._guided_database_now(conn)
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=SessionOperationKind.COMPOSE,
                    now=database_now,
                )
                self._insert_checkpoint_preserving_guided_proposal(
                    conn,
                    session_id=sid,
                    state=state,
                    provenance=provenance,
                    created_at=now,
                    state_id=state_id,
                    session_operation_context=session_operation_context,
                )
                # The prepared packages are executed by the SAME reviewed
                # interpretation writer the guided settlement uses
                # (``_GuidedSessionMutationTransaction.interpretations``), built
                # here without a guided fence because this cohort settles under
                # ordinary COMPOSE authority. ``_insert_composition_state`` is
                # kept rather than ``composition_states.append_state`` because
                # only it carries the custody assert and the dead-site
                # supersession.
                interpretation_state = _RepositoryMutationState(
                    conn,
                    session_id=sid,
                    database_now=database_now,
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

    async def commit_transition_response(
        self,
        *,
        session_id: UUID,
        expected_current_state_id: UUID | None,
        state: CompositionStateData,
        assistant_content: str,
        raw_content: str | None,
        session_operation_context: SessionOperationContext,
    ) -> TransitionResponseSettlement:
        """Persist transition consumption and its assistant response atomically."""
        composer_meta = deep_thaw(state.composer_meta)
        guided_session = composer_meta["guided_session"] if type(composer_meta) is dict and "guided_session" in composer_meta else None
        transition_consumed = (
            guided_session["transition_consumed"] if type(guided_session) is dict and "transition_consumed" in guided_session else None
        )
        if type(guided_session) is not dict or transition_consumed is not True:
            raise AuditIntegrityError("commit_transition_response requires guided_session.transition_consumed=true")

        return await self.commit_composition_response(
            session_id=session_id,
            expected_current_state_id=expected_current_state_id,
            state=state,
            assistant_content=assistant_content,
            raw_content=raw_content,
            session_operation_context=session_operation_context,
        )

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

                state_id = self._insert_checkpoint_preserving_guided_proposal(
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
    ) -> CompositionStateRecord | None:
        """Return the highest-version state for a session, or None."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == str(session_id))
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).fetchone()

        row = await self._run_sync(_sync)

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
                    now=self._guided_database_now(conn),
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
        self, *, session_operation_context: SessionOperationContext, source: TokenUsageSource, run_id: UUID | None = None
    ) -> ProviderAttempt:
        """Admit a provider dispatch and retain pending evidence before sending it."""
        try:
            return cast(
                "ProviderAttempt",
                await self._run_sync(
                    self._begin_provider_attempt_sync,
                    session_operation_context=session_operation_context,
                    source=source,
                    run_id=run_id,
                ),
            )
        except ChargeableAdmissionRefused as exc:
            await self._run_sync(self._record_provider_attempt_quota_refusal_sync, session_operation_context, source, exc)
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

    async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
        """Persist actual terminal call evidence and settle its ledger atomically."""
        from elspeth.web.composer.audit import llm_call_audit_envelope

        if call.call_id is None:
            raise AuditIntegrityError("A provider checkpoint requires its pending attempt identity")
        await self.add_messages_atomic(
            UUID(session_operation_context.fence.session_id),
            (AuditMessageDraft(role="audit", content="Provider call result recorded.", tool_calls=(llm_call_audit_envelope(call),)),),
            writer_principal="compose_loop",
            session_operation_context=session_operation_context,
        )

    async def cancel_undispatched_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, requested_model: str
    ) -> None:
        """Close a COMPOSE intent only when its owner proved SDK entry never occurred.

        A distinct audit message, zero-use ledger entry, and settlement commit
        together under the original live fence. This cannot reconcile a past
        pending attempt or a provider call whose outcome is unknown.
        """
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise ValueError("Undispatched cancellation requires COMPOSE authority")
        sid = session_operation_context.fence.session_id

        def _sync() -> None:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn, session_operation_context, session_id=sid, expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn)
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

        await self._run_sync(_sync)

    async def settle_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        """Settle non-Composer provider evidence under its original session lease."""
        await self._run_sync(
            self._settle_provider_attempt_sync,
            session_operation_context=session_operation_context,
            attempt_id=attempt_id,
            entry=entry,
        )

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
                    conn, session_operation_context, session_id=sid, expected_kind=expected_kind, now=database_now(conn)
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

    async def get_state(self, state_id: UUID) -> CompositionStateRecord:
        """Fetch a composition state by its primary key. Raises ValueError if not found."""

        def _sync() -> Any:
            with self._engine.begin() as conn:
                return conn.execute(select(composition_states_table).where(composition_states_table.c.id == str(state_id))).fetchone()

        row = await self._run_sync(_sync)

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

    async def revert_state_for_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        state_id: UUID,
        expected_current_state_id: UUID,
        expected_current_state_version: int,
        actor: str,
        response_hash_factory: Callable[[CompositionStateRecord], str],
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord:
        """Copy one checkpoint and settle its retry operation atomically.

        The fence check, state copy, system audit message, replay locator, and
        response-domain hash all share one session lock and one database
        transaction.  In particular this is not implemented as a public
        ``require_fence`` followed by an unfenced state-copy helper: takeover
        between those calls would let a stale worker create a durable version.
        """

        sid = str(fence.session_id)
        target_state_id = str(state_id)
        now = self._now()

        def _sync() -> CompositionStateRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self.require_guided_operation_authority_on_connection(conn, fence, session_operation_context)
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=expected_current_state_id,
                    expected_state_version=expected_current_state_version,
                )
                prior_row = conn.execute(
                    select(composition_states_table).where(composition_states_table.c.id == target_state_id)
                ).one_or_none()
                # User-supplied state ids deliberately collapse absence and
                # cross-session ownership to the route's same 404 boundary.
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

                from elspeth.web.composer.guided.errors import InvariantError
                from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
                from elspeth.web.composer.guided.state_machine import GuidedSession
                from elspeth.web.sessions.guided_replay import GUIDED_REPLAY_META_KEY

                def _guided_checkpoint(row: Any, *, role: str) -> tuple[CompositionStateRecord, GuidedSession | None]:
                    checkpoint = self._row_to_state_record(row)
                    metadata = deep_thaw(checkpoint.composer_meta)
                    if metadata is None:
                        return checkpoint, None
                    if type(metadata) is not dict:
                        raise AuditIntegrityError(f"{role} checkpoint composer metadata is malformed")
                    if "guided_session" not in metadata or metadata["guided_session"] is None:
                        return checkpoint, None
                    try:
                        guided = GuidedSession.from_dict(metadata["guided_session"])
                    except (InvariantError, KeyError, TypeError, ValueError) as exc:
                        raise AuditIntegrityError(f"{role} checkpoint guided schema-10 authority is malformed") from exc
                    return checkpoint, guided

                target_record, target_guided = _guided_checkpoint(prior_row, role="target")
                current_record, current_guided = _guided_checkpoint(current_row, role="current")

                referenced_authorities: dict[UUID, AuthoritativePipelineProposal] = {}
                for role, checkpoint, guided in (
                    ("current", current_record, current_guided),
                    ("target", target_record, target_guided),
                ):
                    if guided is None:
                        continue
                    authority = _require_pending_guided_checkpoint_proposal_authority(
                        conn,
                        service=self,
                        session_id=sid,
                        checkpoint=checkpoint,
                        guided=guided,
                        role=role,
                    )
                    if authority is not None:
                        referenced_authorities[authority.row.id] = authority

                def _creation_event_for(proposal_id: UUID) -> ProposalEventRecord:
                    creation_rows = conn.execute(
                        select(proposal_events_table)
                        .where(proposal_events_table.c.session_id == sid)
                        .where(proposal_events_table.c.proposal_id == str(proposal_id))
                        .where(proposal_events_table.c.event_type == "proposal.created")
                    ).fetchall()
                    if len(creation_rows) != 1:
                        raise AuditIntegrityError("state revert pipeline proposal must have exactly one creation event")
                    return _proposal_event_record_from_row(creation_rows[0])

                pending_rows = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.tool_name == "set_pipeline")
                    .where(composition_proposals_table.c.status == "pending")
                    .order_by(composition_proposals_table.c.created_at, composition_proposals_table.c.id)
                ).fetchall()
                pending_authorities: list[AuthoritativePipelineProposal] = []
                for proposal_row in pending_rows:
                    proposal_id = UUID(proposal_row.id)
                    if proposal_id in referenced_authorities:
                        authority = referenced_authorities[proposal_id]
                    else:
                        authority = _restore_authoritative_pipeline_proposal(
                            conn=conn,
                            row=_proposal_record_from_row(proposal_row),
                            creation_event=_creation_event_for(proposal_id),
                            reviewed_facts=None,
                        )
                        _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                        if authority.proposal.surface is not PlannerSurface.FREEFORM:
                            raise AuditIntegrityError("state revert found a dangling pending guided pipeline proposal")
                    if authority.row.status != "pending":
                        raise AuditIntegrityError("state revert pending proposal query restored terminal authority")
                    pending_authorities.append(authority)

                # Verify the complete affected proposal set before the first
                # write.  A malformed ref, event, row, or reviewed-facts
                # binding therefore rolls the whole revert back untouched.
                for authority in pending_authorities:
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        mutation.composer.reject_pending_proposal(
                            authority=authority,
                            actor=actor,
                            created_at=now,
                            reason="superseded",
                        )

                reverted_composer_meta = deep_thaw(target_record.composer_meta)
                if type(reverted_composer_meta) is dict and GUIDED_REPLAY_META_KEY in reverted_composer_meta:
                    del reverted_composer_meta[GUIDED_REPLAY_META_KEY]
                if target_guided is not None:
                    assert type(reverted_composer_meta) is dict
                    rewinds_to_topology = target_guided.terminal is None and (
                        target_guided.active_proposal is not None
                        or target_guided.step in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE}
                    )
                    restored_guided = target_guided
                    if rewinds_to_topology:
                        unanswered = [index for index, turn in enumerate(target_guided.history) if turn.response_hash is None]
                        if len(unanswered) > 1 or (unanswered and unanswered[0] != len(target_guided.history) - 1):
                            raise AuditIntegrityError("state revert guided topology rewind has malformed unanswered history")
                        history = target_guided.history
                        if unanswered:
                            final_turn = history[-1]
                            if final_turn.step not in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE}:
                                raise AuditIntegrityError("state revert guided topology rewind found a non-topology unanswered turn")
                            legal_turn_type = (
                                TurnType.PROPOSE_PIPELINE if final_turn.step is GuidedStep.STEP_3_TRANSFORMS else TurnType.CONFIRM_WIRING
                            )
                            if target_guided.active_proposal is not None and final_turn.turn_type is not legal_turn_type:
                                raise AuditIntegrityError("state revert guided proposal ref lacks its unanswered authority turn")
                            history = history[:-1]
                        restored_guided = replace(
                            target_guided,
                            step=GuidedStep.STEP_3_TRANSFORMS,
                            history=history,
                            terminal=None,
                            transition_consumed=False,
                            active_proposal=None,
                            active_edit_target=None,
                        )
                    reverted_composer_meta["guided_session"] = restored_guided.to_dict()

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
                            composer_meta=reverted_composer_meta,
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
                response_hash = response_hash_factory(record)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.mark_session_updated(updated_at=now)
                    mutation.guided.bind(result_state_id=record.id)
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=record.id),
                        response_hash=response_hash,
                        actor=actor,
                    )
                return record

        return cast("CompositionStateRecord", await self._run_sync(_sync))

    @staticmethod
    def _require_guided_expected_current_state_on_connection(
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
            raise ValueError("expected guided current state id and version must be both present or both absent")
        if expected_state_id is not None and type(expected_state_id) is not UUID:
            raise ValueError("expected guided current state id must be a UUID")
        if expected_state_version is not None and (type(expected_state_version) is not int or expected_state_version < 1):
            raise ValueError("expected guided current state version must be a positive integer")
        current = conn.execute(
            select(composition_states_table.c.id, composition_states_table.c.version)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(desc(composition_states_table.c.version))
            .limit(1)
        ).one_or_none()
        if expected_state_id is None:
            if current is not None:
                raise GuidedOperationSettlementConflictError()
            return
        if current is None or current.id != str(expected_state_id) or current.version != expected_state_version:
            raise GuidedOperationSettlementConflictError()

    @staticmethod
    def _prepare_guided_audit_cohort(
        *,
        audit_evidence: GuidedAuditEvidence,
        payloads: tuple[PreparedGuidedJsonPayload, ...],
        payload_store: PayloadStore | None,
    ) -> tuple[PreparedGuidedAuditRow, ...]:
        """Validate one CAS-bound evidence cohort before opening SQL."""

        if type(audit_evidence) is not GuidedAuditEvidence:
            raise TypeError("audit_evidence must be exact GuidedAuditEvidence")
        if type(payloads) is not tuple or any(type(payload) is not PreparedGuidedJsonPayload for payload in payloads):
            raise TypeError("payloads must be an exact prepared-payload tuple")
        audit_rows = prepare_guided_audit_rows(
            invocations=audit_evidence.invocations,
            llm_calls=audit_evidence.llm_calls,
            chat_turns=audit_evidence.chat_turns,
            planner_attempts=audit_evidence.planner_attempts,
        )
        validate_guided_audit_payload_references(audit_rows, payloads)
        verify_guided_json_payloads(payload_store, payloads)
        return audit_rows

    def _insert_prepared_guided_audit_rows_on_connection(
        self,
        conn: Connection,
        *,
        session_id: str,
        composition_state_id: UUID | None,
        audit_rows: tuple[PreparedGuidedAuditRow, ...],
        sequence_no: int | None,
        created_at: datetime,
        session_operation_context: SessionOperationContext,
    ) -> tuple[ChatMessageRecord, ...]:
        """Insert a prevalidated guided evidence cohort in the caller's transaction."""

        self._assert_session_write_lock_held(
            conn,
            session_id,
            caller="_insert_prepared_guided_audit_rows_on_connection",
        )
        if audit_rows and sequence_no is None:
            raise AuditIntegrityError("Guided audit cohort has no reserved sequence")
        pending_envelopes = uncheckpointed_envelopes(conn, session_id=session_id, envelopes=tuple(row.envelope for row in audit_rows))
        pending_ids = {id(envelope) for envelope in pending_envelopes}
        audit_rows = tuple(row for row in audit_rows if id(row.envelope) in pending_ids)
        records: list[ChatMessageRecord] = []
        for audit_row in audit_rows:
            if sequence_no is None:  # pragma: no cover - guarded above
                raise AuditIntegrityError("Guided audit row has no reserved sequence")
            message_id = self._insert_chat_message(
                conn,
                session_id=session_id,
                role="audit",
                content=audit_row.content,
                raw_content=None,
                tool_calls=[deep_thaw(audit_row.envelope)],
                sequence_no=sequence_no,
                writer_principal="compose_loop",
                composition_state_id=str(composition_state_id) if composition_state_id is not None else None,
                tool_call_id=None,
                parent_assistant_id=None,
                created_at=created_at,
                session_operation_context=session_operation_context,
            )
            records.append(
                ChatMessageRecord(
                    id=UUID(message_id),
                    session_id=UUID(session_id),
                    role="audit",
                    content=audit_row.content,
                    raw_content=None,
                    tool_calls=[deep_thaw(audit_row.envelope)],
                    created_at=created_at,
                    sequence_no=sequence_no,
                    composition_state_id=composition_state_id,
                    writer_principal="compose_loop",
                )
            )
            sequence_no += 1
        entries = llm_call_usage_entries(tuple(audit_row.envelope for audit_row in audit_rows if audit_row.kind == "llm"))
        if entries:
            # Task I1 Composer adapter (guided): the calls this cohort audits are
            # charged in the transaction that makes their audit rows durable.
            record_token_usage_on_connection(
                conn, session_id=session_id, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
            )
        return tuple(records)

    async def seed_or_complete_guided_start_operation(
        self,
        fence: GuidedOperationFence,
        *,
        state: CompositionStateData,
        provenance: CompositionStateProvenance,
        actor: str,
        response_hash_factory: Callable[[CompositionStateRecord], str],
        payloads: tuple[PreparedGuidedJsonPayload, ...] = (),
        audit_evidence: GuidedAuditEvidence | None = None,
        originating_message: GuidedOriginatingUserMessageDraft | None = None,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> GuidedStartStateOutcome:
        """Atomically seed an empty session or settle its exact guided head.

        The response-hash callback is the guided-state validator for an
        existing head: it must fail closed when the record is freeform or
        otherwise cannot produce the strict start response. No generic
        integrity failure is interpreted as convergence.
        """

        settled_audit_evidence = audit_evidence if audit_evidence is not None else GuidedAuditEvidence()
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=settled_audit_evidence,
            payloads=payloads,
            payload_store=payload_store,
        )
        sid = str(fence.session_id)
        now = self._now()
        if originating_message is not None and type(originating_message) is not GuidedOriginatingUserMessageDraft:
            raise TypeError("originating_message must be an exact GuidedOriginatingUserMessageDraft or None")

        def _sync() -> GuidedStartStateOutcome:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self.require_guided_operation_authority_on_connection(conn, fence, session_operation_context)
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                if current_row is None:
                    state_id = self._insert_composition_state(
                        conn,
                        session_id=sid,
                        payload=StatePayload(data=state, derived_from_state_id=None),
                        provenance=provenance,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                    inserted_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == state_id)).one()
                    record = self._row_to_state_record(inserted_row)
                    row_count = len(audit_rows) + (1 if originating_message is not None else 0)
                    if row_count:
                        sequence_no = self._reserve_sequence_range(conn, sid, count=row_count)
                        if originating_message is not None:
                            self._insert_chat_message(
                                conn,
                                session_id=sid,
                                role="user",
                                content=originating_message.content,
                                raw_content=None,
                                tool_calls=None,
                                sequence_no=sequence_no,
                                writer_principal="route_user_message",
                                composition_state_id=state_id,
                                tool_call_id=None,
                                parent_assistant_id=None,
                                created_at=now,
                                message_id=str(originating_message.message_id),
                                session_operation_context=session_operation_context,
                            )
                            sequence_no += 1
                        self._insert_prepared_guided_audit_rows_on_connection(
                            conn,
                            session_id=sid,
                            composition_state_id=record.id,
                            audit_rows=audit_rows,
                            sequence_no=sequence_no,
                            created_at=now,
                            session_operation_context=session_operation_context,
                        )
                        with self._guided_session_mutation_transaction(
                            conn,
                            guided_fence=fence,
                            session_operation_context=session_operation_context,
                        ) as mutation:
                            mutation.guided.mark_session_updated(updated_at=now)
                    outcome: GuidedStartStateOutcome = GuidedStartStateSeeded(state=record)
                else:
                    record = self._row_to_state_record(current_row)
                    outcome = GuidedStartStateConverged(state=record)

                settled_root_message_id: UUID | None = None
                if originating_message is not None:
                    guided = state_from_record(record).guided_session
                    if guided is None:
                        raise AuditIntegrityError("guided start checkpoint has no guided session")
                    owns_root = guided.root_intent_message_id == str(originating_message.message_id)
                    if owns_root:
                        # This operation's OWN root: either this transaction
                        # wrote both the checkpoint and the row (seeded), or
                        # the head already IS this operation's checkpoint and
                        # already names the row (converged onto itself). Bind
                        # it, and hold it to exact content custody.
                        verified_root_message_id = str(originating_message.message_id)
                        settled_root_message_id = originating_message.message_id
                    elif type(outcome) is GuidedStartStateSeeded:
                        raise AuditIntegrityError("guided start checkpoint does not bind its exact root intent")
                    else:
                        # Converged onto a COMPETING start's head, which
                        # appeared between this route's read and this write.
                        # Nothing was written here, so the root row this
                        # request planned does not exist: the operation must
                        # not claim it, and must not claim the winner's either
                        # (two completed operations naming ONE root message is
                        # exactly the "ambiguous start-operation authority" the
                        # custody helper refuses). Verify the winner is rooted
                        # in the SAME goal — otherwise this request's goal
                        # would be silently answered with someone else's
                        # session, and the caller gets the ordinary
                        # stale-conflict 409 the route's pre-check path already
                        # returns for a mismatched winner.
                        if guided.root_intent_message_id is None:
                            raise GuidedOperationSettlementConflictError()
                        verified_root_message_id = guided.root_intent_message_id
                    message_row = conn.execute(
                        select(
                            chat_messages_table.c.session_id,
                            chat_messages_table.c.role,
                            chat_messages_table.c.content,
                            chat_messages_table.c.writer_principal,
                        ).where(chat_messages_table.c.id == verified_root_message_id)
                    ).one_or_none()
                    if (
                        message_row is None
                        or message_row.session_id != sid
                        or message_row.role != "user"
                        or message_row.writer_principal != "route_user_message"
                    ):
                        raise AuditIntegrityError("guided start root intent row failed custody verification")
                    if message_row.content != originating_message.content:
                        if owns_root:
                            raise AuditIntegrityError("guided start root intent row failed custody verification")
                        raise GuidedOperationSettlementConflictError()

                response_hash = response_hash_factory(record)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(
                        originating_message_id=settled_root_message_id,
                        result_state_id=record.id,
                    )
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=record.id),
                        response_hash=response_hash,
                        actor=actor,
                    )
                return outcome

        def _project_seeded_calls(outcome: GuidedStartStateOutcome) -> None:
            if type(outcome) is GuidedStartStateSeeded:
                record_settled_composer_provider_calls(
                    settled_audit_evidence.llm_calls,
                    surface="guided",
                )

        return await self._run_sync_with_post_commit_projection(
            _sync,
            project=_project_seeded_calls,
        )

    async def save_state_for_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        expected_current_state_id: UUID | None,
        expected_current_state_version: int | None,
        state: CompositionStateData,
        provenance: CompositionStateProvenance,
        actor: str,
        response_hash_factory: Callable[[CompositionStateRecord], str],
        system_message: str | None = None,
        payloads: tuple[PreparedGuidedJsonPayload, ...] = (),
        audit_evidence: GuidedAuditEvidence | None = None,
        originating_message: GuidedOriginatingUserMessageDraft | None = None,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord:
        """Persist one guided checkpoint and its replay settlement atomically.

        ``originating_message`` mirrors
        :meth:`seed_or_complete_guided_start_operation`: the goal a
        ``/guided/convert`` carries becomes the session's durable root intent
        row, written inside THIS transaction and bound to the operation, so a
        converted session's root has exactly the custody a started one does.
        Passing it without the checkpoint naming it — or with a row that fails
        session/role/content/writer custody — fails the settlement closed.
        """

        if system_message is not None and (type(system_message) is not str or not system_message):
            raise ValueError("guided operation system_message must be a non-empty string or None")
        if originating_message is not None and type(originating_message) is not GuidedOriginatingUserMessageDraft:
            raise TypeError("originating_message must be an exact GuidedOriginatingUserMessageDraft or None")
        settled_audit_evidence = audit_evidence if audit_evidence is not None else GuidedAuditEvidence()
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=settled_audit_evidence,
            payloads=payloads,
            payload_store=payload_store,
        )
        sid = str(fence.session_id)
        now = self._now()

        def _sync() -> CompositionStateRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self.require_guided_operation_authority_on_connection(conn, fence, session_operation_context)
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=expected_current_state_id,
                    expected_state_version=expected_current_state_version,
                )
                state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=state, derived_from_state_id=None),
                    provenance=provenance,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                state_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == state_id)).one()
                record = self._row_to_state_record(state_row)
                row_count = len(audit_rows) + (1 if system_message is not None else 0) + (1 if originating_message is not None else 0)
                sequence_no = self._reserve_sequence_range(conn, sid, count=row_count) if row_count else None
                if originating_message is not None:
                    if sequence_no is None:  # pragma: no cover - row_count counts this row
                        raise AuditIntegrityError("Guided root intent has no reserved sequence")
                    self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="user",
                        content=originating_message.content,
                        raw_content=None,
                        tool_calls=None,
                        sequence_no=sequence_no,
                        writer_principal="route_user_message",
                        composition_state_id=state_id,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=now,
                        message_id=str(originating_message.message_id),
                        session_operation_context=session_operation_context,
                    )
                    sequence_no += 1
                if system_message is not None:
                    if sequence_no is None:
                        raise AuditIntegrityError("Guided system message has no reserved sequence")
                    self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="system",
                        content=system_message,
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
                    sequence_no += 1
                if audit_rows:
                    self._insert_prepared_guided_audit_rows_on_connection(
                        conn,
                        session_id=sid,
                        composition_state_id=record.id,
                        audit_rows=audit_rows,
                        sequence_no=sequence_no,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                if row_count:
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        mutation.guided.mark_session_updated(updated_at=now)
                if originating_message is not None:
                    # Same binding check the start settlement performs: the
                    # checkpoint being persisted must NAME this root row, and
                    # the row must be exactly what was asked for. Without it a
                    # conversion could write a root row no checkpoint points at.
                    guided = state_from_record(record).guided_session
                    if guided is None or guided.root_intent_message_id != str(originating_message.message_id):
                        raise AuditIntegrityError("guided converted checkpoint does not bind its exact root intent")
                    root_row = conn.execute(
                        select(
                            chat_messages_table.c.session_id,
                            chat_messages_table.c.role,
                            chat_messages_table.c.content,
                            chat_messages_table.c.writer_principal,
                        ).where(chat_messages_table.c.id == str(originating_message.message_id))
                    ).one_or_none()
                    if (
                        root_row is None
                        or root_row.session_id != sid
                        or root_row.role != "user"
                        or root_row.content != originating_message.content
                        or root_row.writer_principal != "route_user_message"
                    ):
                        raise AuditIntegrityError("guided converted root intent row failed custody verification")
                response_hash = response_hash_factory(record)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(
                        originating_message_id=(originating_message.message_id if originating_message is not None else None),
                        result_state_id=record.id,
                    )
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=record.id),
                        response_hash=response_hash,
                        actor=actor,
                    )
                return record

        return cast(
            "CompositionStateRecord",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=settled_audit_evidence.llm_calls,
            ),
        )

    async def settle_guided_state_operation(
        self,
        command: GuidedStateOperationCommand,
        *,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> GuidedStateOperationSettlement:
        """Commit one RESPOND/CHAT state and its evidence under one live fence."""

        if type(command) is not GuidedStateOperationCommand:
            raise TypeError("command must be an exact GuidedStateOperationCommand")
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=command.payloads,
            payload_store=payload_store,
        )
        prepared_state = with_guided_response_descriptor(command.state, command.response)
        sid = str(command.fence.session_id)
        now = self._now()
        interpretation_commands: list[_PreparedPendingInterpretation] = []
        for draft in command.interpretations:
            prepared_interpretation = await self._prepare_or_create_pending_interpretation_event(
                session_id=command.fence.session_id,
                composition_state_id=command.state_id,
                affected_node_id=draft.affected_node_id,
                tool_call_id=draft.tool_call_id,
                user_term=draft.user_term,
                kind=draft.kind,
                llm_draft=draft.llm_draft,
                model_identifier=draft.model_identifier,
                model_version=draft.model_version,
                provider=draft.provider,
                composer_skill_hash=draft.composer_skill_hash,
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                session_operation_context=session_operation_context,
                created_at=now,
                _event_id=draft.event_id,
                _prepare_only=True,
                proposed_state=prepared_state,
            )
            if type(prepared_interpretation) is not _PreparedPendingInterpretation:
                raise AuditIntegrityError("guided interpretation preparation did not return an exact package")
            interpretation_commands.append(prepared_interpretation)

        def _sync() -> GuidedStateOperationSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != command.response.kind:
                    raise AuditIntegrityError("Guided response descriptor kind does not match the reserved operation")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                current_record: CompositionStateRecord | None = None
                if current_row is not None:
                    current_record = self._row_to_state_record(current_row)
                if command.expected_current_content_hash is not None:
                    if current_row is None:  # pragma: no cover - expected-current guard owns absence
                        raise AuditIntegrityError("Guided operation expected content hash without a current state")
                    if current_record is None:  # pragma: no cover - paired with current_row
                        raise AuditIntegrityError("Guided operation current state conversion failed")
                    if composition_content_hash(state_from_record(current_record)) != command.expected_current_content_hash:
                        raise AuditIntegrityError("Guided operation current state content changed before settlement")

                from elspeth.web.composer.guided.errors import InvariantError
                from elspeth.web.composer.guided.state_machine import GuidedSession

                def _guided_checkpoint(composer_meta: object, *, role: str) -> GuidedSession:
                    metadata = deep_thaw(composer_meta)
                    if (
                        type(metadata) is not dict
                        or "guided_session" not in metadata
                        or type(metadata["guided_session"]) is not dict
                        or ("ingress" in metadata and not _valid_compartment_ingress_metadata(metadata["ingress"]))
                        or ("chat_ingress_inputs" in metadata and not _valid_chat_ingress_inputs_metadata(metadata["chat_ingress_inputs"]))
                    ):
                        raise AuditIntegrityError(f"{role} deferred intent state has no exact guided checkpoint")
                    try:
                        return GuidedSession.from_dict(metadata["guided_session"])
                    except (InvariantError, KeyError, TypeError, ValueError) as exc:
                        raise AuditIntegrityError(f"{role} deferred intent guided checkpoint is malformed") from exc

                candidate_guided = _guided_checkpoint(command.state.composer_meta, role="candidate")
                prior_guided = (
                    GuidedSession.initial()
                    if current_row is None
                    else _guided_checkpoint(self._row_to_state_record(current_row).composer_meta, role="prior")
                )
                retained_deferred_intents = _verify_guided_deferred_intent_mutation(
                    conn,
                    session_id=sid,
                    command=command,
                    prior_guided=prior_guided,
                    candidate_guided=candidate_guided,
                )

                pending_proposal_authorities = _verify_guided_pending_proposal_transition(
                    conn,
                    context=_GuidedPendingProposalTransitionContext(
                        service=self,
                        session_id=sid,
                        current_record=current_record,
                        prior_guided=prior_guided,
                        candidate_guided=candidate_guided,
                        expected_current_content_hash=command.expected_current_content_hash,
                        checkpoint_state_id=command.state_id,
                        candidate_content_hash=_composition_state_data_content_hash(command.state),
                        settlement_origin=f"guided {command.response.kind} operation {command.fence.operation_id}",
                    ),
                    invalidation=command.invalidated_pending_proposal,
                    rebase=command.rebased_pending_proposal,
                )
                invalidated_authority = pending_proposal_authorities.invalidated
                rebased_authority = pending_proposal_authorities.rebased

                inserted_state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(
                        data=prepared_state,
                        derived_from_state_id=(
                            str(command.expected_current_state_id) if command.expected_current_state_id is not None else None
                        ),
                    ),
                    provenance=command.provenance,
                    created_at=now,
                    state_id=str(command.state_id),
                    session_operation_context=session_operation_context,
                )
                primary_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == inserted_state_id)).one()
                primary_state = self._row_to_state_record(primary_row)

                if invalidated_authority is not None:
                    if command.invalidated_pending_proposal is None:  # pragma: no cover - invalidation verifier owns this
                        raise AuditIntegrityError("guided proposal invalidation lost its command reason")
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=command.fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        mutation.composer.reject_pending_proposal(
                            authority=invalidated_authority,
                            actor=command.actor,
                            created_at=now,
                            reason=command.invalidated_pending_proposal.reason,
                        )

                if rebased_authority is not None:
                    # After the insert: ``composition_proposals.base_state_id``
                    # carries a foreign key onto ``composition_states``, so the
                    # checkpoint the base moves onto must already exist.
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=command.fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        _rebind_guided_pending_proposal(
                            conn,
                            mutation=mutation,
                            authority=rebased_authority.authority,
                            reason=rebased_authority.reason,
                            actor=command.actor,
                            created_at=now,
                            to_state_id=command.state_id,
                        )

                row_count = len(audit_rows) + (1 if command.originating_message is not None else 0)
                sequence_no = self._reserve_sequence_range(conn, sid, count=row_count) if row_count else None
                originating_record: ChatMessageRecord | None = None
                if command.originating_message is not None:
                    if sequence_no is None:
                        raise AuditIntegrityError("Guided originating message has no reserved sequence")
                    originating = command.originating_message
                    existing_origin = conn.execute(
                        select(
                            chat_messages_table.c.session_id,
                            chat_messages_table.c.role,
                            chat_messages_table.c.content,
                        ).where(chat_messages_table.c.id == str(originating.message_id))
                    ).one_or_none()
                    if existing_origin is not None:
                        raise AuditIntegrityError("Guided originating message id already belongs to a persisted row")
                    self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="user",
                        content=originating.content,
                        raw_content=None,
                        tool_calls=None,
                        sequence_no=sequence_no,
                        writer_principal="route_user_message",
                        composition_state_id=inserted_state_id,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=now,
                        message_id=str(originating.message_id),
                        session_operation_context=session_operation_context,
                    )
                    if retained_deferred_intents:
                        persisted_origin = conn.execute(
                            select(
                                chat_messages_table.c.session_id,
                                chat_messages_table.c.role,
                                chat_messages_table.c.content,
                            ).where(chat_messages_table.c.id == str(originating.message_id))
                        ).one_or_none()
                        if persisted_origin is None or persisted_origin.session_id != sid or persisted_origin.role != "user":
                            raise AuditIntegrityError("retained deferred intent originating message failed session/role/content custody")
                        persisted_hash = stable_hash(persisted_origin.content)
                        if any(intent.message_content_hash != persisted_hash for intent in retained_deferred_intents):
                            raise AuditIntegrityError("retained deferred intent originating message failed session/role/content custody")
                    originating_record = ChatMessageRecord(
                        id=originating.message_id,
                        session_id=command.fence.session_id,
                        role="user",
                        content=originating.content,
                        raw_content=None,
                        tool_calls=None,
                        created_at=now,
                        sequence_no=sequence_no,
                        composition_state_id=primary_state.id,
                        writer_principal="route_user_message",
                    )
                    sequence_no += 1

                audit_records = self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=primary_state.id,
                    audit_rows=audit_rows,
                    sequence_no=sequence_no,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )

                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    if row_count:
                        mutation.guided.mark_session_updated(updated_at=now)
                    interpretation_records = tuple(
                        mutation.interpretations.create_or_reconcile_pending(prepared.command, prepared.validator)
                        for prepared in interpretation_commands
                    )
                result_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one()
                result_state = self._row_to_state_record(result_row)
                response = project_guided_response(result_state, payloads=command.payloads)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(
                        originating_message_id=(
                            command.originating_message.message_id if command.originating_message is not None else None
                        ),
                        result_state_id=result_state.id,
                    )
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=result_state.id),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                return GuidedStateOperationSettlement(
                    primary_state=primary_state,
                    result_state=result_state,
                    audit_messages=audit_records,
                    originating_message=originating_record,
                    interpretations=interpretation_records,
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        return cast(
            "GuidedStateOperationSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )

    async def stage_guided_full_pipeline_proposal(
        self,
        command: GuidedFullPipelineProposalStageCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> GuidedFullPipelineProposalStageSettlement:
        """Publish one guided-full proposal and its replay locator atomically."""

        if type(command) is not GuidedFullPipelineProposalStageCommand:
            raise TypeError("command must be an exact GuidedFullPipelineProposalStageCommand")
        proposal = command.plan.proposal
        if proposal.surface is not PlannerSurface.GUIDED_FULL:
            raise AuditIntegrityError("guided-full stage requires a guided_full planner proposal")
        if type(proposal.base) is not PresentBase or proposal.base.state_id != command.checkpoint_state_id:
            raise AuditIntegrityError("guided-full proposal base must name its checkpoint")
        checkpoint_content_hash = _composition_state_data_content_hash(command.state)
        if proposal.base.composition_content_hash != checkpoint_content_hash:
            raise AuditIntegrityError("guided-full proposal base hash differs from its checkpoint")
        if command.expected_current_content_hash is not None and command.expected_current_content_hash != checkpoint_content_hash:
            raise AuditIntegrityError("guided-full checkpoint content differs from the observed composition head")
        if proposal.reviewed_anchor_hash != reviewed_anchor_hash({}):
            raise AuditIntegrityError("guided-full proposal carries reviewed-stage authority")
        if proposal.covered_deferred_intent_ids or proposal.supersedes_draft_hash is not None:
            raise AuditIntegrityError("guided-full proposal carries staged-only authority")
        expected_redacted = redact_tool_call_arguments(
            "set_pipeline",
            deep_thaw(proposal.pipeline),
            telemetry=NoopRedactionTelemetry(),
        )
        if deep_thaw(command.arguments_redacted_json) != expected_redacted:
            raise AuditIntegrityError("guided-full redacted arguments differ from the manifest projection")

        normalized = _normalize_proposal_composer_provenance(
            composer_model_identifier=command.plan.model_identifier,
            composer_model_version=command.plan.model_version,
            composer_provider=command.plan.provider,
            composer_skill_hash=proposal.skill_hash,
            tool_arguments_hash=composer_authority_hash(proposal.pipeline),
        )
        assert all(value is not None for value in normalized.values())
        creation_payload = _pipeline_created_payload(
            plan=command.plan,
            user_message_id=command.originating_message.message_id,
            composer_model_identifier=cast(str, normalized["composer_model_identifier"]),
            composer_model_version=cast(str, normalized["composer_model_version"]),
            composer_provider=cast(str, normalized["composer_provider"]),
            summary=command.summary,
            rationale=command.rationale,
            affects=command.affects,
            arguments_redacted_json=command.arguments_redacted_json,
            supersedes_proposal_id=None,
        )
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=(),
            payload_store=None,
        )
        custody_preparation = command.plan.custody_preparation
        custody_data_dir: Path | None = None
        if custody_preparation is not None:
            # Deferred inline custody (elspeth-1e3ad83d89): the blob row's
            # composite FK (created_from_message_id, session_id) is
            # satisfiable only after the originating chat message insert
            # below, so the planner carried the preparation here instead of
            # finalizing mid-plan. Bind it to THIS cohort before any write.
            if self._data_dir is None:
                raise AuditIntegrityError("guided-full deferred custody requires a service data_dir")
            custody_data_dir = self._data_dir
            if custody_preparation.request.session_id != command.fence.session_id:
                raise AuditIntegrityError("guided-full deferred custody targets a different session")
            if custody_preparation.request.created_from_message_id != str(command.originating_message.message_id):
                raise AuditIntegrityError("guided-full deferred custody is not anchored to the originating message")
            staged_source = proposal.pipeline["source"] if "source" in proposal.pipeline else None
            staged_blob_id = staged_source["blob_id"] if isinstance(staged_source, Mapping) and "blob_id" in staged_source else None
            if staged_blob_id != str(custody_preparation.blob_id):
                raise AuditIntegrityError("guided-full deferred custody blob differs from the staged source")
        sid = str(command.fence.session_id)
        event_id = str(uuid.uuid4())
        now = self._now()

        def _sync() -> GuidedFullPipelineProposalStageSettlement:
            with (
                staged_pipeline_custody(
                    custody_preparation,
                    engine=self._engine,
                    data_dir=custody_data_dir,
                    write_fence=BlobGuidedOperationWriteFence(
                        session_id=command.fence.session_id,
                        operation_id=command.fence.operation_id,
                        lease_token=command.fence.lease_token,
                        attempt=command.fence.attempt,
                    ),
                    session_operation_context=session_operation_context,
                    session_operation_authority=self._session_operation_authority,
                ) as staged_custody,
                self._session_process_locked_begin(sid) as conn,
                self._session_write_lock(conn, sid),
            ):
                operation_row, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_plan":
                    raise AuditIntegrityError("guided-full stage requires a guided_plan operation")
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                current_record: CompositionStateRecord | None = None
                if current_row is not None:
                    current_record = self._row_to_state_record(current_row)
                if command.expected_current_state_id is None:
                    if current_row is not None:
                        raise GuidedOperationSettlementConflictError()
                else:
                    if (
                        current_row is None
                        or current_row.id != str(command.expected_current_state_id)
                        or current_row.version != command.expected_current_state_version
                    ):
                        raise GuidedOperationSettlementConflictError()
                    if current_record is None:  # pragma: no cover - paired with current_row
                        raise AuditIntegrityError("guided-full stage current state conversion failed")
                    if composition_content_hash(state_from_record(current_record)) != command.expected_current_content_hash:
                        raise AuditIntegrityError("guided-full observed composition content changed before staging")

                # Interim fail-closed refusal (elspeth-da0e3db919). Staging
                # carries the observed head's guided checkpoint metadata
                # (with the new input ingress record), so a
                # guided walk mid-review carries its live ``active_proposal``
                # onto this new checkpoint while the anchor keeps naming the
                # PREVIOUS row — the identical stranding elspeth-ed67eb9d0d
                # fixed on the settlement paths, and the identical outcome: a
                # clean 200 followed by a permanently unreadable guided
                # session. The decline twin's remedy — rebase the anchor onto
                # the checkpoint — is WRONG here, because staging also mints a
                # SECOND pending proposal based on that same checkpoint, and
                # which of the two the walk then owns is a semantics question
                # (refuse, or supersede the predecessor) that belongs to the
                # operator, not to this fix. Refusing costs a coded
                # ``integrity_error`` on a route the SPA never calls;
                # proceeding costs the session. ``active_proposal`` cannot be
                # a stale reference to a terminal row: ``GuidedSession``'s own
                # invariants bind it to a sole unanswered trailing
                # PROPOSE_PIPELINE/CONFIRM_WIRING turn, so its presence on a
                # parsed checkpoint IS a live review.
                staging_prior_guided = _carried_guided_checkpoint_session(
                    current_record.composer_meta if current_record is not None else None,
                    role="guided-full stage prior",
                )
                if staging_prior_guided.active_proposal is not None:
                    raise AuditIntegrityError(
                        "guided-full staging cannot write a checkpoint over a guided walk still reviewing a proposal: "
                        f"operation {command.fence.operation_id} would carry pending proposal "
                        f"{staging_prior_guided.active_proposal.proposal_id} onto checkpoint {command.checkpoint_state_id} "
                        "while staging a second proposal against it (elspeth-da0e3db919)"
                    )

                # Blob-reference validation moved below the deferred-custody
                # settle: the staged source's blob row may be materialized in
                # THIS transaction (elspeth-1e3ad83d89).
                checkpoint_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(
                        data=command.state,
                        derived_from_state_id=(
                            str(command.expected_current_state_id) if command.expected_current_state_id is not None else None
                        ),
                    ),
                    provenance="convergence_persist",
                    created_at=now,
                    state_id=str(command.checkpoint_state_id),
                    session_operation_context=session_operation_context,
                )
                checkpoint_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == checkpoint_id)).one()
                checkpoint = self._row_to_state_record(checkpoint_row)

                sequence_no = self._reserve_sequence_range(conn, sid, count=1 + len(audit_rows))
                self._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="user",
                    content=command.originating_message.content,
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no,
                    writer_principal="route_user_message",
                    composition_state_id=checkpoint_id,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    message_id=str(command.originating_message.message_id),
                    session_operation_context=session_operation_context,
                )
                originating_message = ChatMessageRecord(
                    id=command.originating_message.message_id,
                    session_id=command.fence.session_id,
                    role="user",
                    content=command.originating_message.content,
                    raw_content=None,
                    tool_calls=None,
                    created_at=now,
                    sequence_no=sequence_no,
                    composition_state_id=checkpoint.id,
                    writer_principal="route_user_message",
                )
                if custody_preparation is not None:
                    # After the originating-message insert, before the blob
                    # reference check: the one ordering that satisfies the
                    # lineage FK and keeps message, blob, and proposal in a
                    # single atomic cohort (elspeth-1e3ad83d89).
                    assert command.custody_max_storage_per_session is not None  # command __post_init__ contract
                    if staged_custody is None:
                        raise AuditIntegrityError("guided-full inline custody was not staged before its atomic cohort")
                    finalize_pipeline_custody_on_connection(
                        custody_preparation,
                        conn=conn,
                        staged=staged_custody,
                        max_storage_per_session=command.custody_max_storage_per_session,
                        quota_exceeded_recorder=self._quota_exceeded_recorder,
                        write_fence=BlobGuidedOperationWriteFence(
                            session_id=command.fence.session_id,
                            operation_id=command.fence.operation_id,
                            lease_token=command.fence.lease_token,
                            attempt=command.fence.attempt,
                        ),
                    )
                validate_proposal_blob_references(
                    conn,
                    session_id=sid,
                    tool_name="set_pipeline",
                    arguments=deep_thaw(proposal.pipeline),
                )
                audit_messages = self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=checkpoint.id,
                    audit_rows=audit_rows,
                    sequence_no=sequence_no + 1,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.mark_session_updated(updated_at=now)

                with self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ) as composer_transaction:
                    stored = composer_transaction.composer.create_guided_pipeline_proposal(
                        command=command,
                        event_id=event_id,
                        user_message_id=command.originating_message.message_id,
                        base_state_id=checkpoint_id,
                        normalized_provenance=normalized,
                        payload=creation_payload,
                        created_at=now,
                    )
                authority = AuthoritativePipelineProposal(
                    row=stored,
                    proposal=proposal,
                    creation_event_id=UUID(event_id),
                    custody_result=command.plan.custody_result,
                    supersedes_proposal_id=None,
                    current_base=proposal.base,
                )
                proposal_record = replace(stored, pipeline_metadata=_pipeline_public_metadata(authority))
                response = project_composition_proposal(proposal_record)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(originating_message_id=command.originating_message.message_id)
                    mutation.guided.complete(
                        result=GuidedPipelineProposalResult(
                            proposal_id=command.proposal_id,
                            checkpoint_state_id=checkpoint.id,
                        ),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                settlement = GuidedFullPipelineProposalStageSettlement(
                    checkpoint_state=checkpoint,
                    proposal=proposal_record,
                    originating_message=originating_message,
                    audit_messages=audit_messages,
                    response_json=projected_json,
                    response_hash=response_hash,
                )
            return settlement

        settlement = cast(
            "GuidedFullPipelineProposalStageSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )
        _PIPELINE_PLANNER_COUNTER.add(1, {"surface": "guided_full", "result": "proposal_created"})
        _PIPELINE_CUSTODY_COUNTER.add(1, {"surface": "guided_full", "result": command.plan.custody_result})
        return settlement

    async def decline_guided_full_pipeline_proposal(
        self,
        command: GuidedFullPipelineDeclineCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> GuidedFullPipelineDeclineSettlement:
        """Persist one guided-full planner decline as an ordinary chat turn.

        Sibling of stage_guided_full_pipeline_proposal for the planner's
        other outcome (PlannerDeclined, surfaced as GuidedPlannerDecline —
        an ordinary turn's DECLINE:-marked reply or the escape hatch's
        any-text reply): no proposal is created. The model's own words become an ordinary
        assistant chat message and the operation completes with
        GuidedDeclinedResult — never GuidedOperationFailureCode, per the
        same "decline is a conversational outcome, not a failure" rule the
        freeform surface already applies.
        """

        if type(command) is not GuidedFullPipelineDeclineCommand:
            raise TypeError("command must be an exact GuidedFullPipelineDeclineCommand")
        checkpoint_content_hash = _composition_state_data_content_hash(command.state)
        if command.expected_current_content_hash is not None and command.expected_current_content_hash != checkpoint_content_hash:
            raise AuditIntegrityError("guided-full decline checkpoint content differs from the observed composition head")

        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=(),
            payload_store=None,
        )
        sid = str(command.fence.session_id)
        now = self._now()

        def _sync() -> GuidedFullPipelineDeclineSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_plan":
                    raise AuditIntegrityError("guided-full decline requires a guided_plan operation")
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one_or_none()
                current_record: CompositionStateRecord | None = None
                if current_row is not None:
                    current_record = self._row_to_state_record(current_row)
                if command.expected_current_state_id is None:
                    if current_row is not None:
                        raise GuidedOperationSettlementConflictError()
                else:
                    if (
                        current_row is None
                        or current_row.id != str(command.expected_current_state_id)
                        or current_row.version != command.expected_current_state_version
                    ):
                        raise GuidedOperationSettlementConflictError()
                    if current_record is None:  # pragma: no cover - paired with current_row
                        raise AuditIntegrityError("guided-full decline current state conversion failed")
                    if composition_content_hash(state_from_record(current_record)) != command.expected_current_content_hash:
                        raise AuditIntegrityError("guided-full decline observed composition content changed before staging")

                # A guided-full decline writes a checkpoint over the observed
                # head with ``composer_meta`` carried forward verbatim, so a
                # guided walk holding a pending proposal carries that proposal
                # across this settlement exactly as a RESPOND or CHAT
                # settlement does — and used to strand its anchor on the
                # previous checkpoint in exactly the same way
                # (elspeth-ed67eb9d0d). Same class, same guard: this route was
                # missed the first time because the class was closed by
                # censusing GuidedStateOperationCommand construction sites
                # rather than every writer that carries a live active_proposal
                # onto a new row.
                pending_proposal_authorities = _verify_guided_pending_proposal_transition(
                    conn,
                    context=_GuidedPendingProposalTransitionContext(
                        service=self,
                        session_id=sid,
                        current_record=current_record,
                        prior_guided=_carried_guided_checkpoint_session(
                            current_record.composer_meta if current_record is not None else None,
                            role="guided-full decline prior",
                        ),
                        candidate_guided=_carried_guided_checkpoint_session(
                            command.state.composer_meta,
                            role="guided-full decline candidate",
                        ),
                        expected_current_content_hash=command.expected_current_content_hash,
                        checkpoint_state_id=command.checkpoint_state_id,
                        candidate_content_hash=checkpoint_content_hash,
                        settlement_origin=f"guided-full decline operation {command.fence.operation_id}",
                    ),
                    invalidation=None,
                    rebase=command.rebased_pending_proposal,
                )

                checkpoint_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(
                        data=command.state,
                        derived_from_state_id=(
                            str(command.expected_current_state_id) if command.expected_current_state_id is not None else None
                        ),
                    ),
                    provenance="convergence_persist",
                    created_at=now,
                    state_id=str(command.checkpoint_state_id),
                    session_operation_context=session_operation_context,
                )
                checkpoint_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == checkpoint_id)).one()
                checkpoint = self._row_to_state_record(checkpoint_row)

                rebase_plan = pending_proposal_authorities.rebased
                if rebase_plan is not None:
                    # After the insert: ``composition_proposals.base_state_id``
                    # carries a foreign key onto ``composition_states``, so the
                    # checkpoint the anchor moves onto must already exist.
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=command.fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        _rebind_guided_pending_proposal(
                            conn,
                            mutation=mutation,
                            authority=rebase_plan.authority,
                            reason=rebase_plan.reason,
                            actor=command.actor,
                            created_at=now,
                            to_state_id=command.checkpoint_state_id,
                        )

                sequence_no = self._reserve_sequence_range(conn, sid, count=2 + len(audit_rows))
                self._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="user",
                    content=command.originating_message.content,
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no,
                    writer_principal="route_user_message",
                    composition_state_id=checkpoint_id,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    message_id=str(command.originating_message.message_id),
                    session_operation_context=session_operation_context,
                )
                originating_message = ChatMessageRecord(
                    id=command.originating_message.message_id,
                    session_id=command.fence.session_id,
                    role="user",
                    content=command.originating_message.content,
                    raw_content=None,
                    tool_calls=None,
                    created_at=now,
                    sequence_no=sequence_no,
                    composition_state_id=checkpoint.id,
                    writer_principal="route_user_message",
                )
                decline_message_id = self._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="assistant",
                    content=command.decline_text,
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no + 1,
                    writer_principal="compose_loop",
                    composition_state_id=checkpoint_id,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                decline_row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == decline_message_id)).one()
                decline_message = self._row_to_chat_message_record(decline_row)
                audit_messages = self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=checkpoint.id,
                    audit_rows=audit_rows,
                    sequence_no=sequence_no + 2,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                response = project_guided_full_decline(decline_message)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.mark_session_updated(updated_at=now)
                    mutation.guided.bind(originating_message_id=command.originating_message.message_id)
                    mutation.guided.complete(
                        result=GuidedDeclinedResult(
                            checkpoint_state_id=checkpoint.id,
                            decline_message_id=decline_message.id,
                        ),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                return GuidedFullPipelineDeclineSettlement(
                    checkpoint_state=checkpoint,
                    decline_message=decline_message,
                    originating_message=originating_message,
                    audit_messages=audit_messages,
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        settlement = cast(
            "GuidedFullPipelineDeclineSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )
        _PIPELINE_PLANNER_COUNTER.add(1, {"surface": "guided_full", "result": "declined"})
        return settlement

    async def stage_guided_pipeline_proposal(
        self,
        command: GuidedPipelineProposalStageCommand,
        *,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> GuidedPipelineProposalStageSettlement:
        """Atomically publish one pending guided checkpoint and its authority."""

        if type(command) is not GuidedPipelineProposalStageCommand:
            raise TypeError("command must be an exact GuidedPipelineProposalStageCommand")
        if type(command.plan) is not PipelinePlanResult:
            raise TypeError("command.plan must be an exact PipelinePlanResult")
        proposal = command.plan.proposal
        if proposal.surface not in {PlannerSurface.GUIDED_FULL, PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE}:
            raise AuditIntegrityError("guided proposal stage requires a guided planner surface")
        expected_redacted = redact_tool_call_arguments(
            "set_pipeline",
            deep_thaw(proposal.pipeline),
            telemetry=NoopRedactionTelemetry(),
        )
        if deep_thaw(command.arguments_redacted_json) != expected_redacted:
            raise AuditIntegrityError("guided pipeline redacted arguments differ from the manifest projection")
        if type(proposal.base) is not PresentBase:
            raise AuditIntegrityError("guided pipeline proposal must name its future checkpoint base")
        if proposal.base.state_id != command.checkpoint_state_id:
            raise AuditIntegrityError("guided pipeline proposal base does not name the staged checkpoint")
        checkpoint_content_hash = _composition_state_data_content_hash(command.state)
        if proposal.base.composition_content_hash != checkpoint_content_hash:
            raise AuditIntegrityError("guided pipeline proposal base hash does not bind the staged checkpoint")
        if checkpoint_content_hash != command.expected_current_content_hash:
            raise AuditIntegrityError("guided proposal checkpoint unexpectedly changes authored composition content")
        if (command.supersedes_proposal_id is None) != (proposal.supersedes_draft_hash is None):
            raise AuditIntegrityError("guided proposal supersession id/hash binding is incomplete")

        metadata = deep_thaw(command.state.composer_meta)
        if (
            type(metadata) is not dict
            or not {"guided_session"} <= set(metadata)
            or set(metadata) - {"guided_session", "ingress", "chat_ingress_inputs"}
            or ("ingress" in metadata and not _valid_compartment_ingress_metadata(metadata["ingress"]))
            or ("chat_ingress_inputs" in metadata and not _valid_chat_ingress_inputs_metadata(metadata["chat_ingress_inputs"]))
        ):
            raise AuditIntegrityError("guided proposal checkpoint metadata is malformed")
        from elspeth.web.composer.guided.planning import (
            guided_candidate_state,
            guided_private_reviewed_facts,
            verified_remaining_deferred_intents,
            verify_guided_proposal_projection,
        )
        from elspeth.web.composer.guided.protocol import GuidedStep, Turn, TurnType, validate_current_turn
        from elspeth.web.composer.guided.state_machine import GuidedSession

        guided = GuidedSession.from_dict(metadata["guided_session"])
        checkpoint_reviewed_facts = guided_private_reviewed_facts(guided)
        if reviewed_anchor_hash(checkpoint_reviewed_facts) != proposal.reviewed_anchor_hash:
            raise AuditIntegrityError("guided proposal checkpoint reviewed authority differs from the immutable proposal")
        active = guided.active_proposal
        if (
            active is None
            or active.proposal_id != command.proposal_id
            or active.draft_hash != proposal.draft_hash
            or active.base != proposal.base
            or active.reviewed_anchor_hash != proposal.reviewed_anchor_hash
            or active.covered_deferred_intent_ids != proposal.covered_deferred_intent_ids
            or active.creation_event_schema != _PIPELINE_CREATED_SCHEMA
            or active.supersedes_proposal_id != command.supersedes_proposal_id
            or active.supersedes_draft_hash != proposal.supersedes_draft_hash
        ):
            raise AuditIntegrityError("guided proposal reference does not match private proposal authority")
        next_turn = command.response.next_turn
        if next_turn is None:  # pragma: no cover - command type guards this
            raise AuditIntegrityError("guided proposal stage response lost its proposal turn")
        turn_payloads = [
            payload for payload in command.payloads if payload.payload_id == next_turn.payload_id and payload.purpose == "turn"
        ]
        if len(turn_payloads) != 1:
            raise AuditIntegrityError("guided proposal stage requires one exact durable response turn")
        if not guided.history:
            raise AuditIntegrityError("guided proposal checkpoint has no durable proposal turn")
        durable_turn = guided.history[-1]
        expected_step = GuidedStep.STEP_3_TRANSFORMS if next_turn.turn_type is TurnType.PROPOSE_PIPELINE else GuidedStep.STEP_4_WIRE
        expected_step_index = 2 if expected_step is GuidedStep.STEP_3_TRANSFORMS else 3
        if (
            guided.step is not expected_step
            or durable_turn.step is not expected_step
            or durable_turn.turn_type is not next_turn.turn_type
            or durable_turn.payload_hash != next_turn.payload_id
            or durable_turn.response_hash is not None
            or durable_turn.emitter != "server"
            or next_turn.step_index != expected_step_index
        ):
            raise AuditIntegrityError("guided proposal checkpoint turn differs from the durable response")
        durable_payload_json = cast(Mapping[str, Any], deep_thaw(turn_payloads[0].payload))
        proposal_projection_json = cast(Mapping[str, Any], deep_thaw(command.proposal_projection))
        verify_guided_proposal_projection(
            payload=proposal_projection_json,
            proposal_id=command.proposal_id,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=command.catalog_plugin_ids,
        )
        if next_turn.turn_type is TurnType.PROPOSE_PIPELINE:
            if durable_payload_json != proposal_projection_json:
                raise AuditIntegrityError("guided proposal turn differs from its verified public projection")
        else:
            from elspeth.web.composer.guided.emitters import build_step_4_wire_turn

            candidate = guided_candidate_state(proposal)
            # Independent re-derivation (staging asserts; settlement
            # verifies): rebuild the wire review from the immutable proposal,
            # never from route-supplied state. The rebuild must lower
            # operator-profile options through the session principal's
            # snapshot exactly like the route side does
            # (routes/composer/guided.py review-advance and correction:
            # validation_state is the executable view unless authored
            # validation already errs) — validating the authored candidate
            # crashed profile-bound wire corrections and mis-compared
            # authored-vs-lowered projections into false integrity conflicts.
            if self._plugin_snapshot_factory is None:
                validation_state = candidate
                validation_summary = candidate.validate()
            else:
                plugin_snapshot = await self._plugin_snapshot_for_session(str(command.fence.session_id))
                if plugin_snapshot is None:
                    raise AuditIntegrityError("Profile-aware guided proposal settlement has no principal snapshot")
                from elspeth.web.plugin_policy.validation import validate_authored_composition_state

                assert self._operator_profile_registry is not None
                assert self._catalog is not None
                policy = validate_authored_composition_state(
                    candidate,
                    snapshot=plugin_snapshot,
                    profile_registry=self._operator_profile_registry,
                    catalog=self._catalog,
                )
                validation_state = candidate if policy.validation.errors else policy.executable_state
                validation_summary = policy.validation
            expected_wire = build_step_4_wire_turn(
                candidate,
                proposal_projection=cast("Any", proposal_projection_json),
                guided=guided,
                catalog=None,
                validation_state=validation_state,
                validation_summary=validation_summary,
            )
            try:
                validate_current_turn(
                    GuidedStep.STEP_4_WIRE,
                    Turn(type=TurnType.CONFIRM_WIRING.value, step_index=3, payload=cast("Any", durable_payload_json)),
                )
            except ValueError as exc:
                raise AuditIntegrityError("guided correction produced an invalid wire-review turn") from exc
            for key in (
                "proposal_id",
                "draft_hash",
                "sources",
                "nodes",
                "outputs",
                "connections",
                "semantic_contracts",
            ):
                expected_payload = expected_wire["payload"]
                if key not in durable_payload_json or key not in expected_payload or durable_payload_json[key] != expected_payload[key]:
                    raise AuditIntegrityError(f"guided correction wire projection differs at {key}")
        verified_remaining_deferred_intents(
            guided=guided,
            proposal=proposal,
        )
        if command.originating_message is not None:
            if not guided.correction_messages:
                raise AuditIntegrityError("guided correction checkpoint lost its private message reference")
            correction_ref = guided.correction_messages[-1]
            if correction_ref.message_id != command.originating_message.message_id or correction_ref.content_hash != stable_hash(
                command.originating_message.content
            ):
                raise AuditIntegrityError("guided correction checkpoint does not bind its originating message")

        normalized = _normalize_proposal_composer_provenance(
            composer_model_identifier=command.plan.model_identifier,
            composer_model_version=command.plan.model_version,
            composer_provider=command.plan.provider,
            composer_skill_hash=proposal.skill_hash,
            tool_arguments_hash=composer_authority_hash(proposal.pipeline),
        )
        assert all(value is not None for value in normalized.values())
        creation_payload = _pipeline_created_payload(
            plan=command.plan,
            user_message_id=command.user_message_id,
            composer_model_identifier=cast(str, normalized["composer_model_identifier"]),
            composer_model_version=cast(str, normalized["composer_model_version"]),
            composer_provider=cast(str, normalized["composer_provider"]),
            summary=command.summary,
            rationale=command.rationale,
            affects=command.affects,
            arguments_redacted_json=command.arguments_redacted_json,
            supersedes_proposal_id=command.supersedes_proposal_id,
        )
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=command.payloads,
            payload_store=payload_store,
        )
        prepared_state = with_guided_response_descriptor(command.state, command.response)
        sid = str(command.fence.session_id)
        event_id = str(uuid.uuid4())
        now = self._now()

        def _sync() -> GuidedPipelineProposalStageSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_respond":
                    raise AuditIntegrityError("guided proposal stage requires a guided_respond operation")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                current_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).one()
                current_record = self._row_to_state_record(current_row)
                if composition_content_hash(state_from_record(current_record)) != command.expected_current_content_hash:
                    raise AuditIntegrityError("Guided operation current state content changed before settlement")
                current_guided = state_from_record(current_record).guided_session
                if current_guided is None:
                    raise AuditIntegrityError("guided proposal predecessor lost its guided checkpoint")
                if current_guided.deferred_intents != guided.deferred_intents:
                    raise AuditIntegrityError("guided proposal deferred authority changed before staging")
                _verify_guided_deferred_message_authority(
                    conn,
                    session_id=sid,
                    guided=current_guided,
                )
                _verify_guided_correction_message_authority(
                    conn,
                    session_id=sid,
                    guided=current_guided,
                )
                _verify_guided_root_message_authority(
                    conn,
                    service=self,
                    session_id=sid,
                    guided=current_guided,
                )
                verified_remaining_deferred_intents(
                    guided=current_guided,
                    proposal=proposal,
                )

                if command.user_message_id is not None and command.originating_message is None:
                    message_row = conn.execute(
                        select(chat_messages_table.c.role, chat_messages_table.c.content)
                        .where(chat_messages_table.c.session_id == sid)
                        .where(chat_messages_table.c.id == str(command.user_message_id))
                    ).one_or_none()
                    if message_row is None or message_row.role != "user":
                        raise AuditIntegrityError("guided proposal originating message is missing or cross-session")
                    if stable_hash(message_row.content) != command.user_message_content_hash:
                        raise AuditIntegrityError("guided proposal originating message content changed")

                if command.originating_message is None:
                    if guided.correction_messages != current_guided.correction_messages:
                        raise AuditIntegrityError("guided proposal staging changed private correction custody")
                else:
                    expected_corrections = (
                        *current_guided.correction_messages,
                        guided.correction_messages[-1],
                    )
                    if guided.correction_messages != expected_corrections:
                        raise AuditIntegrityError("guided correction staging did not append exactly one custody reference")
                    existing_correction = conn.execute(
                        select(chat_messages_table.c.id).where(chat_messages_table.c.id == str(command.originating_message.message_id))
                    ).one_or_none()
                    if existing_correction is not None:
                        raise AuditIntegrityError("guided correction message id already belongs to a persisted row")

                if command.supersedes_proposal_id is not None:
                    if current_guided is None or current_guided.active_proposal is None:
                        raise AuditIntegrityError("guided proposal revision predecessor reference is missing")
                    if guided_private_reviewed_facts(current_guided) != checkpoint_reviewed_facts:
                        raise AuditIntegrityError("guided proposal revision reviewed authority changed from its predecessor")
                    if current_guided.deferred_intents != guided.deferred_intents:
                        raise AuditIntegrityError("guided proposal revision deferred authority changed from its predecessor")
                    predecessor_ref = current_guided.active_proposal
                    if (
                        predecessor_ref.proposal_id != command.supersedes_proposal_id
                        or predecessor_ref.draft_hash != proposal.supersedes_draft_hash
                    ):
                        raise AuditIntegrityError("guided proposal revision predecessor reference drifted")
                    current_reviewed_facts = guided_private_reviewed_facts(current_guided)
                    if reviewed_anchor_hash(current_reviewed_facts) != proposal.reviewed_anchor_hash:
                        raise AuditIntegrityError("guided proposal revision reviewed authority drifted")
                    superseded = conn.execute(
                        select(composition_proposals_table)
                        .where(composition_proposals_table.c.session_id == sid)
                        .where(composition_proposals_table.c.id == str(command.supersedes_proposal_id))
                    ).one_or_none()
                    if superseded is None:
                        raise AuditIntegrityError("guided proposal revision predecessor is missing or cross-session")
                    superseded_record = _proposal_record_from_row(superseded)
                    superseded_created_rows = conn.execute(
                        select(proposal_events_table)
                        .where(proposal_events_table.c.session_id == sid)
                        .where(proposal_events_table.c.proposal_id == str(command.supersedes_proposal_id))
                        .where(proposal_events_table.c.event_type == "proposal.created")
                    ).fetchall()
                    if len(superseded_created_rows) != 1:
                        raise AuditIntegrityError("guided proposal supersession draft authority is malformed")
                    superseded_authority = _restore_authoritative_pipeline_proposal(
                        conn=conn,
                        row=superseded_record,
                        creation_event=_proposal_event_record_from_row(superseded_created_rows[0]),
                        reviewed_facts=current_reviewed_facts,
                    )
                    _verify_pipeline_lifecycle_authority(conn, service=self, authority=superseded_authority)
                    if (
                        superseded_authority.row.status != "pending"
                        or superseded_authority.proposal.draft_hash != proposal.supersedes_draft_hash
                    ):
                        raise AuditIntegrityError("guided proposal revision predecessor is no longer pending")
                    with self._guided_session_mutation_transaction(
                        conn,
                        guided_fence=command.fence,
                        session_operation_context=session_operation_context,
                    ) as mutation:
                        mutation.composer.reject_pending_proposal(
                            authority=superseded_authority,
                            actor=command.actor,
                            created_at=now,
                            reason="superseded",
                        )

                validate_proposal_blob_references(
                    conn,
                    session_id=sid,
                    tool_name="set_pipeline",
                    arguments=deep_thaw(proposal.pipeline),
                )
                checkpoint_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=prepared_state, derived_from_state_id=str(command.expected_current_state_id)),
                    provenance="convergence_persist",
                    created_at=now,
                    state_id=str(command.checkpoint_state_id),
                    session_operation_context=session_operation_context,
                )
                correction_sequence_no: int | None = None
                if command.originating_message is not None:
                    correction_sequence_no = self._reserve_sequence_range(conn, sid, count=1 + len(audit_rows))
                    self._insert_chat_message(
                        conn,
                        session_id=sid,
                        role="user",
                        content=command.originating_message.content,
                        raw_content=None,
                        tool_calls=None,
                        sequence_no=correction_sequence_no,
                        writer_principal="route_user_message",
                        composition_state_id=checkpoint_id,
                        tool_call_id=None,
                        parent_assistant_id=None,
                        created_at=now,
                        message_id=str(command.originating_message.message_id),
                        session_operation_context=session_operation_context,
                    )
                with self._session_composer_mutation_transaction(
                    conn,
                    session_id=sid,
                    session_operation_context=session_operation_context,
                    expected_kind=SessionOperationKind.COMPOSE,
                ) as composer_transaction:
                    record = composer_transaction.composer.create_guided_pipeline_proposal(
                        command=command,
                        event_id=event_id,
                        user_message_id=command.user_message_id,
                        base_state_id=checkpoint_id,
                        normalized_provenance=normalized,
                        payload=creation_payload,
                        created_at=now,
                    )
                state_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == checkpoint_id)).one()
                result_state = self._row_to_state_record(state_row)
                authority = AuthoritativePipelineProposal(
                    row=record,
                    proposal=proposal,
                    creation_event_id=UUID(event_id),
                    custody_result=command.plan.custody_result,
                    supersedes_proposal_id=command.supersedes_proposal_id,
                    current_base=proposal.base,
                )
                proposal_record = replace(record, pipeline_metadata=_pipeline_public_metadata(authority))

                sequence_no = (
                    correction_sequence_no + 1
                    if correction_sequence_no is not None
                    else (self._reserve_sequence_range(conn, sid, count=len(audit_rows)) if audit_rows else None)
                )
                audit_messages = self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=result_state.id,
                    audit_rows=audit_rows,
                    sequence_no=sequence_no,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                response = project_guided_response(result_state, payloads=command.payloads)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    if audit_rows or command.originating_message is not None:
                        mutation.guided.mark_session_updated(updated_at=now)
                    mutation.guided.bind(
                        originating_message_id=(
                            command.originating_message.message_id if command.originating_message is not None else None
                        ),
                        proposal_id=command.proposal_id,
                        result_state_id=result_state.id,
                    )
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(
                            state_id=result_state.id,
                            proposal_id=command.proposal_id,
                        ),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                return GuidedPipelineProposalStageSettlement(
                    result_state=result_state,
                    proposal=proposal_record,
                    audit_messages=audit_messages,
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        settlement = cast(
            "GuidedPipelineProposalStageSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )
        _PIPELINE_PLANNER_COUNTER.add(1, {"surface": proposal.surface.value, "result": "proposal_created"})
        _PIPELINE_CUSTODY_COUNTER.add(1, {"surface": proposal.surface.value, "result": command.plan.custody_result})
        return settlement

    async def back_edit_guided_pipeline_proposal(
        self,
        command: GuidedPipelineProposalBackEditCommand,
        *,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> GuidedPipelineProposalStageSettlement:
        """Atomically supersede one proposal and rewind to a component edit."""

        if type(command) is not GuidedPipelineProposalBackEditCommand:
            raise TypeError("command must be an exact GuidedPipelineProposalBackEditCommand")

        from elspeth.web.composer.guided.protocol import GuidedStep, Turn, TurnType, validate_current_turn
        from elspeth.web.composer.guided.state_machine import GuidedSession, TurnRecord

        payloads_by_purpose = {payload.purpose: payload for payload in command.payloads}
        response_payload = payloads_by_purpose["turn_response"]
        turn_payload = payloads_by_purpose["turn"]
        expected_response_payload: dict[str, object] = {
            "action": "revise" if command.origin == "proposal_review" else "edit_reviewed_component",
            "proposal_id": str(command.proposal_id),
            "draft_hash": command.draft_hash,
            "edit_target": command.edit_target.to_dict(),
        }
        if command.origin == "wire_review":
            expected_response_payload["correction_feedback"] = command.correction_feedback
        if deep_thaw(response_payload.payload) != expected_response_payload:
            raise AuditIntegrityError("guided back-edit response payload differs from its exact authority")
        next_turn = command.response.next_turn
        if next_turn is None:  # pragma: no cover - command guards this
            raise AuditIntegrityError("guided back-edit response lost its edit form")
        if next_turn.payload_id != turn_payload.payload_id:
            raise AuditIntegrityError("guided back-edit response does not bind its edit form payload")
        target_step = GuidedStep.STEP_1_SOURCE if command.edit_target.kind == "source" else GuidedStep.STEP_2_SINK
        try:
            validate_current_turn(
                target_step,
                Turn(
                    type=TurnType.SCHEMA_FORM.value,
                    step_index=next_turn.step_index,
                    payload=cast("Any", deep_thaw(turn_payload.payload)),
                ),
            )
        except ValueError as exc:
            raise AuditIntegrityError("guided back-edit form payload is malformed") from exc

        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=command.payloads,
            payload_store=payload_store,
        )
        prepared_state = with_guided_response_descriptor(command.state, command.response)
        sid = str(command.fence.session_id)
        pid = str(command.proposal_id)
        now = self._now()

        def _sync() -> GuidedPipelineProposalStageSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _ = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_respond":
                    raise AuditIntegrityError("guided proposal back-edit requires guided_respond")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                current_row = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.session_id == sid,
                        composition_states_table.c.id == str(command.expected_current_state_id),
                    )
                ).one()
                current_record = self._row_to_state_record(current_row)
                current_state = state_from_record(current_record)
                current_content_hash = composition_content_hash(current_state)
                if current_content_hash != command.expected_current_content_hash:
                    raise AuditIntegrityError("guided back-edit current composition content changed")

                proposal_row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if proposal_row is None:
                    raise AuditIntegrityError("guided back-edit proposal authority is missing or cross-session")
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("guided back-edit proposal must have one creation event")
                authority = _restore_authoritative_pipeline_proposal(
                    conn=conn,
                    row=_proposal_record_from_row(proposal_row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=deep_thaw(command.reviewed_facts),
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                if authority.row.status != "pending" or authority.proposal.draft_hash != command.draft_hash:
                    raise AuditIntegrityError("guided back-edit does not name the active pending draft")
                # Currency is asked of the proposal's ANCHOR, which a guided
                # settlement legally moves forward when it carries the
                # proposal across a new checkpoint (elspeth-ed67eb9d0d).
                # ``proposal.base`` is hashed into ``draft_hash`` and never
                # moves, so it cannot answer this question.
                #
                # The ``wire_review`` one-hop tolerance predates the rebase
                # and is retained: a wire-review back-edit is answered from
                # a turn the author was already looking at, so a settlement
                # that landed between render and press must not kill the
                # affordance.
                allowed_base_state_ids = {current_record.id}
                if command.origin == "wire_review" and current_record.derived_from_state_id is not None:
                    allowed_base_state_ids.add(current_record.derived_from_state_id)
                if type(authority.current_base) is not PresentBase or authority.current_base.state_id not in allowed_base_state_ids:
                    raise AuditIntegrityError("guided back-edit proposal base differs from current checkpoint")
                if authority.current_base.composition_content_hash != current_content_hash:
                    raise AuditIntegrityError("guided back-edit proposal base content hash changed")

                guided = current_state.guided_session
                if guided is None or guided.active_proposal is None:
                    raise AuditIntegrityError("guided back-edit current checkpoint has no active proposal")
                active = guided.active_proposal
                if (
                    active.proposal_id != command.proposal_id
                    or active.draft_hash != authority.proposal.draft_hash
                    or active.base != authority.proposal.base
                    or active.reviewed_anchor_hash != authority.proposal.reviewed_anchor_hash
                    or active.covered_deferred_intent_ids != authority.proposal.covered_deferred_intent_ids
                    or active.creation_event_schema != "pipeline_proposal_created.v1"
                    or active.supersedes_proposal_id != authority.supersedes_proposal_id
                    or active.supersedes_draft_hash != authority.proposal.supersedes_draft_hash
                ):
                    raise AuditIntegrityError("guided back-edit active proposal reference differs from authority")
                turn_payload_json = deep_thaw(turn_payload.payload)
                if command.edit_target.kind == "source":
                    source_target = (
                        guided.reviewed_sources[command.edit_target.stable_id]
                        if command.edit_target.stable_id in guided.reviewed_sources
                        else None
                    )
                    if source_target is None:
                        raise AuditIntegrityError("guided back-edit target is not an exact reviewed component")
                    expected_plugin = source_target.plugin
                    expected_prefill = {"schema": {"mode": "observed"}, **dict(deep_thaw(source_target.options))}
                    blob_ref = source_target.options["blob_ref"] if "blob_ref" in source_target.options else None
                    if blob_ref is not None and "path" in expected_prefill and type(expected_prefill["path"]) is str:
                        expected_prefill["path"] = f"{BLOB_REF_PATH_PREFIX}{blob_ref}"
                    expected_prefill["on_validation_failure"] = source_target.on_validation_failure
                else:
                    output_target = (
                        guided.reviewed_outputs[command.edit_target.stable_id]
                        if command.edit_target.stable_id in guided.reviewed_outputs
                        else None
                    )
                    if output_target is None:
                        raise AuditIntegrityError("guided back-edit target is not an exact reviewed component")
                    expected_plugin = output_target.plugin
                    expected_prefill = {"schema": {"mode": "observed"}, **dict(deep_thaw(output_target.options))}
                    expected_prefill["on_write_failure"] = output_target.on_write_failure
                if turn_payload_json["plugin"] != expected_plugin or deep_thaw(turn_payload_json["prefilled"]) != expected_prefill:
                    raise AuditIntegrityError("guided back-edit form differs from server-held reviewed custody")
                origin_step = GuidedStep.STEP_3_TRANSFORMS if command.origin == "proposal_review" else GuidedStep.STEP_4_WIRE
                origin_turn_type = TurnType.PROPOSE_PIPELINE if command.origin == "proposal_review" else TurnType.CONFIRM_WIRING
                if (
                    guided.step is not origin_step
                    or not guided.history
                    or guided.history[-1].step is not origin_step
                    or guided.history[-1].turn_type is not origin_turn_type
                    or guided.history[-1].response_hash is not None
                    or any(record.response_hash is None for record in guided.history[:-1])
                ):
                    raise AuditIntegrityError("guided back-edit has no exact active review occurrence")

                metadata = deep_thaw(command.state.composer_meta)
                if (
                    type(metadata) is not dict
                    or not {"guided_session"} <= set(metadata)
                    or set(metadata) - {"guided_session", "ingress", "chat_ingress_inputs"}
                    or ("ingress" in metadata and not _valid_compartment_ingress_metadata(metadata["ingress"]))
                    or ("chat_ingress_inputs" in metadata and not _valid_chat_ingress_inputs_metadata(metadata["chat_ingress_inputs"]))
                ):
                    raise AuditIntegrityError("guided back-edit candidate metadata is malformed")
                try:
                    candidate_guided = GuidedSession.from_dict(metadata["guided_session"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise AuditIntegrityError("guided back-edit candidate checkpoint is malformed") from exc
                answered = replace(
                    guided.history[-1],
                    response_hash=response_payload.payload_id,
                    summary=(
                        "Guided pipeline proposal revision requested."
                        if command.origin == "proposal_review"
                        else "Guided pipeline wiring component edit requested."
                    ),
                )
                emitted = TurnRecord(
                    step=target_step,
                    turn_type=TurnType.SCHEMA_FORM,
                    payload_hash=turn_payload.payload_id,
                    response_hash=None,
                    emitter="server",
                )
                expected_guided = replace(
                    guided,
                    step=target_step,
                    history=(*guided.history[:-1], answered, emitted),
                    active_proposal=None,
                    active_edit_target=command.edit_target,
                )
                if candidate_guided != expected_guided:
                    raise AuditIntegrityError("guided back-edit candidate differs from the exact rewind transition")
                if (
                    _composition_state_data_content_hash(command.state) != command.expected_current_content_hash
                    or command.state.is_valid is not current_record.is_valid
                    or command.state.validation_errors != current_record.validation_errors
                ):
                    raise AuditIntegrityError("guided back-edit candidate changed authored composition or validation authority")

                state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=prepared_state, derived_from_state_id=str(current_record.id)),
                    provenance="convergence_persist",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.composer.record_pending_proposal_rejection(
                        authority=authority,
                        actor=command.actor,
                        created_at=now,
                        reason="superseded",
                    )

                result_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == state_id)).one()
                result_state = self._row_to_state_record(result_row)
                sequence_no = self._reserve_sequence_range(conn, sid, count=len(audit_rows)) if audit_rows else None
                audit_messages = self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=result_state.id,
                    audit_rows=audit_rows,
                    sequence_no=sequence_no,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                response = project_guided_response(result_state, payloads=command.payloads)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    if audit_rows:
                        mutation.guided.mark_session_updated(updated_at=now)
                    mutation.guided.bind(proposal_id=command.proposal_id, result_state_id=result_state.id)
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=result_state.id, proposal_id=command.proposal_id),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                updated_row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == pid)).one()
                proposal_record = replace(
                    _proposal_record_from_row(updated_row),
                    pipeline_metadata=_pipeline_public_metadata(authority),
                )
                return GuidedPipelineProposalStageSettlement(
                    result_state=result_state,
                    proposal=proposal_record,
                    audit_messages=audit_messages,
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        settlement = cast(
            "GuidedPipelineProposalStageSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )
        _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "guided_staged", "result": "superseded_for_component_edit"})
        return settlement

    async def reject_guided_pipeline_proposal(
        self,
        command: GuidedPipelineProposalRejectCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> GuidedPipelineProposalStageSettlement:
        """Atomically reject one pending guided proposal and clear its ref."""

        if type(command) is not GuidedPipelineProposalRejectCommand:
            raise TypeError("command must be an exact GuidedPipelineProposalRejectCommand")
        sid = str(command.fence.session_id)
        pid = str(command.proposal_id)
        now = self._now()

        def _sync() -> GuidedPipelineProposalStageSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _ = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_respond":
                    raise AuditIntegrityError("guided proposal rejection requires guided_respond")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                current_row = conn.execute(
                    select(composition_states_table).where(composition_states_table.c.id == str(command.expected_current_state_id))
                ).one()
                current_record = self._row_to_state_record(current_row)
                proposal_row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if proposal_row is None:
                    raise AuditIntegrityError("guided proposal rejection authority is missing")
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("guided proposal rejection requires one creation event")
                authority = _restore_authoritative_pipeline_proposal(
                    conn=conn,
                    row=_proposal_record_from_row(proposal_row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=deep_thaw(command.reviewed_facts),
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                if authority.row.status != "pending" or authority.proposal.draft_hash != command.draft_hash:
                    raise AuditIntegrityError("guided proposal rejection does not name the active pending draft")
                current_state = state_from_record(current_record)
                guided = current_state.guided_session
                if guided is None or guided.active_proposal is None:
                    raise AuditIntegrityError("guided proposal rejection has no active reference")
                active = guided.active_proposal
                if active.proposal_id != command.proposal_id or active.draft_hash != command.draft_hash:
                    raise AuditIntegrityError("guided proposal rejection reference differs from authority")
                from elspeth.web.composer.guided.protocol import TurnType

                if (
                    not guided.history
                    or guided.history[-1].turn_type is not TurnType.PROPOSE_PIPELINE
                    or guided.history[-1].response_hash is not None
                ):
                    raise AuditIntegrityError("guided proposal rejection has no active proposal occurrence")
                cleared = replace(
                    guided,
                    history=guided.history[:-1],
                    active_proposal=None,
                    active_edit_target=None,
                )
                state_dict = current_state.to_dict()
                state_data = with_guided_response_descriptor(
                    CompositionStateData(
                        sources=state_dict["sources"],
                        nodes=state_dict["nodes"],
                        edges=state_dict["edges"],
                        outputs=state_dict["outputs"],
                        metadata_=state_dict["metadata"],
                        is_valid=current_record.is_valid,
                        validation_errors=current_record.validation_errors,
                        composer_meta={"guided_session": cleared.to_dict()},
                    ),
                    command.response,
                )
                state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=state_data, derived_from_state_id=str(current_record.id)),
                    provenance="convergence_persist",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.composer.record_pending_proposal_rejection(
                        authority=authority,
                        actor=command.actor,
                        created_at=now,
                        reason="operator_rejected",
                    )
                result_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == state_id)).one()
                result_state = self._row_to_state_record(result_row)
                updated_row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == pid)).one()
                proposal_record = replace(
                    _proposal_record_from_row(updated_row),
                    pipeline_metadata=_pipeline_public_metadata(authority),
                )
                response = project_guided_response(result_state, payloads=())
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(proposal_id=command.proposal_id, result_state_id=result_state.id)
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=result_state.id, proposal_id=command.proposal_id),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                return GuidedPipelineProposalStageSettlement(
                    result_state=result_state,
                    proposal=proposal_record,
                    audit_messages=(),
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        settlement = cast("GuidedPipelineProposalStageSettlement", await self._run_sync(_sync))
        _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "guided_staged", "result": "rejected"})
        return settlement

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

    def _restore_guided_confirmation_authority_on_connection(
        self,
        conn: Connection,
        *,
        session_id: str,
        proposal_id: UUID,
        draft_hash: str,
        reviewed_facts: Mapping[str, Any],
    ) -> AuthoritativePipelineProposal:
        proposal_row = conn.execute(
            select(composition_proposals_table)
            .where(composition_proposals_table.c.session_id == session_id)
            .where(composition_proposals_table.c.id == str(proposal_id))
        ).one_or_none()
        if proposal_row is None:
            raise AuditIntegrityError("guided confirmation proposal authority is missing")
        creation_rows = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.session_id == session_id)
            .where(proposal_events_table.c.proposal_id == str(proposal_id))
            .where(proposal_events_table.c.event_type == "proposal.created")
        ).fetchall()
        if len(creation_rows) != 1:
            raise AuditIntegrityError("guided confirmation requires one creation event")
        authority = _restore_authoritative_pipeline_proposal(
            conn=conn,
            row=_proposal_record_from_row(proposal_row),
            creation_event=_proposal_event_record_from_row(creation_rows[0]),
            reviewed_facts=deep_thaw(reviewed_facts),
        )
        _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
        if authority.row.status != "pending" or authority.proposal.draft_hash != draft_hash:
            raise GuidedOperationSettlementConflictError()
        return authority

    async def admit_guided_pipeline_confirmation(
        self,
        command: GuidedPipelineConfirmationAdmissionCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> PipelineDispatchRecovery | None:
        """Acquire the proposal fence before any set_pipeline dispatch."""

        if type(command) is not GuidedPipelineConfirmationAdmissionCommand:
            raise TypeError("command must be an exact GuidedPipelineConfirmationAdmissionCommand")
        sid = str(command.fence.session_id)

        def _sync() -> PipelineDispatchRecovery | None:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation, now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation["kind"] != "guided_respond":
                    raise AuditIntegrityError("guided confirmation admission requires guided_respond")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                authority = self._restore_guided_confirmation_authority_on_connection(
                    conn,
                    session_id=sid,
                    proposal_id=command.proposal_id,
                    draft_hash=command.draft_hash,
                    reviewed_facts=command.reviewed_facts,
                )
                current_row = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.session_id == sid,
                        composition_states_table.c.id == str(command.expected_current_state_id),
                    )
                ).one()
                guided = state_from_record(self._row_to_state_record(current_row)).guided_session
                if (
                    guided is None
                    or guided.active_proposal is None
                    or guided.active_proposal.proposal_id != command.proposal_id
                    or guided.active_proposal.draft_hash != command.draft_hash
                ):
                    raise GuidedOperationSettlementConflictError()

                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.claim_confirmation(proposal_id=command.proposal_id, now=now)
                return self._pipeline_dispatch_recovery_on_connection(conn, authority=authority)

        try:
            return cast(PipelineDispatchRecovery | None, await self._run_sync(_sync))
        except IntegrityError as exc:
            raise GuidedOperationSettlementConflictError() from exc

    async def record_guided_pipeline_dispatch(
        self,
        command: GuidedPipelineDispatchRecordCommand,
        *,
        session_operation_context: SessionOperationContext,
    ) -> PipelineDispatchRecovery:
        """Durably record the successful dispatch before final publication."""

        if type(command) is not GuidedPipelineDispatchRecordCommand:
            raise TypeError("command must be an exact GuidedPipelineDispatchRecordCommand")
        prepared = prepare_guided_audit_rows(invocations=(command.invocation,), llm_calls=(), chat_turns=())
        if len(prepared) != 1 or prepared[0].kind != "tool":
            raise AuditIntegrityError("guided dispatch record requires one prepared tool audit row")
        invocation_binding = PipelineDispatchAuditBinding.from_invocation(command.invocation)
        expected_binding = PipelineDispatchAuditBinding.from_persisted_envelope(cast(dict[str, Any], deep_thaw(prepared[0].envelope)))
        sid = str(command.fence.session_id)
        now = self._now()

        def _sync() -> PipelineDispatchRecovery:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation, _database_now = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation["kind"] != "guided_respond" or operation["proposal_id"] != str(command.proposal_id):
                    raise GuidedOperationSettlementConflictError()
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                authority = self._restore_guided_confirmation_authority_on_connection(
                    conn,
                    session_id=sid,
                    proposal_id=command.proposal_id,
                    draft_hash=command.draft_hash,
                    reviewed_facts=command.reviewed_facts,
                )
                if (
                    invocation_binding.tool_call_id != authority.row.tool_call_id
                    or invocation_binding.arguments_hash != authority.row.tool_arguments_hash
                    or expected_binding.tool_call_id != authority.row.tool_call_id
                    or expected_binding.arguments_hash != semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json)
                ):
                    raise AuditIntegrityError("guided dispatch record differs from proposal authority")
                existing = self._pipeline_dispatch_recovery_on_connection(conn, authority=authority)
                if existing is not None:
                    if existing.binding != expected_binding:
                        raise AuditIntegrityError("guided dispatch retry differs from durable dispatch")
                    return existing
                sequence_no = self._reserve_sequence_range(conn, sid, count=1)
                self._insert_prepared_guided_audit_rows_on_connection(
                    conn,
                    session_id=sid,
                    composition_state_id=command.expected_current_state_id,
                    audit_rows=prepared,
                    sequence_no=sequence_no,
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.mark_session_updated(updated_at=now)
                recovery = self._pipeline_dispatch_recovery_on_connection(conn, authority=authority)
                if recovery is None or recovery.binding != expected_binding:
                    raise AuditIntegrityError("guided dispatch did not become durably recoverable")
                return recovery

        return cast(PipelineDispatchRecovery, await self._run_sync(_sync))

    async def accept_guided_pipeline_proposal(
        self,
        command: GuidedPipelineProposalAcceptCommand,
        *,
        payload_store: PayloadStore | None = None,
        session_operation_context: SessionOperationContext,
    ) -> GuidedPipelineProposalStageSettlement:
        """Atomically publish accepted state, terminal event, and operation."""

        if type(command) is not GuidedPipelineProposalAcceptCommand:
            raise TypeError("command must be an exact GuidedPipelineProposalAcceptCommand")
        if command.candidate_content_hash != command.executor_content_hash:
            raise AuditIntegrityError("guided proposal candidate/executor hashes differ")
        if _composition_state_data_content_hash(command.state) != command.candidate_content_hash:
            raise AuditIntegrityError("guided accepted state differs from prepared candidate")
        audit_rows = self._prepare_guided_audit_cohort(
            audit_evidence=command.audit_evidence,
            payloads=command.payloads,
            payload_store=payload_store,
        )
        dispatch = command.dispatch
        sid = str(command.fence.session_id)
        pid = str(command.proposal_id)
        now = self._now()

        def _sync() -> GuidedPipelineProposalStageSettlement:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                operation_row, _ = self.require_guided_operation_authority_on_connection(
                    conn,
                    command.fence,
                    session_operation_context,
                )
                if operation_row["kind"] != "guided_respond" or operation_row["proposal_id"] != pid:
                    raise AuditIntegrityError("guided proposal acceptance requires guided_respond")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=command.expected_current_state_id,
                    expected_state_version=command.expected_current_state_version,
                )
                current_row = conn.execute(
                    select(composition_states_table).where(composition_states_table.c.id == str(command.expected_current_state_id))
                ).one()
                current_record = self._row_to_state_record(current_row)
                proposal_row = conn.execute(
                    select(composition_proposals_table)
                    .where(composition_proposals_table.c.session_id == sid)
                    .where(composition_proposals_table.c.id == pid)
                ).one_or_none()
                if proposal_row is None:
                    raise AuditIntegrityError("guided proposal acceptance authority is missing")
                creation_rows = conn.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == sid)
                    .where(proposal_events_table.c.proposal_id == pid)
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
                if len(creation_rows) != 1:
                    raise AuditIntegrityError("guided proposal acceptance requires one creation event")
                authority = _restore_authoritative_pipeline_proposal(
                    conn=conn,
                    row=_proposal_record_from_row(proposal_row),
                    creation_event=_proposal_event_record_from_row(creation_rows[0]),
                    reviewed_facts=deep_thaw(command.reviewed_facts),
                )
                _verify_pipeline_lifecycle_authority(conn, service=self, authority=authority)
                if authority.row.status != "pending" or authority.proposal.draft_hash != command.draft_hash:
                    raise AuditIntegrityError("guided proposal acceptance does not name the active pending draft")
                if type(authority.proposal.base) is not PresentBase:
                    raise AuditIntegrityError("guided proposal acceptance base is not present")
                base_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == sid)
                    .where(composition_states_table.c.id == str(authority.proposal.base.state_id))
                ).one_or_none()
                if base_row is None:
                    raise AuditIntegrityError("guided proposal acceptance base checkpoint is missing or cross-session")
                base_record = self._row_to_state_record(base_row)
                if composition_content_hash(state_from_record(base_record)) != authority.proposal.base.composition_content_hash:
                    raise AuditIntegrityError("guided proposal acceptance base checkpoint hash changed")
                if composition_content_hash(state_from_record(current_record)) != authority.proposal.base.composition_content_hash:
                    raise AuditIntegrityError("guided proposal acceptance review checkpoint changed authored content")
                if dispatch.tool_call_id != authority.row.tool_call_id:
                    raise AuditIntegrityError("guided proposal acceptance dispatch differs from authority")
                if dispatch.arguments_hash != semantic_redacted_pipeline_arguments_hash(authority.row.arguments_redacted_json):
                    raise AuditIntegrityError("guided proposal acceptance dispatch arguments differ from authority")

                current_guided = state_from_record(current_record).guided_session
                metadata = deep_thaw(command.state.composer_meta)
                if (
                    current_guided is None
                    or current_guided.active_proposal is None
                    or type(metadata) is not dict
                    or ("ingress" in metadata and not _valid_compartment_ingress_metadata(metadata["ingress"]))
                    or ("chat_ingress_inputs" in metadata and not _valid_chat_ingress_inputs_metadata(metadata["chat_ingress_inputs"]))
                ):
                    raise AuditIntegrityError("guided proposal acceptance checkpoint metadata is malformed")
                _verify_guided_deferred_message_authority(
                    conn,
                    session_id=sid,
                    guided=current_guided,
                )
                _verify_guided_correction_message_authority(
                    conn,
                    session_id=sid,
                    guided=current_guided,
                )
                _verify_guided_root_message_authority(
                    conn,
                    service=self,
                    session_id=sid,
                    guided=current_guided,
                )
                from elspeth.web.composer.guided.planning import verified_remaining_deferred_intents
                from elspeth.web.composer.guided.state_machine import GuidedSession

                remaining = verified_remaining_deferred_intents(
                    guided=current_guided,
                    proposal=authority.proposal,
                )
                final_guided = GuidedSession.from_dict(metadata["guided_session"])
                if (
                    final_guided.active_proposal is not None
                    or final_guided.active_edit_target is not None
                    or final_guided.deferred_intents != remaining
                    or final_guided.reviewed_sources != current_guided.reviewed_sources
                    or final_guided.reviewed_outputs != current_guided.reviewed_outputs
                    or final_guided.correction_messages != current_guided.correction_messages
                    or final_guided.root_intent_message_id != current_guided.root_intent_message_id
                ):
                    raise AuditIntegrityError("guided accepted checkpoint did not clear and consume exact authority")

                prepared_state = with_guided_response_descriptor(command.state, command.response)
                state_id = self._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=prepared_state, derived_from_state_id=str(current_record.id)),
                    provenance="tool_call",
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
                result_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == state_id)).one()
                result_state = self._row_to_state_record(result_row)
                if audit_rows:
                    sequence_no = self._reserve_sequence_range(conn, sid, count=len(audit_rows))
                    audit_messages = self._insert_prepared_guided_audit_rows_on_connection(
                        conn,
                        session_id=sid,
                        composition_state_id=result_state.id,
                        audit_rows=audit_rows,
                        sequence_no=sequence_no,
                        created_at=now,
                        session_operation_context=session_operation_context,
                    )
                else:
                    audit_messages = ()
                if _persisted_pipeline_dispatch_content_hashes(
                    conn,
                    session_id=sid,
                    dispatch=dispatch,
                ) != (command.executor_content_hash,):
                    raise AuditIntegrityError("guided proposal acceptance requires one durable exact dispatch")
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.composer.record_pending_proposal_acceptance(
                        authority=authority,
                        actor=command.actor,
                        created_at=now,
                        committed_state_id=state_id,
                        state_content_hash=command.executor_content_hash,
                        committed_state=prepared_state,
                        dispatch=dispatch,
                    )
                updated_row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == pid)).one()
                proposal_record = replace(
                    _proposal_record_from_row(updated_row),
                    pipeline_metadata=_pipeline_public_metadata(authority),
                )
                response = project_guided_response(result_state, payloads=command.payloads)
                projected_json = response_json(response)
                response_hash = guided_response_projection_hash(response)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(proposal_id=command.proposal_id, result_state_id=result_state.id)
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=result_state.id, proposal_id=command.proposal_id),
                        response_hash=response_hash,
                        actor=command.actor,
                    )
                return GuidedPipelineProposalStageSettlement(
                    result_state=result_state,
                    proposal=proposal_record,
                    audit_messages=audit_messages,
                    response_json=projected_json,
                    response_hash=response_hash,
                )

        settlement = cast(
            "GuidedPipelineProposalStageSettlement",
            await self._run_guided_sync_with_provider_projection(
                _sync,
                llm_calls=command.audit_evidence.llm_calls,
            ),
        )
        _PIPELINE_SETTLEMENT_COUNTER.add(1, {"surface": "guided_staged", "result": "accepted"})
        return settlement

    async def complete_existing_state_guided_operation(
        self,
        fence: GuidedOperationFence,
        *,
        state_id: UUID,
        expected_current_state_id: UUID,
        expected_current_state_version: int,
        actor: str,
        response_hash_factory: Callable[[CompositionStateRecord], str],
        session_operation_context: SessionOperationContext,
    ) -> CompositionStateRecord:
        """Settle a no-op/idempotent surface against one immutable state."""

        sid = str(fence.session_id)

        def _sync() -> CompositionStateRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self.require_guided_operation_authority_on_connection(conn, fence, session_operation_context)
                if state_id != expected_current_state_id:
                    raise ValueError("existing guided operation result must be the expected current state")
                self._require_guided_expected_current_state_on_connection(
                    conn,
                    session_id=sid,
                    expected_state_id=expected_current_state_id,
                    expected_state_version=expected_current_state_version,
                )
                row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.id == str(state_id))
                    .where(composition_states_table.c.session_id == sid)
                ).one_or_none()
                if row is None:
                    raise AuditIntegrityError("Guided operation result state is absent from its session")
                record = self._row_to_state_record(row)
                response_hash = response_hash_factory(record)
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=fence,
                    session_operation_context=session_operation_context,
                ) as mutation:
                    mutation.guided.bind(result_state_id=record.id)
                    mutation.guided.complete(
                        result=GuidedCompositionStateResult(state_id=record.id),
                        response_hash=response_hash,
                        actor=actor,
                    )
                return record

        return cast("CompositionStateRecord", await self._run_sync(_sync))

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
            raise AuditIntegrityError("Guided fork bound child failed archived lineage, principal, or fork message validation")

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
            raise AuditIntegrityError("Guided fork bound child must retain exactly one strict blob plan audit row")
        if not public_messages:
            raise AuditIntegrityError("Guided fork bound child has no public messages")
        edited_message = public_messages[-1]
        if (
            edited_message.role != "user"
            or edited_message.writer_principal != "session_fork"
            or edited_message.content != new_message_content
        ):
            raise AuditIntegrityError("Guided fork bound child edited message validation failed")

        state = self._row_to_state_record(state_row) if state_row is not None else None
        if edited_message.composition_state_id != (state.id if state is not None else None):
            raise AuditIntegrityError("Guided fork bound child edited message does not reference its staged current state")
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
        fence = authority.guided_fence
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

        # Allocate every copied chat id before preparing metadata or inserts.
        # Guided references and tool parents use this one exact map.
        source_messages_by_id = {str(msg.id): msg for msg in messages_to_copy}
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
            parent_session_id_str = str(source_session_id)
            new_session_id = UUID(fork_authority.child_context.fence.session_id)
            operation, _database_now = transaction.require_parent_guided_operation(fence)
            if operation["kind"] != "session_fork":
                raise AuditIntegrityError("fork_session fence is not bound to session_fork")
            bound_child_id = operation["result_session_id"]
            if bound_child_id is not None:
                if operation["originating_message_id"] != str(fork_message_id):
                    raise AuditIntegrityError("Guided fork bound child has a different fork message binding")
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
                raise AuditIntegrityError("Guided fork parent failed active custody validation")
            if (
                fork_row is None
                or fork_row.role != "user"
                or fork_row.composition_state_id != (str(source_state_record.id) if source_state_record is not None else None)
            ):
                raise AuditIntegrityError("Guided fork message changed before staging")

            locked_source_state: CompositionStateRecord | None = None
            forked_composer_meta: dict[str, Any] | None = None
            if source_state_record is not None:
                locked_source_row = transaction.read_parent_state(source_state_record.id)
                if locked_source_row is None:
                    raise AuditIntegrityError("Guided fork source checkpoint is missing or cross-session")
                locked_source_state = self._row_to_state_record(locked_source_row)
                forked_composer_meta = _strip_guided_profile_in_meta(
                    locked_source_state.composer_meta,
                    source_to_copied_message_id,
                    source_messages_by_id,
                )
                source_meta = deep_thaw(locked_source_state.composer_meta)
                if type(source_meta) is dict and "guided_session" in source_meta and source_meta["guided_session"] is not None:
                    from elspeth.web.composer.guided.errors import InvariantError
                    from elspeth.web.composer.guided.state_machine import GuidedSession

                    try:
                        source_guided = GuidedSession.from_dict(source_meta["guided_session"])
                    except (
                        InvariantError,
                        KeyError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        raise AuditIntegrityError("fork guided schema-10 authority is malformed") from exc
                    _require_pending_guided_checkpoint_proposal_authority(
                        transaction,
                        service=self,
                        session_id=parent_session_id_str,
                        checkpoint=locked_source_state,
                        guided=source_guided,
                        role="fork source",
                    )
                    if source_guided.root_intent_message_id is not None:
                        _verify_guided_root_message_authority(
                            transaction,
                            service=self,
                            session_id=parent_session_id_str,
                            guided=source_guided,
                        )
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
            transaction.parent_guided_mutations.bind_guided_fork(
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

    async def settle_guided_fork_operation(
        self,
        command: GuidedForkSettlementCommand,
    ) -> SessionRecord:
        """Atomically rewrite, activate, and complete one staged fork."""
        if type(command) is not GuidedForkSettlementCommand:
            raise TypeError("settle_guided_fork_operation command must be exact")
        parent_session_id_str = str(command.fence.session_id)
        child_session_id_str = str(command.child_session_id)

        def _sync() -> SessionRecord:
            with self._session_pair_locked_begin(parent_session_id_str, child_session_id_str) as conn:
                operation, now = self._require_session_fork_authority_on_connection(
                    conn,
                    command.authority,
                )
                if operation["kind"] != "session_fork" or operation["result_session_id"] != child_session_id_str:
                    raise AuditIntegrityError("Guided fork settlement child is not bound to the exact operation fence")
                parent = conn.execute(
                    select(
                        sessions_table.c.user_id,
                        sessions_table.c.auth_provider_type,
                    ).where(sessions_table.c.id == parent_session_id_str)
                ).one_or_none()
                if parent is None:
                    raise AuditIntegrityError("Guided fork settlement parent is missing")

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
                    raise AuditIntegrityError("Guided fork settlement child failed staged custody validation")

                current_state_row = conn.execute(
                    select(composition_states_table)
                    .where(composition_states_table.c.session_id == child_session_id_str)
                    .order_by(composition_states_table.c.version.desc())
                    .limit(1)
                    .with_for_update()
                ).one_or_none()
                current_state_id = UUID(current_state_row.id) if current_state_row is not None else None
                if current_state_id != command.expected_current_state_id:
                    raise AuditIntegrityError("Guided fork settlement staged current state changed")
                child_guided_root: tuple[str, str] | None = None
                if current_state_row is not None:
                    current_record = self._row_to_state_record(current_state_row)
                    current_meta = deep_thaw(current_record.composer_meta)
                    if type(current_meta) is dict and "guided_session" in current_meta and current_meta["guided_session"] is not None:
                        from elspeth.web.composer.guided.errors import InvariantError
                        from elspeth.web.composer.guided.state_machine import GuidedSession

                        try:
                            child_guided = GuidedSession.from_dict(current_meta["guided_session"])
                        except (InvariantError, KeyError, TypeError, ValueError) as exc:
                            raise AuditIntegrityError("Guided fork settlement child checkpoint is malformed") from exc
                    else:
                        child_guided = None
                    if child_guided is not None and child_guided.root_intent_message_id is not None:
                        existing_starts = conn.execute(
                            select(guided_operations_table.c.operation_id).where(
                                guided_operations_table.c.session_id == child_session_id_str,
                                guided_operations_table.c.kind == "guided_start",
                            )
                        ).all()
                        if existing_starts:
                            raise AuditIntegrityError("Guided fork staged child has premature start authority")
                        child_root = conn.execute(
                            select(
                                chat_messages_table.c.role,
                                chat_messages_table.c.content,
                                chat_messages_table.c.writer_principal,
                            ).where(
                                chat_messages_table.c.session_id == child_session_id_str,
                                chat_messages_table.c.id == child_guided.root_intent_message_id,
                            )
                        ).one_or_none()
                        if child_root is None or child_root.role != "user" or child_root.writer_principal != "route_user_message":
                            raise AuditIntegrityError("Guided fork staged root intent failed child custody")
                        child_guided_root = (child_guided.root_intent_message_id, child_root.content)

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
                    raise AuditIntegrityError("Guided fork settlement edited message failed staged custody validation")

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

                # The guided locator terminalizes first in SQL statement order.
                # All child rewrites and activation remain in this transaction,
                # so a later failure rolls the terminal row back atomically.
                with self._guided_session_mutation_transaction(
                    conn,
                    guided_fence=command.fence,
                    session_operation_context=command.authority.parent.parent_context,
                ) as mutation:
                    mutation.guided.complete(
                        result=GuidedSessionResult(session_id=command.child_session_id),
                        response_hash=command.response_hash,
                        actor=command.actor,
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
                        raise AuditIntegrityError("Guided fork settlement lost edited-message compare-and-swap")
                    removed_staged_state = conn.execute(
                        delete(composition_states_table).where(
                            composition_states_table.c.id == str(command.expected_current_state_id),
                            composition_states_table.c.session_id == child_session_id_str,
                        )
                    )
                    if removed_staged_state.rowcount != 1:
                        raise AuditIntegrityError("Guided fork settlement could not remove superseded staged state")
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
                        raise AuditIntegrityError("Guided fork settlement could not bind replacement state")

                final_state_id = command.rewritten_state_id or command.expected_current_state_id
                if child_guided_root is not None:
                    if final_state_id is None:
                        raise AuditIntegrityError("Guided fork start authority has no final child state")
                    from elspeth.web.sessions.guided_operations import guided_operation_request_hash
                    from elspeth.web.sessions.schemas import StartGuidedRequest

                    child_root_id, child_root_content = child_guided_root
                    child_start_operation_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"elspeth:fork-guided-start:{child_session_id_str}:{child_root_id}",
                        )
                    )
                    child_start_request = StartGuidedRequest.model_validate(
                        {
                            "operation_id": child_start_operation_id,
                            "profile": "live",
                            "intent": child_root_content,
                        },
                        strict=True,
                    )
                    child_request_hash = guided_operation_request_hash(
                        session_id=command.child_session_id,
                        kind="guided_start",
                        request=child_start_request,
                    )
                    child_response_hash = stable_hash(
                        {
                            "schema": "fork-guided-start-lineage.v1",
                            "session_id": child_session_id_str,
                            "operation_id": child_start_operation_id,
                            "root_intent_message_id": child_root_id,
                            "result_state_id": str(final_state_id),
                        }
                    )
                    conn.execute(
                        insert(guided_operations_table).values(
                            session_id=child_session_id_str,
                            operation_id=child_start_operation_id,
                            kind="guided_start",
                            status="completed",
                            request_hash=child_request_hash,
                            lease_token=None,
                            lease_expires_at=None,
                            attempt=1,
                            originating_message_id=child_root_id,
                            proposal_id=None,
                            result_kind="composition_state",
                            result_state_id=str(final_state_id),
                            result_message_id=None,
                            result_session_id=None,
                            response_hash=child_response_hash,
                            failure_code=None,
                            unproducible_output_fields=None,
                            failure_diagnostics=None,
                            created_at=now,
                            updated_at=now,
                            settled_at=now,
                        )
                    )
                    self._record_guided_fork_child_terminal_event(
                        conn,
                        authority=command.authority,
                        session_id=child_session_id_str,
                        operation_id=child_start_operation_id,
                        event_kind="completed",
                        actor="session_fork",
                        attempt=1,
                        prior_attempt=None,
                        lease_expires_at=None,
                        request_hash=child_request_hash,
                        failure_audit_cohort=None,
                        occurred_at=now,
                    )
                    final_state_row = conn.execute(
                        select(composition_states_table).where(
                            composition_states_table.c.session_id == child_session_id_str,
                            composition_states_table.c.id == str(final_state_id),
                        )
                    ).one()
                    final_guided = state_from_record(self._row_to_state_record(final_state_row)).guided_session
                    if final_guided is None:
                        raise AuditIntegrityError("Guided fork final state lost guided authority")
                    _verify_guided_root_message_authority(
                        conn,
                        service=self,
                        session_id=child_session_id_str,
                        guided=final_guided,
                    )

                activated = conn.execute(
                    update(sessions_table)
                    .where(
                        sessions_table.c.id == child_session_id_str,
                        sessions_table.c.archived_at.is_not(None),
                    )
                    .values(archived_at=None, updated_at=now)
                )
                if activated.rowcount != 1:
                    raise AuditIntegrityError("Guided fork settlement lost archived-to-active compare-and-swap")

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

    async def _session_principal_context(self, session_id: str) -> tuple[str | None, PluginAvailabilitySnapshot | None]:
        """Read the session principal and build its snapshot before a write transaction."""

        def _sync() -> tuple[str | None, PluginAvailabilitySnapshot | None]:
            with self._engine.connect() as conn:
                user_id = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id)).scalar_one_or_none()
            if user_id is None or self._plugin_snapshot_factory is None:
                return user_id, None
            return user_id, self._plugin_snapshot_factory(user_id)

        return cast(
            "tuple[str | None, PluginAvailabilitySnapshot | None]",
            await self._run_sync(_sync),
        )

    async def _run_sync_with_post_commit_projection[T](
        self,
        func: Callable[[], T],
        *,
        project: Callable[[T], None],
    ) -> T:
        """Drain one worker through cancellation, then project iff it committed."""

        worker = asyncio.create_task(self._run_sync(func))
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

    async def _run_guided_sync_with_provider_projection[T](
        self,
        func: Callable[[], T],
        *,
        llm_calls: tuple[ComposerLLMCall, ...],
    ) -> T:
        """Run one atomic settlement, then project only its committed calls."""

        return await self._run_sync_with_post_commit_projection(
            func,
            project=lambda _result: record_settled_composer_provider_calls(llm_calls, surface="guided"),
        )

    async def record_auto_commit_revocation(
        self,
        *,
        session_id: UUID,
        proposal_id: UUID,
        required_trust_mode: str,
        current_trust_mode: str,
        actor: str,
    ) -> ProposalEventRecord:
        """Idempotently record a non-terminal auto-commit revocation.

        Settlement writes this event inside its own locked transaction. This
        compatibility entry point uses the same exact-binding helper so an
        external retry cannot create a second or conflicting audit outcome.
        """
        now = self._now()
        sid = str(session_id)
        pid = str(proposal_id)

        def _sync() -> ProposalEventRecord:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                return _record_auto_commit_revocation_on_connection(
                    conn,
                    session_id=sid,
                    proposal_id=pid,
                    required_trust_mode=required_trust_mode,
                    current_trust_mode=current_trust_mode,
                    actor=actor,
                    created_at=now,
                )

        return cast(ProposalEventRecord, await self._run_sync(_sync))

    async def add_messages_atomic(
        self,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        *,
        writer_principal: ChatMessageWriterPrincipal,
        composition_state_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        session_operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
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
        if type(session_operation_kind) is not SessionOperationKind:
            raise TypeError("session_operation_kind must be an exact SessionOperationKind")
        if session_operation_kind not in {SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL}:
            raise ValueError("add_messages_atomic fenced writes require COMPOSE or PROPOSAL authority")
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if not drafts:
            return
        now = self._now()
        sid = str(session_id)
        csid = str(composition_state_id) if composition_state_id else None
        effective_state_ids = tuple(draft.composition_state_id if draft.composition_state_id is not None else csid for draft in drafts)

        def _write(conn: Connection) -> tuple[AuditMessageDraft, ...]:
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
            for draft in drafts:
                if draft.tool_calls:
                    envelopes = uncheckpointed_envelopes(conn, session_id=sid, envelopes=draft.tool_calls)
                    if not envelopes and draft.role == "audit":
                        continue
                    draft = replace(draft, tool_calls=envelopes)
                active_drafts.append(draft)
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
                )
            entries = llm_call_usage_entries(
                tuple(envelope for draft in active_drafts if draft.tool_calls is not None for envelope in draft.tool_calls)
            )
            if entries:
                # Task I1 Composer adapter (compose loop, turn cohort, planner
                # evidence): charged in the transaction that makes the audit rows durable.
                record_token_usage_on_connection(
                    conn, session_id=sid, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
                )
            with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
                session_mutations.mark_session_updated(updated_at=now)
            return tuple(active_drafts)

        def _sync() -> tuple[AuditMessageDraft, ...]:
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
                return _write(conn)

        def _project(committed_drafts: tuple[AuditMessageDraft, ...]) -> None:
            for draft in committed_drafts:
                record_settled_composer_audit_message(
                    role=draft.role,
                    writer_principal=writer_principal,
                    tool_calls=draft.tool_calls,
                )

        await self._run_sync_with_post_commit_projection(_sync, project=_project)

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
            database_now=self._guided_database_now(conn),
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
            database_now=self._guided_database_now(conn),
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
            now=self._guided_database_now(conn),
        )
