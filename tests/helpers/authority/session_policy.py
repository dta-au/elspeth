"""Typed Sessions table policy and exact named authority lookup data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Scope = Literal["session", "global"]


@dataclass(frozen=True)
class TablePolicy:
    table: str
    scope: Scope
    authority: str
    operation_authorities: tuple[tuple[str, frozenset[str]], ...] = ()

    def permits(self, site: WriterIdentity) -> bool:
        if site.authority == self.authority:
            return True
        return any(site.authority == authority and site.operation in operations for authority, operations in self.operation_authorities)


@dataclass(frozen=True)
class WriterIdentity:
    path: str
    symbol: str
    table: str
    operation: str
    fingerprint: str
    ordinal: int
    authority: str | None
    line: int = 0
    connection_escape: bool = False


@dataclass(frozen=True)
class AuthoritySymbol:
    """Map a concrete implementation boundary to its policy authority."""

    path: str
    symbol_prefix: str
    authority: str


_TABLE_POLICIES: tuple[TablePolicy, ...] = (
    TablePolicy("auth_session_families", "global", "AuthSessionAuthority"),
    TablePolicy("composer_async_operations", "session", "ComposerAsyncOperationAuthority"),
    TablePolicy("composer_inflight_requests", "session", "SessionComposerProgressAuthority"),
    TablePolicy("composer_progress_snapshots", "session", "SessionComposerProgressAuthority"),
    # ── Identity substrate and workflow governance (epoch 52,
    # elspeth-07cd19ba73) ────────────────────────────────────────────────
    #
    # Scope follows this file's existing convention: "session" where every row
    # carries ``session_id``, "global" otherwise. Durable workflow rows keep
    # their session from physical deletion; linkage does not imply cascade.
    # ``library_entries`` is global DESPITE carrying
    # ``published_from_session_id`` — a published entry deliberately outlives
    # the session it came from, which is exactly why that column makes the
    # archive soft rather than physical.
    #
    # The authority names below classify each table by the concern that owns
    # its writes. They are NOT yet typed authority classes: identity writes
    # currently go through ``sessions/identity_repository.py`` directly, which
    # is why ``test_all_production_sessions_writers_are_reviewed_typed_authorities``
    # reports them. Routing them through real authorities is Phase 4
    # (elspeth-44a9e49139); naming the owner here is what makes that a
    # measurable gap rather than an unclassified table.
    TablePolicy("approval_decisions", "global", "ApprovalAuthority"),
    # Identity withdrawal is a cross-session approval reaction under the
    # target identity lock, without taking a session lock in reverse order.
    # Only the exact lifecycle UPDATE is granted; ordinary approval ownership
    # and session linkage remain unchanged.
    TablePolicy(
        "approvals",
        "session",
        "ApprovalAuthority",
        (("IdentityApprovalLifecycleAuthority", frozenset({"update"})),),
    ),
    TablePolicy("identities", "global", "IdentityAuthority"),
    TablePolicy("identity_relationships", "global", "IdentityAuthority"),
    TablePolicy("identity_roles", "global", "IdentityAuthority"),
    TablePolicy("library_entries", "global", "LibraryAuthority"),
    # D31: an activation writes its allowance in the same transaction, so the
    # identity authority holds exactly one arm here -- insert, never revoke
    # or update; QuotaAuthority is not widened (ruling 9449).
    TablePolicy(
        "quota_policies",
        "global",
        "QuotaAuthority",
        (("IdentityAuthority", frozenset({"insert"})),),
    ),
    TablePolicy("review_attestations", "session", "ReviewAuthority"),
    TablePolicy("review_requests", "session", "ReviewAuthority"),
    TablePolicy("sso_handoffs", "global", "SsoHandoffAuthority"),
    TablePolicy("token_usage_ledger", "session", "QuotaAuthority"),
    TablePolicy("quota_provider_attempts", "session", "QuotaAuthority"),
    TablePolicy("audit_access_log", "global", "AuditAccessLogAuthority"),
    TablePolicy("blob_deletion_cleanups", "session", "SessionBlobMutationAuthority"),
    TablePolicy("blob_replacement_cleanups", "session", "SessionBlobMutationAuthority"),
    TablePolicy("blob_inline_resolutions", "session", "SessionBlobMutationAuthority"),
    TablePolicy("blob_run_links", "session", "SessionBlobMutationAuthority"),
    TablePolicy("blobs", "session", "SessionBlobMutationAuthority"),
    TablePolicy(
        "chat_messages",
        "session",
        "SessionMutationAuthority",
        (
            ("SessionForkChildMutations", frozenset({"insert"})),
            ("SessionForkAuthority", frozenset({"update"})),
            ("RunDiagnosticsAuditMutationAuthority", frozenset({"insert"})),
        ),
    ),
    TablePolicy("message_ingress_receipts", "session", "SessionMutationAuthority"),
    TablePolicy("composer_completion_events", "session", "SessionComposerMutationAuthority"),
    # elspeth-3e28029d2f. Session-scoped: every row carries ``session_id`` and a
    # ``composition_state_id`` naming the state current at rejection. Written
    # only by ``persist_compose_turn``, inside the same transaction and the same
    # per-tool-row loop as the turn's ``chat_messages`` rows -- so it takes the
    # authority its transaction siblings ``chat_messages`` and
    # ``composition_states`` take, not a composer-proposal authority: the row
    # records a refused tool call, and no proposal row is created or moved.
    # ``persist_compose_turn``'s own write path is not yet routed through a
    # named typed authority, so this policy declares where the writer BELONGS;
    # the whole-tree test still reports the writer as unreviewed until the
    # declared burn-down reaches it.
    TablePolicy("composition_rejection_events", "session", "SessionMutationAuthority"),
    TablePolicy("composition_proposals", "session", "SessionComposerMutationAuthority"),
    TablePolicy(
        "composition_states",
        "session",
        "SessionMutationAuthority",
        (
            ("SessionForkChildMutations", frozenset({"insert"})),
            ("SessionForkAuthority", frozenset({"delete"})),
            ("SessionInterpretationAuthority", frozenset({"insert"})),
        ),
    ),
    TablePolicy("elspeth_schema_identity", "global", "SessionSchemaAuthority"),
    TablePolicy("interpretation_events", "session", "SessionInterpretationAuthority"),
    TablePolicy("proposal_events", "session", "SessionComposerMutationAuthority"),
    TablePolicy(
        "proposal_blob_effect_receipts",
        "session",
        "SessionComposerMutationAuthority",
        (("SessionBlobMutationAuthority", frozenset({"insert"})),),
    ),
    TablePolicy("rate_limit_buckets", "global", "RateLimitAuthority"),
    TablePolicy("rate_limit_events", "global", "RateLimitAuthority"),
    TablePolicy(
        "run_events",
        "session",
        "SessionRunMutationAuthority",
        (("RunCancellationAuthority", frozenset({"insert"})),),
    ),
    TablePolicy("run_execution_inputs", "session", "SessionRunMutationAuthority"),
    TablePolicy(
        "run_start_permits",
        "session",
        "RunStartPermitAuthority",
        (("RunCancellationAuthority", frozenset({"update"})),),
    ),
    TablePolicy(
        "runs",
        "session",
        "SessionRunMutationAuthority",
        (
            ("GlobalRunRecoveryAuthority", frozenset({"update"})),
            ("RunCancellationAuthority", frozenset({"update"})),
            ("RunStartPermitAuthority", frozenset({"update"})),
        ),
    ),
    TablePolicy("session_operation_fences", "session", "SessionOperationAuthority"),
    TablePolicy(
        "session_operation_receipt_events",
        "session",
        "SessionOperationReceiptAuthority",
    ),
    TablePolicy(
        "session_operation_receipts",
        "session",
        "SessionOperationReceiptAuthority",
        (("SessionForkParentReceiptMutations", frozenset({"update"})),),
    ),
    # Epoch 53 (elspeth-f98e0ae8b2): one row per live BLOB_READ admission,
    # written only by the operation authority (admit, renew, release, sweep).
    TablePolicy("session_read_admissions", "session", "SessionOperationAuthority"),
    TablePolicy(
        "sessions",
        "session",
        "SessionMutationAuthority",
        (
            ("SessionOperationAuthority", frozenset({"insert", "delete"})),
            ("SessionForkAuthority", frozenset({"update"})),
            ("SessionInterpretationAuthority", frozenset({"update"})),
            ("RunDiagnosticsAuditMutationAuthority", frozenset({"update"})),
            # Ruling 8925 #3 (Task-5 inventory record, P4-D2 elspeth-44751b3265):
            # the session-side preferences writer
            # ``SessionServiceImpl.update_composer_preferences._sync`` updates
            # ``trust_mode`` / ``density_default`` under the per-session write
            # lock and deliberately NEVER under the compose lease, so a
            # mid-compose trust downgrade always lands. It takes the composer
            # authority its ``proposal_events`` audit row already carries: one
            # arm, update only.
            ("SessionComposerMutationAuthority", frozenset({"update"})),
        ),
    ),
    TablePolicy("sessions_cleanup_claims", "global", "SessionCleanupClaimAuthority"),
    TablePolicy("skill_markdown_history", "global", "SkillMarkdownHistoryAuthority"),
    TablePolicy("user_preferences", "global", "UserPreferenceAuthority"),
    TablePolicy("user_secrets", "global", "UserSecretAuthority"),
    TablePolicy("web_instances", "global", "WebInstanceMembershipAuthority"),
    TablePolicy("websocket_tickets", "session", "SessionWebsocketTicketAuthority"),
)


_PROTECTED_LOGICAL_TABLES = {
    "audit_access_log": "audit_access_log",
    "rate_limit_buckets": "rate_limit_buckets",
    "rate_limit_events": "rate_limit_events",
    "run_start_permits": "run_start_permits",
    "schema_identity": "elspeth_schema_identity",
    "session_operation_fences": "session_operation_fences",
    "sessions_cleanup_claims": "sessions_cleanup_claims",
    "web_instances": "web_instances",
}


_NAMED_AUTHORITY_SYMBOLS: tuple[AuthoritySymbol, ...] = (
    AuthoritySymbol(
        "src/elspeth/web/coordination/auth_session_authority.py",
        "RepositoryAuthSessionAuthority",
        "AuthSessionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_record_auto_commit_revocation_on_connection",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._settle_pipeline_composition_proposal._sync",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._record_composer_revocation_on_connection",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.admit",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.claim_next",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.renew_claim",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.release_claim",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.request_cancel",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.settle_unstarted",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority._settle_recovery",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "start_composer_operation_on_connection",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "bind_composer_operation_user_message_on_connection",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "settle_composer_operation_on_connection",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._advance_exclusive_fence_on_connection",
        "SessionOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "begin_provider_attempt_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "settle_provider_attempt_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "cancel_undispatched_provider_attempt_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "record_token_usage_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.complete_admission_refusal",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_lifecycle_authority.py",
        "RepositoryApprovalLifecycleAuthority.apply",
        "IdentityApprovalLifecycleAuthority",
    ),
    # ACA shared UI state and quota transactions; admit exact methods only.
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.begin_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.heartbeat_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.end_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.bind_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.publish",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.replay",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.clear",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.cleanup_expired",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/websocket_ticket_authority.py",
        "RepositorySessionWebsocketTicketAuthority.issue",
        "SessionWebsocketTicketAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/websocket_ticket_authority.py",
        "RepositorySessionWebsocketTicketAuthority.consume",
        "SessionWebsocketTicketAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "RepositoryRateLimitAuthority.admit",
        "RateLimitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "RepositoryRateLimitAuthority.admit_on_connection",
        "RateLimitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "RepositoryRateLimitAuthority._cleanup",
        "RateLimitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations._adopt_pending_run_outputs",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.rebind_run_ownership",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.mark_recovery_required",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.mark_recovery_outputs_finalized",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_start_permit_authority.py",
        "RepositoryRunStartPermitAuthority.create_pending",
        "RunStartPermitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_start_permit_authority.py",
        "RepositoryRunStartPermitAuthority.issue",
        "RunStartPermitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_start_permit_authority.py",
        "RepositoryRunStartPermitAuthority._assess",
        "RunStartPermitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_cancellation_authority.py",
        "RepositoryRunCancellationAuthority.request",
        "RunCancellationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.create_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "SessionComposerMutationAuthority",
    ),
    # ── preferences writers (ruling 8925 #3, Task-5 inventory record): both
    # write directly, serialised by the per-session write lock / the sessions
    # engine's write transaction, never by the compose lease ─────────────
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.update_composer_preferences",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.update_composer_preferences",
        "UserPreferenceAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "SkillMarkdownHistoryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.issue",
        "SsoHandoffAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.consume",
        "SsoHandoffAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "UserSecretAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.delete_secret",
        "UserSecretAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_audit_grade_view",
        "AuditAccessLogAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "RunDiagnosticsAuditMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository",
        "SessionOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.insert_child_state",
        "SessionForkChildMutations",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.append_child_messages",
        "SessionForkChildMutations",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_ForkParentReceiptMutations.bind_fork_receipt",
        "SessionForkParentReceiptMutations",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.revert_state_for_operation_receipt",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_fork_operation_receipt",
        "SessionForkAuthority",
    ),
    *(
        AuthoritySymbol(
            "src/elspeth/web/sessions/operation_receipts.py",
            symbol,
            "SessionOperationReceiptAuthority",
        )
        for symbol in (
            "_append_event",
            "reserve_operation_receipt",
            "renew_operation_receipt",
            "bind_operation_receipt",
            "settle_operation_receipt",
        )
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.decide_and_soft_archive",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.record_plugin_crash_breadcrumb",
        "SessionMutationAuthority",
    ),
    # P4-D6 family A2a (elspeth-99949c96ca): the service's message/state paths
    # bump ``updated_at`` and record refused tool calls through these facets.
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.mark_session_updated",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.record_composition_rejection",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryCompositionStateMutations.append_state",
        "SessionMutationAuthority",
    ),
    # P4-D6 family A2b (elspeth-99949c96ca): the title route holds the same
    # COMPOSE operation the message writers hold, and settling a pending
    # interpretation event is a fenced COMPOSE write on the interpretation
    # authority. Both are method-exact.
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.set_title",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.resolve_pending_event",
        "SessionInterpretationAuthority",
    ),
    # P4-D6 family A2b (elspeth-99949c96ca): the two SHARED row writers are the
    # SessionMutationAuthority boundary itself, not a facet behind one. Each
    # takes a required ``session_operation_context`` and proves it live on the
    # caller's connection (``_require_session_write_authority_on_connection``)
    # immediately before its INSERT, so no unfenced path into ``chat_messages``
    # or ``composition_states`` remains for any of their ~30 call sites.
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._insert_chat_message",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._insert_message_ingress_receipt",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._insert_composition_state",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_session_opt_out",
        "SessionInterpretationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations._create_pending",
        "SessionInterpretationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event",
        "SessionInterpretationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryComposerCompletionMutations.mark_ready_for_review",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryComposerCompletionMutations.record_yaml_export",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.create_pending_run",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.transition_run_status",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.append_run_event",
        "SessionRunMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.cancel_orphaned_run_records",
        "GlobalRunRecoveryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority._cancel_candidate",
        "GlobalRunRecoveryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
        "GlobalRunRecoveryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.insert_blob_run_link",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations._record_applied_blob_proposal_effect",
        "SessionBlobMutationAuthority",
    ),
    *(
        AuthoritySymbol(
            "src/elspeth/web/coordination/repository.py",
            f"_RepositoryBlobMutations.{method}",
            "SessionBlobMutationAuthority",
        )
        for method in (
            "prepare_blob_replacement",
            "mark_blob_replacement_staged",
            "commit_blob_replacement",
            "retire_blob_replacement",
            "abort_blob_replacement",
        )
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.insert_blob_inline_resolutions",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_pending_output_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.finalize_pending_output_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_ready",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.discard_pending_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_abandoned_blob_reservation",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.prepare_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_deletion_staged",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_atomic_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.abort_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_run_output_blob_ready",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_run_output_blob_error",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_reserve_pending_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_finalize_reserved_blob",
        "SessionBlobMutationAuthority",
    ),
    # ── blob service transaction boundaries (D6 family B, elspeth-af0fdc3cc6):
    # the DML helpers that execute on the connection a registered boundary
    # passes in, and the phase helpers whose ``with`` block IS the blob
    # transaction. Method-exact; a replacement or sibling symbol is not bound ──
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_insert_pending_blob_row",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_persist_blob_content",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_discard_nonidempotent_reservation",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "reconcile_inline_custody_publications",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "persist_inline_custody_blob_on_connection",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._delete_fork_blob_row_locked",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._finalize_registered_fork_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._cleanup_blobs_for_fork_sync",
        "SessionBlobMutationAuthority",
    ),
    # ── composer blob tool handlers (D6 family B, elspeth-af0fdc3cc6, ruling
    # Q2): each handler's ``with locked_session_transaction(...)`` block IS
    # its transaction — the row lock, the retention guards, the staged bytes
    # and the DML commit together — so the handler is the boundary. The
    # session lock wrapper is imported, so no acquisition row is reported for
    # these; the writer rows below are the whole inventory ───────────────
    # ── web_instances membership writer (6b-2, elspeth-66a19780b1): one
    # authority, four lifecycle methods, method-exact ─────────────────────
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.register",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.heartbeat",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.begin_drain",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.stop",
        "WebInstanceMembershipAuthority",
    ),
    # ── identity substrate (P4-D6 elspeth-e483fe7f85): RepositoryIdentityAuthority,
    # method-exact; every acquisition stays inside its method ─────────────
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.assert_relationship",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.disable_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.enable_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.ensure_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_role",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.purge_stale_pending_identities",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.retire_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_relationship",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_role",
        "IdentityAuthority",
    ),
    # ── P4-D6 family S: sessions substrate (elspeth-9ebb0fcf10) ─────────────
    # The schema-management authority owns every write to the schema-identity
    # table and the two SQLite header sentinels; nothing else in the tree may.
    AuthoritySymbol(
        "src/elspeth/web/sessions/schema.py",
        "SessionSchemaAuthority",
        "SessionSchemaAuthority",
    ),
    # Substrate DML bound by symbol to the authority that already owns the
    # table (hub ruling (ii) 2026-09-07: no move into repository.py).
    AuthoritySymbol(
        "src/elspeth/web/sessions/dead_site_supersession.py",
        "supersede_dead_site_pending_interpretation_events",
        "SessionInterpretationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/proposal_blob_effects.py",
        "record_applied_blob_proposal_effect",
        "SessionBlobMutationAuthority",
    ),
    # The module-level session-lock wrapper belongs to SessionOperationAuthority
    # (hub ruling (i), amended 2026-09-07).  Its acquisition row is an honest
    # ESCAPE and deliberately NOT admitted: ``_WrapperContainmentProof`` flips
    # it on its own once every caller proves, and on this tree four do not --
    # coordination/repository.py ``PostgresSessionOperationRepository._locked_transaction``
    # and coordination/sqlite_authority.py ``_locked_transaction`` re-yield the
    # connection to base-class callers (ruling 2 keeps ``mutate``'s constructor
    # hand-off an escape), and composer/tools/blobs.py ``_execute_update_blob``
    # / ``_execute_delete_blob_locked`` forward it across the module boundary
    # into ``record_applied_blob_proposal_effect`` (family B2).
    AuthoritySymbol(
        "src/elspeth/web/sessions/locking.py",
        "locked_session_transaction",
        "SessionOperationAuthority",
    ),
    # Identity workflow authorities: exact methods with contained transactions.
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.decide",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.mark_decision_seen",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.request",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.withdraw",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "supersede_open_approvals",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_workflow_inspect",
        "AuditAccessLogAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.accept",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.deprecate",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.publish",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.recall",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.reject",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority._revoke_identity_policy_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority._set_identity_policy_on_connection",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority.revoke_identity_policy",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.attest",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.cancel",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.request",
        "ReviewAuthority",
    ),
)


# Connection acquisition is a separate capability from table mutation.  A
# table authority name alone must never bless an Engine/Connection boundary.
# Entries here are exact (not prefixes) and may only contain the acquisition;
# returned, yielded, or forwarded raw connections remain violations.
_CONTAINED_CONNECTION_AUTHORITIES: tuple[AuthoritySymbol, ...] = (
    AuthoritySymbol(
        "src/elspeth/web/coordination/auth_session_authority.py",
        "RepositoryAuthSessionAuthority.create",
        "AuthSessionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/auth_session_authority.py",
        "RepositoryAuthSessionAuthority.is_active",
        "AuthSessionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/auth_session_authority.py",
        "RepositoryAuthSessionAuthority.revoke",
        "AuthSessionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "ComposerAsyncOperationAuthority.claim_next",
        "ComposerAsyncOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.complete_composer_async_operation._sync",
        "SessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.fail_composer_async_operation._sync",
        "ComposerAsyncOperationAuthority",
    ),
    # ACA shared UI state and quota transactions; admit exact methods only.
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.begin_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.heartbeat_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.end_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.bind_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.publish",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.replay",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.clear",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_authority.py",
        "SessionComposerProgressAuthority.cleanup_expired",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/websocket_ticket_authority.py",
        "RepositorySessionWebsocketTicketAuthority.issue",
        "SessionWebsocketTicketAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/websocket_ticket_authority.py",
        "RepositorySessionWebsocketTicketAuthority.consume",
        "SessionWebsocketTicketAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "RepositoryRateLimitAuthority.admit",
        "RateLimitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "RepositoryRateLimitAuthority._cleanup",
        "RateLimitAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.create_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/mutation_capabilities.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "SessionComposerMutationAuthority",
    ),
    # ── preferences writers (ruling 8925 #3): each ``_sync`` opens its one
    # write transaction inside its own ``with`` and never hands it out ────
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.update_composer_preferences._sync",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.update_composer_preferences._sync",
        "UserPreferenceAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "SkillMarkdownHistoryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.issue",
        "SsoHandoffAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.consume",
        "SsoHandoffAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "UserSecretAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.delete_secret",
        "UserSecretAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_audit_grade_view",
        "AuditAccessLogAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "RunDiagnosticsAuditMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_reserve_pending_blob",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_finalize_reserved_blob",
        "SessionBlobMutationAuthority",
    ),
    # D6 family B (elspeth-af0fdc3cc6): the phase helpers whose non-escaping
    # acquisitions the forward proof contains. Four of them also carry ONE
    # custody-lock forward the proof refuses (finding F1); that row stays a
    # violation in the drift counters regardless of this registration.
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_discard_nonidempotent_reservation",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "reconcile_inline_custody_publications",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._cleanup_blobs_for_fork_sync",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.mutate_fork_creation",
        "SessionOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._finalize_registered_fork_blob_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.cancel_orphaned_run_records",
        "GlobalRunRecoveryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "_persist_blob_content",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
        "GlobalRunRecoveryAuthority",
    ),
    # ── web_instances membership writer (6b-2, elspeth-66a19780b1): each
    # method opens one write_connection and never hands it out ──────────
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.register",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.heartbeat",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.begin_drain",
        "WebInstanceMembershipAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.stop",
        "WebInstanceMembershipAuthority",
    ),
    # ── identity substrate (P4-D6 elspeth-e483fe7f85): RepositoryIdentityAuthority,
    # method-exact; every acquisition stays inside its method ─────────────
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.assert_relationship",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.disable_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.enable_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.ensure_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_role",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.purge_stale_pending_identities",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.retire_identity",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_relationship",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_role",
        "IdentityAuthority",
    ),
    # P4-D6 family S: the schema authority's one write transaction (proven
    # contained by the scanner).
    AuthoritySymbol(
        "src/elspeth/web/sessions/schema.py",
        "SessionSchemaAuthority.stamp_sentinels",
        "SessionSchemaAuthority",
    ),
    # Identity workflow transactions whose connections stay inside the exact
    # typed authority method named here.
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "IdentityAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.publish",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.accept",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.reject",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.deprecate",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/library_authority.py",
        "RepositoryLibraryAuthority.recall",
        "LibraryAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority.set_identity_policy",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_policy_authority.py",
        "RepositoryQuotaPolicyAuthority.revoke_identity_policy",
        "QuotaAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.request",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.cancel",
        "ReviewAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/review_authority.py",
        "RepositoryReviewAuthority.attest",
        "ReviewAuthority",
    ),
)


def _authority_for(path: str, symbol: str) -> str | None:
    for binding in _NAMED_AUTHORITY_SYMBOLS:
        if binding.path == path and (symbol == binding.symbol_prefix or symbol.startswith(f"{binding.symbol_prefix}.")):
            return binding.authority
    return None


def _contained_connection_authority_for(path: str, symbol: str) -> str | None:
    for binding in _CONTAINED_CONNECTION_AUTHORITIES:
        if binding.path == path and symbol == binding.symbol_prefix:
            return binding.authority
    return None
