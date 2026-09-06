"""Fail-closed production inventory for Sessions database mutation authority.

Task 5 deliberately starts RED: the table policy is complete, but direct
production writers are not admitted to the reviewed writer manifest until
they are routed through a named typed authority.  The scanner identities are
AST fingerprints plus occurrence ordinals, so replacing, duplicating, or
removing a site cannot be hidden by an unchanged count.
"""

from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from tests.helpers.tree_gate import iter_gate_files

from elspeth.web.sessions.models import metadata as sessions_metadata
from elspeth_lints.core.ast_dump import stable_ast_dump

Scope = Literal["session", "global"]
DatabaseDomain = Literal["sessions", "non_sessions", "unknown"]


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


@dataclass(frozen=True)
class _NameBinding:
    node: ast.AST
    value: ast.expr | None = None
    imported: str | None = None
    no_return: bool = False


_TABLE_POLICIES: tuple[TablePolicy, ...] = (
    # ── Identity substrate and workflow governance (epoch 52,
    # elspeth-07cd19ba73) ────────────────────────────────────────────────
    #
    # Scope follows this file's existing convention: "session" where every row
    # carries ``session_id`` and dies with the session, "global" otherwise.
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
    TablePolicy("approvals", "session", "ApprovalAuthority"),
    TablePolicy("identities", "global", "IdentityAuthority"),
    TablePolicy("identity_relationships", "global", "IdentityAuthority"),
    TablePolicy("identity_roles", "global", "IdentityAuthority"),
    TablePolicy("library_entries", "global", "LibraryAuthority"),
    # D31: an activation writes its allowance in the same transaction, so the
    # identity authority holds exactly one arm here -- insert, never revoke
    # or update; QuotaAuthority is not widened (ruling 9449).
    TablePolicy("quota_policies", "global", "QuotaAuthority", (("IdentityAuthority", frozenset({"insert"})),)),
    TablePolicy("review_attestations", "session", "ReviewAuthority"),
    TablePolicy("review_requests", "session", "ReviewAuthority"),
    TablePolicy("sso_handoffs", "global", "SsoHandoffAuthority"),
    TablePolicy("token_usage_ledger", "session", "QuotaAuthority"),
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
    TablePolicy(
        "composition_proposals",
        "session",
        "SessionComposerMutationAuthority",
        (("GuidedSessionComposerMutationAuthority", frozenset({"update"})),),
    ),
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
    TablePolicy("elspeth_schema_identity", "global", "SessionSchemaBootstrapAuthority"),
    TablePolicy(
        "guided_operation_admission_blocks",
        "session",
        "GuidedSessionMutationAuthority",
        (("GuidedSessionAdmissionAuthority", frozenset({"insert"})),),
    ),
    TablePolicy(
        "guided_operation_events",
        "session",
        "GuidedSessionMutationAuthority",
        (("SessionForkAuthority", frozenset({"insert"})),),
    ),
    TablePolicy(
        "guided_operations",
        "session",
        "GuidedSessionMutationAuthority",
        (
            ("SessionForkParentGuidedMutations", frozenset({"update"})),
            ("GuidedSessionAdmissionAuthority", frozenset({"insert", "update"})),
            ("SessionForkAuthority", frozenset({"insert"})),
        ),
    ),
    TablePolicy("interpretation_events", "session", "SessionInterpretationAuthority"),
    TablePolicy(
        "proposal_events",
        "session",
        "SessionComposerMutationAuthority",
        (("GuidedSessionComposerMutationAuthority", frozenset({"insert"})),),
    ),
    TablePolicy(
        "proposal_blob_effect_receipts",
        "session",
        "SessionComposerMutationAuthority",
        (("SessionBlobMutationAuthority", frozenset({"insert"})),),
    ),
    TablePolicy("rate_limit_buckets", "global", "RateLimitAuthority"),
    TablePolicy("rate_limit_events", "global", "RateLimitAuthority"),
    TablePolicy("run_events", "session", "SessionRunMutationAuthority"),
    TablePolicy("run_execution_inputs", "session", "SessionRunMutationAuthority"),
    TablePolicy("run_start_permits", "session", "RunStartPermitAuthority"),
    TablePolicy(
        "runs",
        "session",
        "SessionRunMutationAuthority",
        (("GlobalRunRecoveryAuthority", frozenset({"update"})),),
    ),
    TablePolicy("session_operation_fences", "session", "SessionOperationAuthority"),
    # Epoch 53 (elspeth-f98e0ae8b2): one row per live BLOB_READ admission,
    # written only by the operation authority (admit, renew, release, sweep).
    TablePolicy("session_read_admissions", "session", "SessionOperationAuthority"),
    TablePolicy(
        "sessions",
        "session",
        "SessionMutationAuthority",
        (
            ("SessionOperationAuthority", frozenset({"insert", "delete"})),
            ("GuidedSessionMutationAuthority", frozenset({"update"})),
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
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "SessionComposerMutationAuthority",
    ),
    # P4-D6 family A1 (elspeth-99949c96ca): guided staging creates its pending
    # proposal here -- the one authority the composition_proposals policy lets
    # insert -- basing it on the checkpoint the same transaction just wrote.
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_guided_pipeline_proposal",
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
        "_ForkParentGuidedMutations.bind_guided_fork",
        "SessionForkParentGuidedMutations",
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
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_session_opt_out",
        "SessionInterpretationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
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
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reserve_guided_operation",
        "GuidedSessionAdmissionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reconcile_guided_start_operation",
        "GuidedSessionAdmissionAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.renew_guided_operation",
        "GuidedSessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.fail_guided_operation_with_audit",
        "GuidedSessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation",
        "SessionForkAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations",
        "GuidedSessionMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.reject_pending_proposal",
        "GuidedSessionComposerMutationAuthority",
    ),
    # ── P4-D6 family A1 (elspeth-99949c96ca): the guided lifecycle ``_sync``
    # bodies hand their terminal proposal events to these two method-exact
    # facets (the operation's OWN held proposal: no confirmation sweep) ────
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_rejection",
        "GuidedSessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_acceptance",
        "GuidedSessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._record_guided_fork_child_terminal_event",
        "SessionForkAuthority",
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
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._fork_cleanup_transaction",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._prepare_fork_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._mark_fork_deletion_staged",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._commit_fork_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._retire_fork_deletion",
        "SessionBlobMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl._abort_fork_deletion",
        "SessionBlobMutationAuthority",
    ),
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
)

# Connection acquisition is a separate capability from table mutation.  A
# table authority name alone must never bless an Engine/Connection boundary.
# Entries here are exact (not prefixes) and may only contain the acquisition;
# returned, yielded, or forwarded raw connections remain violations.
_CONTAINED_CONNECTION_AUTHORITIES: tuple[AuthoritySymbol, ...] = (
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
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
    AuthoritySymbol(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.mutate_fork_creation",
        "SessionOperationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.cancel_orphaned_run_records",
        "GlobalRunRecoveryAuthority",
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
)

# Literal identities for writers that sit behind an exact named authority.
# This manifest grows only after routing a site through its table-specific
# typed authority.
_REVIEWED_WRITERS: tuple[WriterIdentity, ...] = (
    # append_audit_message -> append_audit_messages (7e1f1f86e): one locked
    # transaction inserts the whole audit cohort and bumps the session once.
    WriterIdentity(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "chat_messages",
        "insert",
        "d0ef6a582e1fc2a3",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=121,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "sessions",
        "update",
        "d0ef6a582e1fc2a3",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=137,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "proposal_events",
        "insert",
        "55c8854837524a3f",
        1,
        "SessionComposerMutationAuthority",
        line=3169,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "composition_proposals",
        "insert",
        "4d7437366c54fbeb",
        1,
        "SessionComposerMutationAuthority",
        line=3185,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "proposal_events",
        "insert",
        "58a94a42ebf58130",
        1,
        "SessionComposerMutationAuthority",
        line=3292,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "composition_proposals",
        "insert",
        "be0a21ec508fea9c",
        1,
        "SessionComposerMutationAuthority",
        line=3303,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "proposal_events",
        "insert",
        "17277db356846ba4",
        1,
        "SessionComposerMutationAuthority",
        line=3502,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "composition_proposals",
        "update",
        "8b790876eab3ca5d",
        1,
        "SessionComposerMutationAuthority",
        line=3513,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "proposal_events",
        "insert",
        "f381d823a069aec1",
        1,
        "SessionComposerMutationAuthority",
        line=3621,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "proposal_blob_effect_receipts",
        "update",
        "838f74d6c673e89a",
        1,
        "SessionComposerMutationAuthority",
        line=3633,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "composition_proposals",
        "update",
        "136c26279232b29b",
        1,
        "SessionComposerMutationAuthority",
        line=3645,
    ),
    # P4-D6 family A1 (elspeth-99949c96ca): guided staging's proposal.created
    # event + pending proposal row, based on the checkpoint written moments
    # earlier in the same transaction.
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_guided_pipeline_proposal",
        "proposal_events",
        "insert",
        "fc9b265fb5f1ff86",
        1,
        "SessionComposerMutationAuthority",
        line=3369,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_guided_pipeline_proposal",
        "composition_proposals",
        "insert",
        "48004d8c43dcec6a",
        1,
        "SessionComposerMutationAuthority",
        line=3380,
    ),
    # ── Ruling 8925 #3 (Task-5 inventory record, P4-D2 elspeth-44751b3265):
    # the preferences writers write directly. Session side: audit row then
    # session row in one transaction under the per-session write lock, never
    # the compose lease. User side: one atomic dialect upsert (sqlite /
    # postgresql arm) inside the sessions engine's write transaction. The
    # compose-leased facet pair that once mirrored the session side is
    # deleted; ``RepositoryUserPreferenceAuthority`` no longer exists. ──────
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.update_composer_preferences._sync",
        "proposal_events",
        "insert",
        "78fe65cf99c28d0f",
        1,
        "SessionComposerMutationAuthority",
        line=7335,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.update_composer_preferences._sync",
        "sessions",
        "update",
        "78fe65cf99c28d0f",
        1,
        "SessionComposerMutationAuthority",
        line=7350,
    ),
    # Each dialect arm is rebound through ``stmt = stmt.on_conflict_do_update``
    # and executed once at :475; since elspeth-a85fb1555b the rebinding no
    # longer reaches itself, so each arm is an ``upsert`` whose identity
    # carries its arm, the rebinding and the execution.
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.update_composer_preferences._sync",
        "user_preferences",
        "upsert",
        "c060410920810694",
        1,
        "UserPreferenceAuthority",
        line=450,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.update_composer_preferences._sync",
        "user_preferences",
        "upsert",
        "17f7d3261ce89a25",
        1,
        "UserPreferenceAuthority",
        line=452,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.update_composer_preferences._sync",
        "<sessions-write-connection>",
        "write_connection",
        "a793cf5b38728669",
        1,
        "UserPreferenceAuthority",
        line=334,
    ),
    # Two dialect arms rebound through ``stmt = stmt.on_conflict_do_nothing``
    # and executed once at :63: each arm is an ``upsert`` bound to its
    # execution (elspeth-a85fb1555b).
    WriterIdentity(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "skill_markdown_history",
        "upsert",
        "b6800432c410bf7d",
        1,
        "SkillMarkdownHistoryAuthority",
        line=55,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "skill_markdown_history",
        "upsert",
        "4a428fcdfa48f3bf",
        1,
        "SkillMarkdownHistoryAuthority",
        line=57,
    ),
    # The single-use SSO handoff (identity sprint step C, elspeth-07cd19ba73;
    # family T of elspeth-e483fe7f85). ``issue`` purges this identity's
    # expired codes then inserts the new one; ``consume`` is ONE conditional
    # ``UPDATE ... RETURNING`` that claims the row and decides it may be
    # claimed in the same statement, then purges expired rows AFTER the
    # claim. The two ``issue`` statements share a fingerprint because they
    # are shaped alike; the (table, operation) pair keeps them distinct.
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.issue",
        "sso_handoffs",
        "delete",
        "637344665f07bd53",
        1,
        "SsoHandoffAuthority",
        line=84,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.issue",
        "sso_handoffs",
        "insert",
        "637344665f07bd53",
        1,
        "SsoHandoffAuthority",
        line=90,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.consume",
        "sso_handoffs",
        "update",
        "3fd95b6dc7620925",
        1,
        "SsoHandoffAuthority",
        line=112,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.consume",
        "sso_handoffs",
        "delete",
        "6c55e19965dc4706",
        1,
        "SsoHandoffAuthority",
        line=124,
    ),
    # upsert_encrypted_secret builds one prebuilt upsert per dialect arm
    # (sqlite :170, postgresql :178, mysql :186) and executes it once at :190;
    # each arm is its own writer identity.  The sqlite and postgresql arms
    # are ``on_conflict_do_update`` upserts; the mysql arm's
    # ``on_duplicate_key_update`` is not an ``on_conflict_do_*`` and stays an
    # insert (elspeth-a85fb1555b).
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "upsert",
        "3c2c7c4ba74f1683",
        1,
        "UserSecretAuthority",
        line=170,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "upsert",
        "9ba92d555134ed3e",
        1,
        "UserSecretAuthority",
        line=178,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "insert",
        "bf6f84502dde47d2",
        1,
        "UserSecretAuthority",
        line=186,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.delete_secret",
        "user_secrets",
        "delete",
        "758e78719a4da071",
        1,
        "UserSecretAuthority",
        line=196,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_audit_grade_view",
        "audit_access_log",
        "insert",
        "73cdfc40ca21ba52",
        1,
        "AuditAccessLogAuthority",
        line=102,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations._record_applied_blob_proposal_effect",
        "proposal_blob_effect_receipts",
        "insert",
        "8c66fbb679cf7fa3",
        1,
        "SessionBlobMutationAuthority",
        line=2797,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.prepare_blob_replacement",
        "blob_replacement_cleanups",
        "insert",
        "bca2ee74a8a93022",
        1,
        "SessionBlobMutationAuthority",
        line=1988,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_replacement_staged",
        "blob_replacement_cleanups",
        "update",
        "6128417bd9a69f02",
        1,
        "SessionBlobMutationAuthority",
        line=2051,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_replacement",
        "blob_replacement_cleanups",
        "update",
        "2ecd759927d60392",
        1,
        "SessionBlobMutationAuthority",
        line=2105,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_replacement",
        "blobs",
        "update",
        "d2ea73ee0e8472e4",
        1,
        "SessionBlobMutationAuthority",
        line=2113,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_blob_replacement",
        "blob_replacement_cleanups",
        "delete",
        "75415f0d12b7a661",
        1,
        "SessionBlobMutationAuthority",
        line=2165,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.abort_blob_replacement",
        "blob_replacement_cleanups",
        "delete",
        "75415f0d12b7a661",
        1,
        "SessionBlobMutationAuthority",
        line=2185,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.record_plugin_crash_breadcrumb",
        "sessions",
        "update",
        "016ef4c50b5e5390",
        1,
        "SessionMutationAuthority",
        line=563,
    ),
    # P4-D6 family A2a (elspeth-99949c96ca): the service's message/state paths
    # route their updated_at bump and refused-tool-call record here.
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.mark_session_updated",
        "sessions",
        "update",
        "44e58336446946af",
        1,
        "SessionMutationAuthority",
        line=589,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.record_composition_rejection",
        "composition_rejection_events",
        "insert",
        "e0386cbdb277f0b0",
        1,
        "SessionMutationAuthority",
        line=624,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryCompositionStateMutations.append_state",
        "composition_states",
        "insert",
        "2eb8d0e06248c64d",
        1,
        "SessionMutationAuthority",
        line=784,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_session_opt_out",
        "interpretation_events",
        "insert",
        "600ab798ca133699",
        1,
        "SessionInterpretationAuthority",
        line=1263,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_session_opt_out",
        "sessions",
        "update",
        "3966641511f4795d",
        1,
        "SessionInterpretationAuthority",
        line=1290,
    ),
    # create_or_reconcile_pending: the stale-site update writes SUPERSEDED, not
    # ABANDONED (elspeth-dbc39dd367, carried by the multi-replica merge); the
    # opt-out marker insert (:1025) and the event insert (:1052) are distinct
    # statements; the appended head insert now sits under the guided-custody
    # assertion and the dead-site retirement in the same transaction.
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
        "interpretation_events",
        "update",
        "b62dec99662793d2",
        1,
        "SessionInterpretationAuthority",
        line=1039,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
        "interpretation_events",
        "insert",
        "83a9e49f7b465443",
        1,
        "SessionInterpretationAuthority",
        line=1116,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
        "interpretation_events",
        "insert",
        "c969a753999273dd",
        1,
        "SessionInterpretationAuthority",
        line=1143,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
        "composition_states",
        "insert",
        "e2f9c8046ec5b809",
        1,
        "SessionInterpretationAuthority",
        line=1193,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event",
        "interpretation_events",
        "insert",
        "33b3cdac54fa6f56",
        1,
        "SessionInterpretationAuthority",
        line=1321,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "sessions",
        "insert",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=4138,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "session_operation_fences",
        "insert",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=4148,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "session_operation_fences",
        "update",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=4162,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.acquire",
        "session_operation_fences",
        "update",
        "83981192adddd83f",
        1,
        "SessionOperationAuthority",
        line=4255,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._compare_and_swap_on_connection",
        "session_operation_fences",
        "update",
        "ff923dcba8b3e8e7",
        1,
        "SessionOperationAuthority",
        line=4456,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.renew",
        "session_operation_fences",
        "update",
        "d41bbaef242667cd",
        1,
        "SessionOperationAuthority",
        line=4535,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.release",
        "session_operation_fences",
        "update",
        "6be0e794bca1608d",
        1,
        "SessionOperationAuthority",
        line=5081,
    ),
    # session_read_admissions (epoch 53, elspeth-f98e0ae8b2): admission sweeps
    # the session's expired rows and inserts its own; renew is the only
    # per-proof write; release deletes the row. All under the operation
    # authority — no other writer exists.
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._admit_blob_read",
        "session_read_admissions",
        "delete",
        "8cf4bc5cc87457bd",
        1,
        "SessionOperationAuthority",
        line=4333,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._admit_blob_read",
        "session_read_admissions",
        "insert",
        "8cf4bc5cc87457bd",
        1,
        "SessionOperationAuthority",
        line=4341,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.renew",
        "session_read_admissions",
        "update",
        "d41bbaef242667cd",
        1,
        "SessionOperationAuthority",
        line=4522,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.release",
        "session_read_admissions",
        "delete",
        "6be0e794bca1608d",
        1,
        "SessionOperationAuthority",
        line=5071,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.archive_delete",
        "sessions",
        "delete",
        "66cc108182d86009",
        1,
        "SessionOperationAuthority",
        line=5101,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "sessions",
        "insert",
        "b964b22650d92b97",
        1,
        "SessionOperationAuthority",
        line=4840,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "session_operation_fences",
        "insert",
        "d5ab9aa3497d191a",
        1,
        "SessionOperationAuthority",
        line=4855,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "session_operation_fences",
        "update",
        "7326f9c03db2a1c7",
        1,
        "SessionOperationAuthority",
        line=4869,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._resume_or_take_over_fork_child",
        "session_operation_fences",
        "update",
        "988ac755147ef9bc",
        1,
        "SessionOperationAuthority",
        line=4930,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.insert_child_state",
        "composition_states",
        "insert",
        "ae78f92031e7eebb",
        1,
        "SessionForkChildMutations",
        line=3463,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.append_child_messages",
        "chat_messages",
        "insert",
        "b3fcd50e04854888",
        1,
        "SessionForkChildMutations",
        line=3516,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkParentGuidedMutations.bind_guided_fork",
        "guided_operations",
        "update",
        "16cc2a98abfd1f5e",
        1,
        "SessionForkParentGuidedMutations",
        line=3649,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.create_pending_run",
        "runs",
        "insert",
        "6e3fcabf86bb6ffa",
        1,
        "SessionRunMutationAuthority",
        line=1410,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.transition_run_status",
        "runs",
        "update",
        "f926157e24accee8",
        1,
        "SessionRunMutationAuthority",
        line=1488,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority._cancel_candidate",
        "runs",
        "update",
        "4ab1313436ac2caf",
        1,
        "GlobalRunRecoveryAuthority",
        line=170,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
        "runs",
        "update",
        "cb2014f436e30ee8",
        1,
        "GlobalRunRecoveryAuthority",
        line=253,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryComposerCompletionMutations.mark_ready_for_review",
        "composer_completion_events",
        "insert",
        "b0eb63d6e0d9027b",
        1,
        "SessionComposerMutationAuthority",
        line=5294,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryComposerCompletionMutations.record_yaml_export",
        "composer_completion_events",
        "insert",
        "2e42be4b632cb581",
        1,
        "SessionComposerMutationAuthority",
        line=5318,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reserve_guided_operation._sync",
        "guided_operations",
        "insert",
        "1161702a3f59ea98",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5308,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reserve_guided_operation._sync",
        "guided_operations",
        "update",
        "1161702a3f59ea98",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5367,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reconcile_guided_start_operation._sync",
        "guided_operation_admission_blocks",
        "insert",
        "ca00ab3741ac8f83",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5565,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reconcile_guided_start_operation._sync",
        "guided_operations",
        "update",
        "ca00ab3741ac8f83",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5603,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.renew_guided_operation._sync",
        "guided_operations",
        "update",
        "343692d13bca60b9",
        1,
        "GuidedSessionMutationAuthority",
        line=5709,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.fail_guided_operation_with_audit._sync",
        "sessions",
        "update",
        "0cce6545ca848e15",
        1,
        "GuidedSessionMutationAuthority",
        line=6020,
    ),
    # Fingerprint rotated 937f08692f6ed0fa -> d02cb6abca95d840 by the landing,
    # and the rotation is the point of the pin, so it is re-argued rather than
    # bulk-updated: the settlement's custody-verification payload gained a
    # ``validation_errors`` entry on both its arms (the rewritten-state arm and
    # the staged-row arm), which is the ONLY diff in this method against the
    # pre-merge branch. ``validation_errors`` is a persisted, served state
    # column that sat outside every custody walker, so the verifier now reads
    # it (pinned by ``test_fork_custody_settlement.py`` T5). It writes nothing
    # new: the five sites below are the same five tables, operations, ordinals
    # and authority as before -- one writer whose dependent write context grew
    # one column, not a new or re-homed writer.
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "chat_messages",
        "update",
        "d02cb6abca95d840",
        1,
        "SessionForkAuthority",
        line=13682,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "composition_states",
        "delete",
        "d02cb6abca95d840",
        1,
        "SessionForkAuthority",
        line=13693,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "chat_messages",
        "update",
        "d02cb6abca95d840",
        2,
        "SessionForkAuthority",
        line=13712,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "guided_operations",
        "insert",
        "d02cb6abca95d840",
        1,
        "SessionForkAuthority",
        line=13760,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "sessions",
        "update",
        "d02cb6abca95d840",
        1,
        "SessionForkAuthority",
        line=13814,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.record_nonterminal_event",
        "guided_operation_events",
        "insert",
        "c66670f774b6404d",
        1,
        "GuidedSessionMutationAuthority",
        line=3772,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.bind",
        "guided_operations",
        "update",
        "0d2776483587fc01",
        1,
        "GuidedSessionMutationAuthority",
        line=3812,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.require_no_active_confirmation",
        "guided_operations",
        "update",
        "d2fd5f53fcc3d7de",
        1,
        "GuidedSessionMutationAuthority",
        line=3833,
    ),
    # claim_confirmation: the first UPDATE (:3891) releases an expired owner's
    # binding and shares its shape with require_no_active_confirmation; the
    # second (:3915) binds the proposal under the caller's own lease fence.
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.claim_confirmation",
        "guided_operations",
        "update",
        "d2fd5f53fcc3d7de",
        1,
        "GuidedSessionMutationAuthority",
        line=3863,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.claim_confirmation",
        "guided_operations",
        "update",
        "95efdd37e97b888e",
        1,
        "GuidedSessionMutationAuthority",
        line=3887,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.complete",
        "guided_operations",
        "update",
        "e7ef88803ab1d8bb",
        1,
        "GuidedSessionMutationAuthority",
        line=3946,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.complete",
        "guided_operation_events",
        "insert",
        "e7ef88803ab1d8bb",
        1,
        "GuidedSessionMutationAuthority",
        line=3976,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.fail",
        "guided_operations",
        "update",
        "9c6096dc61fbf4cd",
        1,
        "GuidedSessionMutationAuthority",
        line=4016,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.fail",
        "guided_operation_events",
        "insert",
        "9c6096dc61fbf4cd",
        1,
        "GuidedSessionMutationAuthority",
        line=4050,
    ),
    # P4-D6 family A1: the updated_at bump every guided settlement makes after
    # appending chat/audit rows (nine _sync bodies route here).
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.mark_session_updated",
        "sessions",
        "update",
        "b175fa9ac09b0b80",
        1,
        "GuidedSessionMutationAuthority",
        line=4086,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.reject_pending_proposal",
        "proposal_events",
        "insert",
        "472e557358d79356",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4125,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.reject_pending_proposal",
        "composition_proposals",
        "update",
        "50a069cc3fb3a8a1",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4136,
    ),
    # P4-D6 family A1: terminal events for the proposal the operation itself
    # holds -- back-edit supersession / operator rejection, and acceptance.
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_rejection",
        "proposal_events",
        "insert",
        "14992d37a16a7085",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4174,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_rejection",
        "composition_proposals",
        "update",
        "385ebb9b57262fac",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4185,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_acceptance",
        "proposal_events",
        "insert",
        "516d95862038f4d9",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4236,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.record_pending_proposal_acceptance",
        "composition_proposals",
        "update",
        "769b7f0ff6157a92",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4247,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._record_guided_fork_child_terminal_event",
        "guided_operation_events",
        "insert",
        "1a8637d3ecf9263b",
        1,
        "SessionForkAuthority",
        line=4937,
    ),
    # ── web_instances membership writer (6b-2, elspeth-66a19780b1): the
    # only production writer of the table; insert + update, never delete ──
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.register",
        "web_instances",
        "insert",
        "9d42ab43e8714140",
        1,
        "WebInstanceMembershipAuthority",
        line=251,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.register",
        "web_instances",
        "update",
        "9d42ab43e8714140",
        1,
        "WebInstanceMembershipAuthority",
        line=257,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.heartbeat",
        "web_instances",
        "update",
        "fd9264070f18566f",
        1,
        "WebInstanceMembershipAuthority",
        line=277,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.begin_drain",
        "web_instances",
        "update",
        "16ca50d5443ff62b",
        1,
        "WebInstanceMembershipAuthority",
        line=299,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.stop",
        "web_instances",
        "update",
        "46a0b81f64e195f0",
        1,
        "WebInstanceMembershipAuthority",
        line=321,
    ),
    # ── identity substrate (P4-D6 elspeth-e483fe7f85): RepositoryIdentityAuthority,
    # method-exact; every acquisition stays inside its method ─────────────
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "identities",
        "insert",
        "52821c0918d19708",
        1,
        "IdentityAuthority",
        line=1121,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "identities",
        "update",
        "52821c0918d19708",
        1,
        "IdentityAuthority",
        line=1153,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "quota_policies",
        "insert",
        "52821c0918d19708",
        1,
        "IdentityAuthority",
        line=1134,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "identities",
        "update",
        "956fdc2fb8be5585",
        1,
        "IdentityAuthority",
        line=1472,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "identity_roles",
        "insert",
        "956fdc2fb8be5585",
        1,
        "IdentityAuthority",
        line=1487,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "quota_policies",
        "insert",
        "956fdc2fb8be5585",
        1,
        "IdentityAuthority",
        line=1497,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.assert_relationship",
        "identity_relationships",
        "insert",
        "0307059e6046fc43",
        1,
        "IdentityAuthority",
        line=1805,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "identities",
        "insert",
        "91cb45004637e097",
        1,
        "IdentityAuthority",
        line=1274,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "identities",
        "update",
        "91cb45004637e097",
        1,
        "IdentityAuthority",
        line=1291,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "identity_roles",
        "insert",
        "91cb45004637e097",
        1,
        "IdentityAuthority",
        line=1307,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "quota_policies",
        "insert",
        "91cb45004637e097",
        1,
        "IdentityAuthority",
        line=1317,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.disable_identity",
        "identities",
        "update",
        "bd9ace6eb929d1ec",
        1,
        "IdentityAuthority",
        line=1587,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.disable_identity",
        "identity_relationships",
        "update",
        "bd9ace6eb929d1ec",
        1,
        "IdentityAuthority",
        line=1601,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.enable_identity",
        "identities",
        "update",
        "d008133458a53f2c",
        1,
        "IdentityAuthority",
        line=1534,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.ensure_identity",
        "identities",
        "update",
        "b7b28d84981c21fd",
        1,
        "IdentityAuthority",
        line=1082,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_role",
        "identity_roles",
        "insert",
        "c642d9839b59a5cd",
        1,
        "IdentityAuthority",
        line=1665,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "identities",
        "insert",
        "79f23eb2f41856c8",
        1,
        "IdentityAuthority",
        line=1372,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "identity_roles",
        "insert",
        "79f23eb2f41856c8",
        1,
        "IdentityAuthority",
        line=1406,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "quota_policies",
        "insert",
        "79f23eb2f41856c8",
        1,
        "IdentityAuthority",
        line=1416,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.purge_stale_pending_identities",
        "identities",
        "delete",
        "bc14f7647a324dad",
        1,
        "IdentityAuthority",
        line=1897,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.retire_identity",
        "identities",
        "update",
        "d5fe93b4219a69e3",
        1,
        "IdentityAuthority",
        line=1205,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_relationship",
        "identity_relationships",
        "update",
        "98fac662dd803471",
        1,
        "IdentityAuthority",
        line=1854,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_role",
        "identity_roles",
        "update",
        "79b8bbdb5bdf12c6",
        1,
        "IdentityAuthority",
        line=1711,
    ),
    # ── identity substrate acquisitions: one write_connection per mutation,
    # contained (never escapes) and admitted by identity like a writer ────
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority._ensure_identity_once",
        "<sessions-write-connection>",
        "write_connection",
        "b0000ee0e0238955",
        1,
        "IdentityAuthority",
        line=1112,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.activate_identity",
        "<sessions-write-connection>",
        "write_connection",
        "eb0301044ea54b03",
        1,
        "IdentityAuthority",
        line=1457,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.assert_relationship",
        "<sessions-write-connection>",
        "write_connection",
        "70a1e8d7cce5c882",
        1,
        "IdentityAuthority",
        line=1756,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.bootstrap_admin",
        "<sessions-write-connection>",
        "write_connection",
        "41998549732172fa",
        1,
        "IdentityAuthority",
        line=1253,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.disable_identity",
        "<sessions-write-connection>",
        "write_connection",
        "43ccf54f9119e8be",
        1,
        "IdentityAuthority",
        line=1568,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.enable_identity",
        "<sessions-write-connection>",
        "write_connection",
        "0f9c7191f5bcc87e",
        1,
        "IdentityAuthority",
        line=1523,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.ensure_identity",
        "<sessions-write-connection>",
        "write_connection",
        "8065904e364a3496",
        1,
        "IdentityAuthority",
        line=1079,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_role",
        "<sessions-write-connection>",
        "write_connection",
        "24bac4354f78de56",
        1,
        "IdentityAuthority",
        line=1639,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.pre_provision_identity",
        "<sessions-write-connection>",
        "write_connection",
        "875c1d10bd111a07",
        1,
        "IdentityAuthority",
        line=1362,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.purge_stale_pending_identities",
        "<sessions-write-connection>",
        "write_connection",
        "ad900de7af568547",
        1,
        "IdentityAuthority",
        line=1887,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.retire_identity",
        "<sessions-write-connection>",
        "write_connection",
        "c2e6e76316a133b3",
        1,
        "IdentityAuthority",
        line=1196,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_relationship",
        "<sessions-write-connection>",
        "write_connection",
        "526bc10b49fc5319",
        1,
        "IdentityAuthority",
        line=1841,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.revoke_role",
        "<sessions-write-connection>",
        "write_connection",
        "1e13dc0fec869d43",
        1,
        "IdentityAuthority",
        line=1689,
    ),
    # ── P4-D6 steps 4-5 admissions (elspeth-e483fe7f85): writers and contained
    # acquisitions the scanner attributes to a named authority, admitted
    # method-exact from live scanner output -- the step-4 facets, then the
    # acquisitions the step-5 forwarding proof contained (mutate_fork_creation's
    # probe; blobs/service.py's phase helpers once the proof followed their
    # advisory-lock import) and the writer inside them.
    #
    # One SessionOperationAuthority acquisition remains an ESCAPE by design and
    # is deliberately NOT admitted (hub ruling 2 on elspeth-e483fe7f85); it stays
    # in the drift counters as an honest row until its seam is restructured.
    # (The base-class ``_locked_transaction`` row is gone, family D of
    # d81de3249d: its plain ``self._engine.begin()`` was unreachable -- both
    # dialect subclasses override it through ``locked_session_transaction`` --
    # so the base is now an abstract hook and the real acquisitions are the
    # sessions/locking.py wrapper's. ``mutate`` still hands that connection to
    # the _RepositoryMutationTransaction constructor, a store by construction.)
    #   __build_locked_fork_pair_controls.locked_pair_transaction (:3952): a
    #     nested factory def, not a class method; it keeps ``(conn, pair)`` in the
    #     pair-lock registry that require_active_locked_fork_pair checks by
    #     connection identity, and forwards to transaction_session_lock.
    # ─────────────────────────────────────────────────────────────────────────
    # src/elspeth/web/coordination/membership_authority.py :: WebInstanceMembershipAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.begin_drain",
        "<sessions-write-connection>",
        "write_connection",
        "98fd14f12508e73a",
        1,
        "WebInstanceMembershipAuthority",
        line=296,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.heartbeat",
        "<sessions-write-connection>",
        "write_connection",
        "71b4334a0f438add",
        1,
        "WebInstanceMembershipAuthority",
        line=274,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.register",
        "<sessions-write-connection>",
        "write_connection",
        "4152160f19e026da",
        1,
        "WebInstanceMembershipAuthority",
        line=229,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/membership_authority.py",
        "RepositoryWebInstanceMembershipAuthority.stop",
        "<sessions-write-connection>",
        "write_connection",
        "476bbd10507b185c",
        1,
        "WebInstanceMembershipAuthority",
        line=318,
    ),
    # src/elspeth/web/coordination/repository.py :: SessionBlobMutationAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.abort_blob_deletion",
        "blob_deletion_cleanups",
        "delete",
        "4f9e61b1cf7247b0",
        1,
        "SessionBlobMutationAuthority",
        line=3029,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_deletion",
        "blob_deletion_cleanups",
        "update",
        "32bd4fca1428b085",
        1,
        "SessionBlobMutationAuthority",
        line=2970,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_deletion",
        "blobs",
        "delete",
        "87f41b9adb5c8772",
        1,
        "SessionBlobMutationAuthority",
        line=2977,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.discard_pending_blob",
        "blobs",
        "delete",
        "0e357be387ddc260",
        1,
        "SessionBlobMutationAuthority",
        line=2511,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.finalize_pending_output_blob",
        "blobs",
        "update",
        "d2eb84d546dbcdd2",
        1,
        "SessionBlobMutationAuthority",
        line=2286,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.insert_blob_inline_resolutions",
        "blob_inline_resolutions",
        "insert",
        "bce659afe8519aae",
        1,
        "SessionBlobMutationAuthority",
        line=3308,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.insert_blob_run_link",
        "blob_run_links",
        "insert",
        "05fd90838bf9a030",
        1,
        "SessionBlobMutationAuthority",
        line=3054,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_deletion_staged",
        "blob_deletion_cleanups",
        "update",
        "afe03ed89b5a16c7",
        1,
        "SessionBlobMutationAuthority",
        line=2929,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_ready",
        "blobs",
        "update",
        "3fd8ace829dbb08b",
        1,
        "SessionBlobMutationAuthority",
        line=2480,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_run_output_blob_error",
        "blobs",
        "update",
        "f7b3307ef1e90c89",
        1,
        "SessionBlobMutationAuthority",
        line=3243,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_run_output_blob_ready",
        "blobs",
        "update",
        "a2e525368ada9268",
        1,
        "SessionBlobMutationAuthority",
        line=3215,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.prepare_blob_deletion",
        "blob_deletion_cleanups",
        "insert",
        "850298970c19565f",
        1,
        "SessionBlobMutationAuthority",
        line=2874,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_blob",
        "blobs",
        "update",
        "d4eba14bc84728e8",
        1,
        "SessionBlobMutationAuthority",
        line=2358,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_blob",
        "blobs",
        "insert",
        "d112eae374c9b904",
        1,
        "SessionBlobMutationAuthority",
        line=2400,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_pending_output_blob",
        "blobs",
        "insert",
        "c997a216a1a51351",
        1,
        "SessionBlobMutationAuthority",
        line=2204,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_abandoned_blob_reservation",
        "blobs",
        "delete",
        "d22b3792b721f8ac",
        1,
        "SessionBlobMutationAuthority",
        line=2572,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_blob_deletion",
        "blob_deletion_cleanups",
        "delete",
        "4f9e61b1cf7247b0",
        1,
        "SessionBlobMutationAuthority",
        line=3011,
    ),
    # src/elspeth/web/coordination/repository.py :: SessionMutationAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.decide_and_soft_archive",
        "sessions",
        "update",
        "2aec308084effcf4",
        1,
        "SessionMutationAuthority",
        line=724,
    ),
    # src/elspeth/web/coordination/repository.py :: SessionOperationAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.renew_fork_child_lease",
        "session_operation_fences",
        "update",
        "b268c9591db479c5",
        1,
        "SessionOperationAuthority",
        line=4639,
    ),
    # src/elspeth/web/coordination/repository.py :: SessionRunMutationAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.append_run_event",
        "run_events",
        "insert",
        "5009526a773c2d16",
        1,
        "SessionRunMutationAuthority",
        line=1556,
    ),
    # src/elspeth/web/coordination/run_recovery_authority.py :: GlobalRunRecoveryAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.cancel_orphaned_run_records",
        "<sessions-write-connection>",
        "write_connection",
        "39fa7162805bc149",
        1,
        "GlobalRunRecoveryAuthority",
        line=185,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
        "<sessions-write-connection>",
        "write_connection",
        "ad4dba99faef80aa",
        1,
        "GlobalRunRecoveryAuthority",
        line=222,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_recovery_authority.py",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
        "<sessions-write-connection>",
        "write_connection",
        "22158e810234d589",
        1,
        "GlobalRunRecoveryAuthority",
        line=229,
    ),
    # src/elspeth/web/secrets/user_store.py :: UserSecretAuthority
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.delete_secret",
        "<sessions-write-connection>",
        "write_connection",
        "bc9adb13d7ec6188",
        1,
        "UserSecretAuthority",
        line=194,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "<sessions-write-connection>",
        "write_connection",
        "1eefe7b199768760",
        1,
        "UserSecretAuthority",
        line=189,
    ),
    # src/elspeth/web/sessions/skill_markdown_history.py :: SkillMarkdownHistoryAuthority
    WriterIdentity(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "<sessions-write-connection>",
        "write_connection",
        "7b8f2374db24e42b",
        1,
        "SkillMarkdownHistoryAuthority",
        line=62,
    ),
    # src/elspeth/web/sessions/sso_handoff_repository.py :: SsoHandoffAuthority
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.issue",
        "<sessions-write-connection>",
        "write_connection",
        "3e5d66c4e0feb243",
        1,
        "SsoHandoffAuthority",
        line=76,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/sso_handoff_repository.py",
        "SsoHandoffRepository.consume",
        "<sessions-write-connection>",
        "write_connection",
        "776f801ca8b2be96",
        1,
        "SsoHandoffAuthority",
        line=109,
    ),
    # src/elspeth/web/coordination/repository.py :: SessionOperationAuthority
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.mutate_fork_creation",
        "<sessions-write-connection>",
        "write_connection",
        "2a24f2fb856584b3",
        1,
        "SessionOperationAuthority",
        line=4976,
    ),
    # src/elspeth/web/blobs/service.py :: SessionBlobMutationAuthority
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "_finalize_reserved_blob",
        "<sessions-write-connection>",
        "write_connection",
        "5fcbd10db9a8bb6a",
        1,
        "SessionBlobMutationAuthority",
        line=1291,
    ),
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "_finalize_reserved_blob",
        "blobs",
        "update",
        "38de196d17740a2c",
        1,
        "SessionBlobMutationAuthority",
        line=1302,
    ),
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "_reserve_pending_blob",
        "<sessions-write-connection>",
        "write_connection",
        "ed8bc9f6e94ae399",
        1,
        "SessionBlobMutationAuthority",
        line=1218,
    ),
)

# These exact connection flows are proven read-only but cannot be resolved to
# a literal ``select(...)`` by the small AST data-flow analysis.  They remain
# separate from the writer manifest: any fingerprint, multiplicity, or symbol
# drift reopens review, and any DML added beside them is independently caught.
_REVIEWED_READ_CONNECTIONS: tuple[WriterIdentity, ...] = (
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl.copy_blobs_for_fork._verify_plan_and_quota",
        "<sessions-write-connection>",
        "write_connection",
        "175d222f809ec084",
        1,
        None,
        line=3061,
    ),
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl.copy_blobs_for_fork._verify_exact_target",
        "<sessions-write-connection>",
        "write_connection",
        "4536b6d6104a3575",
        1,
        None,
        line=3175,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.get_composer_preferences._sync",
        "<sessions-write-connection>",
        "write_connection",
        "b0ee6f0b39b120ec",
        1,
        None,
        line=212,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_guided_operation._sync",
        "<sessions-write-connection>",
        "write_connection",
        "788d302f1873a5e1",
        1,
        None,
        line=5421,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_pipeline_dispatch_recovery._sync",
        "<sessions-write-connection>",
        "write_connection",
        "e55c3bb74cb8ef7c",
        1,
        None,
        line=7925,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_authoritative_composition_proposal._sync",
        "<sessions-write-connection>",
        "write_connection",
        "4cfe2dd7bf8391d1",
        1,
        None,
        line=7559,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.list_composition_proposals._sync",
        "<sessions-write-connection>",
        "write_connection",
        "ec6d5b3014c58a86",
        1,
        None,
        line=8078,
    ),
    # ── identity substrate (P4-D6 elspeth-e483fe7f85): RepositoryIdentityAuthority,
    # method-exact; every acquisition stays inside its method ─────────────
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.active_roles",
        "<sessions-write-connection>",
        "write_connection",
        "9de9c0b30666147c",
        1,
        None,
        line=960,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.count_active_human_admins",
        "<sessions-write-connection>",
        "write_connection",
        "b9d11eb72686f0c0",
        1,
        None,
        line=975,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.holds_active_role",
        "<sessions-write-connection>",
        "write_connection",
        "3e0a739c1de8e182",
        1,
        None,
        line=969,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.list_identities",
        "<sessions-write-connection>",
        "write_connection",
        "aacf64b0b472d7a0",
        1,
        None,
        line=947,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.list_relationships",
        "<sessions-write-connection>",
        "write_connection",
        "da5ad711342dec32",
        1,
        None,
        line=1017,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.list_roles",
        "<sessions-write-connection>",
        "write_connection",
        "d27c1be3c6032030",
        1,
        None,
        line=991,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.read_identity",
        "<sessions-write-connection>",
        "write_connection",
        "447e2ca011fe13a9",
        1,
        None,
        line=925,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.read_identity_by_natural_key",
        "<sessions-write-connection>",
        "write_connection",
        "785026bfc68cad16",
        1,
        None,
        line=932,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.read_identity_summary",
        "<sessions-write-connection>",
        "write_connection",
        "2be09707110fb626",
        1,
        None,
        line=940,
    ),
    # _session_exists (family D, elspeth-43ddb79074 / d81de3249d): the
    # collision probe behind create_session_with_initial_fence — one SELECT on
    # ``self._engine.connect()``, nothing forwarded; a read, not an authority
    # acquisition, so it is admitted here and no longer as a writer row.
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._session_exists",
        "<sessions-write-connection>",
        "write_connection",
        "f98bc6d74193a045",
        1,
        "SessionOperationAuthority",
        line=4193,
    ),
)

# Exact acquisition identities proven to belong wholly to another database
# domain.  This is deliberately separate from writer and read admission.
_REVIEWED_NON_SESSION_CONNECTIONS: tuple[WriterIdentity, ...] = (
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "verify_sqlite_tier1_pragmas",
        "<sessions-write-connection>",
        "write_connection",
        "38007d0f0ea7c327",
        1,
        None,
        line=121,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_sqlite_epoch_is_incompatible",
        "<sessions-write-connection>",
        "write_connection",
        "1084a431b73718a3",
        1,
        None,
        line=819,
    ),
    # Re-pinned by P4-D6 step 5 (cross-module rule): the connection is handed
    # to ``read_schema_identities`` behind a plain import, whose body executes
    # only a SELECT on it; same acquisition, same fingerprint, no escape.
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_landscape_identity_issue",
        "<sessions-write-connection>",
        "write_connection",
        "54f25c9b2650a66b",
        1,
        None,
        line=844,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_collect_token_outcomes_shape_errors",
        "<sessions-write-connection>",
        "write_connection",
        "f95bb339c5816e92",
        1,
        None,
        line=942,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._verify_sqlite_pragmas",
        "<sessions-write-connection>",
        "write_connection",
        "7b14f45607d6611f",
        1,
        None,
        line=1161,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._create_sqlcipher_engine._creator",
        "<sessions-write-connection>",
        "write_connection",
        "bc4b6272008ed6ec",
        1,
        None,
        line=1281,
        connection_escape=True,
    ),
    # Re-pinned by P4-D6 step 5 (cross-module rule): same shape as
    # ``_landscape_identity_issue`` above -- the forward is inspected, not assumed.
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._sync_schema_identity",
        "<non-session-write-connection>",
        "write_connection",
        "b92e2e573b8362dd",
        1,
        None,
        line=1302,
    ),
    # ``with begin_write(self._engine) as conn`` inside LandscapeDB: the
    # in-file wrapper hop is transparent and ``self`` carries the declared
    # engine type, so the caller is the proven acquisition.
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._sync_schema_identity",
        "<non-session-write-connection>",
        "write_connection",
        "91a7ddcfb7d2279c",
        1,
        None,
        line=1315,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._get_sqlite_schema_epoch",
        "<non-session-write-connection>",
        "write_connection",
        "4644a6cc893b4d09",
        1,
        None,
        line=1342,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._validate_schema",
        "<non-session-write-connection>",
        "write_connection",
        "026fc33c365235c4",
        1,
        None,
        line=1601,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB.read_only_connection",
        "<non-session-write-connection>",
        "write_connection",
        "44c4543542ceeb85",
        1,
        None,
        line=2061,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB.write_connection",
        "<non-session-write-connection>",
        "write_connection",
        "222b5f4b0d258dbe",
        1,
        None,
        line=2043,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/export_read_model.py",
        "open_export_read_transaction",
        "<sessions-write-connection>",
        "write_connection",
        "ffdb0616b1c68213",
        1,
        None,
        line=466,
        connection_escape=True,
    ),
    # open_export_read_transaction acquires twice: engine.connect() at :466
    # (yielded, so an escape) and the REPEATABLE READ rebinding at :471.
    WriterIdentity(
        "src/elspeth/core/landscape/export_read_model.py",
        "open_export_read_transaction",
        "<sessions-write-connection>",
        "write_connection",
        "9d39978e72854dca",
        1,
        None,
        line=471,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/leases.py",
        "SchedulerLeaseRepository.heartbeat_lease",
        "<non-session-write-connection>",
        "write_connection",
        "fd990256930fd02c",
        1,
        None,
        line=819,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/leases.py",
        "SchedulerLeaseRepository.peer_active_leases",
        "<non-session-write-connection>",
        "write_connection",
        "791b6fe94ec7c970",
        1,
        None,
        line=974,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/plugins/sinks/database_sink.py",
        "DatabaseSink._inspect_target_contract",
        "<sessions-write-connection>",
        "write_connection",
        "3e92055687329a54",
        1,
        None,
        line=426,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/plugins/sinks/database_sink.py",
        "DatabaseSink._read_committed_effect_result",
        "<sessions-write-connection>",
        "write_connection",
        "cb69520a77f4030e",
        1,
        None,
        line=839,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/plugins/sinks/database_sink.py",
        "DatabaseSink.commit_effect",
        "<sessions-write-connection>",
        "write_connection",
        "a1ed0f32c96a6da4",
        1,
        None,
        line=859,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/web/_aws_ecs_acceptance/operator_telemetry.py",
        "_read_postgres_max_connections",
        "<sessions-write-connection>",
        "write_connection",
        "df00dc245a757212",
        1,
        None,
        line=1053,
    ),
    WriterIdentity(
        "src/elspeth/web/auth/local.py",
        "LocalAuthProvider._get_conn",
        "<non-session-write-connection>",
        "write_connection",
        "df6185754d14a63c",
        1,
        None,
        line=325,
        connection_escape=True,
    ),
    # Proven non-Sessions acquisitions (P4-D6 step 1): every origin is a
    # LandscapeDB / Tier1Engine provider or a declared factory; listed by
    # identity so a moved or rewritten acquisition re-opens review.
    WriterIdentity(
        "src/elspeth/core/checkpoint/manager.py",
        "CheckpointManager.get_latest_checkpoint",
        "<non-session-write-connection>",
        "write_connection",
        "3ff2ad66f23de4bc",
        1,
        None,
        line=219,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/manager.py",
        "CheckpointManager.get_checkpoints",
        "<non-session-write-connection>",
        "write_connection",
        "c3ee61782a940776",
        1,
        None,
        line=241,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "_fetch_run",
        "<non-session-write-connection>",
        "write_connection",
        "288170ec1722ebc2",
        1,
        None,
        line=126,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "check_source_lifecycle_resumable",
        "<non-session-write-connection>",
        "write_connection",
        "4b552d15f53e6c40",
        1,
        None,
        line=233,
    ),
    # Re-pinned by P4-D6 step 5: the connection is forwarded only to a
    # same-module private callee that executes on it, which the forwarding
    # proof now inspects; same acquisition, same fingerprint, no escape.
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "check_group_satisfiability_resumable",
        "<non-session-write-connection>",
        "write_connection",
        "9e291dca4a439605",
        1,
        None,
        line=429,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "RecoveryManager.get_unprocessed_row_data",
        "<non-session-write-connection>",
        "write_connection",
        "758fe32047daee73",
        1,
        None,
        line=908,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "RecoveryManager.get_unprocessed_row_data_by_source",
        "<non-session-write-connection>",
        "write_connection",
        "ccdaa74d89308bbb",
        1,
        None,
        line=969,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "RecoveryManager._get_incomplete_token_work",
        "<non-session-write-connection>",
        "write_connection",
        "63aa60b938d94231",
        1,
        None,
        line=1036,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "RecoveryManager.get_resume_workset",
        "<non-session-write-connection>",
        "write_connection",
        "18b91cab2434c597",
        1,
        None,
        line=1131,
    ),
    WriterIdentity(
        "src/elspeth/core/checkpoint/recovery.py",
        "RecoveryManager.get_resume_workset",
        "<non-session-write-connection>",
        "write_connection",
        "697320a36b78fae3",
        1,
        None,
        line=1173,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "RunCoordinationRepository.live_leader",
        "<non-session-write-connection>",
        "write_connection",
        "2b52f58624e1e33d",
        1,
        None,
        line=866,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "RunCoordinationRepository.dead_non_leader_workers",
        "<non-session-write-connection>",
        "write_connection",
        "ee5e921beae1a1a7",
        1,
        None,
        line=1254,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "RunCoordinationRepository._read_registered_workers",
        "<non-session-write-connection>",
        "write_connection",
        "e69348a5794c1998",
        1,
        None,
        line=1279,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/run_lifecycle_repository.py",
        "RunLifecycleRepository._terminal_status_after_refusal",
        "<non-session-write-connection>",
        "write_connection",
        "c585a80de69ed5e2",
        1,
        None,
        line=620,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/barrier.py",
        "BarrierJournalRepository.list_blocked_barrier_items",
        "<non-session-write-connection>",
        "write_connection",
        "77752b22fc520b7b",
        1,
        None,
        line=1087,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/barrier.py",
        "BarrierJournalRepository.blocked_barrier_token_ids",
        "<non-session-write-connection>",
        "write_connection",
        "89c87457b3d6b1a9",
        1,
        None,
        line=1106,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/barrier.py",
        "BarrierJournalRepository.count_blocked_barrier_items",
        "<non-session-write-connection>",
        "write_connection",
        "c5310a65c3619209",
        1,
        None,
        line=1120,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/barrier.py",
        "BarrierJournalRepository.list_pending_blocked_barrier_items",
        "<non-session-write-connection>",
        "write_connection",
        "3053f6e3f97a64b7",
        1,
        None,
        line=1144,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/group_losses.py",
        "GroupLossRepository.list_unadopted_group_losses",
        "<non-session-write-connection>",
        "write_connection",
        "96de5f8f04d0d42b",
        1,
        None,
        line=226,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/group_losses.py",
        "GroupLossRepository.list_group_losses",
        "<non-session-write-connection>",
        "write_connection",
        "b1352aa1e42a3d57",
        1,
        None,
        line=261,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.count_ready_in_set",
        "<non-session-write-connection>",
        "write_connection",
        "07606e7bd8e70285",
        1,
        None,
        line=92,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.count_failed_in_set",
        "<non-session-write-connection>",
        "write_connection",
        "507106f1f32a3934",
        1,
        None,
        line=124,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.has_peer_owned_work",
        "<non-session-write-connection>",
        "write_connection",
        "9d03e89f3fd122fc",
        1,
        None,
        line=170,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.count_active_work",
        "<non-session-write-connection>",
        "write_connection",
        "bed9203e7ba49ea2",
        1,
        None,
        line=195,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.active_row_ids",
        "<non-session-write-connection>",
        "write_connection",
        "e93c01fa22314ccf",
        1,
        None,
        line=212,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.count_unquiesced_work",
        "<non-session-write-connection>",
        "write_connection",
        "aabf3002d846cbce",
        1,
        None,
        line=227,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.summarize_unquiesced_work",
        "<non-session-write-connection>",
        "write_connection",
        "c511a7a388171ef4",
        1,
        None,
        line=238,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.count_unresolved_work",
        "<non-session-write-connection>",
        "write_connection",
        "435e93f0f1cf5fbc",
        1,
        None,
        line=269,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.summarize_unresolved_work",
        "<non-session-write-connection>",
        "write_connection",
        "dd23d5e0e835da29",
        1,
        None,
        line=280,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/read_model.py",
        "SchedulerReadModel.summarize_active_work",
        "<non-session-write-connection>",
        "write_connection",
        "0100307f496daf20",
        1,
        None,
        line=317,
    ),
    WriterIdentity(
        "src/elspeth/core/rate_limit/limiter.py",
        "RateLimiter.__init__",
        "<non-session-write-connection>",
        "write_connection",
        "002b7e991cd4aa65",
        1,
        None,
        line=224,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/web/_aws_ecs_acceptance/capture.py",
        "verify_local_auth",
        "<non-session-write-connection>",
        "write_connection",
        "7e1168bb961c9d7e",
        1,
        None,
        line=737,
    ),
    # ``with open_landscape_db(settings) as db, db.write_connection() as conn``:
    # the tutorial's live-run projection writes the Landscape ``runs`` row and
    # hands the connection to two counting helpers (hence the escape).
    WriterIdentity(
        "src/elspeth/web/composer/tutorial_service.py",
        "_project_live_tutorial_output",
        "<non-session-write-connection>",
        "write_connection",
        "aca40e2f0a3f3a63",
        1,
        None,
        line=438,
        connection_escape=True,
    ),
)

_TABLE_NAMES = frozenset(policy.table for policy in _TABLE_POLICIES)
_TABLE_IDENTIFIERS = {f"{table}_table": table for table in _TABLE_NAMES}
_SESSION_TABLE_MODULE = "elspeth.web.sessions.models"
_LANDSCAPE_TABLE_MODULE = "elspeth.core.landscape.schema"
_SESSION_ENGINE_FACTORIES = frozenset({"elspeth.web.sessions.engine.create_session_engine"})
_NON_SESSION_ENGINE_FACTORIES = frozenset(
    {
        "sqlite3.connect",
        "elspeth.core.landscape.database.begin_write",
        # One leader-fenced begin_write on a Tier1Engine (ADR-030); yields the
        # Landscape connection it opened.
        "elspeth.core.landscape.run_coordination_repository.fenced_leader_transaction",
        # The web tier's accessor for the Landscape store; yields a LandscapeDB.
        "elspeth.web.landscape_access.open_landscape_db",
    }
)
# LandscapeDB's own acquisition verbs. Recognised ONLY on a plain name bound
# from a declared factory in the same function (``with open_landscape_db(...)
# as db, db.write_connection() as conn``); an attribute receiver such as
# ``self._db.write_connection()`` is deliberately not an acquisition here,
# so the Landscape repositories keep their statement-level classification
# (package premise) rather than acquiring a manifest row per site.
_LANDSCAPE_CONNECTION_VERBS = frozenset({"connection", "write_connection", "read_only_connection"})
_NON_SESSION_ENGINE_TYPES = frozenset(
    {
        "elspeth.core.landscape.database.LandscapeDB",
        "elspeth.core.landscape.database.Tier1Engine",
    }
)
# Package premise (P4-D6 rule 1): a module under one of these prefixes that
# imports nothing from ``elspeth.web`` can neither build a Sessions-model
# statement nor mint a Sessions engine, so a statement it executes on a
# connection IT bound (a ``with ... as conn`` or an assignment in the module,
# never a parameter) is a non-Sessions execution — unless the statement is
# raw SQL naming a Sessions table. The premise is verified per module, never
# assumed from the path.
_DECLARED_NON_SESSION_PACKAGES = (
    "src/elspeth/core/landscape/",
    "src/elspeth/core/checkpoint/",
    "src/elspeth/core/retention/",
)
_SESSION_SHAPED_IMPORT_ROOT = "elspeth.web"
_SESSION_ENGINE_FACTORY_PATH = "src/elspeth/web/sessions/engine.py"
# Acceptance probe seam (P4-D6 family J, ruling 2026-09-07): the deployment
# acceptance drivers execute the controller's probe SQL against the
# DEPLOYMENT's databases through one live seam per protocol -- a class in an
# acceptance module implementing ``SqlSession`` / ``SqlReader`` whose every
# execution is ``text(<its own statement parameter>)`` on a connection it
# took from the ``Engine`` it was constructed with. The scanner admits such a
# class as a whole (``_ProbeSeamProof``) and NOTHING looser: a literal or
# module-built statement, a second base, a decorator, a stored connection
# that reaches any call but ``execute``/``close``, a connection returned or
# forwarded, a shadowed ``text``, or a definition outside these modules keeps
# every row of the class. The seam is admitted, not its callers: the SQL the
# controller pushes through it is typed there, not here.
_ACCEPTANCE_PROBE_MODULES = (
    "src/elspeth/web/azure_container_apps_acceptance.py",
    "src/elspeth/web/_azure_container_apps_acceptance/",
    "src/elspeth/web/aws_ecs_acceptance.py",
    "src/elspeth/web/_aws_ecs_acceptance/",
)
_PROBE_SEAM_PROTOCOLS = frozenset(
    {
        "elspeth.web._azure_container_apps_acceptance.controller.SqlSession",
        "elspeth.web._azure_container_apps_acceptance.controller.SqlReader",
    }
)
_SQLALCHEMY_ENGINE_TYPES = frozenset({"sqlalchemy.Engine", "sqlalchemy.engine.Engine", "sqlalchemy.engine.base.Engine"})
_SQLALCHEMY_TEXT_CONSTRUCTORS = frozenset({"sqlalchemy.text", "sqlalchemy.sql.text", "sqlalchemy.sql.expression.text"})
_TRANSACTION_CONTROL_SQL = frozenset({"BEGIN", "BEGIN IMMEDIATE", "BEGIN DEFERRED", "BEGIN EXCLUSIVE", "COMMIT", "ROLLBACK"})
_PRAGMA_ASSIGNMENT = re.compile(r"PRAGMA\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*[A-Za-z0-9_]+", flags=re.IGNORECASE)
_EXPLICIT_NON_SQL_EXECUTE_RECEIVER_TYPES = frozenset(
    {
        "elspeth.web.execution.protocol.ExecutionService",
        # LLM query strategies: ``execute`` runs the prompt plan, not SQL.
        "elspeth.plugins.transforms.llm.transform.SingleQueryStrategy",
        "elspeth.plugins.transforms.llm.transform.MultiQueryStrategy",
    }
)
_TRANSPARENT_SQLALCHEMY_STATEMENT_METHODS = frozenset(
    {
        "cte",
        "distinct",
        "execution_options",
        "from_select",
        "group_by",
        "inline",
        "join",
        "limit",
        "on_conflict_do_nothing",
        "on_conflict_do_update",
        "order_by",
        "ordered_values",
        "offset",
        "outerjoin",
        "prefix_with",
        "return_defaults",
        "returning",
        "select_from",
        "values",
        "where",
        "with_dialect_options",
        "with_for_update",
    }
)
_SQL_IDENTIFIER = r'(?:[A-Za-z_][A-Za-z0-9_]*|"[A-Za-z_][A-Za-z0-9_]*"|`[A-Za-z_][A-Za-z0-9_]*`|\[[A-Za-z_][A-Za-z0-9_]*\])'
_RAW_WRITE = re.compile(
    rf"\b(?P<operation>INSERT(?:\s+OR\s+(?:ABORT|FAIL|IGNORE|REPLACE|ROLLBACK))?\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?[\"`\[]?(?P<table>{'|'.join(sorted(_TABLE_NAMES, key=len, reverse=True))})"
    r"[\"`\]]?(?=\s|\(|$)",
    re.IGNORECASE,
)
_READ_ONLY_ARGUMENT_PRAGMAS = frozenset(
    {
        "FOREIGN_KEY_LIST",
        "INDEX_INFO",
        "INDEX_LIST",
        "INDEX_XINFO",
        "TABLE_INFO",
        "TABLE_XINFO",
    }
)
_READ_ONLY_BARE_PRAGMAS = frozenset(
    {
        "APPLICATION_ID",
        "DATABASE_LIST",
        "FOREIGN_KEYS",
        "FREELIST_COUNT",
        "JOURNAL_MODE",
        "PAGE_COUNT",
        "PAGE_SIZE",
        "QUERY_ONLY",
        "SCHEMA_VERSION",
        "TABLE_LIST",
        "USER_VERSION",
    }
)
_PRAGMA_IDENTIFIER = r'(?:[A-Z_][A-Z0-9_]*|"[A-Z_][A-Z0-9_]*"|`[A-Z_][A-Z0-9_]*`|\[[A-Z_][A-Z0-9_]*\])'
_PRAGMA_ARGUMENT = r"(?:[A-Z_][A-Z0-9_]*|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]+\])"
_PRAGMA_STATEMENT = re.compile(
    rf"(?:(?P<schema>{_PRAGMA_IDENTIFIER})\s*\.\s*)?"
    rf"(?P<name>{_PRAGMA_IDENTIFIER})"
    rf"(?:\s*\((?P<argument>{_PRAGMA_ARGUMENT})\))?\s*;?"
)
# PostgreSQL's explicit table lock, in its explicit form ONLY: ``LOCK TABLE``,
# one or more bare table names, ``IN <one of the eight modes> MODE``, at most
# one trailing semicolon.  No ``ONLY``, no ``NOWAIT``, no schema qualifier,
# no second statement.  A lock writes no row; it is the row-set analogue of
# the advisory-lock SELECTs the forwarding proof already admits, and the
# bootstrap authority takes one before it counts an EMPTY population (P4-D6,
# hub ruling on the D20 bootstrap race).
_LOCK_TABLE_MODES = (
    "ACCESS SHARE",
    "ROW SHARE",
    "ROW EXCLUSIVE",
    "SHARE UPDATE EXCLUSIVE",
    "SHARE",
    "SHARE ROW EXCLUSIVE",
    "EXCLUSIVE",
    "ACCESS EXCLUSIVE",
)
_LOCK_TABLE_STATEMENT = re.compile(
    r"LOCK\s+TABLE\s+[A-Z_][A-Z0-9_]*(?:\s*,\s*[A-Z_][A-Z0-9_]*)*\s+IN\s+(?:"
    + "|".join(mode.replace(" ", r"\s+") for mode in _LOCK_TABLE_MODES)
    + r")\s+MODE\s*;?"
)

# ``SHOW <run-time setting>``: PostgreSQL's (and MySQL's) read of one server
# or session setting, e.g. the acceptance connection-budget probe's ``SHOW
# max_connections``. One bare, optionally dotted, identifier and nothing
# else: no second statement, no ``TO``/``=`` (that is ``SET``).
_SHOW_SETTING_STATEMENT = re.compile(r"SHOW\s+[A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)*\s*;?")


def _raw_sql_is_obviously_read_only(sql: str) -> bool:
    normalized = sql.strip().upper()
    if normalized.startswith(("SELECT", "WITH RECURSIVE", "EXPLAIN")):
        return True
    if normalized.startswith("LOCK"):
        return _LOCK_TABLE_STATEMENT.fullmatch(normalized) is not None
    if normalized.startswith("SHOW"):
        return _SHOW_SETTING_STATEMENT.fullmatch(normalized) is not None
    if not normalized.startswith("PRAGMA"):
        return False

    pragma = normalized.removeprefix("PRAGMA").strip()
    match = _PRAGMA_STATEMENT.fullmatch(pragma)
    if match is None:
        return False
    bare_name = match.group("name").strip('"`[]')
    argument = match.group("argument")
    if argument is None:
        return bare_name in _READ_ONLY_BARE_PRAGMAS
    return bare_name in _READ_ONLY_ARGUMENT_PRAGMAS


class InventoryScanError(AssertionError):
    """A production file could not be decoded or parsed."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _symbol(node: ast.AST) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = getattr(current, "_inventory_parent", None)
    return ".".join(reversed(names)) or "<module>"


def _attach_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._inventory_parent = parent  # type: ignore[attr-defined]


def _statement_fingerprint(node: ast.AST) -> str:
    current = node
    while True:
        parent = getattr(current, "_inventory_parent", None)
        if parent is None or isinstance(parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
        current = parent
    normalized = stable_ast_dump(current)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


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


# P4-D6 step 5 (hub ruling on elspeth-e483fe7f85): a connection forwarded to a
# callable the scanner can INSPECT is contained when every use inside the callee
# is one of these receivers, a dialect read, an anonymous nested transaction, or
# a further resolvable forward within the depth bound. Everything else escapes.
_EXECUTE_RECEIVER_METHODS = frozenset({"execute", "executemany", "exec_driver_sql", "scalar", "scalars"})
_FORWARDING_MAX_DEPTH = 3
_NESTED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


class _ProductionWriterCollector(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST, *, anchor: Path | None = None) -> None:
        self.path = path
        self.tree = tree
        # Parallel to ``sites``: the node each site was emitted from, so a
        # tree-wide post-pass can reason about a site's enclosing function.
        self.site_nodes: list[ast.AST] = []
        # A forwarded connection's callee behind a plain ``from x import f``
        # is inspected in ITS module (P4-D6 step 5, cross-module ruling): the
        # scanned collectors register here by path, and a module the scan did
        # not include is parsed from disk under ``anchor`` on demand, so the
        # verdict for a site never depends on which files a caller passed.
        # No anchor and no peer: an imported callee is unresolvable.
        self.anchor = anchor
        self.peers: dict[str, _ProductionWriterCollector] = {}
        self.import_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.assignment_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.definition_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.attribute_bindings: dict[str, list[_NameBinding]] = {}
        self.shadowed_names: set[tuple[int, str]] = set()
        self.sites: list[WriterIdentity] = []
        self.write_connection_calls: dict[int, tuple[ast.Call, bool]] = {}
        self.classified_execution_calls: set[int] = set()
        self._dependent_write_context_cache: dict[int, tuple[list[ast.stmt], list[ast.Call]]] = {}
        # ``with wrapper(...) as conn`` where ``wrapper`` is a same-scope
        # ``@contextmanager`` whose yield resolves to an acquisition: the
        # caller's call is the acquisition site, keyed here to the wrapper
        # definition and its resolved yields (an unresolved yield is kept as
        # an empty list so the merged domain stays unknown).
        self.wrapper_calls: dict[
            int,
            tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[tuple[ast.Yield | ast.YieldFrom, list[ast.Call]]]],
        ] = {}
        # ``conn = opener()`` where ``opener`` is a same-scope non-generator
        # whose EVERY return is a qualified-factory acquisition
        # (``_NON_SESSION_ENGINE_FACTORIES``): the call carries the factory's
        # domain for one hop. A return that is not such an acquisition, or a
        # generator callee, is never followed.
        self.factory_return_calls: dict[int, tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.Call]]] = {}
        self.wrapper_call_nodes: dict[int, ast.Call] = {}
        self.method_owners: dict[int, ast.ClassDef] = {}
        self.class_methods: dict[tuple[int, str], ast.FunctionDef | ast.AsyncFunctionDef | None] = {}
        # Unresolved executions whose connection is a parameter of the enclosing
        # function: (index into ``sites``, the function, the parameter name).
        # ``scan_production_writers`` proves or refuses them tree-wide.
        self.parameter_received: list[tuple[int, ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
        self._collect_aliases()
        self._collect_class_methods()
        self.declared_non_session_module = self._declared_non_session_module()

    def _receiver_parameter(self, execution: ast.Call) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str] | None:
        """The enclosing function and parameter name when the execution's receiver is exactly that parameter."""

        receiver = execution.func.value if isinstance(execution.func, ast.Attribute) else None
        if not isinstance(receiver, ast.Name):
            return None
        owner = self._enclosing_function(execution)
        if owner is None:
            return None
        parameters = {argument.arg for argument in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)}
        if receiver.id not in parameters or self._name_reassigned_in(owner, receiver.id):
            return None
        reaching, complete, scope = self._visible_reaching_bindings(execution, receiver.id)
        if not complete or scope is not owner or any(binding.value is not None or binding.node is not owner for binding in reaching):
            return None
        return owner, receiver.id

    def _call_argument_for_parameter(
        self,
        call: ast.Call,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
    ) -> ast.expr | None:
        """The caller's expression bound to ``name`` at this call, or ``None`` when it cannot be known exactly."""

        positional = [*definition.args.posonlyargs, *definition.args.args]
        if id(definition) in self.method_owners and self._is_instance_method(definition) and positional:
            if isinstance(call.func, ast.Attribute):
                positional = positional[1:]
            else:
                return None
        for keyword in call.keywords:
            if keyword.arg is None:
                return None
            if keyword.arg == name:
                return keyword.value
        names = [parameter.arg for parameter in positional]
        if any(isinstance(argument, ast.Starred) for argument in call.args):
            return None
        if name in names:
            index = names.index(name)
            if index < len(call.args):
                return call.args[index]
            # Omitted: the definition's own default is what the parameter holds.
            defaults = definition.args.defaults
            offset = len([*definition.args.posonlyargs, *definition.args.args]) - len(defaults)
            full_index = [parameter.arg for parameter in (*definition.args.posonlyargs, *definition.args.args)].index(name)
            return defaults[full_index - offset] if full_index >= offset else None
        kwonly = [parameter.arg for parameter in definition.args.kwonlyargs]
        if name in kwonly:
            return definition.args.kw_defaults[kwonly.index(name)]
        return None

    def _call_targets_definition(self, call: ast.Call, definition: ast.FunctionDef | ast.AsyncFunctionDef, path: str) -> bool | None:
        """True/False when this call provably does/does not target ``definition``; ``None`` when unknowable."""

        local = self._local_callable_definition(call)
        if local is not None:
            return local is definition and self.path == path
        if isinstance(call.func, ast.Name):
            qualified = self._imported_qualified_name(call.func)
            if qualified is None:
                return None
            module, _, name = qualified.rpartition(".")
            return name == definition.name and f"src/{module.replace('.', '/')}.py" == path and _symbol(definition) == definition.name
        return None

    def _declared_non_session_module(self) -> bool:
        """The package premise, verified on this module's own imports (not asserted from its path)."""

        if not self.path.startswith(_DECLARED_NON_SESSION_PACKAGES):
            return False
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = [imported.name for imported in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = self._relative_import_module(node)
                names = [module] if module is not None else [None]
                names.extend(f"{module}.{imported.name}" for imported in node.names if module is not None)
            else:
                continue
            for name in names:
                if name is None:
                    return False
                if name == _SESSION_SHAPED_IMPORT_ROOT or name.startswith(f"{_SESSION_SHAPED_IMPORT_ROOT}."):
                    return False
        return True

    def _execution_receiver_is_module_bound(self, execution: ast.Call) -> bool:
        """True when the executing connection was bound by this module (with-target or assignment), never a parameter."""

        receiver = execution.func.value if isinstance(execution.func, ast.Attribute) else None
        if not isinstance(receiver, ast.Name):
            return False
        return self._name_is_module_bound(execution, receiver.id, depth=0)

    def _name_is_module_bound(self, use: ast.AST, name: str, *, depth: int) -> bool:
        """Every reaching binding of ``name`` is a call rooted in this module's own state, never in a parameter."""

        if depth > 3:
            return False
        reaching, complete, _ = self._visible_reaching_bindings(use, name)
        if not complete or not reaching:
            return False
        for binding in reaching:
            node = binding.node
            if isinstance(node, (ast.With, ast.AsyncWith)):
                items = [item for item in node.items if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == name]
                if len(items) != 1 or not isinstance(items[0].context_expr, ast.Call):
                    return False
                if not self._call_roots_in_module(items[0].context_expr, use=node, depth=depth):
                    return False
                continue
            if binding.value is None or not isinstance(binding.value, ast.Call):
                return False
            if not self._call_roots_in_module(binding.value, use=node, depth=depth):
                return False
        return True

    def _call_roots_in_module(self, call: ast.Call, *, use: ast.AST, depth: int) -> bool:
        """No Name leaf of the call (receiver chain or arguments) is a parameter of the enclosing function except ``self``."""

        owner = self._enclosing_function(use)
        parameters: set[str] = set()
        self_name: str | None = None
        if owner is not None:
            positional = (*owner.args.posonlyargs, *owner.args.args)
            parameters = {argument.arg for argument in (*positional, *owner.args.kwonlyargs)}
            if owner.args.vararg is not None:
                parameters.add(owner.args.vararg.arg)
            if owner.args.kwarg is not None:
                parameters.add(owner.args.kwarg.arg)
            if positional and self._is_instance_method(owner) and not self._name_reassigned_in(owner, positional[0].arg):
                self_name = positional[0].arg
        for leaf in ast.walk(call):
            if not isinstance(leaf, ast.Name) or not isinstance(leaf.ctx, ast.Load):
                continue
            if leaf.id == self_name:
                continue
            if leaf.id in parameters:
                return False
            scope_key = (id(self._lexical_scope(use)), leaf.id)
            if scope_key in self.shadowed_names and not self._name_is_module_bound(use, leaf.id, depth=depth + 1):
                return False
        return True

    def _raw_sql_names_sessions_table(self, expression: ast.expr | None, *, use: ast.AST) -> bool:
        """True only for raw SQL text that names a Sessions table (the package-premise veto)."""

        if isinstance(expression, ast.Name):
            reaching, complete, _ = self._potentially_reaching_bindings(use, expression.id)
            if not complete or not reaching:
                return False
            return any(
                binding.value is not None and self._raw_sql_names_sessions_table(binding.value, use=binding.node) for binding in reaching
            )
        if isinstance(expression, ast.Call):
            qualified = self._imported_qualified_name(expression.func)
            if qualified and qualified.startswith("sqlalchemy.") and qualified.endswith(".text") and len(expression.args) == 1:
                return self._raw_sql_names_sessions_table(expression.args[0], use=use)
            return False
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            identifiers = {token.strip('"`[]').lower() for token in re.findall(_SQL_IDENTIFIER, expression.value)}
            return bool(identifiers & _TABLE_NAMES)
        return False

    def _with_binding_context(self, binding: _NameBinding, name: str) -> ast.expr | None:
        """The context expression that bound ``name`` in a ``with`` statement, when that is what the binding is."""

        if not isinstance(binding.node, (ast.With, ast.AsyncWith)):
            return None
        items = [item for item in binding.node.items if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == name]
        return items[0].context_expr if len(items) == 1 else None

    def _name_bound_from_declared_factory(self, use: ast.AST, name: str) -> bool:
        """Every reaching binding of ``name`` is a with-target or assignment of a declared non-Sessions factory call."""

        reaching, complete, _ = self._visible_reaching_bindings(use, name)
        if not complete or not reaching:
            return False
        for binding in reaching:
            value = self._with_binding_context(binding, name) if binding.value is None else binding.value
            if not isinstance(value, ast.Call):
                return False
            if self._qualified_database_domain(self._imported_qualified_name(value.func)) != "non_sessions":
                return False
        return True

    def _annotation_type_qualified_names(self, annotation: ast.expr | None) -> frozenset[str] | None:
        """Qualified names of every type in a plain or union annotation; ``None`` when any part is not resolvable."""

        if annotation is None:
            return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left = self._annotation_type_qualified_names(annotation.left)
            right = self._annotation_type_qualified_names(annotation.right)
            return None if left is None or right is None else left | right
        if isinstance(annotation, ast.Constant) and annotation.value is None:
            return frozenset()
        if isinstance(annotation, (ast.Name, ast.Attribute)):
            qualified = self._imported_qualified_name(annotation)
            if qualified is not None:
                return frozenset({qualified})
            if isinstance(annotation, ast.Name):
                bindings = self.definition_bindings.get((id(self.tree), annotation.id), [])
                if len(bindings) == 1 and isinstance(bindings[0].node, ast.ClassDef):
                    return frozenset({f"{self._module_qualified_name()}.{annotation.id}"})
            return None
        return None

    def _self_attribute_annotation(self, execution: ast.Call, attribute: ast.Attribute) -> ast.expr | None:
        """The one ``self.<attr>: T`` annotation in the enclosing class, when exactly one exists."""

        owner = self._enclosing_function(execution)
        if owner is None or not self._is_instance_method(owner):
            return None
        positional = (*owner.args.posonlyargs, *owner.args.args)
        if not positional or not (isinstance(attribute.value, ast.Name) and attribute.value.id == positional[0].arg):
            return None
        owner_class = self.method_owners[id(owner)]
        annotations = [
            node.annotation
            for method in owner_class.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(method)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Attribute)
            and node.target.attr == attribute.attr
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == positional[0].arg
        ]
        return annotations[0] if len(annotations) == 1 else None

    def _is_session_engine_configuration(self, execution: ast.Call, statement: ast.expr | None) -> bool:
        """PRAGMA assignments and transaction control inside create_session_engine are engine configuration, not table writes."""

        if self.path != _SESSION_ENGINE_FACTORY_PATH:
            return False
        symbol = _symbol(execution)
        if not (symbol == "create_session_engine" or symbol.startswith("create_session_engine.")):
            return False
        if not (isinstance(statement, ast.Constant) and isinstance(statement.value, str)):
            return False
        text = statement.value.strip().rstrip(";").strip()
        if text.upper() in _TRANSACTION_CONTROL_SQL:
            return True
        return _PRAGMA_ASSIGNMENT.fullmatch(text) is not None

    def _record_connection(self, acquisition: ast.Call, *, escapes: bool) -> None:
        # A transparent hop (a factory-return call, or a wrapper that acquires
        # from its own state) carries domain only; the engine or factory it
        # reaches is the reported acquisition. A parameter-fed wrapper call is
        # the caller's own acquisition and is recorded here.
        if id(acquisition) in self.factory_return_calls:
            return
        if id(acquisition) in self.wrapper_calls and not self._wrapper_call_is_parameter_fed(acquisition):
            return
        existing = self.write_connection_calls.get(id(acquisition))
        self.write_connection_calls[id(acquisition)] = (
            acquisition,
            escapes or (existing[1] if existing is not None else False),
        )

    def _collect_class_methods(self) -> None:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                self.method_owners[id(child)] = node
                key = (id(node), child.name)
                self.class_methods[key] = None if key in self.class_methods else child

    def _local_callable_definition(self, call: ast.Call) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """Resolve ``name(...)`` or ``self.name(...)`` to exactly one same-file definition, else ``None``."""

        func = call.func
        if isinstance(func, ast.Name):
            reaching, complete, _ = self._visible_reaching_bindings(call, func.id)
            if not complete or not reaching or any(binding.value is not None for binding in reaching):
                return None
            definitions = {id(binding.node): binding.node for binding in reaching}
            if len(definitions) != 1:
                return None
            definition = next(iter(definitions.values()))
            if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
            return definition
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = self._enclosing_function(call)
            if owner is None:
                return None
            owner_class = self.method_owners.get(id(owner))
            positional = (*owner.args.posonlyargs, *owner.args.args)
            if owner_class is None or not positional or positional[0].arg != func.value.id:
                return None
            if self._name_reassigned_in(owner, func.value.id) or not self._is_instance_method(owner):
                return None
            return self.class_methods.get((id(owner_class), func.attr))
        return None

    def _name_reassigned_in(self, scope: ast.AST, name: str) -> bool:
        """True when ``name`` is bound to a value inside ``scope`` (a bare parameter binding has no value)."""

        return any(binding.value is not None for binding in self.assignment_bindings.get((id(scope), name), ()))

    def _is_instance_method(self, definition: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if id(definition) not in self.method_owners:
            return False
        return not any(
            (isinstance(decorator, ast.Name) and decorator.id in {"staticmethod", "classmethod"})
            or self._imported_qualified_name(decorator) in {"builtins.staticmethod", "builtins.classmethod"}
            for decorator in definition.decorator_list
        )

    def _module_qualified_name(self) -> str:
        parts = list(Path(self.path).with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        return ".".join(parts)

    def _declared_engine_type_self_domain(self, definition: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> DatabaseDomain | None:
        """``self`` inside a method of a declared non-Sessions engine type carries that type's domain."""

        if not self._is_instance_method(definition):
            return None
        positional = (*definition.args.posonlyargs, *definition.args.args)
        if not positional or positional[0].arg != name or self._name_reassigned_in(definition, name):
            return None
        owner_class = self.method_owners[id(definition)]
        if f"{self._module_qualified_name()}.{owner_class.name}" in _NON_SESSION_ENGINE_TYPES:
            return "non_sessions"
        return None

    def _is_contextmanager_definition(self, definition: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(
            self._imported_qualified_name(decorator) in {"contextlib.contextmanager", "contextlib.asynccontextmanager"}
            for decorator in definition.decorator_list
        )

    @classmethod
    def _direct_yields(cls, node: ast.AST) -> list[ast.Yield | ast.YieldFrom]:
        found: list[ast.Yield | ast.YieldFrom] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                found.append(child)
            found.extend(cls._direct_yields(child))
        return found

    @classmethod
    def _direct_returns(cls, node: ast.AST) -> list[ast.Return]:
        found: list[ast.Return] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Return):
                found.append(child)
            found.extend(cls._direct_returns(child))
        return found

    def _wrapper_yield_acquisitions(
        self,
        wrapper: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        visited: frozenset[str],
    ) -> list[tuple[ast.Yield | ast.YieldFrom, list[ast.Call]]] | None:
        marker = f"<wrapper:{id(wrapper)}>"
        if marker in visited:
            return None
        # Plain names in ``visited`` belong to the caller's scope; only the
        # definition markers guard recursion across the hop.
        next_visited = self._scope_markers(visited) | {marker}
        yields = self._direct_yields(wrapper)
        if not yields:
            return None
        resolved: list[tuple[ast.Yield | ast.YieldFrom, list[ast.Call]]] = []
        for yielded in yields:
            if isinstance(yielded, ast.YieldFrom) or yielded.value is None:
                resolved.append((yielded, []))
                continue
            resolved.append((yielded, self._connection_acquisitions_for_expression(yielded, yielded.value, visited=next_visited)))
        if not any(acquisitions for _, acquisitions in resolved):
            return None
        return resolved

    def _factory_returned_acquisitions(
        self,
        callee: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        visited: frozenset[str],
    ) -> list[ast.Call] | None:
        marker = f"<factory:{id(callee)}>"
        if marker in visited:
            return None
        next_visited = self._scope_markers(visited) | {marker}
        returns = self._direct_returns(callee)
        if not returns:
            return None
        acquisitions: list[ast.Call] = []
        for returned in returns:
            if returned.value is None:
                return None
            resolved = self._connection_acquisitions_for_expression(returned, returned.value, visited=next_visited)
            if (
                not resolved
                and isinstance(returned.value, ast.Call)
                and self._qualified_database_domain(self._imported_qualified_name(returned.value.func)) is not None
            ):
                # A direct call to a declared factory (begin_write,
                # fenced_leader_transaction, ...) is that factory's acquisition.
                resolved = [returned.value]
            if not resolved:
                return None
            for acquisition in resolved:
                if self._qualified_database_domain(self._imported_qualified_name(acquisition.func)) is None:
                    return None
            acquisitions.extend(resolved)
        return acquisitions

    def _wrapper_argument_for(
        self,
        call: ast.Call,
        wrapper: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
    ) -> ast.expr | None:
        """The caller's expression bound to wrapper parameter ``name``, or ``None`` when it cannot be known exactly."""

        if self._name_reassigned_in(wrapper, name):
            return None
        positional = [*wrapper.args.posonlyargs, *wrapper.args.args]
        if id(wrapper) in self.method_owners and isinstance(call.func, ast.Attribute) and positional:
            positional = positional[1:]
        for keyword in call.keywords:
            if keyword.arg is None:
                return None
            if keyword.arg == name:
                return keyword.value
        names = [parameter.arg for parameter in positional]
        if name not in names:
            return None
        index = names.index(name)
        if index >= len(call.args) or any(isinstance(argument, ast.Starred) for argument in call.args[: index + 1]):
            return None
        return call.args[index]

    def _inner_is_parameter_fed(self, wrapper: ast.FunctionDef | ast.AsyncFunctionDef, inner: ast.Call) -> bool:
        """True when the wrapper's yielded acquisition is built on one of the wrapper's own parameters."""

        if id(inner) in self.wrapper_calls or id(inner) in self.factory_return_calls:
            return False
        if self._qualified_database_domain(self._imported_qualified_name(inner.func)) is not None:
            return False
        receiver = inner.func.value if isinstance(inner.func, ast.Attribute) else inner
        while isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute):
            receiver = receiver.func.value
        if not isinstance(receiver, ast.Name):
            return False
        parameters = {parameter.arg for parameter in (*wrapper.args.posonlyargs, *wrapper.args.args, *wrapper.args.kwonlyargs)}
        if id(wrapper) in self.method_owners:
            positional = (*wrapper.args.posonlyargs, *wrapper.args.args)
            if positional and positional[0].arg == receiver.id:
                return False
        return receiver.id in parameters and not self._name_reassigned_in(wrapper, receiver.id)

    def _wrapper_call_is_parameter_fed(self, call: ast.Call) -> bool:
        wrapper, yields = self.wrapper_calls[id(call)]
        return any(self._inner_is_parameter_fed(wrapper, inner) for _, inner_acquisitions in yields for inner in inner_acquisitions)

    def _wrapper_arms_disagree(self, call: ast.Call) -> bool:
        """True unless EVERY yield arm resolves and every arm proves the same domain (Q7 ruling, per yield)."""

        wrapper, yields = self.wrapper_calls[id(call)]
        arm_domains: set[DatabaseDomain] = set()
        for _, inner_acquisitions in yields:
            if not inner_acquisitions:
                return True
            arm_domains.add(
                self._merge_database_domains(self._wrapper_inner_origin_domain(call, wrapper, inner) for inner in inner_acquisitions)
            )
        return len(arm_domains) > 1

    @staticmethod
    def _scope_markers(visited: frozenset[str]) -> frozenset[str]:
        return frozenset(marker for marker in visited if marker.startswith("<"))

    def _absorbed_wrapper_acquisitions(self) -> set[int]:
        """Parameter-fed acquisitions inside wrappers that at least one caller now carries."""

        fed_wrappers: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for call_id, (wrapper, _) in self.wrapper_calls.items():
            if self._wrapper_call_is_parameter_fed(self.wrapper_call_nodes[call_id]):
                fed_wrappers[id(wrapper)] = wrapper
        absorbed: set[int] = set()
        for call_id, (acquisition, _) in self.write_connection_calls.items():
            owner = self._enclosing_function(acquisition)
            if owner is None or id(owner) not in fed_wrappers:
                continue
            if self._inner_is_parameter_fed(owner, acquisition):
                absorbed.add(call_id)
        return absorbed

    def _wrapper_inner_origin_domain(
        self,
        call: ast.Call,
        wrapper: ast.FunctionDef | ast.AsyncFunctionDef,
        inner: ast.Call,
    ) -> DatabaseDomain:
        if id(inner) in self.wrapper_calls or id(inner) in self.factory_return_calls:
            return self._connection_origin_database_domain(inner)
        qualified_domain = self._qualified_database_domain(self._imported_qualified_name(inner.func))
        if qualified_domain is not None:
            return qualified_domain
        receiver = inner.func.value if isinstance(inner.func, ast.Attribute) else inner
        if isinstance(receiver, ast.Name):
            argument = self._wrapper_argument_for(call, wrapper, receiver.id)
            if argument is not None:
                return self._expression_database_domain(argument, use=call)
        return self._connection_origin_database_domain(inner)

    def _wrapper_call_origin_domain(
        self,
        call: ast.Call,
        wrapper: ast.FunctionDef | ast.AsyncFunctionDef,
        yields: list[tuple[ast.Yield | ast.YieldFrom, list[ast.Call]]],
    ) -> DatabaseDomain:
        domains: list[DatabaseDomain | None] = []
        for _, inner_acquisitions in yields:
            if not inner_acquisitions:
                domains.append("unknown")
                continue
            domains.extend(self._wrapper_inner_origin_domain(call, wrapper, inner) for inner in inner_acquisitions)
        return self._merge_database_domains(domains)

    def _raw_sql_names_no_sessions_table(self, expression: ast.expr | None, *, use: ast.AST) -> bool:
        """True only for raw SQL text whose every identifier is provably outside the Sessions schema."""

        if isinstance(expression, ast.Name):
            reaching, complete, _ = self._potentially_reaching_bindings(use, expression.id)
            if not complete or not reaching:
                return False
            return all(
                binding.value is not None and self._raw_sql_names_no_sessions_table(binding.value, use=binding.node) for binding in reaching
            )
        if isinstance(expression, ast.Call):
            qualified = self._imported_qualified_name(expression.func)
            if qualified and qualified.startswith("sqlalchemy.") and qualified.endswith(".text") and len(expression.args) == 1:
                return self._raw_sql_names_no_sessions_table(expression.args[0], use=use)
            return False
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            identifiers = {token.strip('"`[]').lower() for token in re.findall(_SQL_IDENTIFIER, expression.value)}
            return not (identifiers & _TABLE_NAMES)
        return False

    @classmethod
    def _assignment_target_values(
        cls,
        target: ast.expr,
        value: ast.expr | None,
    ) -> list[tuple[str, ast.expr | None]]:
        if isinstance(target, ast.Name):
            return [(target.id, value)]
        if isinstance(target, ast.Starred):
            return cls._assignment_target_values(target.value, None)
        if isinstance(target, (ast.Tuple, ast.List)):
            values = value.elts if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) else None
            pairs: list[tuple[str, ast.expr | None]] = []
            for index, child in enumerate(target.elts):
                child_value = values[index] if values is not None else None
                pairs.extend(cls._assignment_target_values(child, child_value))
            return pairs
        return []

    def _relative_import_module(self, node: ast.ImportFrom) -> str | None:
        if node.level == 0:
            return node.module
        module_parts = list(Path(self.path).with_suffix("").parts)
        if module_parts and module_parts[0] == "src":
            module_parts = module_parts[1:]
        package_parts = module_parts[:-1]
        parents = node.level - 1
        if parents > len(package_parts):
            return None
        qualified_parts = package_parts[: len(package_parts) - parents]
        if node.module:
            qualified_parts.extend(node.module.split("."))
        return ".".join(qualified_parts) or None

    def _collect_aliases(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    local = imported.asname or imported.name.split(".", maxsplit=1)[0]
                    qualified = imported.name if imported.asname else imported.name.split(".", maxsplit=1)[0]
                    self.import_bindings.setdefault((id(self._lexical_scope(node)), local), []).append(
                        _NameBinding(node, imported=qualified)
                    )
            elif isinstance(node, ast.ImportFrom):
                qualified_module = self._relative_import_module(node)
                for imported in node.names:
                    local = imported.asname or imported.name
                    if qualified_module:
                        self.import_bindings.setdefault((id(self._lexical_scope(node)), local), []).append(
                            _NameBinding(node, imported=f"{qualified_module}.{imported.name}")
                        )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parent = getattr(node, "_inventory_parent", None)
                if parent is not None:
                    scope = self._lexical_scope(parent)
                    self.definition_bindings.setdefault((id(scope), node.name), []).append(_NameBinding(node))

        comprehension_target_names = {
            id(candidate)
            for comprehension in ast.walk(self.tree)
            if isinstance(comprehension, ast.comprehension)
            for candidate in ast.walk(comprehension.target)
            if isinstance(candidate, ast.Name)
        }
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and id(node) not in comprehension_target_names:
                self.shadowed_names.add((id(self._lexical_scope(node)), node.id))
            targets: Sequence[ast.expr] = ()
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = (node.target,)
                value = node.value
            elif isinstance(node, ast.AugAssign):
                targets = (node.target,)
            elif isinstance(node, ast.Delete):
                targets = node.targets
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets = (node.target,)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                targets = tuple(item.optional_vars for item in node.items if item.optional_vars is not None)
            for target in targets:
                for name, bound_value in self._assignment_target_values(target, value):
                    self.assignment_bindings.setdefault(
                        (id(self._lexical_scope(node)), name),
                        [],
                    ).append(_NameBinding(node, value=bound_value))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                for argument in arguments:
                    self.shadowed_names.add((id(node), argument.arg))
                if node.args.vararg:
                    self.shadowed_names.add((id(node), node.args.vararg.arg))
                if node.args.kwarg:
                    self.shadowed_names.add((id(node), node.args.kwarg.arg))

        for node in ast.walk(self.tree):
            targets: Sequence[ast.expr] = ()
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = (node.target,)
                value = node.value
            elif isinstance(node, ast.AugAssign):
                targets = (node.target,)
            elif isinstance(node, ast.Delete):
                targets = node.targets
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                targets = (node.target,)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                targets = tuple(item.optional_vars for item in node.items if item.optional_vars is not None)
            for target in targets:
                for attribute in (
                    candidate
                    for candidate in ast.walk(target)
                    if isinstance(candidate, ast.Attribute) and isinstance(candidate.ctx, (ast.Store, ast.Del))
                ):
                    qualified = self._imported_qualified_name(attribute, honor_attribute_bindings=False)
                    if qualified is not None:
                        self.attribute_bindings.setdefault(qualified, []).append(_NameBinding(node, value=value))

        for bindings in self.definition_bindings.values():
            for index, binding in enumerate(bindings):
                if isinstance(binding.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bindings[index] = replace(
                        binding,
                        no_return=self._annotation_has_no_return_provenance(binding.node),
                    )

    @staticmethod
    def _position(node: ast.AST) -> tuple[int, int]:
        return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))

    @staticmethod
    def _scope_has_parameter(scope: ast.AST, name: str) -> bool:
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        arguments = (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
        return (
            any(argument.arg == name for argument in arguments)
            or (scope.args.vararg is not None and scope.args.vararg.arg == name)
            or (scope.args.kwarg is not None and scope.args.kwarg.arg == name)
        )

    def _conditional_bindings_are_exhaustive(
        self,
        bindings: Sequence[_NameBinding],
        use: ast.AST,
    ) -> bool:
        if not bindings:
            return False
        common_guards: set[ast.If] | None = None
        for binding in bindings:
            guards: set[ast.If] = set()
            current = getattr(binding.node, "_inventory_parent", None)
            while current is not None and current is not self._lexical_scope(binding.node):
                if isinstance(current, ast.If) and not self._is_descendant(use, current):
                    guards.add(current)
                current = getattr(current, "_inventory_parent", None)
            common_guards = guards if common_guards is None else common_guards & guards
        if not common_guards:
            return False
        guard = min(common_guards, key=self._position)
        return self._branch_guarantees_binding(guard.body, bindings) and self._branch_guarantees_binding(
            guard.orelse,
            bindings,
        )

    @staticmethod
    def _if_branch_containing(node: ast.AST, conditional: ast.If) -> Literal["body", "orelse"] | None:
        current = node
        parent = getattr(current, "_inventory_parent", None)
        while parent is not None and parent is not conditional:
            current = parent
            parent = getattr(current, "_inventory_parent", None)
        if parent is not conditional:
            return None
        if current in conditional.body:
            return "body"
        if current in conditional.orelse:
            return "orelse"
        return None

    def _is_in_sibling_branch(self, binding: _NameBinding, use: ast.AST) -> bool:
        scope = self._lexical_scope(binding.node)
        current = getattr(binding.node, "_inventory_parent", None)
        while current is not None and current is not scope:
            if isinstance(current, ast.If) and self._is_descendant(use, current):
                binding_branch = self._if_branch_containing(binding.node, current)
                use_branch = self._if_branch_containing(use, current)
                if binding_branch is not None and use_branch is not None and binding_branch != use_branch:
                    return True
            current = getattr(current, "_inventory_parent", None)
        return False

    def _is_proven_no_return_call(self, call: ast.Call) -> bool:
        if not isinstance(call.func, ast.Name):
            return False
        reaching, complete, _ = self._visible_reaching_bindings(call, call.func.id)
        return complete and len(reaching) == 1 and reaching[0].no_return

    def _branch_guarantees_binding(
        self,
        statements: Sequence[ast.stmt],
        bindings: Sequence[_NameBinding],
    ) -> bool:
        for statement in statements:
            if any(binding.node is statement for binding in bindings):
                return True
            if isinstance(statement, ast.If):
                if self._branch_guarantees_binding(statement.body, bindings) and self._branch_guarantees_binding(
                    statement.orelse,
                    bindings,
                ):
                    return True
                continue
            if isinstance(statement, (ast.Return, ast.Raise)):
                return True
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and self._is_proven_no_return_call(statement.value)
            ):
                return True
        return False

    def _comprehension_target_binding(
        self,
        use: ast.AST,
        name: str,
        *,
        scope: ast.AST,
    ) -> _NameBinding | None:
        current: ast.AST | None = use
        while current is not None:
            if isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) and self._lexical_scope(current) is scope:
                result_expressions = (current.key, current.value) if isinstance(current, ast.DictComp) else (current.elt,)
                bound_generators: Sequence[ast.comprehension] = ()
                if any(self._is_descendant(use, expression) for expression in result_expressions):
                    bound_generators = current.generators
                else:
                    for index, generator in enumerate(current.generators):
                        if self._is_descendant(use, generator.iter):
                            bound_generators = current.generators[:index]
                            break
                        if any(self._is_descendant(use, condition) for condition in generator.ifs):
                            bound_generators = current.generators[: index + 1]
                            break
                for generator in reversed(bound_generators):
                    if any(
                        isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Store) and candidate.id == name
                        for candidate in ast.walk(generator.target)
                    ):
                        return _NameBinding(generator)
            current = getattr(current, "_inventory_parent", None)
        return None

    def _potentially_reaching_bindings(
        self,
        use: ast.AST,
        name: str,
        *,
        scope: ast.AST | None = None,
        include_late_module_bindings: bool = True,
    ) -> tuple[list[_NameBinding], bool, bool]:
        lexical_scope = scope or self._lexical_scope(use)
        comprehension_binding = self._comprehension_target_binding(
            use,
            name,
            scope=lexical_scope,
        )
        if comprehension_binding is not None:
            return [comprehension_binding], True, True
        key = (id(lexical_scope), name)
        candidates = [
            *self.import_bindings.get(key, ()),
            *self.assignment_bindings.get(key, ()),
            *self.definition_bindings.get(key, ()),
        ]
        candidates = [binding for binding in candidates if not self._is_in_sibling_branch(binding, use)]
        # ``stmt = stmt.on_conflict_do_update(...)``: the right-hand side is
        # evaluated before the target is bound, so the assignment never
        # reaches a load inside its own value.  Its position precedes that
        # load's, which used to make it its own reaching binding and cut a
        # prebuilt DML off from its execution (elspeth-a85fb1555b).
        candidates = [
            binding
            for binding in candidates
            if not (
                isinstance(binding.node, (ast.Assign, ast.AnnAssign))
                and binding.value is not None
                and self._is_descendant(use, binding.value)
            )
        ]
        use_position = self._position(use)
        nested_module_lookup = (
            include_late_module_bindings and isinstance(lexical_scope, ast.Module) and self._lexical_scope(use) is not lexical_scope
        )
        if not nested_module_lookup:
            candidates = [binding for binding in candidates if self._position(binding.node) < use_position]
        candidates.sort(key=lambda binding: self._position(binding.node))

        locally_bound = (
            key in self.import_bindings
            or key in self.assignment_bindings
            or key in self.definition_bindings
            or key in self.shadowed_names
            or self._scope_has_parameter(lexical_scope, name)
        )
        initial: _NameBinding | None = None
        if not isinstance(lexical_scope, ast.Module) and locally_bound:
            initial = _NameBinding(lexical_scope)

        def conditional(binding: _NameBinding) -> bool:
            return isinstance(binding.node, (ast.For, ast.AsyncFor)) or self._conditional_guard_between(binding.node, use) is not None

        unconditional = [binding for binding in candidates if not conditional(binding)]
        if initial is not None:
            unconditional.insert(0, initial)

        if unconditional:
            base = max(unconditional, key=lambda binding: self._position(binding.node))
            base_position = self._position(base.node)
            conditional_after_base = [
                binding for binding in candidates if self._position(binding.node) > base_position and conditional(binding)
            ]
            if conditional_after_base and self._conditional_bindings_are_exhaustive(
                conditional_after_base,
                use,
            ):
                return conditional_after_base, True, locally_bound
            reaching = [base, *conditional_after_base]
            return reaching, True, locally_bound

        if candidates:
            return candidates, self._conditional_bindings_are_exhaustive(candidates, use), locally_bound
        return [], False, locally_bound

    def _annotation_imported_qualified_name(
        self,
        expression: ast.expr | None,
        *,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: ast.AST,
    ) -> str | None:
        if isinstance(expression, ast.Attribute):
            base = self._annotation_imported_qualified_name(
                expression.value,
                definition=definition,
                scope=scope,
            )
            return f"{base}.{expression.attr}" if base else None
        if not isinstance(expression, ast.Name):
            return None

        current_scope: ast.AST | None = scope
        while current_scope is not None:
            reaching, complete, locally_bound = self._potentially_reaching_bindings(
                definition,
                expression.id,
                scope=current_scope,
                include_late_module_bindings=False,
            )
            imported = {binding.imported for binding in reaching}
            if complete and len(imported) == 1 and None not in imported:
                return imported.pop()
            if reaching or locally_bound:
                return None
            current_scope = self._next_lookup_scope(current_scope)
        return None

    def _annotation_has_no_return_provenance(
        self,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        parent = getattr(definition, "_inventory_parent", None)
        if parent is None:
            return False
        scope = self._lexical_scope(parent)
        return self._annotation_imported_qualified_name(
            definition.returns,
            definition=definition,
            scope=scope,
        ) in {"typing.NoReturn", "typing.Never"}

    def _imported_qualified_name(
        self,
        expression: ast.expr,
        *,
        visited: frozenset[tuple[int, str]] = frozenset(),
        visited_attributes: frozenset[tuple[str, int]] = frozenset(),
        honor_attribute_bindings: bool = True,
    ) -> str | None:
        if isinstance(expression, ast.Attribute):
            base = self._imported_qualified_name(
                expression.value,
                visited=visited,
                visited_attributes=visited_attributes,
                honor_attribute_bindings=honor_attribute_bindings,
            )
            qualified = f"{base}.{expression.attr}" if base else None
            attribute_key = (qualified, id(expression)) if qualified is not None else None
            if qualified is None or attribute_key in visited_attributes:
                return None
            mutations, original_reaches = (
                self._reaching_attribute_bindings(expression, qualified) if honor_attribute_bindings else ([], True)
            )
            if not mutations and original_reaches:
                return qualified
            next_visited = visited_attributes | {attribute_key}
            resolved = ({qualified} if original_reaches else set()) | {
                self._imported_qualified_name(
                    binding.value,
                    visited=visited,
                    visited_attributes=next_visited,
                    honor_attribute_bindings=honor_attribute_bindings,
                )
                if binding.value is not None
                else None
                for binding in mutations
            }
            if len(resolved) == 1 and None not in resolved:
                return resolved.pop()
            return None
        if not isinstance(expression, ast.Name):
            return None

        scope: ast.AST | None = self._lexical_scope(expression)
        while scope is not None:
            reaching, complete, locally_bound = self._potentially_reaching_bindings(
                expression,
                expression.id,
                scope=scope,
            )
            key = (id(scope), expression.id)
            if key in visited:
                return None
            imported = {
                binding.imported
                if binding.imported is not None
                else (
                    self._imported_qualified_name(
                        binding.value,
                        visited=visited | {key},
                        visited_attributes=visited_attributes,
                        honor_attribute_bindings=honor_attribute_bindings,
                    )
                    if binding.value is not None
                    else None
                )
                for binding in reaching
            }
            if complete and len(imported) == 1 and None not in imported:
                return imported.pop()
            if reaching or locally_bound:
                return None
            scope = self._next_lookup_scope(scope)
        return None

    def _reaching_attribute_bindings(
        self,
        use: ast.AST,
        qualified: str,
    ) -> tuple[list[_NameBinding], bool]:
        lookup_scopes: list[ast.AST] = []
        scope: ast.AST | None = self._lexical_scope(use)
        while scope is not None:
            lookup_scopes.append(scope)
            scope = self._next_lookup_scope(scope)

        reaching: list[_NameBinding] = []
        original_reaches = True
        use_scope = self._lexical_scope(use)
        for lookup_scope in reversed(lookup_scopes):
            candidates = [
                binding
                for binding in self.attribute_bindings.get(qualified, ())
                if self._lexical_scope(binding.node) is lookup_scope and not self._is_in_sibling_branch(binding, use)
            ]
            if lookup_scope is use_scope:
                candidates = [binding for binding in candidates if self._position(binding.node) < self._position(use)]
            candidates.sort(key=lambda binding: self._position(binding.node))
            if not candidates:
                continue

            def conditional(binding: _NameBinding) -> bool:
                return (
                    isinstance(binding.node, (ast.For, ast.AsyncFor, ast.comprehension))
                    or self._conditional_guard_between(
                        binding.node,
                        use,
                    )
                    is not None
                )

            unconditional = [binding for binding in candidates if not conditional(binding)]
            if unconditional:
                base = max(unconditional, key=lambda binding: self._position(binding.node))
                reaching = [base]
                original_reaches = False
                candidates = [
                    binding for binding in candidates if self._position(binding.node) > self._position(base.node) and conditional(binding)
                ]
            if candidates:
                if self._conditional_bindings_are_exhaustive(candidates, use):
                    reaching = candidates
                    original_reaches = False
                else:
                    reaching.extend(candidates)
        return reaching, original_reaches

    @staticmethod
    def _next_lexical_scope(scope: ast.AST) -> ast.AST | None:
        current = getattr(scope, "_inventory_parent", None)
        while current is not None and not isinstance(
            current,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module),
        ):
            current = getattr(current, "_inventory_parent", None)
        return current

    def _next_lookup_scope(self, scope: ast.AST) -> ast.AST | None:
        next_scope = self._next_lexical_scope(scope)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            while isinstance(next_scope, ast.ClassDef):
                next_scope = self._next_lexical_scope(next_scope)
        return next_scope

    def _visible_reaching_bindings(
        self,
        use: ast.AST,
        name: str,
    ) -> tuple[list[_NameBinding], bool, ast.AST | None]:
        scope: ast.AST | None = self._lexical_scope(use)
        while scope is not None:
            reaching, complete, locally_bound = self._potentially_reaching_bindings(
                use,
                name,
                scope=scope,
            )
            if reaching or locally_bound:
                return reaching, complete, scope
            scope = self._next_lookup_scope(scope)
        return [], False, None

    @staticmethod
    def _merge_database_domains(domains: Iterable[DatabaseDomain | None]) -> DatabaseDomain:
        observed = {domain for domain in domains if domain is not None}
        if not observed:
            return "unknown"
        if observed == {"sessions"}:
            return "sessions"
        if observed == {"non_sessions"}:
            return "non_sessions"
        return "unknown"

    @staticmethod
    def _qualified_database_domain(qualified: str | None) -> DatabaseDomain | None:
        if qualified in _SESSION_ENGINE_FACTORIES:
            return "sessions"
        if qualified in _NON_SESSION_ENGINE_FACTORIES or qualified in _NON_SESSION_ENGINE_TYPES:
            return "non_sessions"
        return None

    def _parameter_database_domain(self, use: ast.AST, name: str) -> DatabaseDomain | None:
        scope: ast.AST | None = self._lexical_scope(use)
        while scope is not None:
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
                argument = next((candidate for candidate in arguments if candidate.arg == name), None)
                if argument is not None:
                    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return None
                    self_domain = self._declared_engine_type_self_domain(scope, name)
                    if self_domain is not None:
                        return self_domain
                    parent = getattr(scope, "_inventory_parent", None)
                    if parent is None:
                        return None
                    annotation_scope = self._lexical_scope(parent)
                    return self._annotation_database_domain(
                        argument.annotation,
                        definition=scope,
                        scope=annotation_scope,
                    )
            scope = self._next_lookup_scope(scope)
        return None

    def _binding_annotation_qualified_name(self, binding: _NameBinding, name: str) -> str | None:
        node = binding.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            argument = next((candidate for candidate in arguments if candidate.arg == name), None)
            parent = getattr(node, "_inventory_parent", None)
            if argument is None or parent is None:
                return None
            return self._annotation_imported_qualified_name(
                argument.annotation,
                definition=node,
                scope=self._lexical_scope(parent),
            )
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return self._imported_qualified_name(node.annotation)
        return None

    def _has_explicit_non_sql_execute_receiver(self, execution: ast.Call) -> bool:
        if not (isinstance(execution.func, ast.Attribute) and execution.func.attr == "execute"):
            return False
        if isinstance(execution.func.value, ast.Attribute):
            annotation = self._self_attribute_annotation(execution, execution.func.value)
            declared = self._annotation_type_qualified_names(annotation)
            return bool(declared) and declared <= _EXPLICIT_NON_SQL_EXECUTE_RECEIVER_TYPES
        if not isinstance(execution.func.value, ast.Name):
            return False
        receiver = execution.func.value
        reaching, complete, _ = self._visible_reaching_bindings(execution, receiver.id)
        if not complete or not reaching:
            return False
        receiver_types = {self._binding_annotation_qualified_name(binding, receiver.id) for binding in reaching}
        return bool(receiver_types) and receiver_types <= _EXPLICIT_NON_SQL_EXECUTE_RECEIVER_TYPES

    def _annotation_database_domain(
        self,
        expression: ast.expr | None,
        *,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: ast.AST,
    ) -> DatabaseDomain | None:
        if expression is None or (isinstance(expression, ast.Constant) and expression.value is None):
            return None
        qualified = self._annotation_imported_qualified_name(
            expression,
            definition=definition,
            scope=scope,
        )
        direct = self._qualified_database_domain(qualified)
        if direct is not None:
            return direct
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
            return self._merge_database_domains(
                (
                    self._annotation_database_domain(expression.left, definition=definition, scope=scope),
                    self._annotation_database_domain(expression.right, definition=definition, scope=scope),
                )
            )
        return None

    def _attribute_assignment_values(self, expression: ast.Attribute) -> list[ast.expr | None]:
        def is_non_instance_method(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
            return any(
                (isinstance(decorator, ast.Name) and decorator.id in {"classmethod", "staticmethod"})
                or (isinstance(decorator, ast.Attribute) and decorator.attr in {"classmethod", "staticmethod"})
                for decorator in definition.decorator_list
            )

        if not isinstance(expression.value, ast.Name):
            return []
        method = self._enclosing_function(expression)
        if method is None or is_non_instance_method(method):
            return []
        owner = getattr(method, "_inventory_parent", None)
        if not isinstance(owner, ast.ClassDef) or owner.bases:
            return []
        positional = (*method.args.posonlyargs, *method.args.args)
        if not positional or positional[0].arg != expression.value.id:
            return []
        if any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == expression.attr for member in owner.body):
            return []

        values: list[ast.expr | None] = []
        for member in owner.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            member_positional = (*member.args.posonlyargs, *member.args.args)
            if not member_positional:
                continue
            receiver = member_positional[0].arg
            scoped_nodes: list[ast.AST] = []
            pending = list(reversed(member.body))
            while pending:
                candidate = pending.pop()
                scoped_nodes.append(candidate)
                for child in reversed(list(ast.iter_child_nodes(candidate))):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                        continue
                    pending.append(child)
            if is_non_instance_method(member):
                if any(
                    isinstance(candidate, ast.Attribute)
                    and isinstance(candidate.ctx, (ast.Store, ast.Del))
                    and candidate.attr == expression.attr
                    for candidate in scoped_nodes
                ):
                    return []
                continue
            if any(
                isinstance(candidate, ast.Name) and isinstance(candidate.ctx, (ast.Store, ast.Del)) and candidate.id == receiver
                for candidate in scoped_nodes
            ):
                continue
            for candidate in scoped_nodes:
                targets: Sequence[ast.expr] = ()
                value: ast.expr | None = None
                if isinstance(candidate, ast.Assign):
                    targets = candidate.targets
                    value = candidate.value
                elif isinstance(candidate, ast.AnnAssign):
                    targets = (candidate.target,)
                    value = candidate.value
                elif isinstance(candidate, ast.AugAssign):
                    targets = (candidate.target,)
                elif isinstance(candidate, ast.Delete):
                    targets = candidate.targets
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == receiver
                        and target.attr == expression.attr
                    ):
                        if getattr(candidate, "_inventory_parent", None) is not member:
                            return []
                        values.append(value)
        return values if len(values) == 1 else []

    def _expression_database_domain(
        self,
        expression: ast.expr | None,
        *,
        use: ast.AST,
        visited_names: frozenset[tuple[int, str]] = frozenset(),
        visited_attributes: frozenset[str] = frozenset(),
    ) -> DatabaseDomain:
        if expression is None or (isinstance(expression, ast.Constant) and expression.value is None):
            return "unknown"

        qualified = self._imported_qualified_name(expression)
        direct = self._qualified_database_domain(qualified)
        if direct is not None:
            return direct

        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self._expression_database_domain(
                expression.value,
                use=use,
                visited_names=visited_names,
                visited_attributes=visited_attributes,
            )
        if isinstance(expression, ast.IfExp):
            return self._merge_database_domains(
                self._expression_database_domain(
                    branch,
                    use=use,
                    visited_names=visited_names,
                    visited_attributes=visited_attributes,
                )
                for branch in (expression.body, expression.orelse)
            )
        if isinstance(expression, ast.Call):
            called = self._imported_qualified_name(expression.func)
            called_domain = self._qualified_database_domain(called)
            if called_domain is not None:
                return called_domain
            if isinstance(expression.func, ast.Attribute) and expression.func.attr in {
                "begin",
                "connect",
                "execution_options",
            }:
                return self._expression_database_domain(
                    expression.func.value,
                    use=use,
                    visited_names=visited_names,
                    visited_attributes=visited_attributes,
                )
            return "unknown"
        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(use, expression.id)
            if scope is None:
                return "unknown"
            key = (id(scope), expression.id)
            if key in visited_names:
                return "unknown"
            parameter_domain = self._parameter_database_domain(use, expression.id)
            domains: list[DatabaseDomain | None] = []
            for binding in reaching:
                with_context = self._with_binding_context(binding, expression.id) if binding.value is None else None
                if with_context is not None:
                    domains.append(
                        self._expression_database_domain(
                            with_context,
                            use=binding.node,
                            visited_names=visited_names | {key},
                            visited_attributes=visited_attributes,
                        )
                    )
                elif binding.value is None:
                    domains.append(parameter_domain)
                else:
                    domains.append(
                        self._expression_database_domain(
                            binding.value,
                            use=binding.node,
                            visited_names=visited_names | {key},
                            visited_attributes=visited_attributes,
                        )
                    )
            if not complete:
                domains.append("unknown")
            return self._merge_database_domains(domains)
        if isinstance(expression, ast.Attribute):
            base_domain = self._expression_database_domain(
                expression.value,
                use=use,
                visited_names=visited_names,
                visited_attributes=visited_attributes,
            )
            if base_domain != "unknown":
                return base_domain
            attribute_key = stable_ast_dump(expression)
            if attribute_key in visited_attributes:
                return "unknown"
            values = self._attribute_assignment_values(expression)
            if not values:
                return "unknown"
            return self._merge_database_domains(
                None
                if value is None or (isinstance(value, ast.Constant) and value.value is None)
                else self._expression_database_domain(
                    value,
                    use=value,
                    visited_names=visited_names,
                    visited_attributes=visited_attributes | {attribute_key},
                )
                for value in values
            )
        return "unknown"

    def _table_database_domain(
        self,
        expression: ast.expr | None,
        *,
        visited: frozenset[tuple[int, str]] = frozenset(),
    ) -> DatabaseDomain:
        if expression is None:
            return "unknown"
        qualified = self._imported_qualified_name(expression)
        if qualified and qualified.startswith(f"{_SESSION_TABLE_MODULE}."):
            return "sessions" if qualified.rsplit(".", maxsplit=1)[-1] in _TABLE_IDENTIFIERS else "unknown"
        if qualified and qualified.startswith(f"{_LANDSCAPE_TABLE_MODULE}."):
            return "non_sessions"
        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(expression, expression.id)
            if scope is None:
                return "unknown"
            key = (id(scope), expression.id)
            if key in visited or not complete or not reaching or any(binding.value is None for binding in reaching):
                return "unknown"
            return self._merge_database_domains(self._table_database_domain(binding.value, visited=visited | {key}) for binding in reaching)
        return "unknown"

    def _table(
        self,
        expression: ast.expr | None,
        *,
        visited: frozenset[tuple[int, str]] = frozenset(),
    ) -> str | None:
        if expression is None:
            return None
        qualified = self._imported_qualified_name(expression)
        if qualified and qualified.startswith("elspeth.web.sessions.models."):
            return _TABLE_IDENTIFIERS.get(qualified.rsplit(".", maxsplit=1)[-1])
        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(expression, expression.id)
            if scope is None:
                return None
            key = (id(scope), expression.id)
            if key in visited:
                return None
            if not complete or not reaching or any(binding.value is None for binding in reaching):
                return None
            tables = {
                self._table(
                    binding.value,
                    visited=visited | {key},
                )
                for binding in reaching
            }
            if len(tables) == 1 and None not in tables:
                return tables.pop()
        return None

    def _dml_callable_provenance(
        self,
        expression: ast.expr,
        *,
        visited: frozenset[tuple[int, str]] = frozenset(),
    ) -> tuple[DatabaseDomain, str | None, str] | None:
        if isinstance(expression, ast.Attribute) and expression.attr in {"insert", "update", "delete"}:
            table = self._table(expression.value)
            if table is not None:
                return "sessions", table, expression.attr
            table_domain = self._table_database_domain(expression.value)
            if table_domain == "non_sessions":
                return table_domain, None, expression.attr

        qualified = self._imported_qualified_name(expression)
        if qualified and qualified.startswith("sqlalchemy.") and qualified.rsplit(".", maxsplit=1)[-1] in {"insert", "update", "delete"}:
            return "unknown", None, qualified.rsplit(".", maxsplit=1)[-1]

        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(expression, expression.id)
            if scope is None:
                return None
            key = (id(scope), expression.id)
            if key in visited:
                return None
            if not complete or not reaching or any(binding.value is None for binding in reaching):
                return None
            provenances = {
                self._dml_callable_provenance(
                    binding.value,
                    visited=visited | {key},
                )
                for binding in reaching
            }
            if len(provenances) == 1 and None not in provenances:
                return provenances.pop()
        return None

    def _classify_non_session_execution(self, node: ast.AST) -> None:
        for execution in self._write_executions_for(node):
            statement = execution.args[0] if execution.args else None
            if (
                self._statement_database_domain(statement) == "non_sessions"
                and self._execution_connection_database_domain(execution) == "non_sessions"
            ):
                self.classified_execution_calls.add(id(execution))

    def _emit(self, node: ast.AST, table: str, operation: str) -> None:
        executions = self._write_executions_for(node)
        self.classified_execution_calls.update(id(execution) for execution in executions)
        self._append_site(
            node,
            table,
            operation,
            fingerprint=self._writer_context_fingerprint(node),
        )
        for execution in executions:
            for acquisition in self._connection_acquisitions_for(execution):
                self._record_connection(acquisition, escapes=False)

    def _append_site(
        self,
        node: ast.AST,
        table: str,
        operation: str,
        *,
        fingerprint: str | None = None,
        connection_escape: bool = False,
    ) -> None:
        symbol = _symbol(node)
        self.site_nodes.append(node)
        self.sites.append(
            WriterIdentity(
                path=self.path,
                symbol=symbol,
                table=table,
                operation=operation,
                fingerprint=fingerprint or _statement_fingerprint(node),
                ordinal=0,
                authority=_authority_for(self.path, symbol),
                line=getattr(node, "lineno", 0),
                connection_escape=connection_escape,
            )
        )

    @staticmethod
    def _enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = getattr(current, "_inventory_parent", None)
        return None

    def _connection_context_fingerprint(self, node: ast.AST) -> str:
        context: ast.AST = self._enclosing_function(node) or self.tree
        normalized = "\0".join(
            (
                _statement_fingerprint(node),
                stable_ast_dump(context),
            )
        )
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    @staticmethod
    def _lexical_scope(node: ast.AST) -> ast.AST:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)):
                return current
            current = getattr(current, "_inventory_parent", None)
        raise AssertionError("inventory node is detached from its syntax tree")

    @staticmethod
    def _is_descendant(node: ast.AST, ancestor: ast.AST) -> bool:
        current: ast.AST | None = node
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "_inventory_parent", None)
        return False

    def _conditional_guard_between(self, assignment: ast.AST, use: ast.AST) -> ast.AST | None:
        scope = self._lexical_scope(assignment)
        current = getattr(assignment, "_inventory_parent", None)
        while current is not None and current is not scope:
            if isinstance(current, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match)) and not self._is_descendant(
                use,
                current,
            ):
                return current
            current = getattr(current, "_inventory_parent", None)
        return None

    def _is_connection_callable_alias(
        self,
        use: ast.AST,
        name: str,
        *,
        visited: frozenset[str],
        visited_storage: frozenset[tuple[object, ...]] = frozenset(),
    ) -> bool:
        if name in visited:
            return False
        next_visited = visited | {name}
        reaching, _, _ = self._visible_reaching_bindings(use, name)
        for binding in reaching:
            value = binding.value
            if value is not None and self._is_connection_callable_expression(
                binding.node,
                value,
                visited=next_visited,
                visited_storage=visited_storage,
            ):
                return True
        return False

    def _is_connection_callable_expression(
        self,
        use: ast.AST,
        expression: ast.expr,
        *,
        visited: frozenset[str],
        visited_storage: frozenset[tuple[object, ...]] = frozenset(),
    ) -> bool:
        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self._is_connection_callable_expression(
                use,
                expression.value,
                visited=visited,
                visited_storage=visited_storage,
            )
        if isinstance(expression, ast.IfExp):
            return any(
                self._is_connection_callable_expression(
                    use,
                    branch,
                    visited=visited,
                    visited_storage=visited_storage,
                )
                for branch in (expression.body, expression.orelse)
            )
        if isinstance(expression, ast.BoolOp):
            return any(
                self._is_connection_callable_expression(
                    use,
                    value,
                    visited=visited,
                    visited_storage=visited_storage,
                )
                for value in expression.values
            )
        if isinstance(expression, ast.Attribute) and expression.attr in {"begin", "connect"}:
            return not (
                expression.attr == "begin"
                and isinstance(expression.value, ast.Name)
                and self._connection_acquisitions_for_name(
                    use,
                    expression.value.id,
                    visited=visited,
                )
            )
        if isinstance(expression, (ast.Attribute, ast.Subscript)):
            return self._is_stored_connection_callable(
                use,
                expression,
                visited_storage=visited_storage,
            )
        if isinstance(expression, ast.Name):
            return self._is_connection_callable_alias(
                use,
                expression.id,
                visited=visited,
                visited_storage=visited_storage,
            )
        return False

    def _connection_acquisitions_for_expression(
        self,
        use: ast.AST,
        expression: ast.expr | None,
        *,
        visited: frozenset[str] = frozenset(),
    ) -> list[ast.Call]:
        if isinstance(expression, ast.Name):
            return self._connection_acquisitions_for_name(use, expression.id, visited=visited)
        if isinstance(expression, (ast.Attribute, ast.Subscript)):
            current: ast.AST | None = use
            expression_keys = self._storage_target_keys(expression, use=use)
            while current is not None:
                if isinstance(current, (ast.With, ast.AsyncWith)):
                    for item in current.items:
                        if item.optional_vars is not None and expression_keys & self._storage_target_keys(
                            item.optional_vars,
                            use=item.context_expr,
                        ):
                            return self._connection_acquisitions_for_expression(
                                item.context_expr,
                                item.context_expr,
                                visited=visited,
                            )
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    break
                current = getattr(current, "_inventory_parent", None)
            return []
        if isinstance(expression, ast.Starred):
            return self._connection_acquisitions_for_expression(use, expression.value, visited=visited)
        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self._connection_acquisitions_for_expression(use, expression.value, visited=visited)
        if isinstance(expression, ast.IfExp):
            return [
                acquisition
                for branch in (expression.body, expression.orelse)
                for acquisition in self._connection_acquisitions_for_expression(use, branch, visited=visited)
            ]
        if isinstance(expression, ast.Lambda):
            return self._connection_acquisitions_for_expression(use, expression.body, visited=visited)
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            return [
                acquisition
                for element in expression.elts
                for acquisition in self._connection_acquisitions_for_expression(use, element, visited=visited)
            ]
        if isinstance(expression, ast.Dict):
            return [
                acquisition
                for element in (*expression.keys, *expression.values)
                if element is not None
                for acquisition in self._connection_acquisitions_for_expression(use, element, visited=visited)
            ]
        if isinstance(expression, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            components: list[ast.expr] = [expression.elt]
            for generator in expression.generators:
                components.append(generator.iter)
                components.extend(generator.ifs)
            return [
                acquisition
                for component in components
                for acquisition in self._connection_acquisitions_for_expression(use, component, visited=visited)
            ]
        if isinstance(expression, ast.DictComp):
            components = [expression.key, expression.value]
            for generator in expression.generators:
                components.append(generator.iter)
                components.extend(generator.ifs)
            return [
                acquisition
                for component in components
                for acquisition in self._connection_acquisitions_for_expression(use, component, visited=visited)
            ]
        if not isinstance(expression, ast.Call):
            return []
        if isinstance(expression.func, ast.Attribute) and expression.func.attr in {"begin", "connect"}:
            if expression.func.attr == "begin" and isinstance(expression.func.value, ast.Name):
                receiver_acquisitions = self._connection_acquisitions_for_name(
                    expression,
                    expression.func.value.id,
                    visited=visited,
                )
                if receiver_acquisitions:
                    return []
            return [expression]
        if (
            isinstance(expression.func, ast.Attribute)
            and expression.func.attr in _LANDSCAPE_CONNECTION_VERBS
            and isinstance(expression.func.value, ast.Name)
            and self._name_bound_from_declared_factory(use, expression.func.value.id)
        ):
            return [expression]
        if isinstance(expression.func, ast.Attribute) and expression.func.attr in {"execution_options"}:
            return self._connection_acquisitions_for_expression(
                use,
                expression.func.value,
                visited=visited,
            )
        callee = self._local_callable_definition(expression)
        if callee is not None:
            if self._is_contextmanager_definition(callee):
                yields = self._wrapper_yield_acquisitions(callee, visited=visited)
                if yields is None:
                    return []
                self.wrapper_calls[id(expression)] = (callee, yields)
                self.wrapper_call_nodes[id(expression)] = expression
                return [expression]
            if not self._direct_yields(callee):
                returned = self._factory_returned_acquisitions(callee, visited=visited)
                if returned is None:
                    return []
                self.factory_return_calls[id(expression)] = (callee, returned)
                return [expression]
            return []
        if isinstance(expression.func, ast.Name) and self._is_connection_callable_alias(
            expression,
            expression.func.id,
            visited=visited,
        ):
            return [expression]
        if isinstance(expression.func, (ast.Attribute, ast.Subscript)) and self._is_stored_connection_callable(
            expression,
            expression.func,
        ):
            return [expression]
        return []

    def _storage_target_keys(
        self,
        expression: ast.expr,
        *,
        use: ast.AST,
        visited_names: frozenset[tuple[int, str]] = frozenset(),
    ) -> set[tuple[object, ...]]:
        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(use, expression.id)
            lexical_scope = scope or self._lexical_scope(use)
            marker = (id(lexical_scope), expression.id)
            own = {("name", *marker)}
            if marker in visited_names or not complete or not reaching:
                return own
            resolved: set[tuple[object, ...]] = set()
            for binding in reaching:
                if isinstance(binding.value, (ast.Name, ast.Attribute, ast.Subscript)):
                    resolved.update(
                        self._storage_target_keys(
                            binding.value,
                            use=binding.node,
                            visited_names=visited_names | {marker},
                        )
                    )
                else:
                    resolved.update(own)
            return resolved or own
        if isinstance(expression, ast.Attribute):
            return {
                ("attribute", base, expression.attr)
                for base in self._storage_target_keys(
                    expression.value,
                    use=use,
                    visited_names=visited_names,
                )
            }
        if isinstance(expression, ast.Subscript):
            return {
                ("subscript", base, stable_ast_dump(expression.slice))
                for base in self._storage_target_keys(
                    expression.value,
                    use=use,
                    visited_names=visited_names,
                )
            }
        return set()

    def _is_stored_connection_callable(
        self,
        use: ast.AST,
        expression: ast.expr,
        *,
        visited_storage: frozenset[tuple[object, ...]] = frozenset(),
    ) -> bool:
        keys = self._storage_target_keys(expression, use=use)
        if not keys or keys <= visited_storage:
            return False
        scope = self._lexical_scope(use)
        if isinstance(scope, ast.Lambda):
            return False
        bindings: list[_NameBinding] = []
        pending = list(reversed(scope.body))
        while pending:
            candidate = pending.pop()
            if self._position(candidate) >= self._position(use):
                continue
            if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                targets = candidate.targets if isinstance(candidate, ast.Assign) else (candidate.target,)
                if any(keys & self._storage_target_keys(target, use=candidate) for target in targets):
                    bindings.append(_NameBinding(candidate, value=candidate.value))
            pending.extend(reversed(list(ast.iter_child_nodes(candidate))))
        bindings = [binding for binding in bindings if not self._is_in_sibling_branch(binding, use)]
        unconditional = [binding for binding in bindings if self._conditional_guard_between(binding.node, use) is None]
        if unconditional:
            base = max(unconditional, key=lambda binding: self._position(binding.node))
            bindings = [
                base,
                *(
                    binding
                    for binding in bindings
                    if self._position(binding.node) > self._position(base.node)
                    and self._conditional_guard_between(binding.node, use) is not None
                ),
            ]
        bindings.sort(key=lambda binding: self._position(binding.node))
        next_visited_storage = visited_storage | keys
        return any(
            binding.value is not None
            and self._is_connection_callable_expression(
                binding.node,
                binding.value,
                visited=frozenset(),
                visited_storage=next_visited_storage,
            )
            for binding in bindings
        )

    @staticmethod
    def _assigned_names(node: ast.AST) -> set[str]:
        current: ast.AST | None = node
        while current is not None and not isinstance(current, ast.stmt):
            current = getattr(current, "_inventory_parent", None)
        if isinstance(current, (ast.Assign, ast.AnnAssign)):
            targets = current.targets if isinstance(current, ast.Assign) else [current.target]
            return {
                child.id
                for target in targets
                for child in ast.walk(target)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
            }
        return set()

    def _expression_reaches_sources(
        self,
        expression: ast.AST | None,
        source_values: set[int],
    ) -> bool:
        if expression is None:
            return False
        for reference in ast.walk(expression):
            if not isinstance(reference, ast.Name) or not isinstance(reference.ctx, ast.Load):
                continue
            reaching, complete, _ = self._visible_reaching_bindings(reference, reference.id)
            if complete and any(binding.value is not None and id(binding.value) in source_values for binding in reaching):
                return True
        return False

    def _dependent_write_context(
        self,
        node: ast.AST,
    ) -> tuple[list[ast.stmt], list[ast.Call]]:
        """Trace prebuilt statements through assignment chains to their executions."""

        cached = self._dependent_write_context_cache.get(id(node))
        if cached is not None:
            return cached

        base_statement = self._outer_statement(node)
        assigned_names = self._assigned_names(node)
        scope: ast.AST = self._enclosing_function(node) or self.tree
        if not assigned_names:
            result = ([base_statement], [])
            self._dependent_write_context_cache[id(node)] = result
            return result

        source_values = {id(node)}
        if isinstance(base_statement, (ast.Assign, ast.AnnAssign)) and base_statement.value is not None:
            source_values.add(id(base_statement.value))
        dependent_statements: dict[int, ast.stmt] = {id(base_statement): base_statement}
        assignments = sorted(
            (
                candidate
                for candidate in ast.walk(scope)
                if isinstance(candidate, (ast.Assign, ast.AnnAssign)) and self._position(candidate) > self._position(base_statement)
            ),
            key=self._position,
        )
        changed = True
        while changed:
            changed = False
            for assignment in assignments:
                if id(assignment) in dependent_statements:
                    continue
                if self._expression_reaches_sources(assignment.value, source_values):
                    dependent_statements[id(assignment)] = assignment
                    source_values.add(id(assignment.value))
                    changed = True

        executions: dict[int, ast.Call] = {}
        for candidate in ast.walk(scope):
            if not (
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr in {"execute", "executemany", "exec_driver_sql"}
            ):
                continue
            if any(self._expression_reaches_sources(argument, source_values) for argument in candidate.args):
                executions[id(candidate)] = candidate
        result = (
            sorted(dependent_statements.values(), key=self._position),
            sorted(executions.values(), key=self._position),
        )
        self._dependent_write_context_cache[id(node)] = result
        return result

    def _writer_context_fingerprint(self, node: ast.AST) -> str:
        statements, executions = self._dependent_write_context(node)
        contexts: dict[int, ast.stmt] = {id(statement): statement for statement in statements}
        for execution in executions:
            statement = self._outer_statement(execution)
            contexts[id(statement)] = statement
        ordered = sorted(contexts.values(), key=self._position)
        if len(ordered) == 1:
            return _statement_fingerprint(node)
        normalized = "\0".join(stable_ast_dump(statement) for statement in ordered)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _write_executions_for(self, node: ast.AST) -> list[ast.Call]:
        """Return execute calls that consume this DML construction/string."""

        current: ast.AST | None = node
        while current is not None and not isinstance(current, ast.stmt):
            if (
                isinstance(current, ast.Call)
                and isinstance(current.func, ast.Attribute)
                and current.func.attr in {"execute", "executemany", "exec_driver_sql"}
            ):
                return [current]
            current = getattr(current, "_inventory_parent", None)

        _, executions = self._dependent_write_context(node)
        return executions

    def _connection_acquisitions_for(self, execution: ast.Call) -> list[ast.Call]:
        return self._connection_acquisitions_for_expression(execution, execution.func.value)

    def _connection_acquisitions_for_name(
        self,
        use: ast.AST,
        name: str,
        *,
        visited: frozenset[str] = frozenset(),
    ) -> list[ast.Call]:
        if name in visited:
            return []
        next_visited = visited | {name}
        # ``with engine.begin() as conn: conn.execute(write)``.
        current: ast.AST | None = use
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                for item in current.items:
                    if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == name:
                        acquisitions = self._connection_acquisitions_for_expression(
                            item.context_expr,
                            item.context_expr,
                            visited=next_visited,
                        )
                        if acquisitions:
                            return acquisitions
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            current = getattr(current, "_inventory_parent", None)

        # ``conn = engine.connect()`` and simple aliases in one lexical scope --
        # and a ``with ... as conn`` whose block has already ENDED: the name
        # stays bound afterwards, so a use after the block (return, yield,
        # attribute store, closure capture in an enclosing scope) reaches the
        # same acquisition. A with-binding carries no ``value``; its context
        # expression is the acquisition (comment 9521 on elspeth-e483fe7f85).
        reaching, _, _ = self._visible_reaching_bindings(use, name)
        acquisitions: dict[int, ast.Call] = {}
        for binding in reaching:
            bound = binding.value if binding.value is not None else self._with_binding_context(binding, name)
            for acquisition in self._connection_acquisitions_for_expression(
                binding.node,
                bound,
                visited=next_visited,
            ):
                acquisitions[id(acquisition)] = acquisition
        return list(acquisitions.values())

    def _is_obviously_read_only_statement(
        self,
        expression: ast.expr | None,
        *,
        use: ast.AST,
        visited: frozenset[tuple[int, str, int]] = frozenset(),
    ) -> bool:
        if isinstance(expression, ast.Name):
            key = (id(self._lexical_scope(use)), expression.id, id(use))
            if key in visited:
                return False
            # The name is looked up exactly as the domain resolver looks it
            # up: this scope, then each enclosing function, then the module
            # (class namespaces skipped), every module-level rebinding
            # counted.  A prebuilt ``_ROWS = select(...)`` module constant
            # executed from a method is a read; before elspeth-a85fb1555b
            # the lookup stopped at the method and the statement rode on its
            # acquisition row, so the gap never showed.
            reaching, complete, scope = self._visible_reaching_bindings(use, expression.id)
            if scope is None or not complete or not reaching or any(binding.value is None for binding in reaching):
                return False
            next_visited = visited | {key}
            return all(
                self._is_obviously_read_only_statement(
                    binding.value,
                    use=binding.node,
                    visited=next_visited,
                )
                for binding in reaching
            )
        if isinstance(expression, ast.Call):
            func = expression.func
            qualified = self._imported_qualified_name(func)
            if qualified and qualified.startswith("sqlalchemy.") and qualified.endswith(".select"):
                return True
            if qualified and qualified.startswith("sqlalchemy.") and qualified.endswith(".text"):
                if expression.args and isinstance(expression.args[0], ast.Constant) and isinstance(expression.args[0].value, str):
                    return _raw_sql_is_obviously_read_only(expression.args[0].value)
                return False
            if isinstance(func, ast.Attribute) and func.attr in _TRANSPARENT_SQLALCHEMY_STATEMENT_METHODS:
                return self._is_obviously_read_only_statement(func.value, use=use, visited=visited)
            return self._private_helper_returns_only_read_only_statements(expression, visited=visited)
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return _raw_sql_is_obviously_read_only(expression.value)
        if isinstance(expression, ast.Attribute):
            texts = self._self_attribute_module_constant_texts(expression, use=use)
            return texts is not None and all(_raw_sql_is_obviously_read_only(text) for text in texts)
        return False

    def _private_helper_returns_only_read_only_statements(
        self,
        call: ast.Call,
        *,
        visited: frozenset[tuple[int, str, int]],
    ) -> bool:
        """True when ``call`` resolves to one same-module private function or same-class method whose EVERY return is a read.

        ``conn.execute(_select_rows_for(user_id))``: the statement executed is
        whatever the helper returns, so the helper's returns are the statement.
        Admitted only when the callee is exactly one inspectable definition
        (``_resolvable_private_callee``), is a plain function (no yields, no
        context manager), returns a value on every ``return`` and each of
        those values is itself an obviously read-only statement.  Side effects
        inside the helper are its own rows; only the returned statement is
        judged here.  Anything wider -- a public or imported callee, a bare
        ``return``, a generator -- is refused and the statement stays opaque.
        """

        definition = self._resolvable_private_callee(call)
        if definition is None or self._is_contextmanager_definition(definition) or self._direct_yields(definition):
            return False
        key = (id(definition), "<returns>", id(call))
        if key in visited:
            return False
        returns = self._direct_returns(definition)
        if not returns or any(statement.value is None for statement in returns):
            return False
        next_visited = visited | {key}
        return all(self._is_obviously_read_only_statement(statement.value, use=statement, visited=next_visited) for statement in returns)

    def _unrecognised_literal_statement(self, expression: ast.expr | None, *, use: ast.AST) -> bool:
        """True when every text ``expression`` can hold is readable here and none is a recognised read, lock or raw DML.

        Raw DML literals are already emitted by ``visit_Constant`` as their
        own writer rows and are not counted twice.
        """

        texts = self._literal_statement_texts(expression, use=use)
        if texts is None:
            return False
        if all(_raw_sql_is_obviously_read_only(text) for text in texts):
            return False
        if isinstance(expression, ast.Attribute):
            # A text held in a module dict behind ``self.<attr>`` never passes
            # through ``visit_Constant``: a DML there has no row of its own,
            # so it is unresolved here rather than trusted to another pass.
            return True
        return not all(_raw_sql_is_obviously_read_only(text) or _RAW_WRITE.search(text) for text in texts)

    def _literal_statement_texts(
        self,
        expression: ast.expr | None,
        *,
        use: ast.AST,
        visited: frozenset[tuple[int, str, int]] = frozenset(),
    ) -> list[str] | None:
        """Every string a statement expression can evaluate to, or ``None`` when any path is unreadable."""

        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return [expression.value]
        if isinstance(expression, ast.Call):
            qualified = self._imported_qualified_name(expression.func)
            if qualified and qualified.startswith("sqlalchemy.") and qualified.endswith(".text") and len(expression.args) == 1:
                return self._literal_statement_texts(expression.args[0], use=use, visited=visited)
            return None
        if isinstance(expression, ast.Attribute):
            return self._self_attribute_module_constant_texts(expression, use=use)
        if isinstance(expression, ast.Name):
            key = (id(self._lexical_scope(use)), expression.id, id(use))
            if key in visited:
                return None
            reaching, complete, _ = self._potentially_reaching_bindings(use, expression.id)
            if not complete or not reaching or any(binding.value is None for binding in reaching):
                return None
            texts: list[str] = []
            for binding in reaching:
                bound = self._literal_statement_texts(binding.value, use=binding.node, visited=visited | {key})
                if bound is None:
                    return None
                texts.extend(bound)
            return texts
        return None

    def _self_attribute_module_constant_texts(self, attribute: ast.Attribute, *, use: ast.AST) -> list[str] | None:
        """The string constants ``self.<attr>`` can hold, when that is provable from this module alone.

        Admitted shape, and nothing wider: ``use`` sits in an instance method;
        the enclosing class assigns the attribute EXACTLY once anywhere in its
        body, as ``self.<attr> = NAME[...]``; ``NAME`` is bound exactly once at
        module level to a dict display whose every value is a string literal.
        The dialect-keyed clock SQL of the Sessions authorities is this shape.
        Anything else -- an import, a second assignment, a ``.get``, a computed
        value, a non-literal entry -- returns ``None`` and the statement stays
        unresolved.  This is the only way an attribute-held statement becomes
        visible to the manifest; before it, a DELETE behind ``self._clock_sql``
        was invisible (P4-D6, measured 2026-09-05).
        """

        owner = self._enclosing_function(use)
        if owner is None or not self._is_instance_method(owner):
            return None
        positional = (*owner.args.posonlyargs, *owner.args.args)
        if not positional or not (isinstance(attribute.value, ast.Name) and attribute.value.id == positional[0].arg):
            return None
        owner_class = self.method_owners[id(owner)]
        assignments: list[ast.expr | None] = []
        for method in owner_class.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_positional = (*method.args.posonlyargs, *method.args.args)
            self_name = method_positional[0].arg if method_positional and self._is_instance_method(method) else None
            for node in ast.walk(method):
                targets: list[ast.expr]
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                    value: ast.expr | None = node.value
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                    value = node.value
                elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                    targets = [node.target]
                    value = None
                else:
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == attribute.attr
                        and isinstance(target.value, ast.Name)
                        and target.value.id == self_name
                    ):
                        assignments.append(value)
        if len(assignments) != 1 or assignments[0] is None:
            return None
        bound = assignments[0]
        if not (isinstance(bound, ast.Subscript) and isinstance(bound.value, ast.Name)):
            return None
        module_bindings = [
            node.value
            for node in self.tree.body
            if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == bound.value.id for t in node.targets))
            or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == bound.value.id)
        ]
        if len(module_bindings) != 1 or not isinstance(module_bindings[0], ast.Dict):
            return None
        texts: list[str] = []
        for value in module_bindings[0].values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None
            texts.append(value.value)
        return texts if texts else None

    def _statement_database_evidence(
        self,
        expression: ast.expr | None,
        *,
        use: ast.AST | None = None,
        visited: frozenset[tuple[int, str]] = frozenset(),
    ) -> tuple[DatabaseDomain, bool]:
        """Return the statement domain and whether opaque syntax forces an unresolved site."""

        if expression is None:
            return "unknown", False
        use = use or expression
        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self._statement_database_evidence(
                expression.value,
                use=use,
                visited=visited,
            )
        if isinstance(expression, ast.IfExp):
            branches = [
                self._statement_database_evidence(branch, use=use, visited=visited) for branch in (expression.body, expression.orelse)
            ]
            domain = self._merge_database_domains(branch_domain for branch_domain, _ in branches)
            return domain, domain == "unknown" or any(force_unresolved for _, force_unresolved in branches)
        if isinstance(expression, ast.BoolOp):
            values = [self._statement_database_evidence(value, use=use, visited=visited) for value in expression.values]
            domain = self._merge_database_domains(value_domain for value_domain, _ in values)
            return domain, domain == "unknown" or any(force_unresolved for _, force_unresolved in values)
        if isinstance(expression, ast.Name):
            reaching, complete, scope = self._visible_reaching_bindings(use, expression.id)
            if scope is None:
                return "unknown", False
            key = (id(scope), expression.id)
            if key in visited:
                return "unknown", False

            if scope is not self._lexical_scope(use):
                late_bindings = [
                    binding for binding in self.assignment_bindings.get(key, ()) if self._position(binding.node) > self._position(use)
                ]
                reaching = [*reaching, *(binding for binding in late_bindings if binding not in reaching)]

            evidence = [
                self._statement_database_evidence(
                    binding.value,
                    use=binding.node,
                    visited=visited | {key},
                )
                if binding.value is not None
                else ("unknown", False)
                for binding in reaching
            ]
            if not complete:
                evidence.append(("unknown", False))
            domain = self._merge_database_domains(binding_domain for binding_domain, _ in evidence)
            return domain, any(force_unresolved for _, force_unresolved in evidence)
        if isinstance(expression, ast.Call):
            provenance = self._dml_callable_provenance(expression.func)
            if provenance is not None:
                domain, _, _ = provenance
                if domain == "unknown" and expression.args:
                    domain = self._table_database_domain(expression.args[0])
                return domain, False

            qualified = self._imported_qualified_name(expression.func)
            if qualified is not None and qualified.startswith("sqlalchemy.") and qualified.endswith(".select"):
                domains = [
                    domain
                    for argument in expression.args
                    for candidate in ast.walk(argument)
                    if isinstance(candidate, (ast.Name, ast.Attribute))
                    if (domain := self._table_database_domain(candidate)) != "unknown"
                ]
                domain = self._merge_database_domains(domains)
                return domain, False

            if isinstance(expression.func, ast.Attribute) and expression.func.attr in _TRANSPARENT_SQLALCHEMY_STATEMENT_METHODS:
                return self._statement_database_evidence(
                    expression.func.value,
                    use=use,
                    visited=visited,
                )
            nested_expressions: list[ast.expr] = [
                *expression.args,
                *(keyword.value for keyword in expression.keywords),
            ]
            if isinstance(expression.func, ast.Attribute):
                nested_expressions.append(expression.func.value)
            nested_evidence = [
                self._statement_database_evidence(
                    nested,
                    use=use,
                    visited=visited,
                )
                for nested in nested_expressions
            ]
            return "unknown", any(nested_domain != "unknown" or force_unresolved for nested_domain, force_unresolved in nested_evidence)

        if isinstance(expression, ast.Attribute):
            domain = self._table_database_domain(expression)
            return domain, False
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return "unknown", False
        return "unknown", True

    def _statement_database_domain(self, expression: ast.expr | None) -> DatabaseDomain:
        return self._statement_database_evidence(expression)[0]

    def _connection_origin_database_domain(self, acquisition: ast.Call) -> DatabaseDomain:
        wrapped = self.wrapper_calls.get(id(acquisition))
        if wrapped is not None:
            return self._wrapper_call_origin_domain(acquisition, *wrapped)
        factory = self.factory_return_calls.get(id(acquisition))
        if factory is not None:
            return self._merge_database_domains(self._connection_origin_database_domain(inner) for inner in factory[1])
        # The generic SQLAlchemy factory inside the canonical Sessions engine
        # factory mints a Sessions engine.  Outside that exact lexical scope,
        # endpoint-agnostic factories remain unknown.
        symbol = _symbol(acquisition)
        if self.path == "src/elspeth/web/sessions/engine.py" and (
            symbol == "create_session_engine" or symbol.startswith("create_session_engine.")
        ):
            return "sessions"
        called_domain = self._qualified_database_domain(self._imported_qualified_name(acquisition.func))
        if called_domain is not None:
            return called_domain
        if isinstance(acquisition.func, ast.Attribute):
            return self._expression_database_domain(
                acquisition.func.value,
                use=acquisition,
            )
        return self._expression_database_domain(acquisition, use=acquisition)

    def _execution_connection_database_domain(self, execution: ast.Call) -> DatabaseDomain:
        acquisitions = self._connection_acquisitions_for(execution)
        if acquisitions:
            return self._merge_database_domains(self._connection_database_domain(acquisition) for acquisition in acquisitions)
        if isinstance(execution.func, ast.Attribute):
            return self._expression_database_domain(execution.func.value, use=execution)
        return "unknown"

    def _connection_database_domain(self, acquisition: ast.Call) -> DatabaseDomain:
        origin = self._connection_origin_database_domain(acquisition)
        if origin == "unknown":
            return "unknown"
        context = self._enclosing_function(acquisition) or self.tree
        statement_domains: list[DatabaseDomain] = []
        for candidate in ast.walk(context):
            if not (
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr in {"execute", "executemany", "exec_driver_sql"}
            ):
                continue
            if any(id(linked) == id(acquisition) for linked in self._connection_acquisitions_for(candidate)):
                statement = candidate.args[0] if candidate.args else None
                statement_domain = self._statement_database_domain(statement)
                if statement_domain == "unknown" and self._is_obviously_read_only_statement(statement, use=candidate):
                    continue
                # Raw SQL that names no Sessions table cannot contradict a
                # proven non-Sessions origin; raw SQL naming one still does.
                if (
                    statement_domain == "unknown"
                    and origin == "non_sessions"
                    and self._raw_sql_names_no_sessions_table(statement, use=candidate)
                ):
                    continue
                statement_domains.append(statement_domain)
        statement_domain = self._merge_database_domains(statement_domains) if statement_domains else None
        if statement_domain is None or statement_domain == origin:
            return origin
        return "unknown"

    @staticmethod
    def _target_stores_connection_externally(target: ast.expr) -> bool:
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            return True
        if isinstance(target, ast.Starred):
            return _ProductionWriterCollector._target_stores_connection_externally(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(_ProductionWriterCollector._target_stores_connection_externally(child) for child in target.elts)
        return False

    # -- P4-D6 step 5: forwarding proof (hub ruling, six conditions) ----------

    def _resolvable_private_callee(self, call: ast.Call) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """The callee when it is exactly one same-class method (``self.m(...)``) or one same-module
        private function (``_f(...)``); ``None`` for anything dispatched through another object,
        imported, public at module level, or ambiguous (condition 1)."""

        definition = self._local_callable_definition(call)
        if definition is None:
            return None
        if isinstance(call.func, ast.Attribute):
            return definition if id(definition) in self.method_owners else None
        parent = getattr(definition, "_inventory_parent", None)
        if not isinstance(parent, ast.Module) or not definition.name.startswith("_"):
            return None
        return definition

    def _resolvable_imported_callee(
        self,
        call: ast.Call,
    ) -> tuple[_ProductionWriterCollector, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        """The callee behind a plain ``from elspeth.<module> import f`` binding, located in the peer
        collector that scanned that module: every reaching binding of the name must be that one
        import (no alias, no reassignment), the module must be under ``src/elspeth`` and among the
        scanned peers, and it must define exactly one module-level function of that name."""

        func = call.func
        if not isinstance(func, ast.Name):
            return None
        qualified = self._imported_qualified_name(func)
        if qualified is None or not qualified.startswith("elspeth."):
            return None
        module, _, name = qualified.rpartition(".")
        if name != func.id:
            return None
        reaching, complete, _ = self._visible_reaching_bindings(call, func.id)
        if not complete or not reaching or any(binding.imported != qualified or binding.value is not None for binding in reaching):
            return None
        peer = self._peer_collector(f"src/{module.replace('.', '/')}.py")
        if peer is None:
            return None
        definitions = [
            node
            for node in ast.iter_child_nodes(peer.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]
        if len(definitions) != 1 or any(isinstance(node, ast.ClassDef) and node.name == name for node in ast.iter_child_nodes(peer.tree)):
            return None
        return peer, definitions[0]

    def _peer_collector(self, path: str) -> _ProductionWriterCollector | None:
        """The collector for ``path``: a scanned peer, else the module parsed from disk under the anchor."""

        peer = self.peers.get(path)
        if peer is not None:
            return peer
        if self.anchor is None:
            return None
        source_file = self.anchor / path
        if not source_file.is_file():
            return None
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        except (SyntaxError, UnicodeDecodeError):
            return None
        _attach_parents(tree)
        peer = _ProductionWriterCollector(path, tree, anchor=self.anchor)
        peer.peers = self.peers
        self.peers[path] = peer
        return peer

    def _is_static_method(self, definition: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(
            (isinstance(decorator, ast.Name) and decorator.id == "staticmethod")
            or self._imported_qualified_name(decorator) == "builtins.staticmethod"
            for decorator in definition.decorator_list
        )

    def _forwarded_parameter(
        self,
        call: ast.Call,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        argument: ast.expr,
    ) -> str | None:
        """The callee parameter that receives ``argument`` at ``call``, or ``None`` unless exactly knowable."""

        if any(isinstance(item, ast.Starred) for item in call.args) or any(keyword.arg is None for keyword in call.keywords):
            return None
        positional = [*definition.args.posonlyargs, *definition.args.args]
        if id(definition) in self.method_owners:
            if not isinstance(call.func, ast.Attribute):
                return None
            if not self._is_static_method(definition):
                if not positional:
                    return None
                positional = positional[1:]
        for index, item in enumerate(call.args):
            if item is argument:
                return positional[index].arg if index < len(positional) else None
        for keyword in call.keywords:
            if keyword.value is argument:
                names = {parameter.arg for parameter in (*positional, *definition.args.kwonlyargs)}
                return keyword.arg if keyword.arg in names else None
        return None

    @classmethod
    def _scope_nodes(cls, scope: ast.AST) -> Iterator[ast.AST]:
        """Every node of ``scope`` without descending into a nested scope (the nested scope itself is yielded)."""

        for child in ast.iter_child_nodes(scope):
            yield child
            if isinstance(child, _NESTED_SCOPES):
                continue
            yield from cls._scope_nodes(child)

    def _connection_uses_are_contained(
        self,
        scope: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
        allow_yield: bool = False,
        strict: bool = False,
    ) -> bool:
        """Every use of connection ``name`` inside ``scope`` keeps the capability there.

        Permitted: the name is executed on (``_EXECUTE_RECEIVER_METHODS``), its
        ``dialect`` is read, it opens an anonymous ``with name.begin_nested():``,
        is ``del``-ed, or is forwarded to a resolvable callee whose own uses are
        contained (recursively, ``_FORWARDING_MAX_DEPTH``, cycle-refusing) -- a
        same-class method, a same-module private function, or a module-level
        function behind a plain ``from elspeth.<module> import f`` inspected in
        its own module. Past a module boundary the walk is ``strict``: every
        execution on the forwarded connection must be a provably read-only or
        advisory-lock statement, so an imported callee can never carry table
        DML. ``allow_yield`` admits the one ``yield name`` a contextmanager
        wrapper is. Anything else -- a store, return, comparison, closure or
        comprehension capture, star-argument, reassignment, raw DBAPI access, a
        bound nested transaction, an aliased import, a callee outside
        ``src/elspeth`` or an unresolvable forward -- refuses.
        """

        if depth > _FORWARDING_MAX_DEPTH or self._name_reassigned_in(scope, name):
            return False
        for node in self._scope_nodes(scope):
            if isinstance(node, _NESTED_SCOPES):
                if any(isinstance(inner, ast.Name) and inner.id == name for inner in ast.walk(node)):
                    return False
                continue
            if not (isinstance(node, ast.Name) and node.id == name):
                continue
            if not self._connection_use_is_contained(node, depth=depth, active=active, allow_yield=allow_yield, strict=strict):
                return False
        return True

    def _connection_use_is_contained(
        self,
        use: ast.Name,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
        allow_yield: bool,
        strict: bool,
    ) -> bool:
        parent = getattr(use, "_inventory_parent", None)
        if isinstance(use.ctx, ast.Del):
            # ``del conn`` unbinds the local name; nothing receives the capability.
            return True
        if not isinstance(use.ctx, ast.Load):
            # The with-binding that introduces the name is the one permitted store.
            return isinstance(parent, ast.withitem) and parent.optional_vars is use
        if isinstance(parent, ast.Attribute) and parent.value is use:
            grandparent = getattr(parent, "_inventory_parent", None)
            if parent.attr == "dialect":
                return True
            if isinstance(grandparent, ast.Call) and grandparent.func is parent:
                if parent.attr in _EXECUTE_RECEIVER_METHODS:
                    if not strict:
                        return True
                    # Past a module boundary only a provably read-only /
                    # advisory-lock statement may run on the forwarded connection.
                    statement = grandparent.args[0] if grandparent.args else None
                    return statement is not None and self._is_obviously_read_only_statement(statement, use=grandparent)
                if parent.attr in {"begin", "begin_nested"}:
                    item = getattr(grandparent, "_inventory_parent", None)
                    return isinstance(item, ast.withitem) and item.context_expr is grandparent and item.optional_vars is None
            return False
        if allow_yield and isinstance(parent, ast.Yield) and parent.value is use:
            return isinstance(getattr(parent, "_inventory_parent", None), ast.Expr)
        call = getattr(parent, "_inventory_parent", None) if isinstance(parent, ast.keyword) else parent
        if not isinstance(call, ast.Call) or call.func is use:
            return False
        if use not in call.args and not any(keyword.value is use for keyword in call.keywords):
            return False
        return self._forward_is_contained(call, use, depth=depth + 1, active=active, strict=strict)

    def _forward_is_contained(
        self,
        call: ast.Call,
        argument: ast.Name,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
        strict: bool,
    ) -> bool:
        """``callee(conn)`` keeps the connection contained iff the callee is inspectable and proves it.

        A same-file callee is inspected here; an imported one in its own module's
        collector, and from there on the walk is strict.
        """

        owner: _ProductionWriterCollector = self
        callee = self._resolvable_private_callee(call)
        if callee is None:
            imported = self._resolvable_imported_callee(call)
            if imported is None:
                return False
            owner, callee = imported
            strict = True
        parameter = owner._forwarded_parameter(call, callee, argument)
        if parameter is None:
            return False
        key = (id(callee), parameter)
        if key in active:
            return False
        return owner._connection_uses_are_contained(callee, parameter, depth=depth, active=active | {key}, strict=strict)

    def _forwarding_is_contained(self, call: ast.Call, argument: ast.Name) -> bool:
        return self._forward_is_contained(call, argument, depth=1, active=frozenset(), strict=False)

    def _collect_unresolved_connection_flows(self) -> None:
        """Fail closed when an acquired connection reaches an unknown write-capable sink."""

        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
                for acquisition in self._connection_acquisitions_for_expression(node, node.value):
                    self._record_connection(acquisition, escapes=True)
                continue

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(self._target_stores_connection_externally(target) for target in targets):
                    for acquisition in self._connection_acquisitions_for_expression(node, node.value):
                        self._record_connection(acquisition, escapes=True)
                continue

            if isinstance(node, ast.AugAssign):
                for acquisition in self._connection_acquisitions_for_expression(node, node.value):
                    self._record_connection(acquisition, escapes=True)
                continue

            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is None or not self._target_stores_connection_externally(item.optional_vars):
                        continue
                    for acquisition in self._connection_acquisitions_for_expression(item.context_expr, item.context_expr):
                        self._record_connection(acquisition, escapes=True)
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [*node.args.defaults, *(default for default in node.args.kw_defaults if default is not None)]
                for default in defaults:
                    for acquisition in self._connection_acquisitions_for_expression(node, default):
                        self._record_connection(acquisition, escapes=True)
                continue

            if not isinstance(node, ast.Call):
                continue
            call = node
            func = call.func
            if self._has_explicit_non_sql_execute_receiver(call):
                self.classified_execution_calls.add(id(call))
                continue
            for acquisition in self._connection_acquisitions_for_expression(call, call):
                stored_callable = isinstance(acquisition.func, (ast.Attribute, ast.Subscript)) and not (
                    isinstance(acquisition.func, ast.Attribute) and acquisition.func.attr in {"begin", "connect"}
                )
                transparent = id(acquisition) in self.wrapper_calls or id(acquisition) in self.factory_return_calls
                self._record_connection(acquisition, escapes=stored_callable and not transparent)
            if isinstance(func, ast.Attribute) and func.attr in {"execute", "executemany", "exec_driver_sql"}:
                acquisitions = self._connection_acquisitions_for(call)
                statement = call.args[0] if call.args else None
                if self._is_obviously_read_only_statement(
                    statement,
                    use=call,
                ):
                    continue
                if self._is_session_engine_configuration(call, statement):
                    self.classified_execution_calls.add(id(call))
                    continue
                statement_domain, force_unresolved = self._statement_database_evidence(statement)
                connection_domain = self._execution_connection_database_domain(call)
                if connection_domain == "non_sessions":
                    self.classified_execution_calls.add(id(call))
                    continue
                if (
                    self.declared_non_session_module
                    and connection_domain == "unknown"
                    and statement_domain != "sessions"
                    and self._execution_receiver_is_module_bound(call)
                    and not self._raw_sql_names_sessions_table(statement, use=call)
                ):
                    # Package premise: this module bound the connection itself
                    # and can hold no Sessions engine, so the execution is
                    # non-Sessions by construction (rule 1; see the constant).
                    self.classified_execution_calls.add(id(call))
                    for acquisition in acquisitions:
                        self._record_connection(acquisition, escapes=False)
                    continue
                if statement_domain == "non_sessions":
                    self._append_site(
                        call,
                        "<unresolved-session-write>",
                        f"unknown_{func.attr}",
                    )
                    received = self._receiver_parameter(call)
                    if received is not None and not acquisitions:
                        self.parameter_received.append((len(self.sites) - 1, *received))
                    for acquisition in acquisitions:
                        self._record_connection(acquisition, escapes=False)
                    continue
                # A wrapper whose yield arms do not all prove one domain gives
                # its callers nothing to inherit: an unknown statement on it is
                # unresolved, never silently classified (Q7 ruling, per yield).
                mixed_wrapper = any(
                    id(acquisition) in self.wrapper_calls and self._wrapper_arms_disagree(acquisition) for acquisition in acquisitions
                )
                # A statement whose TEXT the scanner can read but no recogniser
                # admits is an unresolved write whether or not the connection
                # was acquired in scope.  Until P4-D6 an acquisition in scope
                # swallowed it: only the acquisition row was recorded, so a
                # TRUNCATE, an unknown PRAGMA or a loosened LOCK inside an
                # admitted authority's block moved the acquisition's
                # fingerprint and nothing else, and a mechanical re-pin
                # admitted it unread.
                literal_unknown = self._unrecognised_literal_statement(statement, use=call)
                if statement_domain == "unknown" and (
                    force_unresolved
                    or mixed_wrapper
                    or literal_unknown
                    or (not acquisitions and id(call) not in self.classified_execution_calls)
                ):
                    self._append_site(
                        call,
                        "<unresolved-session-write>",
                        f"unknown_{func.attr}",
                    )
                    received = self._receiver_parameter(call)
                    if (
                        received is not None
                        and not acquisitions
                        and not force_unresolved
                        and not self._raw_sql_names_sessions_table(statement, use=call)
                    ):
                        self.parameter_received.append((len(self.sites) - 1, *received))
                elif acquisitions and id(call) not in self.classified_execution_calls:
                    # Every statement executed inside an acquired block is its
                    # own row (elspeth-a85fb1555b).  One with NO readable text
                    # (a helper's return, a parameter, branch-built SQL) used
                    # to ride on the acquisition row alone, so a re-pin of
                    # that row admitted whatever the helper now returns
                    # unread.  Its only evidence is the code around it, so it
                    # is fingerprinted exactly as the acquisition is: the
                    # statement plus its enclosing function.
                    self._append_site(
                        call,
                        "<unresolved-session-write>",
                        "unknown_opaque",
                        fingerprint=self._connection_context_fingerprint(call),
                    )
                for acquisition in acquisitions:
                    self._record_connection(acquisition, escapes=False)
                continue

            # ``with engine.begin() as conn: imported_or_local_helper(conn)``.
            # A helper can hide a prebuilt or dynamically generated write, so
            # forwarding a raw connection is write-capable until a typed
            # authority removes that path -- unless the callee can be INSPECTED
            # and every use inside it is proven contained (P4-D6 step 5); a
            # callee the scanner cannot resolve keeps the escape.
            for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
                acquisitions = self._connection_acquisitions_for_expression(call, argument)
                if not acquisitions:
                    continue
                contained = isinstance(argument, ast.Name) and self._forwarding_is_contained(call, argument)
                for acquisition in acquisitions:
                    self._record_connection(acquisition, escapes=not contained)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        provenance = self._dml_callable_provenance(func)
        domain: DatabaseDomain = "unknown"
        table: str | None = None
        operation: str | None = None
        if provenance is not None:
            domain, table, operation = provenance
        if table is None and operation is not None and node.args:
            table = self._table(node.args[0])
            domain = self._table_database_domain(node.args[0])
        if domain == "sessions" and table and operation:
            statements, _ = self._dependent_write_context(node)
            if operation == "insert" and any("on_conflict_do_" in stable_ast_dump(statement) for statement in statements):
                operation = "upsert"
            self._emit(node, table, operation)
        elif domain == "non_sessions" and operation:
            self._classify_non_session_execution(node)
        self.generic_visit(node)

    @staticmethod
    def _outer_statement(node: ast.AST) -> ast.AST:
        current = node
        while not isinstance(current, ast.stmt):
            parent = getattr(current, "_inventory_parent", None)
            if parent is None:
                return current
            current = parent
        return current

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        matches = list(_RAW_WRITE.finditer(node.value))
        if not matches:
            return
        executions = self._write_executions_for(node)
        if not executions:
            return
        if all(self._execution_connection_database_domain(execution) == "non_sessions" for execution in executions):
            self.classified_execution_calls.update(id(execution) for execution in executions)
            return
        for match in matches:
            operation = match.group("operation").lower().replace(" ", "_")
            self._emit(node, match.group("table").lower(), f"raw_{operation}")

    def collect(self) -> list[WriterIdentity]:
        self.visit(self.tree)
        self._collect_unresolved_connection_flows()
        absorbed = self._absorbed_wrapper_acquisitions()
        for call, escapes in self.write_connection_calls.values():
            if id(call) in absorbed:
                continue
            table = "<sessions-write-connection>"
            if self._connection_database_domain(call) == "non_sessions":
                table = "<non-session-write-connection>"
            self._append_site(
                call,
                table,
                "write_connection",
                fingerprint=self._connection_context_fingerprint(call),
                connection_escape=escapes,
            )
        counters: Counter[tuple[str, str, str, str, str]] = Counter()
        result: list[WriterIdentity] = []
        for site in self.sites:
            key = (site.path, site.symbol, site.table, site.operation, site.fingerprint)
            counters[key] += 1
            result.append(replace(site, ordinal=counters[key]))
        return result


def scan_production_writers(files: Iterable[Path], *, anchor: Path) -> list[WriterIdentity]:
    collected: list[tuple[_ProductionWriterCollector, list[WriterIdentity]]] = []
    for source_file in sorted(files):
        relative_path = source_file.resolve().relative_to(anchor.resolve())
        if "node_modules" in relative_path.parts:
            continue
        try:
            source = source_file.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise InventoryScanError(f"cannot decode production source {source_file}: {error}") from error
        try:
            tree = ast.parse(source, filename=str(source_file))
        except SyntaxError as error:
            raise InventoryScanError(f"cannot parse production source {source_file}: {error}") from error
        _attach_parents(tree)
        relative = relative_path.as_posix()
        collected.append((_ProductionWriterCollector(relative, tree, anchor=anchor.resolve()), []))
    # Two phases: every collector exists before any collects, so a forwarded
    # connection's imported callee can be inspected in its own module (one it
    # was not scanned with is parsed from disk under the anchor on demand).
    peers = {collector.path: collector for collector, _ in collected}
    for collector, _ in collected:
        collector.peers = peers
    collected = [(collector, collector.collect()) for collector, _ in collected]
    proven = _CallerSideProof(collected).proven_site_indexes()
    contained = _WrapperContainmentProof(collected).contained_site_indexes()
    seams = _ProbeSeamProof(collected).seam_site_indexes()
    sites: list[WriterIdentity] = []
    for collector, collector_sites in collected:
        dropped = proven.get(id(collector), set()) | seams.get(id(collector), set())
        flipped = contained.get(id(collector), set())
        for index, site in enumerate(collector_sites):
            if index in dropped:
                continue
            sites.append(replace(site, connection_escape=False) if index in flipped else site)
    return sites


class _ProbeSeamProof:
    """Tree-wide admission of acceptance probe seams (P4-D6 family J).

    A class is a probe seam only when EVERY clause below holds; one miss
    keeps every site of the class in the inventory (fail closed):

    * defined in an ``_ACCEPTANCE_PROBE_MODULES`` module with exactly one
      base that resolves, through import provenance, to a
      ``_PROBE_SEAM_PROTOCOLS`` port;
    * its members are undecorated instance methods (a docstring aside);
    * ``__init__(self, engine: Engine)`` binds exactly one attribute: the
      engine itself, or a ``engine.connect()`` chain with constant-only
      keyword options (the autocommit session);
    * every other method executes only ``text(<own positional parameter>)``
      -- optionally followed by the method's own ``**parameters`` -- on the
      bound connection (or on a ``with self.<engine>.connect() as conn``
      target of that method), with ``text`` resolving to SQLAlchemy;
    * the bound attribute and any such ``conn`` reach nothing but those
      executions, that ``connect()``, and ``close()``; no method returns,
      forwards, yields or nests them.
    """

    _CONNECT_CHAIN = frozenset({"connect", "execution_options"})

    def __init__(self, collected: Sequence[tuple[_ProductionWriterCollector, list[WriterIdentity]]]) -> None:
        self._collected = collected

    def seam_site_indexes(self) -> dict[int, set[int]]:
        admitted: dict[int, set[int]] = {}
        for collector, sites in self._collected:
            if not collector.path.startswith(_ACCEPTANCE_PROBE_MODULES):
                continue
            seam_classes = [node for node in ast.walk(collector.tree) if isinstance(node, ast.ClassDef) and self._is_seam(collector, node)]
            if not seam_classes:
                continue
            for index in range(len(sites)):
                method = collector._enclosing_function(collector.site_nodes[index])
                owner = collector.method_owners.get(id(method)) if method is not None else None
                if owner is not None and any(owner is seam for seam in seam_classes):
                    admitted.setdefault(id(collector), set()).add(index)
        return admitted

    def _is_seam(self, collector: _ProductionWriterCollector, cls: ast.ClassDef) -> bool:
        if len(cls.bases) != 1 or cls.keywords or cls.decorator_list:
            return False
        if collector._imported_qualified_name(cls.bases[0]) not in _PROBE_SEAM_PROTOCOLS:
            return False
        methods: list[ast.FunctionDef] = []
        for index, member in enumerate(cls.body):
            if (
                index == 0
                and isinstance(member, ast.Expr)
                and isinstance(member.value, ast.Constant)
                and isinstance(member.value.value, str)
            ):
                continue
            if not isinstance(member, ast.FunctionDef) or member.decorator_list:
                return False
            methods.append(member)
        init = next((method for method in methods if method.name == "__init__"), None)
        if init is None:
            return False
        bound = self._bound_attribute(collector, cls, init)
        if bound is None:
            return False
        attribute, kind = bound
        return all(self._method_is_probe(collector, method, attribute, kind) for method in methods if method is not init)

    def _bound_attribute(self, collector: _ProductionWriterCollector, cls: ast.ClassDef, init: ast.FunctionDef) -> tuple[str, str] | None:
        """``(attribute, "engine" | "connection")`` for ``__init__(self, engine: Engine)`` binding exactly one attribute."""

        arguments = init.args
        if (
            arguments.posonlyargs
            or arguments.kwonlyargs
            or arguments.vararg
            or arguments.kwarg
            or len(arguments.args) != 2
            or arguments.defaults
        ):
            return None
        self_name, engine = arguments.args[0].arg, arguments.args[1]
        if collector._annotation_imported_qualified_name(engine.annotation, definition=init, scope=cls) not in _SQLALCHEMY_ENGINE_TYPES:
            return None
        body = [statement for statement in init.body if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))]
        if len(body) != 1 or not isinstance(body[0], ast.Assign) or len(body[0].targets) != 1:
            return None
        target, value = body[0].targets[0], body[0].value
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == self_name):
            return None
        if isinstance(value, ast.Name) and value.id == engine.arg:
            return target.attr, "engine"
        if self._is_connect_chain(value, engine.arg):
            return target.attr, "connection"
        return None

    def _is_connect_chain(self, value: ast.expr, engine_name: str) -> bool:
        """``engine.connect()`` optionally followed by ``.execution_options(<constant keywords>)``."""

        seen_connect = False
        current = value
        while isinstance(current, ast.Call):
            if not isinstance(current.func, ast.Attribute) or current.func.attr not in self._CONNECT_CHAIN:
                return False
            if current.args or any(keyword.arg is None or not isinstance(keyword.value, ast.Constant) for keyword in current.keywords):
                return False
            if current.func.attr == "connect":
                if current.keywords or seen_connect:
                    return False
                seen_connect = True
            current = current.func.value
        return seen_connect and isinstance(current, ast.Name) and current.id == engine_name

    def _method_is_probe(self, collector: _ProductionWriterCollector, method: ast.FunctionDef, attribute: str, kind: str) -> bool:
        arguments = method.args
        if arguments.posonlyargs or arguments.kwonlyargs or arguments.vararg or arguments.defaults or not arguments.args:
            return False
        self_name = arguments.args[0].arg
        statement_parameters = {argument.arg for argument in arguments.args[1:]}
        bind_parameter = arguments.kwarg.arg if arguments.kwarg is not None else None
        if method.name == "close":
            return self._is_close(method, self_name, attribute, kind)
        # The connections this method may execute on: the bound one, or the
        # targets of ``with self.<engine>.connect() as conn`` items.
        connection_names: set[str] = set()
        # The nodes through which the bound attribute / a with-bound
        # connection may be reached: the ``connect()`` receivers here and the
        # execution receivers below. Any other reach refuses the class.
        admitted_receivers: set[int] = set()
        for node in ast.walk(method):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Yield, ast.YieldFrom, ast.Await))
                and node is not method
            ):
                return False
            if isinstance(node, ast.With):
                for item in node.items:
                    if kind != "engine" or not isinstance(item.optional_vars, ast.Name):
                        return False
                    context = item.context_expr
                    if not (
                        isinstance(context, ast.Call)
                        and not context.args
                        and not context.keywords
                        and isinstance(context.func, ast.Attribute)
                        and context.func.attr == "connect"
                        and self._is_self_attribute(context.func.value, self_name, attribute)
                    ):
                        return False
                    connection_names.add(item.optional_vars.id)
                    admitted_receivers.add(id(context.func.value))
        if kind == "connection":
            receivers = [lambda node: self._is_self_attribute(node, self_name, attribute)]
        else:
            receivers = [lambda node: isinstance(node, ast.Name) and node.id in connection_names]
        # Executions on an admitted receiver; a zero-argument result accessor
        # chained on one (``execute(...).scalar()``) is the result, not a
        # second execution, even though ``scalar`` is an execute verb.
        executions: list[ast.Call] = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _EXECUTE_RECEIVER_METHODS
            and any(receiver(node.func.value) for receiver in receivers)
        ]
        if not executions:
            return False
        for node in ast.walk(method):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _EXECUTE_RECEIVER_METHODS):
                continue
            if node in executions:
                if not self._is_parameter_text_execution(collector, node, statement_parameters, bind_parameter):
                    return False
                admitted_receivers.add(id(node.func.value))
            elif node.args or node.keywords or node.func.value not in executions:
                return False
        # Every reach of the bound attribute / the with-bound connection IS
        # one of the admitted receiver nodes (by identity): nothing else --
        # no ``commit()``, no return, no forward.
        for node in ast.walk(method):
            reaches = self._is_self_attribute(node, self_name, attribute) or (
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in connection_names
            )
            if reaches and id(node) not in admitted_receivers:
                return False
        # ``self.<anything else>`` is a second binding the seam does not declare.
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == self_name
                and node.attr != attribute
            ):
                return False
        return True

    def _is_close(self, method: ast.FunctionDef, self_name: str, attribute: str, kind: str) -> bool:
        if kind != "connection" or len(method.args.args) != 1 or method.args.kwarg is not None:
            return False
        body = [
            statement for statement in method.body if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        ]
        if len(body) != 1 or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
            return False
        call = body[0].value
        return (
            not call.args
            and not call.keywords
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "close"
            and self._is_self_attribute(call.func.value, self_name, attribute)
        )

    @staticmethod
    def _is_self_attribute(node: ast.AST, self_name: str, attribute: str) -> bool:
        return (
            isinstance(node, ast.Attribute) and node.attr == attribute and isinstance(node.value, ast.Name) and node.value.id == self_name
        )

    def _is_parameter_text_execution(
        self,
        collector: _ProductionWriterCollector,
        execution: ast.Call,
        statement_parameters: set[str],
        bind_parameter: str | None,
    ) -> bool:
        """``<conn>.execute(text(<statement parameter>)[, <**parameters>])`` and nothing else."""

        if execution.keywords or not 1 <= len(execution.args) <= 2:
            return False
        statement = execution.args[0]
        if not (
            isinstance(statement, ast.Call)
            and len(statement.args) == 1
            and not statement.keywords
            and isinstance(statement.args[0], ast.Name)
            and statement.args[0].id in statement_parameters
            and collector._imported_qualified_name(statement.func) in _SQLALCHEMY_TEXT_CONSTRUCTORS
        ):
            return False
        if len(execution.args) == 2:
            binds = execution.args[1]
            if bind_parameter is None or not (isinstance(binds, ast.Name) and binds.id == bind_parameter):
                return False
        return True


class _WrapperContainmentProof:
    """Same-class, state-fed ``@contextmanager`` wrappers (P4-D6 step 5, condition 2).

    A wrapper that acquires from the enclosing instance's own state
    (``with self._engine.begin() as conn: yield conn``) reports its acquisition
    as an escape because it yields the connection. It is CONTAINED only when,
    tree-wide, every reference to the wrapper's name is a ``with self.<wrapper>(...)
    as target:`` from an instance method of the same class, the wrapper's own
    uses of the connection are contained (the yield being the one permitted),
    and the with-target never escapes in ANY caller by the forwarding rule --
    all call sites, never any; one unproven caller or one foreign reference
    keeps the wrapper row escaped.
    """

    def __init__(self, collected: Sequence[tuple[_ProductionWriterCollector, list[WriterIdentity]]]) -> None:
        self._collected = collected

    def contained_site_indexes(self) -> dict[int, set[int]]:
        contained: dict[int, set[int]] = {}
        for collector, sites in self._collected:
            for index, site in enumerate(sites):
                if site.operation != "write_connection" or not site.connection_escape:
                    continue
                if self._wrapper_is_contained(collector, collector.site_nodes[index]):
                    contained.setdefault(id(collector), set()).add(index)
        return contained

    @staticmethod
    def _self_parameter(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
        positional = (*definition.args.posonlyargs, *definition.args.args)
        return positional[0].arg if positional else None

    def _acquisition_rooted_in_self(self, wrapper: ast.FunctionDef | ast.AsyncFunctionDef, acquisition: ast.AST) -> bool:
        if not (isinstance(acquisition, ast.Call) and isinstance(acquisition.func, ast.Attribute)):
            return False
        if acquisition.func.attr not in {"begin", "connect"}:
            return False
        receiver: ast.expr = acquisition.func.value
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        return isinstance(receiver, ast.Name) and receiver.id == self._self_parameter(wrapper)

    def _wrapper_is_contained(self, collector: _ProductionWriterCollector, acquisition: ast.AST) -> bool:
        wrapper = collector._enclosing_function(acquisition)
        if wrapper is None or id(wrapper) not in collector.method_owners or not collector._is_instance_method(wrapper):
            return False
        item = getattr(acquisition, "_inventory_parent", None)
        if not (isinstance(item, ast.withitem) and item.context_expr is acquisition and isinstance(item.optional_vars, ast.Name)):
            return False
        if collector._enclosing_function(getattr(item, "_inventory_parent", None)) is not wrapper:
            return False
        if not self._acquisition_rooted_in_self(wrapper, acquisition):
            return False
        name = item.optional_vars.id
        yields = collector._direct_yields(wrapper)
        if not yields or any(
            not (isinstance(yielded, ast.Yield) and isinstance(yielded.value, ast.Name) and yielded.value.id == name) for yielded in yields
        ):
            return False
        if not collector._connection_uses_are_contained(wrapper, name, depth=0, active=frozenset(), allow_yield=True):
            return False
        owner_class = collector.method_owners[id(wrapper)]
        callers: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
        for other, _ in self._collected:
            for node in ast.walk(other.tree):
                if isinstance(node, ast.Name) and node.id == wrapper.name:
                    return False
                if not (isinstance(node, ast.Attribute) and node.attr == wrapper.name):
                    continue
                if other is not collector:
                    return False
                call = getattr(node, "_inventory_parent", None)
                if not (isinstance(call, ast.Call) and call.func is node):
                    return False
                with_item = getattr(call, "_inventory_parent", None)
                if not (
                    isinstance(with_item, ast.withitem) and with_item.context_expr is call and isinstance(with_item.optional_vars, ast.Name)
                ):
                    return False
                caller = collector._enclosing_function(call)
                if (
                    caller is None
                    or collector.method_owners.get(id(caller)) is not owner_class
                    or not collector._is_instance_method(caller)
                ):
                    return False
                if not (isinstance(node.value, ast.Name) and node.value.id == self._self_parameter(caller)):
                    return False
                callers.append((caller, with_item.optional_vars.id))
        if not callers:
            return False
        # The caller's own body is depth 0: its forwards count against the bound.
        return all(collector._connection_uses_are_contained(caller, target, depth=0, active=frozenset()) for caller, target in callers)


class _CallerSideProof:
    """Tree-wide proof for executions on a parameter-received connection (P4-D6 option (b)).

    A helper that executes on a ``conn`` it was handed is classified
    non-Sessions ONLY when at least one call site exists among the scanned
    units and EVERY call site that may target it (call sites are matched by
    callee name across all classes, an over-approximation) binds that
    parameter to an expression that proves non-Sessions: by domain, by the
    caller's own verified module premise, or through the caller's own
    parameter (recursively, bounded). No call site, a star argument, an
    unresolvable argument, a cycle, or one contrary call site refuses.
    """

    _MAX_DEPTH = 3

    def __init__(self, collected: Sequence[tuple[_ProductionWriterCollector, list[WriterIdentity]]]) -> None:
        self._collectors = [collector for collector, _ in collected]
        self._calls_by_name: dict[str, list[tuple[_ProductionWriterCollector, ast.Call]]] = {}
        for collector in self._collectors:
            for node in ast.walk(collector.tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    self._calls_by_name.setdefault(node.func.id, []).append((collector, node))
                elif isinstance(node.func, ast.Attribute):
                    self._calls_by_name.setdefault(node.func.attr, []).append((collector, node))
        self._memo: dict[tuple[int, str], bool] = {}

    def proven_site_indexes(self) -> dict[int, set[int]]:
        proven: dict[int, set[int]] = {}
        for collector in self._collectors:
            for index, definition, parameter in collector.parameter_received:
                if self._parameter_proves_non_session(collector, definition, parameter, depth=0, active=frozenset()):
                    proven.setdefault(id(collector), set()).add(index)
        return proven

    def _parameter_proves_non_session(
        self,
        collector: _ProductionWriterCollector,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        parameter: str,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
    ) -> bool:
        key = (id(definition), parameter)
        if key in self._memo:
            return self._memo[key]
        if key in active or depth > self._MAX_DEPTH:
            return False
        result = self._prove(collector, definition, parameter, depth=depth, active=active | {key})
        self._memo[key] = result
        return result

    def _prove(
        self,
        collector: _ProductionWriterCollector,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
        parameter: str,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
    ) -> bool:
        call_sites = 0
        for caller, call in self._calls_by_name.get(definition.name, ()):
            targets = caller._call_targets_definition(call, definition, collector.path)
            if targets is False:
                continue
            argument = caller._call_argument_for_parameter(call, definition, parameter)
            if argument is None:
                return False
            if isinstance(argument, ast.Constant) and argument.value is None:
                # ``conn=None`` (or an omitted optional parameter): the callee
                # takes its own connection on that path, so this call site
                # neither proves nor refuses the parameter-received execute.
                continue
            call_sites += 1
            if not self._argument_proves_non_session(caller, call, argument, depth=depth, active=active):
                return False
        return call_sites > 0

    def _argument_proves_non_session(
        self,
        caller: _ProductionWriterCollector,
        call: ast.Call,
        argument: ast.expr,
        *,
        depth: int,
        active: frozenset[tuple[int, str]],
    ) -> bool:
        acquisitions = caller._connection_acquisitions_for_expression(call, argument)
        if (
            acquisitions
            and caller._merge_database_domains(caller._connection_database_domain(acquisition) for acquisition in acquisitions)
            == "non_sessions"
        ):
            return True
        if caller._expression_database_domain(argument, use=call) == "non_sessions":
            return True
        if not isinstance(argument, ast.Name):
            return False
        if caller.declared_non_session_module and caller._name_is_module_bound(call, argument.id, depth=0):
            return True
        received = caller._enclosing_function(call)
        if received is None:
            return False
        parameters = {item.arg for item in (*received.args.posonlyargs, *received.args.args, *received.args.kwonlyargs)}
        if argument.id not in parameters or caller._name_reassigned_in(received, argument.id):
            return False
        return self._parameter_proves_non_session(caller, received, argument.id, depth=depth + 1, active=active)


def _identity_key(site: WriterIdentity) -> tuple[str, str, str, str, str, int, str | None, int, bool]:
    return (
        site.path,
        site.symbol,
        site.table,
        site.operation,
        site.fingerprint,
        site.ordinal,
        site.authority,
        site.line,
        site.connection_escape,
    )


def inventory_drift(
    live: Sequence[WriterIdentity],
    reviewed: Sequence[WriterIdentity],
) -> tuple[list[WriterIdentity], list[WriterIdentity]]:
    reviewed_counts = Counter(_identity_key(site) for site in reviewed)
    unexpected: list[WriterIdentity] = []
    for site in live:
        key = _identity_key(site)
        if reviewed_counts[key]:
            reviewed_counts[key] -= 1
        else:
            unexpected.append(site)

    live_counts = Counter(_identity_key(site) for site in live)
    stale: list[WriterIdentity] = []
    for site in reviewed:
        key = _identity_key(site)
        if live_counts[key]:
            live_counts[key] -= 1
        else:
            stale.append(site)
    return unexpected, stale


def subtract_reviewed_identities(
    live: Sequence[WriterIdentity],
    reviewed: Sequence[WriterIdentity],
) -> tuple[list[WriterIdentity], list[WriterIdentity]]:
    """Remove exact reviewed identities once and return stale review entries."""

    reviewed_counts = Counter(_identity_key(site) for site in reviewed)
    remaining: list[WriterIdentity] = []
    for site in live:
        key = _identity_key(site)
        if reviewed_counts[key]:
            reviewed_counts[key] -= 1
        else:
            remaining.append(site)

    stale: list[WriterIdentity] = []
    for site in reviewed:
        key = _identity_key(site)
        if reviewed_counts[key]:
            reviewed_counts[key] -= 1
            stale.append(site)
    return remaining, stale


def subtract_reviewed_read_identities(
    live: Sequence[WriterIdentity],
    reviewed: Sequence[WriterIdentity],
) -> tuple[list[WriterIdentity], list[WriterIdentity]]:
    """Subtract only read-review identities that cannot transfer capability."""

    admissible = [
        site
        for site in reviewed
        if site.operation == "write_connection" and site.table == "<sessions-write-connection>" and not site.connection_escape
    ]
    invalid = [site for site in reviewed if site not in admissible]
    remaining, stale = subtract_reviewed_identities(live, admissible)
    return remaining, [*stale, *invalid]


def authority_policy_violations(
    live: Sequence[WriterIdentity],
    policies: Sequence[TablePolicy],
) -> tuple[list[WriterIdentity], list[tuple[WriterIdentity, TablePolicy]]]:
    policies_by_table = {policy.table: policy for policy in policies}
    unclassified: list[WriterIdentity] = []
    mismatched: list[tuple[WriterIdentity, TablePolicy]] = []
    for site in live:
        policy = policies_by_table.get(site.table)
        if policy is None:
            continue
        if site.authority is None:
            unclassified.append(site)
        elif not policy.permits(site):
            mismatched.append((site, policy))
    return unclassified, mismatched


def connection_authority_violations(live: Sequence[WriterIdentity]) -> list[WriterIdentity]:
    violations: list[WriterIdentity] = []
    for site in live:
        if site.operation != "write_connection":
            continue
        contained_authority = _contained_connection_authority_for(site.path, site.symbol)
        if site.connection_escape or contained_authority is None or contained_authority != site.authority:
            violations.append(site)
    return violations


def reviewed_read_connection_policy_violations(reviewed: Sequence[WriterIdentity]) -> list[WriterIdentity]:
    """Reject read-review entries that can transfer connection capability."""

    return [
        site
        for site in reviewed
        if site.operation != "write_connection" or site.table != "<sessions-write-connection>" or site.connection_escape
    ]


def test_sessions_metadata_table_policy_is_exact_and_protected() -> None:
    live_tables = Counter(sessions_metadata.tables.keys())
    reviewed_tables = Counter(policy.table for policy in _TABLE_POLICIES)
    assert live_tables == reviewed_tables
    policies = {policy.table: policy for policy in _TABLE_POLICIES}
    for logical_name, table in _PROTECTED_LOGICAL_TABLES.items():
        assert table in policies, f"protected Sessions table {logical_name!r} ({table!r}) is unclassified"
        assert policies[table].authority, f"protected Sessions table {logical_name!r} has no named authority"


def test_skill_markdown_history_authority_is_exact_contained_and_complete() -> None:
    root = _repo_root()
    authority_path = root / "src/elspeth/web/sessions/skill_markdown_history.py"
    service_path = root / "src/elspeth/web/sessions/service.py"
    symbol = "RepositorySkillMarkdownHistoryAuthority.upsert_exact"
    authority_live = scan_production_writers([authority_path], anchor=root)
    writes = [site for site in authority_live if site.table == "skill_markdown_history"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "skill_markdown_history"]
    connections = [site for site in authority_live if site.operation == "write_connection"]

    assert len(writes) == len(reviewed) == 2
    assert inventory_drift(writes, reviewed) == ([], [])
    assert {site.authority for site in writes} == {"SkillMarkdownHistoryAuthority"}
    assert connections == [
        WriterIdentity(
            "src/elspeth/web/sessions/skill_markdown_history.py",
            symbol,
            "<sessions-write-connection>",
            "write_connection",
            "7b8f2374db24e42b",
            1,
            "SkillMarkdownHistoryAuthority",
            line=62,
        )
    ]
    assert connection_authority_violations(authority_live) == []
    assert not [site for site in authority_live if site.table == "<unresolved-session-write>"]
    assert not [
        site
        for site in scan_production_writers([service_path], anchor=root)
        if site.table == "skill_markdown_history" or site.symbol == "SessionServiceImpl.upsert_skill_markdown_history._sync"
    ]


def test_user_preference_authority_is_exact_contained_and_complete() -> None:
    """Ruling 8925 #3 (Task-5 inventory record): the ``user_preferences`` writer is
    ``PreferencesService.update_composer_preferences._sync`` itself -- one atomic
    dialect upsert inside the sessions engine's write transaction, never a compose
    lease. ``RepositoryUserPreferenceAuthority`` is retired, and every
    ``user_preferences`` write in the module must sit under the named writer."""
    root = _repo_root()
    path = "src/elspeth/web/preferences/service.py"
    service_path = root / path
    authority_prefix = "PreferencesService.update_composer_preferences"
    authority_symbol = f"{authority_prefix}._sync"
    assert _authority_for(path, authority_prefix) == "UserPreferenceAuthority"
    assert _authority_for(path, authority_symbol) == "UserPreferenceAuthority"
    assert _authority_for(path, f"{authority_prefix}_replacement") is None
    assert _authority_for(path, "PreferencesService.get_composer_preferences") is None
    assert _contained_connection_authority_for(path, authority_symbol) == "UserPreferenceAuthority"
    assert _contained_connection_authority_for(path, authority_prefix) is None
    live = scan_production_writers([service_path], anchor=root)
    authority_live = [site for site in live if site.symbol.startswith(f"{authority_prefix}.")]
    writes = [site for site in authority_live if site.table == "user_preferences"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "user_preferences"]
    connections = [site for site in authority_live if site.operation == "write_connection"]
    reviewed_connections = [site for site in _REVIEWED_WRITERS if site.symbol == authority_symbol and site.operation == "write_connection"]

    assert len(writes) == len(reviewed) == 2
    assert inventory_drift(writes, reviewed) == ([], [])
    assert {site.authority for site in writes} == {"UserPreferenceAuthority"}
    assert authority_policy_violations(writes, _TABLE_POLICIES) == ([], [])
    assert connections == [
        WriterIdentity(
            path,
            authority_symbol,
            "<sessions-write-connection>",
            "write_connection",
            "a793cf5b38728669",
            1,
            "UserPreferenceAuthority",
            line=334,
        )
    ]
    assert inventory_drift(connections, reviewed_connections) == ([], [])
    assert connection_authority_violations(authority_live) == []
    assert not [site for site in authority_live if site.table == "<unresolved-session-write>"]
    assert not [site for site in live if site.table == "user_preferences" and site not in writes]
    assert not [site for site in live if site.symbol.startswith("RepositoryUserPreferenceAuthority")]


def test_named_authority_registry_is_explicit_extensible_and_exact() -> None:
    path = "src/elspeth/web/coordination/repository.py"
    session_service_path = "src/elspeth/web/sessions/service.py"
    assert (
        _authority_for(session_service_path, "_SessionComposerMutations.create_composition_proposal") == "SessionComposerMutationAuthority"
    )
    assert (
        _authority_for(session_service_path, "_SessionComposerMutations.create_pipeline_composition_proposal")
        == "SessionComposerMutationAuthority"
    )
    assert (
        _authority_for(session_service_path, "_SessionComposerMutations.accept_pending_ordinary_proposal")
        == "SessionComposerMutationAuthority"
    )
    assert _authority_for(session_service_path, "_SessionComposerMutations.create_composition_proposal_replacement") is None
    assert _authority_for(session_service_path, "_SessionComposerMutations.future_method") is None
    assert _authority_for(path, "_RepositorySessionMutations.decide_and_soft_archive") == "SessionMutationAuthority"
    assert _authority_for(path, "_RepositoryCompositionStateMutations.append_state") == "SessionMutationAuthority"
    assert _authority_for(path, "_RepositoryCompositionStateMutations.append_state_replacement") is None
    assert _authority_for(path, "_RepositoryComposerCompletionMutations.mark_ready_for_review") == "SessionComposerMutationAuthority"
    assert _authority_for(path, "_RepositoryComposerCompletionMutations.record_yaml_export") == "SessionComposerMutationAuthority"
    assert _authority_for(path, "_RepositoryRunMutations.create_pending_run") == "SessionRunMutationAuthority"
    assert _authority_for(path, "_RepositoryRunMutations.transition_run_status") == "SessionRunMutationAuthority"
    assert _authority_for(path, "_RepositoryRunMutations.append_run_event") == "SessionRunMutationAuthority"
    assert _authority_for(path, "_RepositoryBlobMutations.insert_blob_run_link") == "SessionBlobMutationAuthority"
    assert _authority_for(path, "_RepositoryBlobMutations._record_applied_blob_proposal_effect") == "SessionBlobMutationAuthority"
    for method in (
        "prepare_blob_replacement",
        "mark_blob_replacement_staged",
        "commit_blob_replacement",
        "retire_blob_replacement",
        "abort_blob_replacement",
    ):
        assert _authority_for(path, f"_RepositoryBlobMutations.{method}") == "SessionBlobMutationAuthority"
    assert _authority_for(path, "_SessionOperationAuthorityRepository.mutate") == "SessionOperationAuthority"
    assert _authority_for(path, "_SessionOperationAuthorityRepository._insert_fork_child") == "SessionOperationAuthority"
    assert _authority_for(path, "_SessionOperationAuthorityRepository._resume_or_take_over_fork_child") == "SessionOperationAuthority"
    assert _authority_for(path, "_ForkChildSessionMutations.insert_child_state") == "SessionForkChildMutations"
    assert _authority_for(path, "_ForkChildSessionMutations.append_child_messages") == "SessionForkChildMutations"
    assert _authority_for(path, "_ForkParentGuidedMutations.bind_guided_fork") == "SessionForkParentGuidedMutations"
    assert _authority_for(path, "_ForkCreationTransaction.read_parent_session") is None
    assert _authority_for(path, "_RepositoryRunMutationsReplacement.append_run_event") is None
    assert _authority_for(path, "_RepositorySessionMutations.future_method") is None
    assert _authority_for(path, "_RepositoryRunMutations._private_helper") is None
    assert _authority_for(path, "_RepositoryBlobMutations.__getattr__") is None
    assert _authority_for(path, "_SessionOperationAuthorityRepositoryReplacement._insert_fork_child") is None
    assert _authority_for(path, "_ForkChildSessionMutationsReplacement.insert_child_state") is None
    assert _authority_for(path, "_ForkParentGuidedMutationsReplacement.bind_guided_fork") is None
    assert _authority_for(path, "SessionMutationAuthority.append_run_event") is None
    assert _authority_for("src/elspeth/web/sessions/service.py", "_RepositoryRunMutations.append_run_event") is None
    assert _authority_for(path, "_RepositoryRunMutations.create_pending_run.helper") == "SessionRunMutationAuthority"
    assert _authority_for(path, "_RepositoryRunMutations.create_pending_runner") is None
    assert _authority_for(path, "_RepositoryRunMutations.future_method") is None
    assert _authority_for(path, "_RepositoryComposerCompletionMutations.future_method") is None
    assert _authority_for(path, "RepositoryComposerProgressMutations.start_request") is None
    policies = {policy.table: policy for policy in _TABLE_POLICIES}
    assert policies["chat_messages"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"update"})),
        ("RunDiagnosticsAuditMutationAuthority", frozenset({"insert"})),
    )
    assert policies["composition_states"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"delete"})),
        ("SessionInterpretationAuthority", frozenset({"insert"})),
    )
    assert policies["guided_operations"].operation_authorities == (
        ("SessionForkParentGuidedMutations", frozenset({"update"})),
        ("GuidedSessionAdmissionAuthority", frozenset({"insert", "update"})),
        ("SessionForkAuthority", frozenset({"insert"})),
    )
    assert policies["sessions"].operation_authorities == (
        ("SessionOperationAuthority", frozenset({"insert", "delete"})),
        ("GuidedSessionMutationAuthority", frozenset({"update"})),
        ("SessionForkAuthority", frozenset({"update"})),
        ("SessionInterpretationAuthority", frozenset({"update"})),
        ("RunDiagnosticsAuditMutationAuthority", frozenset({"update"})),
        ("SessionComposerMutationAuthority", frozenset({"update"})),
    )
    policy_authorities = {policy.authority for policy in _TABLE_POLICIES} | {
        authority for policy in _TABLE_POLICIES for authority, _operations in policy.operation_authorities
    }
    assert {binding.authority for binding in _NAMED_AUTHORITY_SYMBOLS} <= policy_authorities


def test_audit_access_log_writer_is_exactly_bound_to_its_handle_free_authority() -> None:
    root = _repo_root()
    repository_path = "src/elspeth/web/coordination/audit_access_log_authority.py"
    authority_symbol = "RepositoryAuditAccessLogAuthority.record_audit_grade_view"
    assert _authority_for(repository_path, authority_symbol) == "AuditAccessLogAuthority"
    assert _contained_connection_authority_for(repository_path, authority_symbol) == "AuditAccessLogAuthority"
    assert _authority_for(repository_path, f"{authority_symbol}_replacement") is None
    assert _authority_for(repository_path, "RepositoryAuditAccessLogAuthority.future_method") is None

    paths = [
        root / repository_path,
        root / "src/elspeth/web/sessions/service.py",
    ]
    live = [site for site in scan_production_writers(paths, anchor=root) if site.table == "audit_access_log"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "audit_access_log"]
    assert len(live) == len(reviewed) == 1
    assert live[0].symbol == authority_symbol
    assert inventory_drift(live, reviewed) == ([], [])


def test_run_diagnostics_writer_is_exactly_bound_to_its_handle_free_authority() -> None:
    root = _repo_root()
    repository_path = "src/elspeth/web/coordination/run_diagnostics_authority.py"
    service_path = "src/elspeth/web/sessions/service.py"
    authority_symbol = "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages"
    assert _authority_for(repository_path, authority_symbol) == "RunDiagnosticsAuditMutationAuthority"
    assert _contained_connection_authority_for(repository_path, authority_symbol) == "RunDiagnosticsAuditMutationAuthority"
    assert _authority_for(repository_path, f"{authority_symbol}_replacement") is None

    scanned = scan_production_writers([root / repository_path, root / service_path], anchor=root)
    assert not [
        site
        for site in scanned
        if site.symbol == "SessionServiceImpl.add_run_diagnostics_audit_message._sync" and site.table in {"chat_messages", "sessions"}
    ]
    live = [site for site in scanned if site.symbol == authority_symbol]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol == authority_symbol]
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []


def test_user_secret_writers_are_exactly_bound_to_the_handle_free_authority() -> None:
    root = _repo_root()
    path = "src/elspeth/web/secrets/user_store.py"
    symbols = {
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "RepositoryUserSecretAuthority.delete_secret",
    }
    for symbol in symbols:
        assert _authority_for(path, symbol) == "UserSecretAuthority"
        assert _contained_connection_authority_for(path, symbol) == "UserSecretAuthority"
        assert _authority_for(path, f"{symbol}_replacement") is None

    scanned = scan_production_writers([root / path], anchor=root)
    live = [site for site in scanned if site.table == "user_secrets"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "user_secrets"]
    assert len(live) == len(reviewed) == 4
    assert {site.symbol for site in live} == symbols
    assert {site.authority for site in live} == {"UserSecretAuthority"}
    assert inventory_drift(live, reviewed) == ([], [])
    assert not any(
        site.symbol in {"UserSecretStore.set_secret", "UserSecretStore.delete_secret"}
        and site.table in {"user_secrets", "<unresolved-session-write>"}
        for site in scanned
    )


def test_sso_handoff_writers_are_exactly_bound_to_the_handle_free_authority() -> None:
    """Family T (elspeth-e483fe7f85): the single-use SSO handoff store.

    ``SsoHandoffRepository`` already has the handle-free shape -- it holds
    the engine, opens one committed transaction per method and hands no
    connection out -- so the binding keys on its two methods where they
    live rather than renaming the class into ``Repository*Authority`` form.
    """
    root = _repo_root()
    path = "src/elspeth/web/sessions/sso_handoff_repository.py"
    symbols = {"SsoHandoffRepository.issue", "SsoHandoffRepository.consume"}
    for symbol in symbols:
        assert _authority_for(path, symbol) == "SsoHandoffAuthority"
        assert _contained_connection_authority_for(path, symbol) == "SsoHandoffAuthority"
        assert _authority_for(path, f"{symbol}_replacement") is None
    assert _authority_for(path, "SsoHandoffRepository.future_method") is None

    scanned = scan_production_writers([root / path], anchor=root)
    writes = [site for site in scanned if site.table == "sso_handoffs"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "sso_handoffs"]
    connections = [site for site in scanned if site.operation == "write_connection"]

    assert len(writes) == len(reviewed) == 4
    assert {site.symbol for site in writes} == symbols
    assert {(site.symbol, site.operation) for site in writes} == {
        ("SsoHandoffRepository.issue", "delete"),
        ("SsoHandoffRepository.issue", "insert"),
        ("SsoHandoffRepository.consume", "update"),
        ("SsoHandoffRepository.consume", "delete"),
    }
    assert {site.authority for site in writes} == {"SsoHandoffAuthority"}
    assert inventory_drift(writes, reviewed) == ([], [])
    assert authority_policy_violations(writes, _TABLE_POLICIES) == ([], [])

    assert len(connections) == 2
    assert {site.symbol for site in connections} == symbols
    assert not any(site.connection_escape for site in connections)
    assert connection_authority_violations(connections) == []
    assert inventory_drift(
        connections, [site for site in _REVIEWED_WRITERS if site.path == path and site.operation == "write_connection"]
    ) == ([], [])
    assert not [site for site in scanned if site.table == "<unresolved-session-write>"]


def test_fork_facet_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/coordination/repository.py"
    symbols = {
        "_ForkChildSessionMutations.insert_child_state",
        "_ForkChildSessionMutations.append_child_messages",
        "_ForkParentGuidedMutations.bind_guided_fork",
    }
    live = [site for site in scan_production_writers([path], anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 3
    assert inventory_drift(live, reviewed) == ([], [])


def test_blob_replacement_facet_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/coordination/repository.py"
    symbols = {
        "_RepositoryBlobMutations.prepare_blob_replacement",
        "_RepositoryBlobMutations.mark_blob_replacement_staged",
        "_RepositoryBlobMutations.commit_blob_replacement",
        "_RepositoryBlobMutations.retire_blob_replacement",
        "_RepositoryBlobMutations.abort_blob_replacement",
    }
    live = [site for site in scan_production_writers([path], anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 6
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])


def test_plugin_crash_breadcrumb_facet_identity_is_exact_and_bidirectional() -> None:
    root = _repo_root()
    repository_path = root / "src/elspeth/web/coordination/repository.py"
    composer_path = root / "src/elspeth/web/composer/service.py"
    symbol = "_RepositorySessionMutations.record_plugin_crash_breadcrumb"

    assert _authority_for("src/elspeth/web/coordination/repository.py", symbol) == "SessionMutationAuthority"
    assert _authority_for("src/elspeth/web/coordination/repository.py", f"{symbol}_replacement") is None
    scanned = scan_production_writers([repository_path, composer_path], anchor=root)
    assert not [site for site in scanned if site.symbol == "ComposerServiceImpl._persist_crashed_session"]
    live = [site for site in scanned if site.symbol == symbol]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol == symbol]
    assert len(live) == len(reviewed) == 1
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])


def test_composition_state_facet_identity_is_exact_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/coordination/repository.py"
    symbol = "_RepositoryCompositionStateMutations.append_state"

    live = [site for site in scan_production_writers([path], anchor=root) if site.symbol == symbol]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol == symbol]
    assert len(live) == len(reviewed) == 1
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])


def test_simple_interpretation_facet_identities_are_exact_and_replace_legacy_service_writers() -> None:
    root = _repo_root()
    repository_path = root / "src/elspeth/web/coordination/repository.py"
    service_path = root / "src/elspeth/web/sessions/service.py"
    repository_relpath = "src/elspeth/web/coordination/repository.py"
    symbols = {
        "_RepositoryInterpretationMutations.create_or_reconcile_pending",
        "_RepositoryInterpretationMutations.record_session_opt_out",
        "_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event",
    }
    legacy_symbols = {
        "SessionServiceImpl._prepare_or_create_pending_interpretation_event._sync",
        "SessionServiceImpl.record_session_interpretation_opt_out._sync",
        "SessionServiceImpl.record_auto_interpreted_no_surfaces_event._sync",
    }

    for symbol in symbols:
        assert _authority_for(repository_relpath, symbol) == "SessionInterpretationAuthority"
        assert _authority_for(repository_relpath, f"{symbol}_replacement") is None

    scanned = scan_production_writers([repository_path, service_path], anchor=root)
    assert not [site for site in scanned if site.symbol in legacy_symbols]
    live = [site for site in scanned if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 7
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])


def test_run_facet_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    paths = [root / "src/elspeth/web/coordination/repository.py"]
    symbols = {
        "_RepositoryRunMutations.create_pending_run",
        "_RepositoryRunMutations.transition_run_status",
    }
    live = [site for site in scan_production_writers(paths, anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])


def test_global_run_recovery_writer_is_exact_contained_and_replaces_direct_service_writes() -> None:
    root = _repo_root()
    repository_path = root / "src/elspeth/web/coordination/run_recovery_authority.py"
    service_path = root / "src/elspeth/web/sessions/service.py"
    repository_relpath = "src/elspeth/web/coordination/run_recovery_authority.py"
    symbols = {
        "RepositoryGlobalRunRecoveryAuthority._cancel_candidate",
        "RepositoryGlobalRunRecoveryAuthority.cancel_orphaned_run_records",
        "RepositoryGlobalRunRecoveryAuthority.mark_landscape_reconciliation_outcomes",
    }

    for symbol in symbols:
        assert _authority_for(repository_relpath, symbol) == "GlobalRunRecoveryAuthority"
        assert _authority_for(repository_relpath, f"{symbol}_replacement") is None
    for symbol in symbols - {"RepositoryGlobalRunRecoveryAuthority._cancel_candidate"}:
        assert _contained_connection_authority_for(repository_relpath, symbol) == "GlobalRunRecoveryAuthority"

    scanned = scan_production_writers([repository_path, service_path], anchor=root)
    assert not [
        site
        for site in scanned
        if site.symbol
        in {
            "SessionServiceImpl.cancel_orphaned_runs._sync",
            "SessionServiceImpl.cancel_all_orphaned_run_records",
            "SessionServiceImpl.mark_landscape_reconciliation_outcomes",
        }
        and site.table in {"runs", "<unresolved-session-write>"}
    ]
    authority_live = [site for site in scanned if site.symbol in symbols]
    run_writes = [site for site in authority_live if site.table == "runs"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols and site.table == "runs"]
    assert len(run_writes) == len(reviewed) == 2
    assert inventory_drift(run_writes, reviewed) == ([], [])
    assert authority_policy_violations(run_writes, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(authority_live) == []
    # The authority's own acquisitions are admitted like writers (P4-D6 step 4):
    # every live contained acquisition is reviewed, and nothing reviewed has
    # drifted or escaped.
    acquisitions = [site for site in authority_live if site.operation == "write_connection"]
    reviewed_acquisitions = [site for site in _REVIEWED_WRITERS if site.symbol in symbols and site.operation == "write_connection"]
    assert len(acquisitions) == len(reviewed_acquisitions) == 3
    assert not any(site.connection_escape for site in acquisitions)
    assert inventory_drift(acquisitions, reviewed_acquisitions) == ([], [])


def test_web_instance_membership_writer_is_exact_contained_and_operation_exact() -> None:
    """``web_instances`` has one writer: the membership authority's four lifecycle methods.

    A peer joins an expired fence's ``owner_instance_id`` to this table before it
    may take the fence over (``PostgresSessionOperationRepository._expired_owner_allows_takeover``
    and ``RepositoryGlobalRunRecoveryAuthority._session_allows_recovery`` both read
    it and fail closed), so the row's writer set is a takeover-safety property:
    exactly ``register`` (insert, or update to reclaim a dead incarnation),
    ``heartbeat``, ``begin_drain`` and ``stop`` (update), never a delete, and
    every write inside a connection the method opens and never hands out. The
    readers, the lifecycle wrapper and the app factory never write it.
    """

    root = _repo_root()
    authority_relpath = "src/elspeth/web/coordination/membership_authority.py"
    expected_operations = {
        "RepositoryWebInstanceMembershipAuthority.register": {"insert", "update"},
        "RepositoryWebInstanceMembershipAuthority.heartbeat": {"update"},
        "RepositoryWebInstanceMembershipAuthority.begin_drain": {"update"},
        "RepositoryWebInstanceMembershipAuthority.stop": {"update"},
    }
    for symbol in expected_operations:
        assert _authority_for(authority_relpath, symbol) == "WebInstanceMembershipAuthority"
        assert _contained_connection_authority_for(authority_relpath, symbol) == "WebInstanceMembershipAuthority"
        assert _authority_for(authority_relpath, f"{symbol}_replacement") is None
    assert _authority_for(authority_relpath, "RepositoryWebInstanceMembershipAuthority.future_method") is None
    assert _contained_connection_authority_for(authority_relpath, "RepositoryWebInstanceMembershipAuthority") is None

    authority_live = scan_production_writers([root / authority_relpath], anchor=root)
    writes = [site for site in authority_live if site.table == "web_instances"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "web_instances"]
    assert len(writes) == len(reviewed) == 5
    assert inventory_drift(writes, reviewed) == ([], [])
    assert {site.authority for site in writes} == {"WebInstanceMembershipAuthority"}
    live_operations: dict[str, set[str]] = {}
    for site in writes:
        live_operations.setdefault(site.symbol, set()).add(site.operation)
    assert live_operations == expected_operations
    assert authority_policy_violations(writes, _TABLE_POLICIES) == ([], [])
    assert not [site for site in authority_live if site.table == "<unresolved-session-write>"]

    connections = sorted((site for site in authority_live if site.operation == "write_connection"), key=lambda site: site.line)
    assert connections == [
        WriterIdentity(
            authority_relpath,
            "RepositoryWebInstanceMembershipAuthority.register",
            "<sessions-write-connection>",
            "write_connection",
            "4152160f19e026da",
            1,
            "WebInstanceMembershipAuthority",
            line=229,
        ),
        WriterIdentity(
            authority_relpath,
            "RepositoryWebInstanceMembershipAuthority.heartbeat",
            "<sessions-write-connection>",
            "write_connection",
            "71b4334a0f438add",
            1,
            "WebInstanceMembershipAuthority",
            line=274,
        ),
        WriterIdentity(
            authority_relpath,
            "RepositoryWebInstanceMembershipAuthority.begin_drain",
            "<sessions-write-connection>",
            "write_connection",
            "98fd14f12508e73a",
            1,
            "WebInstanceMembershipAuthority",
            line=296,
        ),
        WriterIdentity(
            authority_relpath,
            "RepositoryWebInstanceMembershipAuthority.stop",
            "<sessions-write-connection>",
            "write_connection",
            "476bbd10507b185c",
            1,
            "WebInstanceMembershipAuthority",
            line=318,
        ),
    ]
    assert connection_authority_violations(authority_live) == []

    non_writers = [
        path for path in iter_gate_files(root / "src/elspeth/web/coordination") if path.resolve() != (root / authority_relpath).resolve()
    ]
    non_writers.extend([root / "src/elspeth/web/app.py", root / "src/elspeth/web/sessions/service.py"])
    assert not [site for site in scan_production_writers(non_writers, anchor=root) if site.table == "web_instances"]


def test_removed_unfenced_state_pruner_is_absent() -> None:
    root = _repo_root()
    service_path = root / "src/elspeth/web/sessions/service.py"
    scanned = scan_production_writers([service_path], anchor=root)

    assert not [site for site in scanned if site.symbol == "SessionServiceImpl.prune_state_versions._sync"]


def test_removed_unfenced_active_state_setter_is_absent() -> None:
    root = _repo_root()
    service_path = root / "src/elspeth/web/sessions/service.py"
    scanned = scan_production_writers([service_path], anchor=root)

    assert not [site for site in scanned if site.symbol == "SessionServiceImpl.set_active_state._sync"]


def test_composer_completion_facet_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/coordination/repository.py"
    symbols = {
        "_RepositoryComposerCompletionMutations.mark_ready_for_review",
        "_RepositoryComposerCompletionMutations.record_yaml_export",
    }
    live = [site for site in scan_production_writers([path], anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])


def test_ordinary_composer_proposal_facets_are_exact_contained_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/sessions/service.py"
    symbols = {
        "_SessionComposerMutations.create_composition_proposal",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "_SessionComposerMutations.reject_pending_proposal",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
    }
    for symbol in symbols:
        assert _authority_for("src/elspeth/web/sessions/service.py", symbol) == "SessionComposerMutationAuthority"
        assert _contained_connection_authority_for("src/elspeth/web/sessions/service.py", symbol) == ("SessionComposerMutationAuthority")
    scanned = scan_production_writers([path], anchor=root)
    assert not [
        site
        for site in scanned
        if site.symbol
        in {
            "SessionServiceImpl.create_composition_proposal._sync",
            "SessionServiceImpl.create_pipeline_composition_proposal._sync",
            "SessionServiceImpl.reject_composition_proposal._sync",
            "SessionServiceImpl.accept_composition_proposal._sync",
        }
        and site.table in {"proposal_events", "composition_proposals"}
    ]
    live = [site for site in scanned if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 9
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []


def test_blob_proposal_effect_receipt_writers_are_exact_authorized_and_bidirectional() -> None:
    root = _repo_root()
    paths = [
        root / "src/elspeth/web/coordination/repository.py",
        root / "src/elspeth/web/sessions/service.py",
    ]
    authorities = {
        "_RepositoryBlobMutations._record_applied_blob_proposal_effect": "SessionBlobMutationAuthority",
        "_SessionComposerMutations.accept_pending_ordinary_proposal": "SessionComposerMutationAuthority",
    }
    live = [
        site
        for site in scan_production_writers(paths, anchor=root)
        if site.table == "proposal_blob_effect_receipts" and site.symbol in authorities
    ]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "proposal_blob_effect_receipts"]

    assert len(live) == len(reviewed) == 2
    assert {(site.symbol, site.operation, site.authority) for site in live} == {
        (
            "_RepositoryBlobMutations._record_applied_blob_proposal_effect",
            "insert",
            "SessionBlobMutationAuthority",
        ),
        (
            "_SessionComposerMutations.accept_pending_ordinary_proposal",
            "update",
            "SessionComposerMutationAuthority",
        ),
    }
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []
    policy = {entry.table: entry for entry in _TABLE_POLICIES}["proposal_blob_effect_receipts"]
    assert policy.authority == "SessionComposerMutationAuthority"
    assert policy.operation_authorities == (("SessionBlobMutationAuthority", frozenset({"insert"})),)


def test_composer_preferences_facets_are_exact_contained_and_bidirectional() -> None:
    """Ruling 8925 #3 (Task-5 inventory record): the session-side preferences writer
    is ``SessionServiceImpl.update_composer_preferences._sync`` -- audit row, then
    session row, in one transaction serialised by the per-session write lock and
    deliberately NEVER by the compose lease, so a mid-compose trust downgrade always
    lands. The compose-leased facet pair that mirrored it
    (``_SessionComposerMutations.record_preferences_changed`` /
    ``_SessionMutations.update_composer_preferences``) is deleted and stays deleted."""
    root = _repo_root()
    path = "src/elspeth/web/sessions/service.py"
    prefix = "SessionServiceImpl.update_composer_preferences"
    symbol = f"{prefix}._sync"
    authority = "SessionComposerMutationAuthority"
    assert _authority_for(path, prefix) == authority
    assert _authority_for(path, symbol) == authority
    assert _authority_for(path, f"{prefix}_replacement") is None
    assert _authority_for(path, "SessionServiceImpl.get_composer_preferences") is None
    assert _contained_connection_authority_for(path, symbol) == authority
    assert _contained_connection_authority_for(path, prefix) is None
    scanned = scan_production_writers([root / path], anchor=root)
    assert not [
        site for site in scanned if site.symbol.startswith(("_SessionComposerMutations.record_preferences_changed", "_SessionMutations."))
    ]
    live = [site for site in scanned if site.symbol == symbol]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol == symbol]
    assert {(site.table, site.operation) for site in live} == {("proposal_events", "insert"), ("sessions", "update")}
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []
    assert not [site for site in live if site.table == "<unresolved-session-write>"]
    sessions_policy = {entry.table: entry for entry in _TABLE_POLICIES}["sessions"]
    assert (authority, frozenset({"update"})) in sessions_policy.operation_authorities
    assert not sessions_policy.permits(replace(live[0], table="sessions", operation="delete"))


def test_existing_guided_authority_registry_is_exact_and_keeps_connection_helpers_blocked() -> None:
    path = "src/elspeth/web/sessions/service.py"
    expected = {
        "SessionServiceImpl.reserve_guided_operation": "GuidedSessionAdmissionAuthority",
        "SessionServiceImpl.reconcile_guided_start_operation": "GuidedSessionAdmissionAuthority",
        "SessionServiceImpl.renew_guided_operation": "GuidedSessionMutationAuthority",
        "SessionServiceImpl.fail_guided_operation_with_audit": "GuidedSessionMutationAuthority",
        "SessionServiceImpl.settle_guided_fork_operation": "SessionForkAuthority",
    }
    for symbol, authority in expected.items():
        assert _authority_for(path, symbol) == authority
        assert _authority_for(path, f"{symbol}._sync") == authority
        assert _authority_for(path, f"{symbol}_replacement") is None
        assert _authority_for(path, f"SessionServiceImplReplacement.{symbol.rsplit('.', 1)[-1]}") is None
        assert _authority_for("src/elspeth/web/sessions/routes/guided_operations.py", symbol) is None

    for symbol in (
        "SessionServiceImpl._insert_guided_operation_event",
        "SessionServiceImpl.bind_guided_operation_on_connection",
        "SessionServiceImpl.complete_guided_operation_on_connection",
        "SessionServiceImpl.fail_guided_operation_on_connection",
    ):
        assert _authority_for(path, symbol) is None

    assert _authority_for(path, "_GuidedSessionMutations.bind") == "GuidedSessionMutationAuthority"
    assert _authority_for(path, "_GuidedComposerMutations.reject_pending_proposal") == "GuidedSessionComposerMutationAuthority"
    assert _authority_for(path, "SessionServiceImpl._record_guided_fork_child_terminal_event") == "SessionForkAuthority"


def test_existing_guided_authority_table_policies_are_operation_exact() -> None:
    policies = {policy.table: policy for policy in _TABLE_POLICIES}
    assert policies["guided_operation_admission_blocks"].operation_authorities == (
        ("GuidedSessionAdmissionAuthority", frozenset({"insert"})),
    )
    assert policies["guided_operations"].operation_authorities == (
        ("SessionForkParentGuidedMutations", frozenset({"update"})),
        ("GuidedSessionAdmissionAuthority", frozenset({"insert", "update"})),
        ("SessionForkAuthority", frozenset({"insert"})),
    )
    assert policies["guided_operation_events"].operation_authorities == (("SessionForkAuthority", frozenset({"insert"})),)
    assert policies["composition_proposals"].operation_authorities == (("GuidedSessionComposerMutationAuthority", frozenset({"update"})),)
    assert policies["proposal_events"].operation_authorities == (("GuidedSessionComposerMutationAuthority", frozenset({"insert"})),)
    assert policies["sessions"].operation_authorities == (
        ("SessionOperationAuthority", frozenset({"insert", "delete"})),
        ("GuidedSessionMutationAuthority", frozenset({"update"})),
        ("SessionForkAuthority", frozenset({"update"})),
        ("SessionInterpretationAuthority", frozenset({"update"})),
        ("RunDiagnosticsAuditMutationAuthority", frozenset({"update"})),
        ("SessionComposerMutationAuthority", frozenset({"update"})),
    )
    assert policies["chat_messages"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"update"})),
        ("RunDiagnosticsAuditMutationAuthority", frozenset({"insert"})),
    )
    assert policies["composition_states"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"delete"})),
        ("SessionInterpretationAuthority", frozenset({"insert"})),
    )


def test_existing_guided_authority_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/sessions/service.py"
    symbols = {
        "SessionServiceImpl.reserve_guided_operation._sync",
        "SessionServiceImpl.reconcile_guided_start_operation._sync",
        "SessionServiceImpl.renew_guided_operation._sync",
        "SessionServiceImpl.fail_guided_operation_with_audit._sync",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
    }
    live = [site for site in scan_production_writers([path], anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 11
    assert inventory_drift(live, reviewed) == ([], [])


def test_guided_composite_facets_replace_every_raw_connection_helper_exactly() -> None:
    root = _repo_root()
    path = root / "src/elspeth/web/sessions/service.py"
    removed_symbols = {
        "SessionServiceImpl._insert_guided_operation_event",
        "SessionServiceImpl.bind_guided_operation_on_connection",
        "SessionServiceImpl.complete_guided_operation_on_connection",
        "SessionServiceImpl.fail_guided_operation_on_connection",
        "_reject_guided_pending_proposal",
        "_require_no_active_guided_confirmation_admission",
        "SessionServiceImpl.admit_guided_pipeline_confirmation._sync",
    }
    facet_symbols = {
        "_GuidedSessionMutations.record_nonterminal_event",
        "_GuidedSessionMutations.bind",
        "_GuidedSessionMutations.require_no_active_confirmation",
        "_GuidedSessionMutations.claim_confirmation",
        "_GuidedSessionMutations.complete",
        "_GuidedSessionMutations.fail",
        "_GuidedComposerMutations.reject_pending_proposal",
        "SessionServiceImpl._record_guided_fork_child_terminal_event",
    }
    scanned = scan_production_writers([path], anchor=root)
    assert not [site for site in scanned if site.symbol in removed_symbols]
    live = [site for site in scanned if site.symbol in facet_symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in facet_symbols]
    assert len(live) == len(reviewed) == 12
    assert inventory_drift(live, reviewed) == ([], [])
    unclassified, mismatched = authority_policy_violations(live, _TABLE_POLICIES)
    assert unclassified == mismatched == []


def test_guided_lifecycle_facets_replace_every_raw_sync_writer_exactly() -> None:
    """P4-D6 family A1 (elspeth-99949c96ca): the guided proposal-lifecycle
    ``_sync`` bodies keep the lock/lease ceremony and hand every Sessions DML
    to a facet -- the ``updated_at`` bump to ``_GuidedSessionMutations``,
    terminal proposal events to ``_GuidedComposerMutations``, guided proposal
    creation to ``_SessionComposerMutations`` (the one authority the
    ``composition_proposals`` policy lets insert)."""
    root = _repo_root()
    path = "src/elspeth/web/sessions/service.py"
    sync_symbols = {
        f"SessionServiceImpl.{method}._sync"
        for method in (
            "revert_state_for_guided_operation",
            "seed_or_complete_guided_start_operation",
            "save_state_for_guided_operation",
            "settle_guided_state_operation",
            "stage_guided_full_pipeline_proposal",
            "decline_guided_full_pipeline_proposal",
            "stage_guided_pipeline_proposal",
            "back_edit_guided_pipeline_proposal",
            "reject_guided_pipeline_proposal",
            "record_guided_pipeline_dispatch",
            "accept_guided_pipeline_proposal",
        )
    }
    method_exact_facets = {
        "_GuidedComposerMutations.record_pending_proposal_rejection": "GuidedSessionComposerMutationAuthority",
        "_GuidedComposerMutations.record_pending_proposal_acceptance": "GuidedSessionComposerMutationAuthority",
        "_SessionComposerMutations.create_guided_pipeline_proposal": "SessionComposerMutationAuthority",
    }
    for symbol, authority in method_exact_facets.items():
        assert _authority_for(path, symbol) == authority
        assert _authority_for(path, f"{symbol}_replacement") is None
    facets = {**method_exact_facets, "_GuidedSessionMutations.mark_session_updated": "GuidedSessionMutationAuthority"}
    assert _authority_for(path, "_GuidedSessionMutations.mark_session_updated") == "GuidedSessionMutationAuthority"
    scanned = scan_production_writers([root / path], anchor=root)
    assert not [
        site for site in scanned if site.symbol in sync_symbols and site.table in {"sessions", "proposal_events", "composition_proposals"}
    ]
    live = [site for site in scanned if site.symbol in facets]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in facets]
    assert {(site.symbol, site.table, site.operation, site.authority) for site in live} == {
        ("_GuidedSessionMutations.mark_session_updated", "sessions", "update", "GuidedSessionMutationAuthority"),
        (
            "_GuidedComposerMutations.record_pending_proposal_rejection",
            "proposal_events",
            "insert",
            "GuidedSessionComposerMutationAuthority",
        ),
        (
            "_GuidedComposerMutations.record_pending_proposal_rejection",
            "composition_proposals",
            "update",
            "GuidedSessionComposerMutationAuthority",
        ),
        (
            "_GuidedComposerMutations.record_pending_proposal_acceptance",
            "proposal_events",
            "insert",
            "GuidedSessionComposerMutationAuthority",
        ),
        (
            "_GuidedComposerMutations.record_pending_proposal_acceptance",
            "composition_proposals",
            "update",
            "GuidedSessionComposerMutationAuthority",
        ),
        ("_SessionComposerMutations.create_guided_pipeline_proposal", "proposal_events", "insert", "SessionComposerMutationAuthority"),
        (
            "_SessionComposerMutations.create_guided_pipeline_proposal",
            "composition_proposals",
            "insert",
            "SessionComposerMutationAuthority",
        ),
    }
    assert len(live) == len(reviewed) == 7
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []


def test_messages_state_writers_route_through_repository_session_facets_exactly() -> None:
    """P4-D6 family A2a (elspeth-99949c96ca): the ``updated_at`` bump of the
    transition-assistant and transcript writers and the refused-tool-call
    record of ``persist_compose_turn`` leave the service for two method-exact
    ``_RepositorySessionMutations`` facets (SessionMutationAuthority, the
    ``sessions`` / ``composition_rejection_events`` policy owner)."""
    root = _repo_root()
    service_path = "src/elspeth/web/sessions/service.py"
    repository_path = "src/elspeth/web/coordination/repository.py"
    routed = {
        "SessionServiceImpl._insert_transition_assistant": {"sessions"},
        "SessionServiceImpl.add_message_with_transcript._sync": {"sessions"},
        "SessionServiceImpl.persist_compose_turn": {"composition_rejection_events"},
    }
    facets = {
        "_RepositorySessionMutations.mark_session_updated": ("sessions", "update"),
        "_RepositorySessionMutations.record_composition_rejection": ("composition_rejection_events", "insert"),
    }
    for symbol in facets:
        assert _authority_for(repository_path, symbol) == "SessionMutationAuthority"
        assert _authority_for(repository_path, f"{symbol}_replacement") is None
        assert _authority_for(service_path, symbol) is None
    scanned_service = scan_production_writers([root / service_path], anchor=root)
    assert not [site for site in scanned_service if site.symbol in routed and site.table in routed[site.symbol]]
    scanned_repository = scan_production_writers([root / repository_path], anchor=root)
    live = [site for site in scanned_repository if site.symbol in facets]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in facets]
    assert {(site.symbol, site.table, site.operation, site.authority) for site in live} == {
        (symbol, table, operation, "SessionMutationAuthority") for symbol, (table, operation) in facets.items()
    }
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []


def test_named_table_authority_cannot_authorize_a_raw_connection(tmp_path: Path) -> None:
    source = tmp_path / "src/elspeth/web/blobs/service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            class BlobServiceImpl:
                def _fork_cleanup_transaction(self, engine, statement):
                    with engine.begin() as conn:
                        conn.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("BlobServiceImpl._fork_cleanup_transaction", "<sessions-write-connection>", "write_connection"): 1,
            # The parameter statement is its own opaque row (elspeth-a85fb1555b).
            ("BlobServiceImpl._fork_cleanup_transaction", "<unresolved-session-write>", "unknown_opaque"): 1,
        }
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert len(connections) == 1
    assert connections[0].authority == "SessionBlobMutationAuthority"
    assert connections[0].connection_escape is False
    assert connection_authority_violations(connections) == connections


def test_contained_connection_policy_is_exact_and_rejects_raw_escape(tmp_path: Path) -> None:
    source = tmp_path / "src/elspeth/web/coordination/repository.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            class _SessionOperationAuthorityRepository:
                def mutate_fork_creation(self, engine):
                    with engine.begin() as conn:
                        conn.execute(sa.update(models.sessions_table).values(title="contained"))
                    return engine.connect()
            """
        )
    )

    connections = sorted(
        (site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.connection_escape) for site in connections] == [
        ("_SessionOperationAuthorityRepository.mutate_fork_creation", False),
        ("_SessionOperationAuthorityRepository.mutate_fork_creation", True),
    ]
    assert connection_authority_violations(connections) == [connections[1]]


def test_production_scanner_covers_aliases_prebuilt_upsert_bulk_cte_and_raw_sql(tmp_path: Path) -> None:
    source = tmp_path / "writers.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from elspeth.web.sessions.models import chat_messages_table as messages
            from elspeth.web.sessions import models

            alias = messages

            def writer(engine, rows, select_rows):
                prebuilt = sa.update(models.sessions_table).values(title="new")
                engine.begin().execute(prebuilt)
                engine.begin().execute(alias.insert(), rows)
                engine.begin().execute(pg_insert(alias).on_conflict_do_update(index_elements=["id"], set_={"content": "x"}))
                engine.begin().execute(sa.insert(alias).from_select(["id"], select_rows.cte()))
                engine.begin().exec_driver_sql("DELETE FROM chat_messages WHERE id = ?")
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    operations = Counter((site.table, site.operation) for site in sites)
    assert operations[("sessions", "update")] == 1
    assert operations[("chat_messages", "insert")] == 2
    assert operations[("chat_messages", "upsert")] == 1
    assert operations[("chat_messages", "raw_delete_from")] == 1
    assert operations[("<sessions-write-connection>", "write_connection")] == 5


def test_injected_connections_with_unknown_execute_payloads_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "injected_unknown_writes.py"
    source.write_text(
        textwrap.dedent(
            """\
            def execute_unknown(conn, statement):
                conn.execute(statement)

            def executemany_unknown(conn, statement, rows):
                conn.executemany(statement, rows)

            def driver_sql_unknown(conn, statement):
                conn.exec_driver_sql(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("execute_unknown", "<unresolved-session-write>", "unknown_execute"): 1,
            ("executemany_unknown", "<unresolved-session-write>", "unknown_executemany"): 1,
            ("driver_sql_unknown", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
        }
    )


def test_explicit_non_sql_execute_receiver_types_are_not_database_writers(tmp_path: Path) -> None:
    source = tmp_path / "typed_non_sql_execute.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.web.execution.protocol import ExecutionService

            async def injected_service(service: ExecutionService, session_id):
                return await service.execute(session_id)

            async def locally_annotated_service(request, session_id):
                service: ExecutionService = request.app.state.execution_service
                return await service.execute(session_id)

            def dynamic_connection(conn, statement):
                return conn.execute(statement)

            def reassigned_service(service: ExecutionService, replacement, statement):
                service = replacement
                return service.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("dynamic_connection", "<unresolved-session-write>", "unknown_execute"),
        ("reassigned_service", "<unresolved-session-write>", "unknown_execute"),
    ]


_PROBE_SEAM_FIXTURE = """\
from sqlalchemy import Engine, text

from elspeth.web._azure_container_apps_acceptance.controller import SqlReader, SqlSession


class Session(SqlSession):
    def __init__(self, engine: Engine) -> None:
        self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

    def execute_scalar(self, statement: str) -> object:
        return self._connection.execute(text(statement)).scalar()

    def close(self) -> None:
        self._connection.close()


class Reader(SqlReader):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def scalar(self, statement: str, **parameters: object) -> object:
        with self._engine.connect() as connection:
            return connection.execute(text(statement), parameters).scalar()

    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
        with self._engine.connect() as connection:
            return tuple(tuple(row) for row in connection.execute(text(statement), parameters).all())
"""


def _probe_seam_module(root: Path, relative: str, source: str) -> Path:
    module = root / relative
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8")
    return module


def test_acceptance_probe_seam_classes_are_admitted_whole_and_nothing_looser(tmp_path: Path) -> None:
    """P4-D6 family J: the acceptance drivers' ``SqlSession``/``SqlReader`` seams leave the inventory as a class.

    Red-first against ``_ProbeSeamProof``: without it the seam module keeps
    its acquisition, execute and opaque rows. Every loosened form in the
    second module keeps every row of ITS class -- the admission is whole or
    nothing.
    """

    seam = _probe_seam_module(tmp_path, "src/elspeth/web/_azure_container_apps_acceptance/seam.py", _PROBE_SEAM_FIXTURE)
    assert scan_production_writers([seam], anchor=tmp_path) == []

    loosened = _probe_seam_module(
        tmp_path,
        "src/elspeth/web/_azure_container_apps_acceptance/loosened.py",
        textwrap.dedent(
            """\
            from sqlalchemy import Engine, text

            from elspeth.web._azure_container_apps_acceptance.controller import SqlReader, SqlSession


            class LiteralReader(SqlReader):
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def scalar(self, statement: str, **parameters: object) -> object:
                    with self._engine.connect() as connection:
                        return connection.execute(text("SELECT 1")).scalar()

                def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
                    with self._engine.connect() as connection:
                        return tuple(tuple(row) for row in connection.execute(text(statement), parameters).all())


            class LeakingSession(SqlSession):
                def __init__(self, engine: Engine) -> None:
                    self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

                def execute_scalar(self, statement: str) -> object:
                    return self._connection.execute(text(statement)).scalar()

                def connection(self):
                    return self._connection

                def close(self) -> None:
                    self._connection.close()


            class CommittingSession(SqlSession):
                def __init__(self, engine: Engine) -> None:
                    self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

                def execute_scalar(self, statement: str) -> object:
                    value = self._connection.execute(text(statement)).scalar()
                    self._connection.commit()
                    return value

                def close(self) -> None:
                    self._connection.close()


            class LeakingCloseSession(SqlSession):
                def __init__(self, engine: Engine) -> None:
                    self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

                def execute_scalar(self, statement: str) -> object:
                    return self._connection.execute(text(statement)).scalar()

                def close(self):
                    return self._connection


            class UntypedEngineSession(SqlSession):
                def __init__(self, engine) -> None:
                    self._connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")

                def execute_scalar(self, statement: str) -> object:
                    return self._connection.execute(text(statement)).scalar()

                def close(self) -> None:
                    self._connection.close()


            class PlainReader:
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def scalar(self, statement: str, **parameters: object) -> object:
                    with self._engine.connect() as connection:
                        return connection.execute(text(statement), parameters).scalar()


            class ForeignBaseReader(ABC):
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def scalar(self, statement: str, **parameters: object) -> object:
                    with self._engine.connect() as connection:
                        return connection.execute(text(statement), parameters).scalar()

                def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
                    with self._engine.connect() as connection:
                        return tuple(tuple(row) for row in connection.execute(text(statement), parameters).all())
            """
        ).replace("from sqlalchemy import Engine, text\n", "from abc import ABC\n\nfrom sqlalchemy import Engine, text\n"),
    )
    loosened_sites = scan_production_writers([loosened], anchor=tmp_path)
    assert Counter((site.symbol, site.operation) for site in loosened_sites) == Counter(
        {
            ("LiteralReader.scalar", "write_connection"): 1,
            ("LiteralReader.rows", "write_connection"): 1,
            ("LiteralReader.rows", "unknown_opaque"): 1,
            ("LeakingSession.__init__", "write_connection"): 1,
            ("LeakingSession.execute_scalar", "unknown_execute"): 1,
            ("CommittingSession.__init__", "write_connection"): 1,
            ("CommittingSession.execute_scalar", "unknown_execute"): 1,
            ("LeakingCloseSession.__init__", "write_connection"): 1,
            ("LeakingCloseSession.execute_scalar", "unknown_execute"): 1,
            ("UntypedEngineSession.__init__", "write_connection"): 1,
            ("UntypedEngineSession.execute_scalar", "unknown_execute"): 1,
            ("PlainReader.scalar", "write_connection"): 1,
            ("PlainReader.scalar", "unknown_opaque"): 1,
            ("ForeignBaseReader.scalar", "write_connection"): 1,
            ("ForeignBaseReader.scalar", "unknown_opaque"): 1,
            ("ForeignBaseReader.rows", "write_connection"): 1,
            ("ForeignBaseReader.rows", "unknown_opaque"): 1,
        }
    )

    # A module-level ``text`` shadowing SQLAlchemy's breaks the provenance the
    # seam rests on: the otherwise-exact seam keeps its rows.
    shadowed = _probe_seam_module(
        tmp_path,
        "src/elspeth/web/_azure_container_apps_acceptance/shadowed.py",
        _PROBE_SEAM_FIXTURE + "\n\ndef text(statement):\n    return statement\n",
    )
    assert {site.symbol for site in scan_production_writers([shadowed], anchor=tmp_path)} == {
        "Session.__init__",
        "Session.execute_scalar",
        "Reader.scalar",
        "Reader.rows",
    }

    # The same seam outside an acceptance module is no seam.
    elsewhere = _probe_seam_module(tmp_path, "src/elspeth/web/sessions/seam.py", _PROBE_SEAM_FIXTURE)
    assert Counter((site.symbol, site.operation) for site in scan_production_writers([elsewhere], anchor=tmp_path)) == Counter(
        {
            ("Session.__init__", "write_connection"): 1,
            ("Session.execute_scalar", "unknown_execute"): 1,
            ("Reader.scalar", "write_connection"): 1,
            ("Reader.scalar", "unknown_opaque"): 1,
            ("Reader.rows", "write_connection"): 1,
            ("Reader.rows", "unknown_opaque"): 1,
        }
    )


def test_show_setting_reads_use_a_closed_grammar_and_every_loosened_form_is_unresolved(tmp_path: Path) -> None:
    """``SHOW <setting>`` is a read of one server setting (the connection-budget probe); anything more is unresolved."""

    source = tmp_path / "show_grammar.py"
    source.write_text(
        textwrap.dedent(
            """\
            def setting(conn):
                conn.exec_driver_sql("SHOW max_connections")

            def dotted_setting(conn):
                conn.exec_driver_sql("show pg_catalog.max_connections;")

            def bare(conn):
                conn.exec_driver_sql("SHOW")

            def second_statement(conn):
                conn.exec_driver_sql("SHOW max_connections; DROP TABLE sessions")

            def assignment(conn):
                conn.exec_driver_sql("SHOW max_connections = 5")

            def set_not_show(conn):
                conn.exec_driver_sql("SET max_connections TO 5")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("bare", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("second_statement", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("assignment", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("set_not_show", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
        }
    )


def test_chained_and_container_escaped_connections_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "chained_and_container_connections.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def chained_writer(engine):
                engine.connect().execution_options(stream_results=True).execute(
                    sa.update(models.sessions_table).values(title="changed")
                )

            def tuple_escape(engine):
                return (engine.connect(),)

            def list_escape(engine):
                return [engine.begin()]

            def dict_escape(engine):
                return {"connection": engine.connect()}
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter(site.symbol for site in sites if site.operation == "write_connection") == Counter(
        {
            "chained_writer": 1,
            "tuple_escape": 1,
            "list_escape": 1,
            "dict_escape": 1,
        }
    )


def test_expression_and_storage_connection_escapes_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "expression_and_storage_connections.py"
    source.write_text(
        textwrap.dedent(
            """\
            def attribute_store(engine, holder):
                holder.conn = engine.connect()

            def subscript_store(engine, holder):
                holder["conn"] = engine.connect()

            def yield_from_escape(engine):
                yield from (engine.connect(),)

            def comprehension_escape(engine):
                return [engine.connect() for _ in range(1)]

            def conditional_escape(engine, flag):
                return engine.connect() if flag else engine.begin()

            async def awaited_escape(engine):
                return await engine.connect()

            def walrus_escape(engine):
                return (conn := engine.connect())

            def default_escape(conn=engine.connect()):
                return None
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("attribute_store", True): 1,
            ("subscript_store", True): 1,
            ("yield_from_escape", True): 1,
            ("comprehension_escape", True): 1,
            ("conditional_escape", True): 2,
            ("awaited_escape", True): 1,
            ("walrus_escape", True): 1,
            ("default_escape", True): 1,
        }
    )


def test_bare_acquisitions_and_callable_comprehension_escapes_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "bare_and_callable_connections.py"
    source.write_text(
        textwrap.dedent(
            """\
            def unused_assigned(engine):
                conn = engine.connect()

            def unused_expression(engine):
                engine.begin()

            def comprehension_variants(engine):
                return (
                    {engine.connect() for _ in range(1)},
                    (engine.begin() for _ in range(1)),
                    {index: engine.connect() for index in range(1)},
                )

            def lambda_return(engine):
                return lambda: engine.connect()

            def lambda_default(factory=lambda: engine.begin()):
                return factory
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("unused_assigned", False): 1,
            ("unused_expression", False): 1,
            ("comprehension_variants", True): 3,
            ("lambda_return", True): 1,
            ("lambda_default", True): 1,
        }
    )


def test_augmented_complex_target_and_stored_callable_acquisitions_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "additional_connection_flows.py"
    source.write_text(
        textwrap.dedent(
            """\
            def augmented_store(engine, holder):
                holder.connections += [engine.connect()]

            def with_attribute_target(engine, holder, dynamic):
                with engine.connect() as holder.connection:
                    holder.connection.execute(dynamic)

            def with_subscript_target(engine, holder, dynamic):
                with engine.begin() as holder["connection"]:
                    holder["connection"].execute(dynamic)

            def stored_attribute_callable(engine, holder):
                holder.acquire = engine.connect
                holder.acquire()

            def stored_subscript_callable(engine, holder):
                holder["acquire"] = engine.begin
                holder["acquire"]()
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("augmented_store", True): 1,
            ("with_attribute_target", True): 1,
            ("with_subscript_target", True): 1,
            ("stored_attribute_callable", True): 1,
            ("stored_subscript_callable", True): 1,
        }
    )


def test_stored_connection_callable_chains_retain_escape_provenance(tmp_path: Path) -> None:
    source = tmp_path / "stored_connection_callable_chains.py"
    source.write_text(
        textwrap.dedent(
            """\
            def attribute_chain(engine, holder):
                holder.acquire = engine.connect
                invoke = holder.acquire
                return invoke()

            def subscript_chain(engine, holder):
                holder["acquire"] = engine.connect
                invoke = holder["acquire"]
                return invoke()
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("attribute_chain", True): 1,
            ("subscript_chain", True): 1,
        }
    )


def test_conditional_connection_callable_aliases_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "conditional_connection_callable_aliases.py"
    source.write_text(
        textwrap.dedent(
            """\
            def name_conditional(engine, flag, safe):
                acquire = engine.connect if flag else safe
                return acquire()

            def attribute_conditional(engine, holder, flag, safe):
                holder.acquire = engine.connect if flag else safe
                return holder.acquire()

            def subscript_conditional(engine, holder, flag, safe):
                holder["acquire"] = engine.connect if flag else safe
                return holder["acquire"]()
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("name_conditional", True): 1,
            ("attribute_conditional", True): 1,
            ("subscript_conditional", True): 1,
        }
    )


def test_stored_connection_callable_provenance_is_alias_and_reaching_aware(tmp_path: Path) -> None:
    source = tmp_path / "stored_connection_callable_reaching.py"
    source.write_text(
        textwrap.dedent(
            """\
            def aliased_target(engine, holder):
                alias = holder
                alias.acquire = engine.connect
                return holder.acquire()

            def unconditional_overwrite(engine, holder, safe):
                holder.acquire = engine.connect
                holder.acquire = safe
                return holder.acquire()

            def conditional_overwrite(engine, holder, flag, safe):
                holder.acquire = engine.connect
                if flag:
                    holder.acquire = safe
                return holder.acquire()

            def call_before_late_overwrite(engine, holder, safe):
                holder.acquire = engine.connect
                result = holder.acquire()
                holder.acquire = safe
                return result

            def conditional_late_acquisition(engine, holder, flag, safe):
                holder.acquire = safe
                if flag:
                    holder.acquire = engine.connect
                return holder.acquire()
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("aliased_target", True): 1,
            ("conditional_overwrite", True): 1,
            ("call_before_late_overwrite", True): 1,
            ("conditional_late_acquisition", True): 1,
        }
    )


def test_mutating_pragma_is_not_classified_as_an_obvious_read(tmp_path: Path) -> None:
    source = tmp_path / "mutating_pragma.py"
    source.write_text(
        textwrap.dedent(
            """\
            def writer(engine):
                with engine.begin() as conn:
                    conn.exec_driver_sql("PRAGMA user_version = 42")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # Not a read, so an unresolved write of its own -- since P4-D6 even
    # inside the acquired block, not only the acquisition row.
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("writer", "<unresolved-session-write>", "unknown_exec_driver_sql"),
        ("writer", "<sessions-write-connection>", "write_connection"),
    ]


def test_pragma_reads_use_a_closed_allowlist_and_unknown_or_mutating_forms_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "pragma_allowlist.py"
    source.write_text(
        textwrap.dedent(
            """\
            def known_bare_read(conn):
                conn.exec_driver_sql("PRAGMA foreign_keys")

            def known_argument_read(conn):
                conn.exec_driver_sql("PRAGMA table_info('runs')")

            def malformed_argument(conn):
                conn.exec_driver_sql("PRAGMA table_info('runs'))")

            def optimize(conn):
                conn.exec_driver_sql("PRAGMA optimize")

            def vacuum(conn):
                conn.exec_driver_sql("PRAGMA incremental_vacuum")

            def checkpoint(conn):
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

            def unrecognized(conn):
                conn.exec_driver_sql("PRAGMA future_read_maybe")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("optimize", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("vacuum", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("checkpoint", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("unrecognized", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("malformed_argument", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
        }
    )


def test_unknown_statements_on_an_acquired_connection_are_reported_not_swallowed(tmp_path: Path) -> None:
    """P4-D6: an acquisition in scope used to swallow every literal the scanner could not classify.

    A TRUNCATE, an unknown PRAGMA, a session setting inside an authority's own
    ``with engine.begin()`` block moved the acquisition's fingerprint and
    nothing else, so a mechanical re-pin admitted it unread.  Every literal
    the scanner can read and no recogniser admits is an unresolved write now,
    exactly as it is on a bare connection.  A statement with NO readable
    text (a helper's return, a parameter, branch-built SQL) is its own
    ``unknown_opaque`` row too (elspeth-a85fb1555b): it used to ride on the
    acquisition row alone, so re-pinning that row admitted whatever the
    helper now returns, unread.  Its fingerprint is the acquisition's shape
    (statement plus enclosing function), because the surrounding code is
    the only evidence of what it executes.  On a bare connection the same
    statement keeps its ``unknown_<method>`` row; a raw DML literal keeps
    its one raw row and gains no opaque twin.
    """
    source = tmp_path / "acquired_unknowns.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import text
            from sqlalchemy.engine import Engine

            class Authority:
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def helper_return(self):
                    with self._engine.begin() as conn:
                        conn.execute(self._build()).first()

                def parameter(self, statement):
                    with self._engine.begin() as conn:
                        conn.execute(statement)

                def parameter_twin(self, statement):
                    with self._engine.begin() as conn:
                        conn.execute(statement)

                def branch_built(self, dialect):
                    if dialect == "sqlite":
                        statement = self._sqlite()
                    else:
                        statement = self._postgresql()
                    with self._engine.begin() as conn:
                        conn.execute(statement).one()

                def bare_parameter(self, conn, statement):
                    conn.execute(statement)

                def truncate(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql("TRUNCATE identity_roles")

                def unknown_pragma(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql("PRAGMA future_read_maybe")

                def session_setting(self):
                    with self._engine.begin() as conn:
                        conn.execute(text("SET LOCAL statement_timeout = '1000ms'"))

                def bound_name(self):
                    statement = "RESET max_connections"
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql(statement).scalar_one()

                def visible_read(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql("SELECT 1").scalar_one()

                def raw_write_is_its_own_row(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql("DELETE FROM identity_roles")

                def bound_raw_write_is_its_own_row(self):
                    statement = "DELETE FROM identity_roles"
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql(statement)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites if site.operation != "write_connection") == Counter(
        {
            ("Authority.helper_return", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("Authority.parameter", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("Authority.parameter_twin", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("Authority.branch_built", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("Authority.bare_parameter", "<unresolved-session-write>", "unknown_execute"): 1,
            ("Authority.truncate", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("Authority.unknown_pragma", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("Authority.session_setting", "<unresolved-session-write>", "unknown_execute"): 1,
            ("Authority.bound_name", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("Authority.raw_write_is_its_own_row", "identity_roles", "raw_delete_from"): 1,
            ("Authority.bound_raw_write_is_its_own_row", "identity_roles", "raw_delete_from"): 1,
        }
    )
    by_symbol = {(site.symbol, site.operation): site for site in sites}
    # The opaque row is fingerprinted as its acquisition is, never as the bare
    # statement: the textually identical ``conn.execute(statement)`` of
    # ``parameter`` and ``parameter_twin`` are different rows, and neither
    # is the statement's own fingerprint.
    parameter_row = by_symbol[("Authority.parameter", "unknown_opaque")]
    twin_row = by_symbol[("Authority.parameter_twin", "unknown_opaque")]
    assert parameter_row.fingerprint != twin_row.fingerprint
    bare_statement = ast.parse("conn.execute(statement)").body[0]
    assert _statement_fingerprint(bare_statement) not in {parameter_row.fingerprint, twin_row.fingerprint}
    assert Counter(site.symbol for site in sites if site.operation == "write_connection") == Counter(
        {
            "Authority.helper_return": 1,
            "Authority.parameter": 1,
            "Authority.parameter_twin": 1,
            "Authority.branch_built": 1,
            "Authority.truncate": 1,
            "Authority.unknown_pragma": 1,
            "Authority.session_setting": 1,
            "Authority.bound_name": 1,
            "Authority.visible_read": 1,
            "Authority.raw_write_is_its_own_row": 1,
            "Authority.bound_raw_write_is_its_own_row": 1,
        }
    )


def test_explicit_lock_table_is_a_lock_acquisition_and_every_loosened_form_is_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "lock_table.py"
    source.write_text(
        textwrap.dedent(
            """\
            def two_tables(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles, identities IN SHARE ROW EXCLUSIVE MODE")

            def one_table_lower_semicolon(conn):
                conn.exec_driver_sql("lock table identity_roles in access share mode;")

            def guarded(conn):
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("LOCK TABLE identity_roles IN EXCLUSIVE MODE")

            def no_mode(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles")

            def nowait(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles IN SHARE ROW EXCLUSIVE MODE NOWAIT")

            def no_table_keyword(conn):
                conn.exec_driver_sql("LOCK identity_roles IN ACCESS SHARE MODE")

            def only_keyword(conn):
                conn.exec_driver_sql("LOCK TABLE ONLY identity_roles IN SHARE MODE")

            def schema_qualified(conn):
                conn.exec_driver_sql("LOCK TABLE public.identity_roles IN SHARE MODE")

            def second_statement(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles IN SHARE MODE; DELETE FROM identity_roles")

            def unknown_mode(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles IN FROB MODE")

            def extra_semicolons(conn):
                conn.exec_driver_sql("LOCK TABLE identity_roles IN SHARE MODE;;")
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    unresolved = Counter(site.symbol for site in sites if site.operation == "unknown_exec_driver_sql")
    assert unresolved == Counter(
        {
            "no_mode": 1,
            "nowait": 1,
            "no_table_keyword": 1,
            "only_keyword": 1,
            "schema_qualified": 1,
            "unknown_mode": 1,
            "extra_semicolons": 1,
        }
    )
    # The two-statement text carries a raw DELETE: that is its row, once.
    assert Counter((site.symbol, site.table, site.operation) for site in sites if site.operation == "raw_delete_from") == Counter(
        {("second_statement", "identity_roles", "raw_delete_from"): 1}
    )


def test_self_attribute_bound_once_from_a_module_dict_of_literals_is_classified_by_its_texts(tmp_path: Path) -> None:
    """The dialect-keyed clock SQL shape, and every way it can be loosened.

    Before P4-D6 a statement held in ``self.<attr>`` was invisible: a DELETE
    behind ``self._clock_sql`` produced no row.  Now the attribute is
    classified by the literals it can hold, and only under the one shape a
    reader can verify from the module alone.  The refusals are exercised on
    a parameter-received connection, where an unclassified statement has
    always been reported; the acquired-block case proves a DML in the dict
    is reported there too, where it used to ride silently on the acquisition.
    """
    source = tmp_path / "attribute_statements.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy.engine import Engine
            from elsewhere import IMPORTED

            _CLOCK: dict[str, str] = {"postgresql": "SELECT clock_timestamp()", "sqlite": "SELECT CURRENT_TIMESTAMP"}
            _MIXED: dict[str, str] = {"postgresql": "SELECT 1", "sqlite": "DELETE FROM identity_roles"}
            _COMPUTED = {"postgresql": "SELECT 1", "sqlite": IMPORTED}
            _TWICE = {"postgresql": "SELECT 1"}
            _TWICE = {"postgresql": "SELECT 1"}
            _NOT_A_DICT = ("SELECT 1",)

            class Authority:
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine
                    self._clock_sql = _CLOCK[engine.dialect.name]
                    self._mixed_sql = _MIXED[engine.dialect.name]
                    self._computed_sql = _COMPUTED[engine.dialect.name]
                    self._twice_sql = _TWICE[engine.dialect.name]
                    self._tuple_sql = _NOT_A_DICT[0]
                    self._imported_sql = IMPORTED[engine.dialect.name]
                    self._got_sql = _CLOCK.get(engine.dialect.name)
                    self._reassigned_sql = _CLOCK[engine.dialect.name]

                def rebind(self, text):
                    self._reassigned_sql = text

                def clock(self, conn):
                    conn.exec_driver_sql(self._clock_sql).scalar_one()

                def clock_acquired(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql(self._clock_sql).scalar_one()

                def mixed(self, conn):
                    conn.exec_driver_sql(self._mixed_sql).scalar_one()

                def mixed_acquired(self):
                    with self._engine.begin() as conn:
                        conn.exec_driver_sql(self._mixed_sql).scalar_one()

                def computed(self, conn):
                    conn.exec_driver_sql(self._computed_sql).scalar_one()

                def twice(self, conn):
                    conn.exec_driver_sql(self._twice_sql).scalar_one()

                def tuple_bound(self, conn):
                    conn.exec_driver_sql(self._tuple_sql).scalar_one()

                def imported(self, conn):
                    conn.exec_driver_sql(self._imported_sql).scalar_one()

                def got(self, conn):
                    conn.exec_driver_sql(self._got_sql).scalar_one()

                def reassigned(self, conn):
                    conn.exec_driver_sql(self._reassigned_sql).scalar_one()

            def not_a_method(self, conn):
                conn.exec_driver_sql(self._clock_sql).scalar_one()
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    unresolved = Counter(site.symbol for site in sites if site.operation == "unknown_exec_driver_sql")
    assert unresolved == Counter(
        {
            "Authority.mixed": 1,
            "Authority.mixed_acquired": 1,
            "Authority.computed": 1,
            "Authority.twice": 1,
            "Authority.tuple_bound": 1,
            "Authority.imported": 1,
            "Authority.got": 1,
            "Authority.reassigned": 1,
            "not_a_method": 1,
        }
    )
    assert Counter(site.symbol for site in sites if site.operation == "write_connection") == Counter(
        {"Authority.clock_acquired": 1, "Authority.mixed_acquired": 1}
    )


def test_prebuilt_reads_resolve_through_module_constants_and_private_helpers(tmp_path: Path) -> None:
    """The two prebuilt-read shapes the opaque row exposed (elspeth-a85fb1555b), and every loosening.

    A module constant ``_ROWS = select(...)`` executed from a method, and a
    same-module private helper whose every ``return`` is a select, are reads.
    A constant rebound anywhere at module level to something unreadable, a
    helper with a bare ``return``, a generator, a public or imported helper,
    and a helper one of whose returns is a parameter, all stay opaque.
    """
    source = tmp_path / "prebuilt_reads.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import bindparam, select
            from sqlalchemy.engine import Engine
            from elsewhere import imported_helper
            from elspeth.web.sessions.models import sessions_table

            _ROWS = select(sessions_table).where(sessions_table.c.session_id == bindparam("session_id"))
            _ROWS_FOR_UPDATE = _ROWS.with_for_update()
            _REBOUND = select(sessions_table)
            _REBOUND = imported_helper()

            def _rows_for(session_id):
                return select(sessions_table).where(sessions_table.c.session_id == session_id)

            def _rows_or_dynamic(session_id, dynamic):
                if session_id:
                    return select(sessions_table)
                return dynamic

            def _bare_return(session_id):
                if session_id:
                    return select(sessions_table)
                return

            def _generator(session_id):
                yield select(sessions_table)

            def public_rows(session_id):
                return select(sessions_table)

            class Authority:
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def _method_rows(self, session_id):
                    return select(sessions_table).where(sessions_table.c.session_id == session_id)

                def constant(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(_ROWS, {"session_id": session_id}).all()

                def constant_for_update(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(_ROWS_FOR_UPDATE, {"session_id": session_id}).all()

                def rebound_constant(self):
                    with self._engine.begin() as conn:
                        conn.execute(_REBOUND).all()

                def private_helper(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(_rows_for(session_id)).first()

                def method_helper(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(self._method_rows(session_id)).first()

                def helper_with_dynamic_arm(self, session_id, dynamic):
                    with self._engine.begin() as conn:
                        conn.execute(_rows_or_dynamic(session_id, dynamic)).first()

                def helper_with_bare_return(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(_bare_return(session_id)).first()

                def generator_helper(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(_generator(session_id)).first()

                def public_helper(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(public_rows(session_id)).first()

                def imported(self, session_id):
                    with self._engine.begin() as conn:
                        conn.execute(imported_helper(session_id)).first()
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.operation) for site in sites if site.operation != "write_connection") == Counter(
        {
            ("Authority.rebound_constant", "unknown_opaque"): 1,
            ("Authority.helper_with_dynamic_arm", "unknown_opaque"): 1,
            ("Authority.helper_with_bare_return", "unknown_opaque"): 1,
            ("Authority.generator_helper", "unknown_opaque"): 1,
            ("Authority.public_helper", "unknown_opaque"): 1,
            ("Authority.imported", "unknown_opaque"): 1,
        }
    )
    assert Counter(site.symbol for site in sites if site.operation == "write_connection") == Counter(
        {
            "Authority.constant": 1,
            "Authority.constant_for_update": 1,
            "Authority.rebound_constant": 1,
            "Authority.private_helper": 1,
            "Authority.method_helper": 1,
            "Authority.helper_with_dynamic_arm": 1,
            "Authority.helper_with_bare_return": 1,
            "Authority.generator_helper": 1,
            "Authority.public_helper": 1,
            "Authority.imported": 1,
        }
    )


def test_dml_rebound_through_its_own_transparent_chain_links_to_its_execution(tmp_path: Path) -> None:
    """``stmt = stmt.on_conflict_do_update(...)`` is the same upsert, and ``conn.execute(stmt)`` is its execution.

    The rebinding's right-hand side is evaluated before its target is bound,
    so it never reaches itself; the load inside it resolves to the dialect
    arms above and the execution is the arms' execution -- one row per arm,
    no opaque twin (elspeth-a85fb1555b).  A rebinding to something that does
    not reach the arms breaks the chain honestly: the execution is opaque.
    """
    source = tmp_path / "rebound_upsert.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy.engine import Engine
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            from elspeth.web.sessions.models import sessions_table

            class Authority:
                def __init__(self, engine: Engine) -> None:
                    self._engine = engine

                def upsert(self, values):
                    if self._engine.dialect.name == "sqlite":
                        stmt = sqlite_insert(sessions_table).values(**values)
                    else:
                        stmt = postgresql_insert(sessions_table).values(**values)
                    stmt = stmt.on_conflict_do_update(index_elements=["session_id"], set_=values)
                    with self._engine.begin() as conn:
                        conn.execute(stmt.returning(sessions_table.c.session_id)).one()

                def severed(self, values, dynamic):
                    stmt = sqlite_insert(sessions_table).values(**values)
                    stmt = dynamic
                    with self._engine.begin() as conn:
                        conn.execute(stmt)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites if site.operation != "write_connection") == Counter(
        {
            ("Authority.upsert", "sessions", "upsert"): 2,
            ("Authority.severed", "sessions", "insert"): 1,
            ("Authority.severed", "<unresolved-session-write>", "unknown_opaque"): 1,
        }
    )


def test_pragma_read_grammar_rejects_extra_delimiters_and_multiple_statements(tmp_path: Path) -> None:
    source = tmp_path / "pragma_statement_grammar.py"
    source.write_text(
        textwrap.dedent(
            """\
            def valid_bare(conn):
                conn.exec_driver_sql("PRAGMA foreign_keys")

            def valid_bare_semicolon(conn):
                conn.exec_driver_sql("PRAGMA foreign_keys;")

            def valid_argument(conn):
                conn.exec_driver_sql("PRAGMA table_info('runs')")

            def valid_argument_semicolon(conn):
                conn.exec_driver_sql("PRAGMA table_info('runs');")

            def valid_schema_argument(conn):
                conn.exec_driver_sql("PRAGMA main.table_info('runs')")

            def valid_schema_bare(conn):
                conn.exec_driver_sql("PRAGMA main.foreign_keys;")

            def extra_bare_semicolons(conn):
                conn.exec_driver_sql("PRAGMA foreign_keys;;")

            def extra_argument_semicolons(conn):
                conn.exec_driver_sql("PRAGMA table_info('runs');;;")

            def multiple_statements(conn):
                conn.exec_driver_sql("PRAGMA foreign_keys; PRAGMA main.table_info('runs')")

            def invalid_schema_qualification(conn):
                conn.exec_driver_sql("PRAGMA main.extra.foreign_keys")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("extra_bare_semicolons", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("extra_argument_semicolons", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("multiple_statements", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("invalid_schema_qualification", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
        }
    )


def test_reviewed_read_connections_cannot_approve_raw_escape() -> None:
    escaped = replace(_REVIEWED_READ_CONNECTIONS[0], connection_escape=True)
    assert reviewed_read_connection_policy_violations((escaped,)) == [escaped]
    assert reviewed_read_connection_policy_violations(_REVIEWED_READ_CONNECTIONS) == []


def test_escaped_connection_identity_cannot_be_subtracted_by_a_review_manifest() -> None:
    escaped = replace(_REVIEWED_READ_CONNECTIONS[0], connection_escape=True)
    remaining, stale = subtract_reviewed_read_identities((escaped,), (escaped,))
    assert remaining == [escaped]
    assert stale == [escaped]
    assert connection_authority_violations(remaining) == [escaped]


def test_assigned_module_and_destructured_table_aliases_resolve_exactly(tmp_path: Path) -> None:
    source = tmp_path / "assigned_table_aliases.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            model_alias = models
            (session_table,) = (models.sessions_table,)

            def writer(conn):
                conn.execute(sa.update(model_alias.sessions_table).values(title="module alias"))
                conn.execute(sa.delete(session_table))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("sessions", "update"): 1,
            ("sessions", "delete"): 1,
        }
    )


def test_module_prebuilt_and_quoted_raw_sql_writes_resolve_exactly(tmp_path: Path) -> None:
    source = tmp_path / "module_raw_sql.py"
    source.write_text(
        textwrap.dedent(
            """\
            DELETE_SQL = "DELETE FROM sessions WHERE id = :id"
            QUOTED_SQL = 'UPDATE "main"."sessions" SET title = :title'
            REPLACE_SQL = "INSERT OR REPLACE INTO sessions (id) VALUES (:id)"

            def writer(conn):
                conn.exec_driver_sql(DELETE_SQL)
                conn.exec_driver_sql(QUOTED_SQL)
                conn.exec_driver_sql(REPLACE_SQL)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("sessions", "raw_delete_from"): 1,
            ("sessions", "raw_update"): 1,
            ("sessions", "raw_insert_or_replace_into"): 1,
        }
    )


def test_split_prebuilt_upsert_modifiers_change_exact_identity(tmp_path: Path) -> None:
    source = tmp_path / "split_prebuilt_upsert.py"

    def write_source(value: str) -> None:
        source.write_text(
            textwrap.dedent(
                f"""\
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from elspeth.web.sessions import models

                def writer(conn):
                    base = pg_insert(models.sessions_table)
                    statement = base.on_conflict_do_update(index_elements=["id"], set_={{"title": "{value}"}})
                    conn.execute(statement)
                """
            )
        )

    write_source("first")
    first = [site for site in scan_production_writers([source], anchor=tmp_path) if site.table == "sessions"]
    write_source("second")
    second = [site for site in scan_production_writers([source], anchor=tmp_path) if site.table == "sessions"]

    assert [(site.operation, site.ordinal) for site in first] == [("upsert", 1)]
    assert [(site.operation, site.ordinal) for site in second] == [("upsert", 1)]
    assert first[0].fingerprint != second[0].fingerprint
    assert inventory_drift(second, first) == (second, first)


def test_production_scanner_inventories_read_and_write_connections(tmp_path: Path) -> None:
    source = tmp_path / "connection_use.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def reader(engine):
                with engine.connect() as conn:
                    return conn.execute(sa.select(models.sessions_table)).all()

            def writer(engine):
                with engine.begin() as conn:
                    conn.execute(sa.update(models.sessions_table).values(title="new"))
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = sorted(
        (site for site in sites if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.line) for site in acquisitions] == [
        ("reader", 5),
        ("writer", 9),
    ]


def test_read_only_select_requires_imported_sqlalchemy_provenance(tmp_path: Path) -> None:
    source = tmp_path / "select_provenance.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import select as sqlalchemy_select

            def direct_reader(conn, table):
                return conn.execute(sqlalchemy_select(table)).all()

            def module_reader(conn, table):
                return conn.execute(sa.select(table)).all()

            def chained_reader(conn, table):
                return conn.execute(sa.select(table).where(table.c.id == "id")).all()

            def unknown_factory(conn, factory, table):
                return conn.execute(factory.select(table)).all()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("unknown_factory", "<unresolved-session-write>", "unknown_execute"),
    ]


def test_read_only_text_requires_imported_sqlalchemy_provenance(tmp_path: Path) -> None:
    positive = tmp_path / "text_readers.py"
    positive.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import text as sqlalchemy_text

            def direct_reader(conn):
                return conn.execute(sqlalchemy_text("SELECT 1")).scalar()

            def module_reader(conn):
                return conn.execute(sa.text("SELECT 1")).scalar()
            """
        )
    )
    assert scan_production_writers([positive], anchor=tmp_path) == []

    shadowed = tmp_path / "shadowed_text.py"
    shadowed.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import text

            sa = provider
            text = factory

            def module_alias_shadow(conn):
                return conn.execute(sa.text("SELECT 1")).scalar()

            def direct_alias_shadow(conn):
                return conn.execute(text("SELECT 1")).scalar()

            def parameter_shadow(conn, text):
                return conn.execute(text("SELECT 1")).scalar()
            """
        )
    )
    sites = scan_production_writers([shadowed], anchor=tmp_path)
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("module_alias_shadow", "<unresolved-session-write>", "unknown_execute"),
        ("direct_alias_shadow", "<unresolved-session-write>", "unknown_execute"),
        ("parameter_shadow", "<unresolved-session-write>", "unknown_execute"),
    ]


def test_module_rebinding_after_nested_use_invalidates_sqlalchemy_provenance(tmp_path: Path) -> None:
    source = tmp_path / "post_definition_sqlalchemy_shadow.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import text

            def module_alias_reader(engine):
                with engine.connect() as conn:
                    return conn.execute(sa.text("SELECT 1")).scalar()

            def direct_alias_reader(engine):
                with engine.connect() as conn:
                    return conn.execute(text("SELECT 1")).scalar()

            sa = provider
            text = provider
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # With ``sa`` / ``text`` rebound after the readers, ``text("SELECT 1")``
    # is no longer provably a read: each statement is an opaque row of its
    # own beside its acquisition (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.operation, site.line) for site in sites) == [
        ("direct_alias_reader", "unknown_opaque", 10),
        ("direct_alias_reader", "write_connection", 9),
        ("module_alias_reader", "unknown_opaque", 6),
        ("module_alias_reader", "write_connection", 5),
    ]


def test_import_provenance_is_invalidated_by_module_and_function_shadowing(tmp_path: Path) -> None:
    source = tmp_path / "shadowed_imports.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            import sqlalchemy.dialects.postgresql as pg
            from elspeth.web.sessions import models

            sa = factory
            sqlite3 = provider
            models = other_models
            pg = dialect_provider

            def module_select(engine, table):
                with engine.connect() as conn:
                    return conn.execute(sa.select(table)).all()

            def module_sqlite():
                return sqlite3.connect(":memory:")

            def function_select(engine, sa, table):
                with engine.connect() as conn:
                    return conn.execute(sa.select(table)).all()

            def function_sqlite(sqlite3):
                return sqlite3.connect(":memory:")

            def shadowed_dml(conn, models, pg):
                conn.execute(sa.update(models.sessions_table))
                conn.execute(pg.insert(models.sessions_table))
            """
        )
    )

    sites = sorted(scan_production_writers([source], anchor=tmp_path), key=lambda site: site.line)
    # A shadowed ``sa.select`` is not a proven read: the statement is an
    # opaque row beside its acquisition (elspeth-a85fb1555b).
    assert [(site.symbol, site.table, site.line) for site in sites] == [
        ("module_select", "<sessions-write-connection>", 12),
        ("module_select", "<unresolved-session-write>", 13),
        ("module_sqlite", "<sessions-write-connection>", 16),
        ("function_select", "<sessions-write-connection>", 19),
        ("function_select", "<unresolved-session-write>", 20),
        ("function_sqlite", "<sessions-write-connection>", 23),
        ("shadowed_dml", "<unresolved-session-write>", 26),
        ("shadowed_dml", "<unresolved-session-write>", 27),
    ]


def test_import_shadowing_reopens_external_manifest_review(tmp_path: Path) -> None:
    source = tmp_path / "sqlite_shadow_drift.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            provider = object()

            def connection():
                return sqlite3.connect(":memory:")
            """
        )
    )
    reviewed_external = scan_production_writers([source], anchor=tmp_path)
    assert [site.table for site in reviewed_external] == ["<non-session-write-connection>"]

    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            sqlite3 = provider

            def connection():
                return sqlite3.connect(":memory:")
            """
        )
    )
    live = scan_production_writers([source], anchor=tmp_path)
    assert [site.table for site in live] == ["<sessions-write-connection>"]
    assert inventory_drift(live, reviewed_external) == (live, reviewed_external)


def test_imported_member_mutation_invalidates_qualified_provenance(tmp_path: Path) -> None:
    rebound = tmp_path / "rebound_import_members.py"
    rebound.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            from elspeth.core.landscape import schema
            from elspeth.web.sessions.engine import create_session_engine
            from elspeth.web.sessions.models import sessions_table

            schema.runs_table = sessions_table
            sqlite3.connect = create_session_engine

            def writer(conn):
                conn.execute(sa.update(schema.runs_table))

            def connection(url):
                return sqlite3.connect(url)
            """
        )
    )
    deleted = tmp_path / "deleted_import_member.py"
    deleted.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            del sqlite3.connect

            def connection(url):
                return sqlite3.connect(url)
            """
        )
    )
    rebound_twice = tmp_path / "twice_rebound_import_member.py"
    rebound_twice.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            from elspeth.core.landscape.database import begin_write

            sqlite3.connect = begin_write
            sqlite3.connect = provider

            def connection(url):
                return sqlite3.connect(url)
            """
        )
    )

    sites = scan_production_writers([rebound, deleted, rebound_twice], anchor=tmp_path)
    assert Counter((site.path, site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("rebound_import_members.py", "writer", "sessions", "update"): 1,
            (
                "rebound_import_members.py",
                "connection",
                "<sessions-write-connection>",
                "write_connection",
            ): 1,
            (
                "deleted_import_member.py",
                "connection",
                "<sessions-write-connection>",
                "write_connection",
            ): 1,
            (
                "twice_rebound_import_member.py",
                "connection",
                "<sessions-write-connection>",
                "write_connection",
            ): 1,
        }
    )


def test_complex_imported_member_store_targets_invalidate_provenance(tmp_path: Path) -> None:
    source = tmp_path / "complex_import_member_targets.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            from elspeth.core.landscape import schema

            def with_target(conn, provider):
                with provider() as schema.runs_table:
                    pass
                conn.execute(sa.update(schema.runs_table))

            def for_target(providers, url):
                for sqlite3.connect in providers:
                    pass
                return sqlite3.connect(url)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("with_target", "<unresolved-session-write>", "unknown_execute"): 1,
            ("for_target", "<sessions-write-connection>", "write_connection"): 1,
        }
    )


def test_imported_member_provenance_uses_reaching_order_and_exact_restoration(tmp_path: Path) -> None:
    restored = tmp_path / "restored_import_member.py"
    restored.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            original_connect = sqlite3.connect
            sqlite3.connect = provider
            sqlite3.connect = original_connect

            def connection(url):
                return sqlite3.connect(url)
            """
        )
    )
    later = tmp_path / "later_import_member_mutation.py"
    later.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            def connection(url):
                return sqlite3.connect(url)

            sqlite3.connect = provider
            """
        )
    )
    direct_before_later = tmp_path / "direct_use_before_later_mutation.py"
    direct_before_later.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            connection = sqlite3.connect(":memory:")
            sqlite3.connect = provider
            """
        )
    )
    conditional = tmp_path / "conditional_import_member_mutation.py"
    conditional.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            if flag:
                sqlite3.connect = provider

            def connection(url):
                return sqlite3.connect(url)
            """
        )
    )

    sites = scan_production_writers(
        [restored, later, direct_before_later, conditional],
        anchor=tmp_path,
    )
    assert {(site.path, site.table) for site in sites if site.operation == "write_connection"} == {
        ("restored_import_member.py", "<non-session-write-connection>"),
        ("later_import_member_mutation.py", "<sessions-write-connection>"),
        ("direct_use_before_later_mutation.py", "<non-session-write-connection>"),
        ("conditional_import_member_mutation.py", "<sessions-write-connection>"),
    }


def test_module_non_assignment_rebindings_invalidate_import_provenance_without_comprehension_leakage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module_non_assignment_rebindings.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import select as aug_select
            from sqlalchemy import select as conditional_aug_select
            from sqlalchemy import select as for_select
            from sqlalchemy import select as conditional_for_select
            from sqlalchemy import select as with_select
            from sqlalchemy import select as conditional_with_select
            from sqlalchemy import select as deleted_select
            from sqlalchemy import select as conditional_deleted_select
            from sqlalchemy import select as restored_select
            from sqlalchemy import select as comprehension_select
            from elspeth.core.landscape.schema import runs_table

            aug_select += replacement
            if condition:
                conditional_aug_select += replacement

            for for_select in providers:
                pass
            if condition:
                for conditional_for_select in providers:
                    pass

            with provider() as with_select:
                pass
            if condition:
                with provider() as conditional_with_select:
                    pass

            del deleted_select
            if condition:
                del conditional_deleted_select

            restored_select += replacement
            from sqlalchemy import select as restored_select

            [comprehension_select for comprehension_select in providers]

            def after_aug(conn):
                conn.execute(aug_select(runs_table))

            def after_conditional_aug(conn):
                conn.execute(conditional_aug_select(runs_table))

            def after_for(conn):
                conn.execute(for_select(runs_table))

            def after_conditional_for(conn):
                conn.execute(conditional_for_select(runs_table))

            def after_with(conn):
                conn.execute(with_select(runs_table))

            def after_conditional_with(conn):
                conn.execute(conditional_with_select(runs_table))

            def after_delete(conn):
                conn.execute(deleted_select(runs_table))

            def after_conditional_delete(conn):
                conn.execute(conditional_deleted_select(runs_table))

            def after_exact_restore(conn):
                conn.execute(restored_select(runs_table))

            def after_comprehension(conn):
                conn.execute(comprehension_select(runs_table))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            (symbol, "<unresolved-session-write>", "unknown_execute"): 1
            for symbol in (
                "after_aug",
                "after_conditional_aug",
                "after_for",
                "after_conditional_for",
                "after_with",
                "after_conditional_with",
                "after_delete",
                "after_conditional_delete",
            )
        }
    )


def test_comprehension_targets_bind_only_inside_their_ordered_implicit_scope(tmp_path: Path) -> None:
    source = tmp_path / "comprehension_binding_regions.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import select
            from elspeth.core.landscape.schema import runs_table

            def list_body(conn, providers):
                return [conn.execute(select(runs_table)) for select in providers]

            def set_body(conn, providers):
                return {conn.execute(select(runs_table)) for select in providers}

            def dict_body(conn, providers):
                return {conn.execute(select(runs_table)): select for select in providers}

            def generator_body(conn, providers):
                return (conn.execute(select(runs_table)) for select in providers)

            def statement_alias(conn, providers):
                statement = select(runs_table)
                return [conn.execute(statement) for statement in providers]

            def target_if(conn, providers):
                return [select for select in providers if conn.execute(select(runs_table))]

            def later_generator_iterable(conn, providers):
                return [value for select in providers for value in conn.execute(select(runs_table))]

            def later_generator_if(conn, providers, values):
                return [
                    value
                    for select in providers
                    for value in values
                    if conn.execute(select(runs_table))
                ]

            def nested_comprehension(conn, providers, inner_providers):
                return [
                    [conn.execute(select(runs_table)) for inner in inner_providers]
                    for select in providers
                ]

            def own_iterable_uses_outer_import(conn, values):
                return [value for select in conn.execute(select(runs_table)) for value in values]

            def target_does_not_leak(conn, providers):
                [select for select in providers]
                return conn.execute(select(runs_table))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            (symbol, "<unresolved-session-write>", "unknown_execute"): 1
            for symbol in (
                "list_body",
                "set_body",
                "dict_body",
                "generator_body",
                "statement_alias",
                "target_if",
                "later_generator_iterable",
                "later_generator_if",
                "nested_comprehension",
            )
        }
    )


def test_read_only_statement_requires_all_reaching_branches_to_be_selects(tmp_path: Path) -> None:
    source = tmp_path / "branching_statements.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa

            def mixed_branches(conn, condition, dynamic, table):
                if condition:
                    statement = sa.select(table)
                else:
                    statement = dynamic
                return conn.execute(statement).all()

            def select_branches(conn, condition, table):
                if condition:
                    statement = sa.select(table)
                else:
                    statement = sa.select(table).where(table.c.id == "id")
                return conn.execute(statement).all()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("mixed_branches", "<unresolved-session-write>", "unknown_execute"),
    ]


def test_conditional_statement_provenance_merges_every_branch_and_unknown_poisons(tmp_path: Path) -> None:
    source = tmp_path / "conditional_statement_domain.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.core.landscape.schema import runs_table

            def conditional_writer(conn, flag, dynamic):
                conn.execute(sa.update(runs_table) if flag else dynamic)

            def nested_wrapper_writer(conn, outer, inner, dynamic):
                conn.execute((sa.update(runs_table) if inner else dynamic) if outer else sa.update(runs_table))
            """
        )
    )


def test_wrapped_conditional_statement_evidence_preserves_unknown_paths(tmp_path: Path) -> None:
    source = tmp_path / "wrapped_conditional_statement_domain.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.core.landscape.schema import runs_table

            def wrapper(statement):
                return statement

            def call_wrapper(engine, flag, dynamic):
                with engine.connect() as conn:
                    conn.execute(wrapper(sa.update(runs_table) if flag else dynamic))

            def method_wrapper(engine, flag, dynamic):
                with engine.connect() as conn:
                    conn.execute((sa.update(runs_table) if flag else dynamic).execution_options())
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("call_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("call_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("method_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("method_wrapper", "<sessions-write-connection>", "write_connection"): 1,
        }
    )


def test_wrapped_boolean_statement_evidence_preserves_unknown_paths(tmp_path: Path) -> None:
    source = tmp_path / "wrapped_boolean_statement_domain.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions.models import runs_table

            def wrapper(statement):
                return statement

            def call_wrapper(engine, dynamic):
                with engine.connect() as conn:
                    conn.execute(wrapper(sa.update(runs_table) or dynamic))

            def method_wrapper(engine, dynamic):
                with engine.connect() as conn:
                    conn.execute((sa.update(runs_table) or dynamic).execution_options())

            def nested_wrapper(engine, first, second):
                with engine.connect() as conn:
                    conn.execute(wrapper(sa.update(runs_table) or (first and second)))

            def known_wrapper(engine):
                with engine.connect() as conn:
                    conn.execute(wrapper(sa.update(runs_table)))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("call_wrapper", "runs", "update"): 1,
            ("call_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("call_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("method_wrapper", "runs", "update"): 1,
            ("method_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("method_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("nested_wrapper", "runs", "update"): 1,
            ("nested_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("nested_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("known_wrapper", "runs", "update"): 1,
            ("known_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("known_wrapper", "<sessions-write-connection>", "write_connection"): 1,
        }
    )


def test_sibling_branch_bindings_do_not_reach_each_other_but_post_branch_bindings_remain_conservative(
    tmp_path: Path,
) -> None:
    source = tmp_path / "branch_feasibility.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa

            def sibling_use(engine, condition, dynamic, table):
                conn = engine.connect()
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    conn.execute(statement)

            def post_branch_use(engine, condition, dynamic, table):
                conn = engine.connect()
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                conn.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # ``dynamic`` reaches both executions (the select binding sits in a
    # sibling branch, or is only one of the reaching bindings), so each is an
    # opaque row of its own (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.operation, site.line) for site in sites) == [
        ("post_branch_use", "unknown_opaque", 16),
        ("post_branch_use", "write_connection", 12),
        ("sibling_use", "unknown_opaque", 9),
        ("sibling_use", "write_connection", 4),
    ]


def test_no_return_branch_termination_requires_unshadowed_definition_provenance(tmp_path: Path) -> None:
    source = tmp_path / "no_return_provenance.py"
    source.write_text(
        textwrap.dedent(
            """\
            from typing import NoReturn
            import sqlalchemy as sa

            def trigger() -> NoReturn:
                raise RuntimeError

            def proven_reader(conn, condition, dynamic, table):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    trigger()
                conn.execute(statement)

            def shadowed_trigger(conn, condition, dynamic, table, trigger):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    trigger()
                conn.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
        ("shadowed_trigger", "<unresolved-session-write>", "unknown_execute"),
    ]


def test_method_bare_name_lookup_skips_class_namespace(tmp_path: Path) -> None:
    source = tmp_path / "method_scope.py"
    source.write_text(
        textwrap.dedent(
            """\
            sa = provider

            class Reader:
                import sqlalchemy as sa

                def read(self, engine):
                    with engine.connect() as conn:
                        return conn.execute(sa.text("SELECT 1")).scalar()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # The opaque row IS the proof: with the class namespace skipped, ``sa``
    # is the module's ``provider`` and ``sa.text("SELECT 1")`` is not a
    # read.  Before elspeth-a85fb1555b the swallowed statement made this
    # assertion pass whether or not the namespace was skipped.
    assert sorted((site.symbol, site.operation, site.line) for site in sites) == [
        ("Reader.read", "unknown_opaque", 8),
        ("Reader.read", "write_connection", 7),
    ]


def test_relative_session_model_imports_resolve_from_collector_package(tmp_path: Path) -> None:
    source = tmp_path / "src" / "elspeth" / "web" / "sessions" / "relative_writer.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            from . import models
            from .models import chat_messages_table as messages

            def writer(conn):
                conn.execute(models.sessions_table.update().values(title="new"))
                conn.execute(messages.insert().values(content="hello"))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    operations = Counter((site.table, site.operation) for site in sites)
    assert operations == Counter(
        {
            ("sessions", "update"): 1,
            ("chat_messages", "insert"): 1,
        }
    )


def test_connection_acquisition_resolves_through_enclosing_function_closure(tmp_path: Path) -> None:
    source = tmp_path / "closure_connection.py"
    source.write_text(
        textwrap.dedent(
            """\
            def writer(engine, dynamic):
                conn = engine.connect()

                def inner():
                    conn.execute(dynamic)

                inner()

            def locally_shadowed(engine, dynamic, provider):
                conn = engine.connect()

                def inner():
                    conn = provider
                    conn.execute(dynamic)

                inner()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # ``writer.inner`` executes ``dynamic`` on the closure's acquisition:
    # its own opaque row, attributed to the inner function (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.line) for site in sites) == [
        ("locally_shadowed", 10),
        ("locally_shadowed.inner", 14),
        ("writer", 2),
        ("writer.inner", 5),
    ]


def test_connection_identity_drifts_when_sibling_write_capable_use_is_added(tmp_path: Path) -> None:
    cases = {
        "sessions_connection.py": (
            "",
            "conn = engine.connect()",
            "<sessions-write-connection>",
        ),
        "sqlite_connection.py": (
            "import sqlite3\n\n",
            'conn = sqlite3.connect(":memory:")',
            "<non-session-write-connection>",
        ),
    }
    for filename, (prefix, acquisition, table) in cases.items():
        source = tmp_path / filename
        source.write_text(prefix + "def connection(engine, dynamic):\n" + f"    {acquisition}\n" + "    return conn\n")
        reviewed = scan_production_writers([source], anchor=tmp_path)
        assert [(site.table, site.operation) for site in reviewed] == [
            (table, "write_connection"),
        ]

        source.write_text(
            prefix + "def connection(engine, dynamic):\n" + f"    {acquisition}\n" + "    conn.execute(dynamic)\n" + "    return conn\n"
        )
        live = scan_production_writers([source], anchor=tmp_path)
        # The added ``conn.execute(dynamic)`` is an opaque row of its own and
        # poisons the connection's domain (elspeth-a85fb1555b).
        assert sorted((site.table, site.operation) for site in live) == [
            ("<sessions-write-connection>", "write_connection"),
            ("<unresolved-session-write>", "unknown_opaque"),
        ]
        assert inventory_drift(live, reviewed) == (live, reviewed)


def test_no_return_annotations_require_live_typing_import_provenance(tmp_path: Path) -> None:
    source = tmp_path / "no_return_annotation_provenance.py"
    source.write_text(
        textwrap.dedent(
            """\
            import typing as t
            import sqlalchemy as sa
            from typing import NoReturn as NR

            NoReturn = object()
            fake = object()

            def direct_trigger() -> NR:
                raise RuntimeError

            def qualified_trigger() -> t.Never:
                raise RuntimeError

            def rebound_trigger() -> NoReturn:
                return None

            def arbitrary_trigger() -> fake.NoReturn:
                return None

            def direct_reader(conn, condition, dynamic, table):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    direct_trigger()
                conn.execute(statement)

            def qualified_reader(conn, condition, dynamic, table):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    qualified_trigger()
                conn.execute(statement)

            def rebound_reader(conn, condition, dynamic, table):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    rebound_trigger()
                conn.execute(statement)

            def arbitrary_reader(conn, condition, dynamic, table):
                statement = dynamic
                if condition:
                    statement = sa.select(table)
                else:
                    arbitrary_trigger()
                conn.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert sorted(site.symbol for site in sites) == [
        "arbitrary_reader",
        "rebound_reader",
    ]


def test_nested_method_bare_name_lookup_skips_all_class_namespaces(tmp_path: Path) -> None:
    source = tmp_path / "nested_method_scope.py"
    source.write_text(
        textwrap.dedent(
            """\
            sa = provider

            class Outer:
                import sqlalchemy as sa

                class Inner:
                    def read(self, engine):
                        with engine.connect() as conn:
                            return conn.execute(sa.text("SELECT 1")).scalar()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # As in the single-class case: the opaque row proves every class
    # namespace was skipped (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.table, site.line) for site in sites) == [
        ("Outer.Inner.read", "<sessions-write-connection>", 8),
        ("Outer.Inner.read", "<unresolved-session-write>", 9),
    ]


def test_connection_callable_alias_resolves_through_enclosing_function_scope(tmp_path: Path) -> None:
    source = tmp_path / "closure_acquisition_callable.py"
    source.write_text(
        textwrap.dedent(
            """\
            def writer(engine, dynamic):
                acquire = engine.connect

                def inner():
                    conn = acquire()
                    conn.execute(dynamic)

                inner()

            def shadowed(engine, dynamic, provider):
                acquire = engine.connect

                def inner(acquire):
                    conn = acquire()
                    conn.execute(dynamic)

                inner(provider)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # ``writer.inner`` acquires through the closure alias and executes
    # ``dynamic`` on it: an opaque row beside the acquisition; ``shadowed.inner``
    # executes on an unknown receiver and keeps its ``unknown_execute`` row.
    assert sorted((site.symbol, site.operation, site.line) for site in sites) == [
        ("shadowed.inner", "unknown_execute", 15),
        ("writer.inner", "unknown_opaque", 6),
        ("writer.inner", "write_connection", 5),
    ]


def test_production_scanner_flags_connection_forwarding_and_unknown_statements(tmp_path: Path) -> None:
    source = tmp_path / "hidden_writers.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def imported_or_local_helper(conn):
                pass

            def forwarded(engine):
                with engine.begin() as conn:
                    imported_or_local_helper(conn)

            def prebuilt(engine, statement):
                with engine.connect() as conn:
                    conn.execute(statement)

            def dynamic_sql(engine, sql):
                with engine.connect() as conn:
                    conn.exec_driver_sql(sql)

            def obvious_reader(engine, table):
                with engine.connect() as conn:
                    return conn.execute(sa.select(table)).all()
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = [site for site in sites if site.operation == "write_connection"]
    assert [(site.symbol, site.line) for site in acquisitions] == [
        ("forwarded", 8),
        ("prebuilt", 12),
        ("dynamic_sql", 16),
        ("obvious_reader", 20),
    ]


def test_production_scanner_flags_wrapper_escapes_and_keyword_forwarding(tmp_path: Path) -> None:
    source = tmp_path / "wrapper_flows.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def helper(*, connection):
                pass

            def yielded(engine):
                with engine.begin() as conn:
                    yield conn
                with engine.begin() as conn:
                    yield conn

            def returned(engine):
                conn = engine.connect()
                return conn

            def yielded_direct(engine):
                yield engine.begin()

            def returned_direct(engine):
                return engine.connect()

            def keyword_forwarded(engine):
                with engine.begin() as conn:
                    helper(connection=conn)

            def obvious_reader(engine):
                with engine.connect() as conn:
                    return conn.execute(sa.select(models.sessions_table)).all()
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = sorted(
        (site for site in sites if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.line, site.ordinal) for site in acquisitions] == [
        ("yielded", 8, 1),
        ("yielded", 10, 2),
        ("returned", 14, 1),
        ("yielded_direct", 18, 1),
        ("returned_direct", 21, 1),
        ("keyword_forwarded", 24, 1),
        ("obvious_reader", 28, 1),
    ]


def test_parameter_fed_contextmanager_wrapper_acquisitions_are_attributed_to_callers(tmp_path: Path) -> None:
    """A wrapper that acquires from its own parameter is transparent: the caller is the boundary.

    The domain comes from the caller's argument, so one wrapper serves a
    Landscape engine, a Sessions engine and an unknown engine differently, and
    the wrapper itself no longer reports the acquisition it hands out.
    """

    source = tmp_path / "wrapper_callers.py"
    source.write_text(
        textwrap.dedent(
            """\
            from contextlib import contextmanager
            from sqlalchemy import update
            from elspeth.core.landscape.database import Tier1Engine
            from elspeth.core.landscape.schema import runs_table
            from elspeth.web.sessions.engine import create_session_engine

            @contextmanager
            def phase(engine):
                with engine.begin() as conn:
                    yield conn

            def landscape_caller(engine: Tier1Engine):
                with phase(engine) as conn:
                    conn.execute(update(runs_table).values(status="failed"))

            def sessions_caller(url, dynamic):
                engine = create_session_engine(url)
                with phase(engine) as conn:
                    conn.execute(dynamic)

            def unknown_caller(engine, dynamic):
                with phase(engine) as conn:
                    conn.execute(dynamic)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation, site.connection_escape) for site in sites) == Counter(
        {
            ("landscape_caller", "<non-session-write-connection>", "write_connection", False): 1,
            ("sessions_caller", "<sessions-write-connection>", "write_connection", False): 1,
            ("unknown_caller", "<sessions-write-connection>", "write_connection", False): 1,
            # ``dynamic`` on a Sessions or unknown engine is an opaque row of
            # the caller's own (elspeth-a85fb1555b); the Landscape caller's
            # update is classified by its connection's proven domain.
            ("sessions_caller", "<unresolved-session-write>", "unknown_opaque", False): 1,
            ("unknown_caller", "<unresolved-session-write>", "unknown_opaque", False): 1,
        }
    )


def test_self_fed_contextmanager_wrapper_remains_the_reported_boundary(tmp_path: Path) -> None:
    """A wrapper that acquires from its own state is the boundary; callers resolve through it, not around it.

    The acquisition is reported ONCE, in the wrapper's name, never re-homed to a
    caller. Whether it is an escape is the callers' doing (P4-D6 step 5, hub
    ruling condition 2): here the one caller is a same-class method that only
    executes on the with-target, so the wrapper is contained; a caller that
    leaks it, a foreign caller, or an escape inside the wrapper keeps the
    flag (``test_wrapper_containment_is_all_callers_and_same_class_only``).
    """

    source = tmp_path / "self_fed_wrapper.py"
    source.write_text(
        textwrap.dedent(
            """\
            from contextlib import contextmanager

            class Store:
                def __init__(self, engine):
                    self._engine = engine

                @contextmanager
                def _begin(self):
                    with self._engine.begin() as conn:
                        yield conn

                def write(self, dynamic):
                    with self._begin() as conn:
                        conn.execute(dynamic)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    # The acquisition stays the wrapper's; the opaque statement is the
    # caller's own row (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.table, site.operation, site.connection_escape) for site in sites) == [
        ("Store._begin", "<sessions-write-connection>", "write_connection", False),
        ("Store.write", "<unresolved-session-write>", "unknown_opaque", False),
    ]


def test_wrapper_with_disagreeing_yield_arms_stays_unresolved(tmp_path: Path) -> None:
    """Q7 ruling, case 1: one arm yields the sqlite3 store, the other a Sessions engine connection.

    The caller's raw auth-table write is UNRESOLVED, not non-session: a later
    branch yielding a Sessions connection must never reclassify the store's
    writes from a change nowhere near them.
    """

    source = tmp_path / "disagreeing_arms.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            from contextlib import contextmanager
            from elspeth.web.sessions.engine import create_session_engine

            class Store:
                def _get_conn(self):
                    return sqlite3.connect(":memory:")

                @contextmanager
                def _connect(self, *, shared: bool):
                    if not shared:
                        yield self._get_conn()
                        return
                    engine = create_session_engine("sqlite://")
                    with engine.begin() as conn:
                        yield conn

                def write(self):
                    with self._connect(shared=False) as conn:
                        conn.execute("INSERT INTO users (id) VALUES (1)")
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation, site.connection_escape) for site in sites) == Counter(
        {
            ("Store._get_conn", "<non-session-write-connection>", "write_connection", True): 1,
            ("Store._connect", "<sessions-write-connection>", "write_connection", True): 1,
            ("Store.write", "<unresolved-session-write>", "unknown_execute", False): 1,
        }
    )


def test_wrapper_with_an_unprovable_second_arm_stays_unresolved(tmp_path: Path) -> None:
    """Q7 ruling, case 2: a yield the scanner cannot resolve gives the caller nothing to inherit."""

    source = tmp_path / "unresolved_wrapper_yield.py"
    source.write_text(
        textwrap.dedent(
            """\
            from contextlib import contextmanager
            from elspeth.core.landscape.database import Tier1Engine

            @contextmanager
            def opaque(provider):
                conn = provider.open()
                yield conn

            def opaque_caller(provider, dynamic):
                with opaque(provider) as conn:
                    conn.execute(dynamic)

            @contextmanager
            def mixed(engine, held):
                if held is None:
                    with engine.begin() as conn:
                        yield conn
                    return
                with held.begin():
                    yield held

            def mixed_caller(engine: Tier1Engine, held, dynamic):
                with mixed(engine, held) as conn:
                    conn.execute(dynamic)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("opaque_caller", "<unresolved-session-write>", "unknown_execute"): 1,
            # The proven arm is attributed to the caller (fail-closed as a
            # Sessions acquisition); the unproven arm keeps the dynamic
            # statement unresolved rather than classified.
            ("mixed_caller", "<sessions-write-connection>", "write_connection"): 1,
            ("mixed_caller", "<unresolved-session-write>", "unknown_execute"): 1,
        }
    )


def test_stored_wrapper_connection_is_still_an_escape(tmp_path: Path) -> None:
    source = tmp_path / "stored_wrapper_connection.py"
    source.write_text(
        textwrap.dedent(
            """\
            from contextlib import contextmanager

            @contextmanager
            def phase(engine):
                with engine.begin() as conn:
                    yield conn

            class Holder:
                def stash(self, engine):
                    with phase(engine) as conn:
                        self._conn = conn
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.operation, site.connection_escape) for site in sites] == [
        ("Holder.stash", "write_connection", True),
    ]


def test_non_generator_callee_is_never_followed_as_a_wrapper(tmp_path: Path) -> None:
    source = tmp_path / "plain_callee.py"
    source.write_text(
        textwrap.dedent(
            """\
            def plain(engine):
                return engine.begin()

            def caller(engine, dynamic):
                with plain(engine) as conn:
                    conn.execute(dynamic)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation, site.connection_escape) for site in sites) == Counter(
        {
            ("plain", "<sessions-write-connection>", "write_connection", True): 1,
            ("caller", "<unresolved-session-write>", "unknown_execute", False): 1,
        }
    )


def test_qualified_factory_return_hop_carries_the_factory_domain_one_hop(tmp_path: Path) -> None:
    """``conn = self._open()`` where every return of ``_open`` is ``sqlite3.connect`` is that factory's connection.

    The factory's own return stays the reported acquisition; the hop only
    carries domain, and raw SQL naming a Sessions table still poisons it.
    """

    source = tmp_path / "factory_return_hop.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            from contextlib import contextmanager

            class Store:
                def _open(self):
                    return sqlite3.connect(":memory:")

                @contextmanager
                def _connect(self):
                    conn = self._open()
                    try:
                        yield conn
                    finally:
                        conn.close()

                def write(self):
                    with self._connect() as conn:
                        conn.execute("INSERT INTO users (id) VALUES (1)")

                def forbidden(self):
                    with self._connect() as conn:
                        conn.execute("UPDATE sessions SET status = 'x'")

                def _wrapped(self):
                    return self._open()

                def two_hops(self):
                    conn = self._wrapped()
                    conn.execute("INSERT INTO users (id) VALUES (2)")
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation, site.connection_escape) for site in sites) == Counter(
        {
            ("Store._open", "<non-session-write-connection>", "write_connection", True): 1,
            ("Store.forbidden", "sessions", "raw_update", False): 1,
            ("Store.two_hops", "<unresolved-session-write>", "unknown_execute", False): 1,
        }
    )


def test_self_inside_a_declared_engine_type_carries_its_domain(tmp_path: Path) -> None:
    landscape = tmp_path / "src/elspeth/core/landscape/database.py"
    landscape.parent.mkdir(parents=True)
    body = textwrap.dedent(
        """\
        from sqlalchemy import update
        from elspeth.core.landscape.schema import runs_table

        class {name}:
            def __init__(self, engine):
                self._engine = engine

            def write(self):
                with self._engine.begin() as conn:
                    conn.execute(update(runs_table).values(status="failed"))

            @staticmethod
            def detached(self):
                with self._engine.begin() as conn:
                    conn.execute(update(runs_table).values(status="failed"))
        """
    )
    landscape.write_text(body.format(name="LandscapeDB"))
    other = tmp_path / "src/elspeth/other.py"
    other.write_text(body.format(name="LandscapeDB"))
    sites = scan_production_writers([landscape, other], anchor=tmp_path)
    assert Counter((site.path, site.symbol, site.table) for site in sites if site.operation == "write_connection") == Counter(
        {
            ("src/elspeth/core/landscape/database.py", "LandscapeDB.write", "<non-session-write-connection>"): 1,
            ("src/elspeth/core/landscape/database.py", "LandscapeDB.detached", "<sessions-write-connection>"): 1,
            ("src/elspeth/other.py", "LandscapeDB.write", "<sessions-write-connection>"): 1,
            ("src/elspeth/other.py", "LandscapeDB.detached", "<sessions-write-connection>"): 1,
        }
    )


_PACKAGE_PREMISE_MODULE = textwrap.dedent(
    """\
    {imports}
    class Repo:
        def __init__(self, db):
            self._db = db

        def module_bound(self, stmt):
            with self._db.write_connection() as conn:
                conn.execute(stmt)

        def raw_landscape(self):
            with self._db.write_connection() as conn:
                conn.execute("UPDATE token_outcomes SET outcome = 'x'")

        def raw_sessions(self):
            with self._db.write_connection() as conn:
                conn.execute("UPDATE sessions SET status = 'x'")

    def parameter_received(conn, stmt):
        conn.execute(stmt)
    """
)


def _package_premise_findings(tmp_path: Path, relative: str, *, imports: str = "") -> Counter[tuple[str, str, str]]:
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_PACKAGE_PREMISE_MODULE.format(imports=imports))
    return Counter((site.symbol, site.table, site.operation) for site in scan_production_writers([source], anchor=tmp_path))


def test_package_premise_classifies_module_bound_executions_in_a_declared_landscape_module(tmp_path: Path) -> None:
    """Rule 1 acceptance: a declared module with no elspeth.web import executes on the connection it bound."""

    assert _package_premise_findings(tmp_path, "src/elspeth/core/landscape/repo.py") == Counter(
        {
            # The parameter-received helper stays fail-closed (merge writer's default).
            ("parameter_received", "<unresolved-session-write>", "unknown_execute"): 1,
            # Raw SQL naming a Sessions table is vetoed even here.
            ("Repo.raw_sessions", "sessions", "raw_update"): 1,
        }
    )


def test_package_premise_does_not_apply_outside_the_declared_packages(tmp_path: Path) -> None:
    """Rule 1 refusal: the same module under web/ keeps every finding."""

    assert _package_premise_findings(tmp_path, "src/elspeth/web/repo.py") == Counter(
        {
            ("Repo.module_bound", "<unresolved-session-write>", "unknown_execute"): 1,
            ("Repo.raw_landscape", "<unresolved-session-write>", "unknown_execute"): 1,
            ("Repo.raw_sessions", "sessions", "raw_update"): 1,
            ("parameter_received", "<unresolved-session-write>", "unknown_execute"): 1,
        }
    )


def test_package_premise_is_revoked_by_a_session_shaped_import(tmp_path: Path) -> None:
    """Rule 1 tripwire: one elspeth.web import in a declared module re-opens every execution it classified."""

    for imports in (
        "from elspeth.web.sessions.models import sessions_table\n",
        "from elspeth.web.sessions.engine import create_session_engine\n",
        "import elspeth.web.sessions.models\n",
    ):
        assert _package_premise_findings(tmp_path, "src/elspeth/core/landscape/repo.py", imports=imports) == Counter(
            {
                ("Repo.module_bound", "<unresolved-session-write>", "unknown_execute"): 1,
                ("Repo.raw_landscape", "<unresolved-session-write>", "unknown_execute"): 1,
                ("Repo.raw_sessions", "sessions", "raw_update"): 1,
                ("parameter_received", "<unresolved-session-write>", "unknown_execute"): 1,
            }
        ), imports


_CALLER_SIDE_PROOF_MODULE = textwrap.dedent(
    """\
    class Repo:
        def __init__(self, db):
            self._db = db

        def caller(self, stmt):
            with self._db.write_connection() as conn:
                helper(conn, stmt)

        def chained_caller(self, stmt):
            with self._db.write_connection() as conn:
                outer(conn, stmt)

    def helper(conn, stmt):
        conn.execute(stmt)

    def outer(conn, stmt):
        inner(conn, stmt)

    def inner(conn, stmt):
        conn.execute(stmt)
    """
)


def _caller_side_findings(tmp_path: Path, extra: dict[str, str] | None = None) -> Counter[tuple[str, str, str]]:
    """Scan the declared Landscape module plus any extra modules, keyed (path, symbol, table)."""

    declared = tmp_path / "src/elspeth/core/landscape/repo.py"
    declared.parent.mkdir(parents=True, exist_ok=True)
    declared.write_text(_CALLER_SIDE_PROOF_MODULE)
    files = [declared]
    for relative, body in (extra or {}).items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(textwrap.dedent(body))
        files.append(source)
    return Counter(
        (site.path, site.symbol, site.table)
        for site in scan_production_writers(files, anchor=tmp_path)
        if site.table == "<unresolved-session-write>"
    )


def test_caller_side_proof_clears_a_helper_every_caller_binds_to_a_module_bound_connection(tmp_path: Path) -> None:
    """Option (b) acceptance: ``helper`` executes on a parameter, and its only caller binds that parameter
    from the declared module's own handle, so the execution is proven non-Sessions tree-wide. The chain
    ``chained_caller -> outer -> inner`` proves ``inner`` through ``outer``'s own parameter."""

    assert _caller_side_findings(tmp_path) == Counter()


def test_caller_side_proof_refuses_when_one_caller_hands_a_sessions_connection(tmp_path: Path) -> None:
    """Adversarial: a web module imports the same helper and passes an engine-bound connection.
    One contrary call site keeps the parameter-received execution unresolved -- the proof is
    all-call-sites, never any-call-site."""

    web = """\
        from elspeth.core.landscape.repo import helper

        def web_caller(engine, stmt):
            with engine.begin() as conn:
                helper(conn, stmt)
        """
    findings = _caller_side_findings(tmp_path, {"src/elspeth/web/escape.py": web})
    assert findings[("src/elspeth/core/landscape/repo.py", "helper", "<unresolved-session-write>")] == 1
    # The chain has no contrary caller and stays proven.
    assert findings[("src/elspeth/core/landscape/repo.py", "inner", "<unresolved-session-write>")] == 0


def test_caller_side_proof_refuses_a_call_whose_argument_cannot_be_known(tmp_path: Path) -> None:
    """Adversarial: a star-argument call site cannot bind the parameter to any expression, so it refuses."""

    starred = """\
        from elspeth.core.landscape.repo import inner

        def relay(*args):
            inner(*args)
        """
    findings = _caller_side_findings(tmp_path, {"src/elspeth/core/landscape/relay.py": starred})
    assert findings[("src/elspeth/core/landscape/repo.py", "inner", "<unresolved-session-write>")] == 1
    assert findings[("src/elspeth/core/landscape/repo.py", "helper", "<unresolved-session-write>")] == 0


def test_caller_side_proof_refuses_a_cycle_and_a_helper_nobody_calls(tmp_path: Path) -> None:
    """Adversarial: two helpers that only hand the connection to each other prove nothing (a cycle is
    refused, not assumed), and a parameter-received helper with no call site at all stays unresolved."""

    cyclic = """\
        def ping(conn, stmt):
            pong(conn, stmt)

        def pong(conn, stmt):
            ping(conn, stmt)
            conn.execute(stmt)

        def orphan(conn, stmt):
            conn.execute(stmt)
        """
    findings = _caller_side_findings(tmp_path, {"src/elspeth/core/landscape/cycle.py": cyclic})
    assert findings[("src/elspeth/core/landscape/cycle.py", "pong", "<unresolved-session-write>")] == 1
    assert findings[("src/elspeth/core/landscape/cycle.py", "orphan", "<unresolved-session-write>")] == 1


_CONTAINED_FORWARDING_MODULE = """\
    from contextlib import contextmanager

    class Repo:
        def __init__(self, engine):
            self._engine = engine

        @contextmanager
        def _tx(self):
            with self._engine.begin() as conn:
                yield conn

        def direct(self, stmt):
            with self._tx() as conn:
                conn.execute(stmt)

        def forwarded(self, stmt):
            with self._tx() as conn:
                self._apply(conn, stmt)

        def _apply(self, conn, stmt):
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql(stmt)
            with conn.begin_nested():
                self._deeper(conn, stmt=stmt)

        def _deeper(self, conn, *, stmt):
            conn.execute(stmt)
            _module_helper(conn, stmt)
            del conn

        def own_acquisition(self, stmt):
            with self._engine.begin() as conn:
                _module_helper(conn, stmt)

    def _module_helper(conn, stmt):
        conn.execute(stmt)
    """

_ESCAPING_FORWARDING_MODULE = """\
    from elspeth.web.other import imported_helper

    def helper(conn, stmt):
        conn.execute(stmt)

    class Repo:
        def __init__(self, engine, handler):
            self._engine = engine
            self._handler = handler

        def stores(self, stmt):
            with self._engine.begin() as conn:
                self._keep(conn)

        def _keep(self, conn):
            self._held = conn

        def returns(self, stmt):
            with self._engine.begin() as conn:
                self._give(conn)

        def _give(self, conn):
            return conn

        def yields(self, stmt):
            with self._engine.begin() as conn:
                self._gen(conn)

        def _gen(self, conn):
            yield conn

        def closure(self, stmt):
            with self._engine.begin() as conn:
                self._defer(conn, stmt)

        def _defer(self, conn, stmt):
            return lambda: conn.execute(stmt)

        def starred(self, stmt, *rest):
            with self._engine.begin() as conn:
                self._exec(conn, *rest)

        def _exec(self, conn, *rest):
            conn.execute(rest[0])

        def cross_module(self, stmt):
            with self._engine.begin() as conn:
                imported_helper(conn, stmt)

        def dispatched(self, stmt):
            with self._engine.begin() as conn:
                self._handler.run(conn, stmt)

        def public_helper(self, stmt):
            with self._engine.begin() as conn:
                helper(conn, stmt)

        def too_deep(self, stmt):
            with self._engine.begin() as conn:
                self._d1(conn, stmt)

        def _d1(self, conn, stmt):
            self._d2(conn, stmt)

        def _d2(self, conn, stmt):
            self._d3(conn, stmt)

        def _d3(self, conn, stmt):
            self._d4(conn, stmt)

        def _d4(self, conn, stmt):
            conn.execute(stmt)

        def raw_dbapi(self, stmt):
            with self._engine.begin() as conn:
                self._raw(conn, stmt)

        def _raw(self, conn, stmt):
            return conn.connection.cursor().execute(stmt)

        def bound_nested(self, stmt):
            with self._engine.begin() as conn:
                self._savepoint(conn, stmt)

        def _savepoint(self, conn, stmt):
            savepoint = conn.begin_nested()
            conn.execute(stmt)
            savepoint.rollback()

        def compared(self, stmt):
            with self._engine.begin() as conn:
                self._identity(conn)

        def _identity(self, conn):
            return id(conn)
    """

_WRAPPER_MODULE = """\
    from contextlib import contextmanager
    from elspeth.web.sessions.locking import transaction_lock

    class Repo:
        def __init__(self, engine):
            self._engine = engine

        @contextmanager
        def _one_bad_caller(self):
            with self._engine.begin() as conn:
                yield conn

        def fine(self, stmt):
            with self._one_bad_caller() as conn:
                conn.execute(stmt)

        def leaks(self, stmt):
            with self._one_bad_caller() as conn:
                self._held = conn

        @contextmanager
        def _foreign_reference(self):
            with self._engine.begin() as conn:
                yield conn

        def uses_foreign(self, stmt):
            with self._foreign_reference() as conn:
                conn.execute(stmt)

        @contextmanager
        def _escapes_inside(self, session_id):
            with self._engine.begin() as conn:
                transaction_lock(conn, session_id)
                yield conn

        def uses_escaping(self, stmt):
            with self._escapes_inside("s") as conn:
                conn.execute(stmt)

        @contextmanager
        def _passed_not_entered(self):
            with self._engine.begin() as conn:
                yield conn

        def hands_out(self):
            return self._passed_not_entered
    """

_WRAPPER_FOREIGN_MODULE = """\
    def borrow(repo, stmt):
        with repo._foreign_reference() as conn:
            conn.execute(stmt)
    """


def _acquisition_escapes(tmp_path: Path, modules: dict[str, str]) -> dict[str, bool]:
    """``symbol -> connection_escape`` for every write_connection row the scan of ``modules`` reports."""

    files = []
    for relative, body in modules.items():
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(textwrap.dedent(body))
        files.append(source)
    escapes: dict[str, bool] = {}
    for site in scan_production_writers(files, anchor=tmp_path):
        if site.operation == "write_connection":
            escapes[site.symbol] = escapes.get(site.symbol, False) or site.connection_escape
    return escapes


def test_forwarding_proof_contains_a_connection_handed_only_to_inspectable_callees(tmp_path: Path) -> None:
    """Step-5 acceptance: a same-class method chain and a same-module private helper that only
    execute on the connection (a dialect read, an anonymous nested transaction and a ``del`` of the
    local name included) keep it contained, three forwards deep; the state-fed wrapper whose every
    caller is such a method is contained too."""

    assert _acquisition_escapes(tmp_path, {"src/elspeth/web/contained.py": _CONTAINED_FORWARDING_MODULE}) == {
        "Repo._tx": False,
        "Repo.own_acquisition": False,
    }


def test_forwarding_proof_refuses_every_escape_form(tmp_path: Path) -> None:
    """Adversarial: each callee leaks the connection one way -- attribute store, return, yield,
    closure capture, star-argument call, cross-module callee, attribute-dispatched callee, a PUBLIC
    module function, depth beyond the bound, raw DBAPI access, a bound nested transaction, a
    comparison through a builtin -- and each acquisition stays an escape."""

    escapes = _acquisition_escapes(tmp_path, {"src/elspeth/web/escapes.py": _ESCAPING_FORWARDING_MODULE})
    expected = {
        "Repo.stores",
        "Repo.returns",
        "Repo.yields",
        "Repo.closure",
        "Repo.starred",
        "Repo.cross_module",
        "Repo.dispatched",
        "Repo.public_helper",
        "Repo.too_deep",
        "Repo.raw_dbapi",
        "Repo.bound_nested",
        "Repo.compared",
    }
    assert {symbol for symbol, escaped in escapes.items() if escaped} == expected


def test_wrapper_containment_is_all_callers_and_same_class_only(tmp_path: Path) -> None:
    """Adversarial: one caller that stores the with-target, one foreign-module caller, an escape
    inside the wrapper's own body, and a wrapper handed out uncalled each keep the wrapper's
    acquisition escaped."""

    escapes = _acquisition_escapes(
        tmp_path,
        {"src/elspeth/web/wrappers.py": _WRAPPER_MODULE, "src/elspeth/web/elsewhere.py": _WRAPPER_FOREIGN_MODULE},
    )
    assert escapes == {
        "Repo._one_bad_caller": True,
        "Repo._foreign_reference": True,
        "Repo._escapes_inside": True,
        "Repo._passed_not_entered": True,
    }


_CROSS_MODULE_LOCKING = """\
    def acquire_lock(conn, key):
        conn.exec_driver_sql("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (key,))

    def custody_lock(conn, key):
        _advisory(conn, key)

    def _advisory(conn, key):
        conn.exec_driver_sql("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (key,))

    def poison(conn, key):
        conn.exec_driver_sql("UPDATE sessions SET status = 'x'")

    def hop1(conn, key):
        _hop2(conn, key)

    def _hop2(conn, key):
        _hop3(conn, key)

    def _hop3(conn, key):
        _hop4(conn, key)

    def _hop4(conn, key):
        conn.exec_driver_sql("SELECT 1")
    """

_CROSS_MODULE_CALLERS = """\
    from elspeth.web.sessions.locking import acquire_lock, custody_lock, hop1, poison
    from elspeth.web.sessions.locking import acquire_lock as grab
    from lib.outside import outside_lock
    from elspeth.web.missing import ghost_lock

    class Repo:
        def __init__(self, engine):
            self._engine = engine

        def phase(self, key):
            with self._engine.begin() as conn:
                _lock(conn, key)

        def two_hops(self, key):
            with self._engine.begin() as conn:
                custody_lock(conn, key)

        def dml_behind_import(self, key):
            with self._engine.begin() as conn:
                poison(conn, key)

        def aliased(self, key):
            with self._engine.begin() as conn:
                grab(conn, key)

        def outside(self, key):
            with self._engine.begin() as conn:
                outside_lock(conn, key)

        def unscanned(self, key):
            with self._engine.begin() as conn:
                ghost_lock(conn, key)

        def too_deep(self, key):
            with self._engine.begin() as conn:
                hop1(conn, key)

    def _lock(conn, key):
        acquire_lock(conn, key)
    """

_OUTSIDE_LOCKING = """\
    def outside_lock(conn, key):
        conn.exec_driver_sql("SELECT 1")
    """


def test_forwarding_proof_inspects_an_imported_callee_in_its_own_module(tmp_path: Path) -> None:
    """Cross-module ruling: behind a plain ``from elspeth.<module> import f`` the callee is inspected
    in its own module; one hop (through a same-module private helper) and two hops (the imported
    function forwarding to its module's private helper) prove contained when every execution on the
    connection is an advisory-lock SELECT."""

    escapes = _acquisition_escapes(
        tmp_path,
        {
            "src/elspeth/web/phase.py": _CROSS_MODULE_CALLERS,
            "src/elspeth/web/sessions/locking.py": _CROSS_MODULE_LOCKING,
            "lib/outside.py": _OUTSIDE_LOCKING,
        },
    )
    assert escapes["Repo.phase"] is False
    assert escapes["Repo.two_hops"] is False


def test_forwarding_proof_refuses_imports_it_cannot_inspect_or_that_carry_dml(tmp_path: Path) -> None:
    """Adversarial, cross-module: table DML on the forwarded connection behind the import, an aliased
    import, a callee outside ``src/elspeth``, a module with no source file under the anchor, and a
    chain beyond the depth bound each keep the acquisition escaped."""

    escapes = _acquisition_escapes(
        tmp_path,
        {
            "src/elspeth/web/phase.py": _CROSS_MODULE_CALLERS,
            "src/elspeth/web/sessions/locking.py": _CROSS_MODULE_LOCKING,
            "lib/outside.py": _OUTSIDE_LOCKING,
        },
    )
    assert {symbol for symbol, escaped in escapes.items() if escaped} == {
        "Repo.dml_behind_import",
        "Repo.aliased",
        "Repo.outside",
        "Repo.unscanned",
        "Repo.too_deep",
    }


def test_declared_factory_handle_verbs_are_acquisitions_only_on_a_plain_name(tmp_path: Path) -> None:
    """``with open_landscape_db(...) as db, db.write_connection() as conn`` is a Landscape acquisition; ``self._db.write_connection()`` is not one."""

    source = tmp_path / "src/elspeth/web/tutorial.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.core.landscape.schema import runs_table
            from elspeth.web.landscape_access import open_landscape_db

            def project(settings, run_id):
                with open_landscape_db(settings) as db, db.write_connection() as conn:
                    conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(llm_call_count=1))

            class Holder:
                def __init__(self, db):
                    self._db = db

                def project(self, run_id):
                    with self._db.write_connection() as conn:
                        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(llm_call_count=1))

            def unknown_handle(db, run_id):
                with db.write_connection() as conn:
                    conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(llm_call_count=1))
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("project", "<non-session-write-connection>", "write_connection"): 1,
            ("Holder.project", "<unresolved-session-write>", "unknown_execute"): 1,
            ("unknown_handle", "<unresolved-session-write>", "unknown_execute"): 1,
        }
    )


def test_factory_return_hop_follows_a_method_returning_either_declared_landscape_factory(tmp_path: Path) -> None:
    """checkpoint/manager's shape: a method returning begin_write(...) or fenced_leader_transaction(...) carries the Landscape origin."""

    source = tmp_path / "src/elspeth/core/checkpoint/manager.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import insert
            from elspeth.core.landscape.database import begin_write
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import checkpoints_table

            class CheckpointManager:
                def __init__(self, db):
                    self._db = db

                def _fenced_or_plain_write(self, *, token, verb):
                    if token is None:
                        return begin_write(self._db.engine)
                    return fenced_leader_transaction(self._db.engine, token=token, verb=verb)

                def create(self, token):
                    with self._fenced_or_plain_write(token=token, verb="create") as conn:
                        conn.execute(insert(checkpoints_table).values(run_id="r"))

                def _leaky(self, provider):
                    if provider is None:
                        return begin_write(self._db.engine)
                    return provider.connect()

                def create_leaky(self, provider):
                    with self._leaky(provider) as conn:
                        conn.execute(insert(checkpoints_table).values(run_id="r"))
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            # The refused hop: one return is not a declared factory, so the
            # caller's execute stays unresolved even inside a declared package
            # (its connection roots in a parameter-fed helper, not the module),
            # and the returned generic connection is the helper's own escape.
            ("CheckpointManager._leaky", "<sessions-write-connection>", "write_connection"): 1,
            ("CheckpointManager.create_leaky", "<unresolved-session-write>", "unknown_execute"): 1,
        }
    )


def test_self_attribute_with_a_declared_non_sql_type_is_not_a_database_execute(tmp_path: Path) -> None:
    source = tmp_path / "src/elspeth/plugins/transforms/llm/transform.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            class SingleQueryStrategy:
                def execute(self, row):
                    return row

            class MultiQueryStrategy:
                def execute(self, row):
                    return row

            class Other:
                def execute(self, row):
                    return row

            class Transform:
                def __init__(self, single):
                    if single:
                        self._strategy: SingleQueryStrategy | MultiQueryStrategy = SingleQueryStrategy()
                    else:
                        self._strategy = MultiQueryStrategy()
                    self._other: Other = Other()
                    self._untyped = Other()

                def process(self, row):
                    return self._strategy.execute(row)

                def process_other(self, row):
                    return self._other.execute(row)

                def process_untyped(self, row):
                    return self._untyped.execute(row)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("Transform.process_other", "<unresolved-session-write>", "unknown_execute"): 1,
            ("Transform.process_untyped", "<unresolved-session-write>", "unknown_execute"): 1,
        }
    )


def test_session_engine_factory_configuration_is_not_a_table_write(tmp_path: Path) -> None:
    """Rule 3: PRAGMA assignments and BEGIN inside create_session_engine are engine configuration; elsewhere they are not."""

    source = tmp_path / "src/elspeth/web/sessions/engine.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            def create_session_engine(url):
                def _configure(dbapi_conn, _record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA journal_mode=WAL")

                def _begin_immediate(conn):
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                    conn.exec_driver_sql("BEGIN")

                def _not_configuration(conn):
                    conn.exec_driver_sql("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL")
                    conn.execute("VACUUM")

            def elsewhere(dbapi_conn, conn):
                dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("create_session_engine._not_configuration", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
            ("create_session_engine._not_configuration", "<unresolved-session-write>", "unknown_execute"): 1,
            ("elsewhere", "<unresolved-session-write>", "unknown_execute"): 1,
            ("elsewhere", "<unresolved-session-write>", "unknown_exec_driver_sql"): 1,
        }
    )


def test_production_scanner_flags_connection_flows_without_session_model_imports(tmp_path: Path) -> None:
    source = tmp_path / "model_free_connection_flows.py"
    source.write_text(
        textwrap.dedent(
            """\
            def helper(connection):
                pass

            def returned(engine):
                return engine.connect()

            def yielded(engine):
                yield engine.begin()

            def forwarded(engine):
                helper(engine.connect())
                helper(connection=engine.begin())
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = sorted(
        (site for site in sites if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.line, site.ordinal) for site in acquisitions] == [
        ("returned", 5, 1),
        ("yielded", 8, 1),
        ("forwarded", 11, 1),
        ("forwarded", 12, 1),
    ]


def test_production_scanner_distinguishes_imported_sqlite3_connections(tmp_path: Path) -> None:
    source = tmp_path / "connection_domains.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3 as db

            def helper(connection):
                pass

            def sqlite_connection():
                return db.connect(":memory:")

            def unknown_connection(factory):
                return factory.connect()

            def mixed_helper(factory):
                helper(factory.connect())

            def sessions_engine(engine):
                yield engine.begin()
            """
        )
    )
    sites = sorted(
        (site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.table, site.line) for site in sites] == [
        ("sqlite_connection", "<non-session-write-connection>", 7),
        ("unknown_connection", "<sessions-write-connection>", 10),
        ("mixed_helper", "<sessions-write-connection>", 13),
        ("sessions_engine", "<sessions-write-connection>", 16),
    ]


def test_production_scanner_proves_database_domains_from_semantic_provenance(tmp_path: Path) -> None:
    source = tmp_path / "database_domains.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import create_engine, update
            from sqlalchemy.engine import Engine
            from elspeth.core.landscape.database import LandscapeDB, Tier1Engine
            from elspeth.core.landscape.schema import runs_table
            from elspeth.web.sessions.engine import create_session_engine
            from elspeth.web.sessions.models import sessions_table

            def landscape_table_writer(conn):
                conn.execute(update(runs_table).values(status="failed"))

            class LandscapeEngineReader:
                def __init__(self, engine: Tier1Engine):
                    self._engine = engine

                def dynamic_read(self, statement):
                    with self._engine.connect() as conn:
                        conn.execute(statement)

            class LandscapeDatabaseReader:
                def __init__(self, database: LandscapeDB):
                    self._database = database

                def dynamic_read(self, statement):
                    with self._database.engine.connect() as conn:
                        conn.execute(statement)

            class PluginTarget:
                def __init__(self, url: str):
                    self._engine: Engine | None = None
                    self._engine = create_engine(url)

                def dynamic_write(self, statement):
                    assert self._engine is not None
                    with self._engine.begin() as conn:
                        conn.execute(statement)

            def sessions_writer(url):
                engine = create_session_engine(url)
                with engine.begin() as conn:
                    conn.execute(update(sessions_table).values(status="archived"))

            def unknown_writer(engine, statement):
                with engine.begin() as conn:
                    conn.execute(statement)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # Every ``statement`` executed on a connection whose domain is not proven
    # non-Sessions is an opaque row of its own (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.table, site.operation) for site in sites if site.operation != "write_connection") == [
        ("LandscapeDatabaseReader.dynamic_read", "<unresolved-session-write>", "unknown_opaque"),
        ("LandscapeEngineReader.dynamic_read", "<unresolved-session-write>", "unknown_opaque"),
        ("PluginTarget.dynamic_write", "<unresolved-session-write>", "unknown_opaque"),
        ("landscape_table_writer", "<unresolved-session-write>", "unknown_execute"),
        ("sessions_writer", "sessions", "update"),
        ("unknown_writer", "<unresolved-session-write>", "unknown_opaque"),
    ]
    connections = sorted(
        ((site.symbol, site.table) for site in sites if site.operation == "write_connection"),
        key=lambda item: item[0],
    )
    assert connections == [
        ("LandscapeDatabaseReader.dynamic_read", "<sessions-write-connection>"),
        ("LandscapeEngineReader.dynamic_read", "<sessions-write-connection>"),
        ("PluginTarget.dynamic_write", "<sessions-write-connection>"),
        ("sessions_writer", "<sessions-write-connection>"),
        ("unknown_writer", "<sessions-write-connection>"),
    ]


def test_attribute_domain_proof_is_receiver_and_scope_aware(tmp_path: Path) -> None:
    source = tmp_path / "attribute_scope_domains.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.core.landscape.database import LandscapeDB

            class Direct:
                def __init__(self, engine: LandscapeDB):
                    self._engine = engine

                def read(self):
                    return self._engine.connect()

            class NestedOnly:
                class Nested:
                    def __init__(self, engine: LandscapeDB):
                        self._engine = engine

                def read(self):
                    return self._engine.connect()

            class ReboundReceiver:
                def __init__(self, engine: LandscapeDB, replacement):
                    self = replacement
                    self._engine = engine

                def read(self):
                    return self._engine.connect()

            class Reassigned:
                def __init__(self, first: LandscapeDB, second: LandscapeDB):
                    self._engine = first
                    self._engine = second

                def read(self):
                    return self._engine.connect()

            class PropertyBacked:
                @property
                def _engine(self):
                    return provider

                def configure(self, engine: LandscapeDB):
                    self._engine = engine

                def read(self):
                    return self._engine.connect()

            class Base:
                def __init__(self, engine: LandscapeDB):
                    self._engine = engine

            class Inherited(Base):
                def read(self):
                    return self._engine.connect()
            """
        )
    )

    connections = {
        site.symbol: site.table for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"
    }
    assert connections == {
        "Direct.read": "<non-session-write-connection>",
        "NestedOnly.read": "<sessions-write-connection>",
        "ReboundReceiver.read": "<sessions-write-connection>",
        "Reassigned.read": "<sessions-write-connection>",
        "PropertyBacked.read": "<sessions-write-connection>",
        "Inherited.read": "<sessions-write-connection>",
    }


def test_static_and_class_method_assignments_cannot_forge_instance_attribute_provenance(tmp_path: Path) -> None:
    source = tmp_path / "forged_attribute_receivers.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.core.landscape.database import LandscapeDB

            class StaticForged:
                @staticmethod
                def configure(self, engine: LandscapeDB):
                    self._engine = engine

                def read(self):
                    return self._engine.connect()

            class ClassForged:
                @classmethod
                def configure(cls, engine: LandscapeDB):
                    cls._engine = engine

                def read(self):
                    return self._engine.connect()
            """
        )
    )

    connections = {
        site.symbol: site.table for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"
    }
    assert connections == {
        "StaticForged.read": "<sessions-write-connection>",
        "ClassForged.read": "<sessions-write-connection>",
    }


def test_non_session_connection_proof_does_not_hide_unknown_raw_sessions_sql(tmp_path: Path) -> None:
    source = tmp_path / "raw_database_domains.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import create_engine

            def plugin_target(url):
                engine = create_engine(url)
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE sessions SET status = 'external'")

            def unknown_target(engine):
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE sessions SET status = 'unknown'")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    identities = sorted(
        ((site.symbol, site.table, site.operation) for site in sites),
        key=lambda item: (item[0], item[2]),
    )
    assert identities == [
        ("plugin_target", "sessions", "raw_update"),
        ("plugin_target", "<sessions-write-connection>", "write_connection"),
        ("unknown_target", "sessions", "raw_update"),
        ("unknown_target", "<sessions-write-connection>", "write_connection"),
    ]


def test_non_session_statement_requires_a_proven_non_session_execution_connection(tmp_path: Path) -> None:
    source = tmp_path / "non_session_statement_connection_boundary.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.core.landscape.database import Tier1Engine
            from elspeth.core.landscape.schema import runs_table
            from elspeth.web.sessions.engine import create_session_engine

            def unknown_connection(conn):
                conn.execute(update(runs_table).values(status="failed"))

            def sessions_connection(url):
                engine = create_session_engine(url)
                with engine.begin() as conn:
                    conn.execute(update(runs_table).values(status="failed"))

            def non_session_connection(engine: Tier1Engine):
                with engine.begin() as conn:
                    conn.execute(update(runs_table).values(status="failed"))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("unknown_connection", "<unresolved-session-write>", "unknown_execute"): 1,
            ("sessions_connection", "<unresolved-session-write>", "unknown_execute"): 1,
            ("sessions_connection", "<sessions-write-connection>", "write_connection"): 1,
            ("non_session_connection", "<non-session-write-connection>", "write_connection"): 1,
        }
    )


def test_generic_sqlalchemy_connections_cannot_inherit_domain_from_executed_statements(tmp_path: Path) -> None:
    source = tmp_path / "generic_connection_statement_domain.py"
    source.write_text(
        textwrap.dedent(
            """\
            import elspeth.core.landscape.schema
            import sqlalchemy as sa
            from sqlalchemy import create_engine, update

            def direct_factory(url):
                engine = create_engine(url)
                with engine.begin() as conn:
                    conn.execute(
                        update(elspeth.core.landscape.schema.runs_table).values(status="failed")
                    )

            def aliased_factory(url):
                factory = sa.create_engine
                engine = factory(url)
                with engine.begin() as conn:
                    conn.execute(
                        sa.update(elspeth.core.landscape.schema.runs_table).values(status="failed")
                    )
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            (symbol, table, operation): 1
            for symbol in ("direct_factory", "aliased_factory")
            for table, operation in (
                ("<unresolved-session-write>", "unknown_execute"),
                ("<sessions-write-connection>", "write_connection"),
            )
        }
    )


def test_generic_sqlalchemy_factories_and_aliases_do_not_prove_non_sessions(tmp_path: Path) -> None:
    source = tmp_path / "generic_sqlalchemy_factories.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from sqlalchemy import create_engine

            def direct(url):
                engine = create_engine(url)
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE sessions SET title = 'direct'")

            def aliased(url):
                factory = sa.create_engine
                engine = factory(url)
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE sessions SET title = 'aliased'")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("direct", "sessions", "raw_update"): 1,
            ("direct", "<sessions-write-connection>", "write_connection"): 1,
            ("aliased", "sessions", "raw_update"): 1,
            ("aliased", "<sessions-write-connection>", "write_connection"): 1,
        }
    )


def test_unknown_or_mixed_statement_evidence_poisons_a_proven_connection_origin(tmp_path: Path) -> None:
    source = tmp_path / "mixed_statement_evidence.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            from elspeth.core.landscape.schema import runs_table
            from elspeth.web.sessions.models import sessions_table

            def no_statement():
                return sqlite3.connect(":memory:")

            def unknown_statement(dynamic):
                conn = sqlite3.connect(":memory:")
                conn.execute(dynamic)

            def mixed_statement(flag):
                conn = sqlite3.connect(":memory:")
                conn.execute(sa.update(runs_table) if flag else sa.update(sessions_table))
            """
        )
    )

    connections = {
        site.symbol: site.table for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"
    }
    assert connections == {
        "no_statement": "<non-session-write-connection>",
        "unknown_statement": "<sessions-write-connection>",
        "mixed_statement": "<sessions-write-connection>",
    }


def test_arbitrary_statement_wrappers_cannot_inherit_nested_table_domains(tmp_path: Path) -> None:
    source = tmp_path / "arbitrary_statement_wrappers.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            from elspeth.core.landscape.schema import runs_table

            def choose(first, second):
                return first

            def wrapper(statement):
                return statement

            def multi_argument_wrapper(dynamic):
                conn = sqlite3.connect(":memory:")
                conn.execute(choose(sa.update(runs_table), dynamic))

            def one_argument_wrapper():
                conn = sqlite3.connect(":memory:")
                conn.execute(wrapper(sa.update(runs_table)))

            def nested_wrappers(dynamic):
                conn = sqlite3.connect(":memory:")
                conn.execute(wrapper(choose(sa.update(runs_table), dynamic)))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("multi_argument_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("multi_argument_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("one_argument_wrapper", "<unresolved-session-write>", "unknown_execute"): 1,
            ("one_argument_wrapper", "<sessions-write-connection>", "write_connection"): 1,
            ("nested_wrappers", "<unresolved-session-write>", "unknown_execute"): 1,
            ("nested_wrappers", "<sessions-write-connection>", "write_connection"): 1,
        }
    )


def test_statement_domains_follow_exact_reaching_assignments_and_poison_ambiguity(tmp_path: Path) -> None:
    source = tmp_path / "reaching_statement_assignments.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3
            import sqlalchemy as sa
            from elspeth.core.landscape.schema import runs_table

            def exact_assignment():
                statement = sa.update(runs_table)
                conn = sqlite3.connect(":memory:")
                conn.execute(statement)

            def chained_aliases():
                first = sa.update(runs_table)
                second = first
                statement = second
                conn = sqlite3.connect(":memory:")
                conn.execute(statement)

            def conditional_mixed_assignment(flag, dynamic):
                if flag:
                    statement = sa.update(runs_table)
                else:
                    statement = dynamic
                conn = sqlite3.connect(":memory:")
                conn.execute(statement)

            def unknown_assignment(dynamic):
                statement = dynamic
                conn = sqlite3.connect(":memory:")
                conn.execute(statement)

            module_statement = sa.update(runs_table)

            def late_module_rebind():
                conn = sqlite3.connect(":memory:")
                conn.execute(module_statement)

            module_statement = dynamic_statement

            def enclosing_late_rebind(dynamic):
                statement = sa.update(runs_table)

                def inner():
                    conn = sqlite3.connect(":memory:")
                    conn.execute(statement)

                statement = dynamic
                return inner()
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            ("exact_assignment", "<non-session-write-connection>", "write_connection"): 1,
            ("chained_aliases", "<non-session-write-connection>", "write_connection"): 1,
            ("conditional_mixed_assignment", "<sessions-write-connection>", "write_connection"): 1,
            ("unknown_assignment", "<sessions-write-connection>", "write_connection"): 1,
            ("late_module_rebind", "<sessions-write-connection>", "write_connection"): 1,
            ("enclosing_late_rebind.inner", "<sessions-write-connection>", "write_connection"): 1,
            # The four poisoned connections each carry the opaque statement
            # that poisoned them as a row of its own (elspeth-a85fb1555b).
            ("conditional_mixed_assignment", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("unknown_assignment", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("late_module_rebind", "<unresolved-session-write>", "unknown_opaque"): 1,
            ("enclosing_late_rebind.inner", "<unresolved-session-write>", "unknown_opaque"): 1,
        }
    )


def test_live_sqlalchemy_fluent_methods_and_read_only_pragma_preserve_non_session_precision(tmp_path: Path) -> None:
    source = tmp_path / "live_sqlalchemy_fluent_methods.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.core.landscape.database import Tier1Engine
            from elspeth.core.landscape.schema import runs_table

            def direct_select(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table))

            def where(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).where(runs_table.c.run_id == "run-1"))

            def order_by(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).order_by(runs_table.c.run_id))

            def select_from(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table.c.run_id).select_from(runs_table))

            def limit(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).limit(1))

            def distinct(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table.c.run_id).distinct())

            def group_by(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table.c.run_id).group_by(runs_table.c.run_id))

            def outerjoin(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).outerjoin(runs_table, runs_table.c.run_id == runs_table.c.run_id))

            def join(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).join(runs_table, runs_table.c.run_id == runs_table.c.run_id))

            def offset(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).offset(1))

            def exact_assignment(engine: Tier1Engine):
                statement = sa.select(runs_table).limit(1)
                with engine.connect() as conn:
                    conn.execute(statement)

            def chained_assignment(engine: Tier1Engine):
                first = sa.select(runs_table).order_by(runs_table.c.run_id)
                second = first
                with engine.connect() as conn:
                    conn.execute(second)

            def read_only_journal_mode(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute("PRAGMA journal_mode")
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            (symbol, "<non-session-write-connection>", "write_connection"): 1
            for symbol in (
                "direct_select",
                "where",
                "order_by",
                "select_from",
                "limit",
                "distinct",
                "group_by",
                "outerjoin",
                "join",
                "offset",
                "exact_assignment",
                "chained_assignment",
                "read_only_journal_mode",
            )
        }
    )


def test_read_only_detection_rejects_arbitrary_wrappers_but_accepts_closed_fluent_chains(tmp_path: Path) -> None:
    source = tmp_path / "read_only_fluent_boundary.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.core.landscape.database import Tier1Engine
            from elspeth.core.landscape.schema import runs_table

            def wrapper(statement):
                return statement

            class Holder:
                def wrapper(self, statement):
                    return statement

            def arbitrary_fluent(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(sa.select(runs_table).arbitrary_wrapper())

            def function_wrapper(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(wrapper(sa.select(runs_table)))

            def holder_method_wrapper(engine: Tier1Engine, holder: Holder):
                with engine.connect() as conn:
                    conn.execute(holder.wrapper(sa.select(runs_table)))

            def closed_fluent_chain(engine: Tier1Engine):
                with engine.connect() as conn:
                    conn.execute(
                        sa.select(runs_table)
                        .select_from(runs_table)
                        .where(runs_table.c.run_id == "run-1")
                        .order_by(runs_table.c.run_id)
                        .limit(1)
                    )
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.symbol, site.table, site.operation) for site in sites) == Counter(
        {
            (symbol, "<unresolved-session-write>", "unknown_execute"): 1
            for symbol in ("arbitrary_fluent", "function_wrapper", "holder_method_wrapper")
        }
        | {
            (symbol, "<sessions-write-connection>", "write_connection"): 1
            for symbol in ("arbitrary_fluent", "function_wrapper", "holder_method_wrapper")
        }
        | {
            ("closed_fluent_chain", "<non-session-write-connection>", "write_connection"): 1,
        }
    )


def test_sqlite_connection_domain_requires_live_import_provenance(tmp_path: Path) -> None:
    source = tmp_path / "sqlite_provenance.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            def connection():
                return sqlite3.connect(":memory:")
            """
        )
    )
    reviewed_external = scan_production_writers([source], anchor=tmp_path)
    assert [(site.table, site.line) for site in reviewed_external] == [
        ("<non-session-write-connection>", 4),
    ]

    source.write_text(
        textwrap.dedent(
            """\


            def connection(sqlite3):
                return sqlite3.connect(":memory:")
            """
        )
    )
    live = scan_production_writers([source], anchor=tmp_path)
    assert [(site.table, site.line) for site in live] == [
        ("<sessions-write-connection>", 4),
    ]
    assert inventory_drift(live, reviewed_external) == (live, reviewed_external)


def test_post_definition_sqlite_rebinding_reopens_external_manifest_review(tmp_path: Path) -> None:
    source = tmp_path / "post_definition_sqlite_shadow.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            def connection():
                return sqlite3.connect(":memory:")
            """
        )
    )
    reviewed_external = scan_production_writers([source], anchor=tmp_path)
    assert [(site.table, site.line) for site in reviewed_external] == [
        ("<non-session-write-connection>", 4),
    ]

    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            def connection():
                return sqlite3.connect(":memory:")

            sqlite3 = provider
            """
        )
    )
    live = scan_production_writers([source], anchor=tmp_path)
    assert [(site.table, site.line) for site in live] == [
        ("<sessions-write-connection>", 4),
    ]
    assert inventory_drift(live, reviewed_external) == (live, reviewed_external)


def test_canonical_sessions_factory_match_is_exact_but_includes_lexical_descendants(tmp_path: Path) -> None:
    source = tmp_path / "src/elspeth/web/sessions/engine.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            import sqlite3

            def create_session_engine():
                def _creator():
                    return sqlite3.connect(":memory:")
                return _creator()

            def create_session_engine_external():
                return sqlite3.connect(":memory:")
            """
        )
    )

    connections = sorted(
        ((site.symbol, site.table) for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"),
        key=lambda item: item[0],
    )
    assert connections == [
        ("create_session_engine._creator", "<sessions-write-connection>"),
        ("create_session_engine_external", "<non-session-write-connection>"),
    ]


def test_production_scanner_flags_aliased_and_direct_connection_flows(tmp_path: Path) -> None:
    source = tmp_path / "aliased_connection_flows.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.web.sessions import models

            def helper(connection):
                pass

            def alias_yield(engine):
                with engine.begin() as conn:
                    alias = conn
                    yield alias

            def alias_return(engine):
                with engine.connect() as conn:
                    alias = conn
                    return alias

            def alias_forward(engine):
                with engine.connect() as conn:
                    alias = conn
                    helper(connection=alias)

            def direct_forward(engine):
                helper(engine.connect())
                helper(connection=engine.begin())

            def callable_alias_flows(engine):
                acquire = engine.connect
                first = acquire()
                yield first
                second = acquire()
                helper(connection=second)

            def callable_alias_return(engine):
                acquire = engine.connect
                conn = acquire()
                return conn
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = sorted(
        (site for site in sites if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.line, site.ordinal) for site in acquisitions] == [
        ("alias_yield", 7, 1),
        ("alias_return", 12, 1),
        ("alias_forward", 17, 1),
        ("direct_forward", 22, 1),
        ("direct_forward", 23, 1),
        ("callable_alias_flows", 27, 1),
        ("callable_alias_flows", 29, 1),
        ("callable_alias_return", 34, 1),
    ]


def test_conditional_connection_replacements_do_not_erase_reaching_acquisitions(tmp_path: Path) -> None:
    source = tmp_path / "conditional_connection_flows.py"
    source.write_text(
        textwrap.dedent(
            """\
            def helper(connection):
                pass

            def replaced_before_escape(engine, condition, replacement):
                connection = engine.connect()
                if condition:
                    connection = replacement
                return connection

            def sibling_branches(engine, condition, replacement):
                if condition:
                    connection = engine.begin()
                else:
                    connection = replacement
                helper(connection)
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.line) for site in sites] == [
        ("replaced_before_escape", 5),
        ("sibling_branches", 12),
    ]


def test_production_scanner_covers_qualified_and_callable_dml_aliases(tmp_path: Path) -> None:
    source = tmp_path / "qualified_dml.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            import sqlalchemy.dialects.postgresql as pg
            from elspeth.web.sessions import models

            def writer(conn):
                qualified = pg.insert(models.sessions_table)
                conn.execute(qualified)

                dml = sa.update
                callable_alias = dml
                updated = callable_alias(models.sessions_table)
                conn.execute(updated)

                first = sa.delete
                second = first
                chained_alias = second
                deleted = chained_alias(models.sessions_table)
                conn.execute(deleted)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("sessions", "insert"): 1,
            ("sessions", "update"): 1,
            ("sessions", "delete"): 1,
        }
    )


def test_bound_table_method_aliases_retain_exact_table_operation_provenance(tmp_path: Path) -> None:
    source = tmp_path / "bound_table_methods.py"
    source.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from elspeth.web.sessions import models
            from elspeth.web.sessions.models import sessions_table

            def bound_direct(conn):
                mutate = sessions_table.update
                statement = mutate().values(title="bound")
                conn.execute(statement)

            def bound_models(conn):
                remove = models.sessions_table.delete
                conn.execute(remove())

            def chained_bound(conn):
                first = models.sessions_table.update
                second = first
                third = second
                conn.execute(third().values(title="chained"))

            def preserved_forms(conn):
                conn.execute(sessions_table.insert())
                conn.execute(update(sessions_table))
                conn.execute(pg_insert(sessions_table))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("sessions", "insert"): 2,
            ("sessions", "update"): 3,
            ("sessions", "delete"): 1,
        }
    )


def test_bound_table_method_alias_ambiguity_fails_closed_at_connection(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous_bound_table_method.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.web.sessions import models

            def ambiguous(engine, condition, dynamic):
                mutate = models.sessions_table.update
                if condition:
                    mutate = dynamic
                with engine.connect() as conn:
                    statement = mutate().values(title="ambiguous")
                    conn.execute(statement)

            def shadowed(engine, dynamic):
                mutate = models.sessions_table.delete
                mutate = dynamic
                with engine.connect() as conn:
                    conn.execute(mutate())
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    # The ambiguous or shadowed statement is an opaque row of its own beside
    # the acquisition it fails closed at (elspeth-a85fb1555b).
    assert sorted((site.symbol, site.table, site.operation, site.line) for site in sites) == [
        ("ambiguous", "<sessions-write-connection>", "write_connection", 7),
        ("ambiguous", "<unresolved-session-write>", "unknown_opaque", 9),
        ("shadowed", "<sessions-write-connection>", "write_connection", 14),
        ("shadowed", "<unresolved-session-write>", "unknown_opaque", 15),
    ]


def test_production_scanner_resolves_fully_qualified_imported_dml_and_tables(tmp_path: Path) -> None:
    source = tmp_path / "fully_qualified_dml.py"
    source.write_text(
        textwrap.dedent(
            """\
            import elspeth.web.sessions.models
            import sqlalchemy
            import sqlalchemy.dialects.postgresql
            import sqlalchemy as sa
            import sqlalchemy.dialects.postgresql as pg
            from elspeth.web.sessions import models as session_models

            def writer(conn):
                conn.execute(sqlalchemy.update(elspeth.web.sessions.models.sessions_table))
                conn.execute(sqlalchemy.dialects.postgresql.insert(session_models.sessions_table))
                conn.execute(sa.delete(elspeth.web.sessions.models.sessions_table))
                conn.execute(pg.insert(session_models.sessions_table))
            """
        )
    )

    sites = scan_production_writers([source], anchor=tmp_path)
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("sessions", "insert"): 2,
            ("sessions", "update"): 1,
            ("sessions", "delete"): 1,
        }
    )


def test_read_only_resolution_uses_nearest_reassignment(tmp_path: Path) -> None:
    source = tmp_path / "reassigned_statement.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def reassigned(engine, dynamic):
                with engine.connect() as conn:
                    statement = sa.select(models.sessions_table)
                    statement = dynamic
                    conn.execute(statement)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.line) for site in sites if site.operation == "write_connection"] == [
        ("reassigned", 5),
    ]


def test_read_only_resolution_excludes_nested_scope_assignments(tmp_path: Path) -> None:
    source = tmp_path / "nested_statement.py"
    source.write_text(
        textwrap.dedent(
            """\
            import sqlalchemy as sa
            from elspeth.web.sessions import models

            def nested_shadow(engine, statement):
                with engine.connect() as conn:
                    def nested():
                        statement = sa.select(models.sessions_table)
                        return statement
                    conn.execute(statement)
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    assert [(site.symbol, site.line) for site in sites if site.operation == "write_connection"] == [
        ("nested_shadow", 5),
    ]


def test_read_only_resolution_fails_closed_on_cyclic_assignment(tmp_path: Path) -> None:
    source = tmp_path / "cyclic_statement.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.web.sessions import models

            def cyclic(engine):
                with engine.connect() as conn:
                    statement = statement
                    conn.execute(statement)
            """
        )
    )
    try:
        sites = scan_production_writers([source], anchor=tmp_path)
    except RecursionError as error:
        raise AssertionError("cyclic statement resolution must fail closed without recursion") from error
    assert [(site.symbol, site.line) for site in sites if site.operation == "write_connection"] == [
        ("cyclic", 4),
    ]


def test_writer_identity_detects_unchanged_block_moved_within_symbol(tmp_path: Path) -> None:
    source = tmp_path / "moved.py"
    source.write_text(
        "from sqlalchemy import insert\n"
        "from elspeth.web.sessions.models import sessions_table\n"
        "def writer(conn):\n"
        "    conn.execute(insert(sessions_table))\n"
    )
    reviewed = scan_production_writers([source], anchor=tmp_path)

    source.write_text(
        "from sqlalchemy import insert\n"
        "from elspeth.web.sessions.models import sessions_table\n"
        "def writer(conn):\n"
        "    pass\n"
        "    conn.execute(insert(sessions_table))\n"
    )
    moved = scan_production_writers([source], anchor=tmp_path)

    assert _statement_fingerprint(ast.parse("conn.execute(insert(sessions_table))").body[0]) == reviewed[0].fingerprint
    unexpected, stale = inventory_drift(moved, reviewed)
    assert unexpected == moved
    assert stale == reviewed


def test_connection_begin_transaction_handle_is_not_raw_acquisition(tmp_path: Path) -> None:
    source = tmp_path / "connection_receivers.py"
    source.write_text(
        textwrap.dedent(
            """\
            from elspeth.web.sessions import models

            def helper(value):
                pass

            def engine_acquisitions(engine):
                helper(engine.begin())
                helper(engine.connect())

            def transaction_handle(engine):
                with engine.connect() as conn:
                    helper(conn.begin())
            """
        )
    )
    sites = scan_production_writers([source], anchor=tmp_path)
    acquisitions = sorted(
        (site for site in sites if site.operation == "write_connection"),
        key=lambda site: site.line,
    )
    assert [(site.symbol, site.line) for site in acquisitions] == [
        ("engine_acquisitions", 7),
        ("engine_acquisitions", 8),
        ("transaction_handle", 11),
    ]


def test_live_scan_includes_write_capable_wrapper_boundaries() -> None:
    root = _repo_root()
    scanned = scan_production_writers(
        [
            root / "src/elspeth/web/blobs/service.py",
            root / "src/elspeth/web/sessions/service.py",
        ],
        anchor=root,
    )
    actual = {(site.path, site.symbol) for site in scanned if site.operation == "write_connection"}
    assert {
        ("src/elspeth/web/blobs/service.py", "_reserve_pending_blob"),
        ("src/elspeth/web/blobs/service.py", "_finalize_reserved_blob"),
        (
            "src/elspeth/web/sessions/service.py",
            "SessionServiceImpl._session_process_locked_begin",
        ),
    } <= actual
    assert ("src/elspeth/web/blobs/service.py", "_blob_phase_transaction") not in actual


def test_production_scanner_ignores_sql_words_in_prose(tmp_path: Path) -> None:
    source = tmp_path / "prose.py"
    source.write_text(
        textwrap.dedent(
            '''\
            def documentation_only():
                """Never INSERT INTO sessions or UPDATE interpretation_events directly."""
            '''
        )
    )
    assert scan_production_writers([source], anchor=tmp_path) == []


def test_production_scanner_prunes_node_modules(tmp_path: Path) -> None:
    source = tmp_path / "src/elspeth/web/frontend/node_modules/package/embedded.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        textwrap.dedent(
            """\
            def unrelated_library_method(program, value):
                return program.execute(value)
            """
        )
    )

    assert scan_production_writers([source], anchor=tmp_path) == []


def test_production_scanner_fails_closed_on_decode_and_parse(tmp_path: Path) -> None:
    undecodable = tmp_path / "undecodable.py"
    undecodable.write_bytes(b"\xff")
    try:
        scan_production_writers([undecodable], anchor=tmp_path)
    except InventoryScanError as error:
        assert "cannot decode" in str(error)
    else:
        raise AssertionError("undecodable production source was silently skipped")

    invalid = tmp_path / "invalid.py"
    invalid.write_text("def broken(:\n")
    try:
        scan_production_writers([invalid], anchor=tmp_path)
    except InventoryScanError as error:
        assert "cannot parse" in str(error)
    else:
        raise AssertionError("invalid production source was silently skipped")


def test_writer_manifest_is_bidirectional_and_multiplicity_aware(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.py"
    source.write_text(
        "from sqlalchemy import insert\n"
        "from elspeth.web.sessions.models import sessions_table\n"
        "def writer(conn):\n"
        "    conn.execute(insert(sessions_table))\n"
        "    conn.execute(insert(sessions_table))\n"
    )
    live = scan_production_writers([source], anchor=tmp_path)
    writer_sites = [site for site in live if site.operation == "insert"]
    assert [site.ordinal for site in writer_sites] == [1, 2]

    unexpected, stale = inventory_drift(live, writer_sites[:1])
    assert any(site.ordinal == 2 for site in unexpected)
    assert stale == []

    replaced = replace(writer_sites[0], operation="delete")
    unexpected, stale = inventory_drift(writer_sites[:1], [replaced])
    assert unexpected == writer_sites[:1]
    assert stale == [replaced]

    unexpected, stale = inventory_drift([], writer_sites[:1])
    assert unexpected == []
    assert stale == writer_sites[:1]

    remaining, stale = subtract_reviewed_identities(live, writer_sites[:1])
    assert any(site.ordinal == 2 for site in remaining)
    assert stale == []
    assert subtract_reviewed_identities([], writer_sites[:1]) == ([], writer_sites[:1])


def test_non_session_connection_manifest_is_bidirectional_and_exact() -> None:
    reviewed = WriterIdentity(
        path="src/elspeth/example.py",
        symbol="other_database_connection",
        table="<non-session-write-connection>",
        operation="write_connection",
        fingerprint="other-database",
        ordinal=1,
        authority=None,
        line=10,
    )

    assert inventory_drift([reviewed], [reviewed]) == ([], [])
    assert inventory_drift([reviewed, reviewed], [reviewed]) == ([reviewed], [])
    assert inventory_drift([], [reviewed]) == ([], [reviewed])

    moved = replace(reviewed, line=11)
    assert inventory_drift([moved], [reviewed]) == ([moved], [reviewed])

    replacement = replace(reviewed, fingerprint="replacement")
    assert inventory_drift([replacement], [reviewed]) == ([replacement], [reviewed])


def test_live_connection_domain_classification_is_exact() -> None:
    export_read_transaction = WriterIdentity(
        "src/elspeth/core/landscape/export_read_model.py",
        "open_export_read_transaction",
        "<sessions-write-connection>",
        "write_connection",
        "ffdb0616b1c68213",
        1,
        None,
        line=466,
        connection_escape=True,
    )
    assert len(_REVIEWED_NON_SESSION_CONNECTIONS) == 54
    assert export_read_transaction in _REVIEWED_NON_SESSION_CONNECTIONS
    expected_session_reachable: tuple[WriterIdentity, ...] = (
        # The f-string ``PRAGMA user_version = {epoch}`` is opaque raw SQL,
        # so it poisons the otherwise-proven Landscape origin of this
        # ``begin_write`` acquisition; it stays session-reachable until the
        # Landscape package premise classifies the statement.
        WriterIdentity(
            "src/elspeth/core/landscape/database.py",
            "LandscapeDB._set_sqlite_schema_epoch",
            "<sessions-write-connection>",
            "write_connection",
            "145b5590f940eae3",
            1,
            None,
            line=1354,
        ),
        WriterIdentity(
            "src/elspeth/core/schema_shape.py",
            "_collect_sqlite_table_option_issues",
            "<sessions-write-connection>",
            "write_connection",
            "0564af7982a64ad9",
            1,
            None,
            line=707,
        ),
        # Exhaustive Engine|Connection|None dispatch with assert_never
        # (fbb2c8392): the acquisition moved under the try and the
        # connection is still forwarded to the catalog-proof helper.
        WriterIdentity(
            "src/elspeth/core/schema_shape.py",
            "_proven_pg_catalog_text_builtin_calls",
            "<sessions-write-connection>",
            "write_connection",
            "9d26eba31115d8ff",
            1,
            None,
            line=728,
            connection_escape=True,
        ),
        # _inspect_database / _initialize_database became the explicit-return
        # _inspect_via_engine / _initialize_via_engine (9e5f5c2fb, 0041948b9);
        # both still hand the connection to the probe callable.
        WriterIdentity(
            "src/elspeth/web/doctor.py",
            "_inspect_via_engine",
            "<sessions-write-connection>",
            "write_connection",
            "0d13e49af38d8ee4",
            1,
            None,
            line=381,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/doctor.py",
            "_initialize_via_engine",
            "<sessions-write-connection>",
            "write_connection",
            "b87b8f33fcdb54e2",
            1,
            None,
            line=476,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/external_state_startup.py",
            "_probe_with_connection_budget",
            "<sessions-write-connection>",
            "write_connection",
            "e9abbfc4fb7d4b08",
            1,
            None,
            line=117,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/readiness.py",
            "_probe_database_engine",
            "<sessions-write-connection>",
            "write_connection",
            "65d5d82276d99f4d",
            1,
            None,
            line=147,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/schema_probe.py",
            "_run_locked",
            "<sessions-write-connection>",
            "write_connection",
            "4312f607f547b35b",
            1,
            None,
            line=249,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/sessions/locking.py",
            "locked_session_transaction",
            "<sessions-write-connection>",
            "write_connection",
            "e938e86c91ec013e",
            1,
            None,
            line=282,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/sessions/schema.py",
            "_stamp_schema_sentinels",
            "<sessions-write-connection>",
            "write_connection",
            "afc7d978d541eadc",
            1,
            None,
            line=251,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/sessions/schema.py",
            "_assert_schema_sentinels",
            "<sessions-write-connection>",
            "write_connection",
            "883e79c104c66d8f",
            1,
            None,
            line=308,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/sessions/schema.py",
            "_validate_required_triggers",
            "<sessions-write-connection>",
            "write_connection",
            "bbce7dbcfc31f6ec",
            1,
            None,
            line=515,
        ),
    )
    expected = _REVIEWED_NON_SESSION_CONNECTIONS + expected_session_reachable
    root = _repo_root()
    files = {root / site.path for site in expected}
    live = [site for site in scan_production_writers(files, anchor=root) if site.operation == "write_connection"]
    assert inventory_drift(live, expected) == ([], [])


def test_writer_authority_must_match_the_table_policy() -> None:
    unclassified = WriterIdentity(
        path="src/elspeth/example.py",
        symbol="direct_writer",
        table="blobs",
        operation="update",
        fingerprint="unclassified",
        ordinal=1,
        authority=None,
    )
    mismatched = replace(
        unclassified,
        symbol="_RepositoryMutationTransaction.update_blob",
        fingerprint="wrong-authority",
        authority="SessionMutationAuthority",
    )
    correctly_classified = replace(
        unclassified,
        symbol="_SessionBlobMutationTransaction.update_blob",
        fingerprint="correct-authority",
        authority="SessionBlobMutationAuthority",
    )
    overly_broad_session_operation = replace(
        unclassified,
        table="sessions",
        operation="update",
        fingerprint="operation-specific",
        authority="SessionOperationAuthority",
    )
    connection_site = replace(
        unclassified,
        table="<sessions-write-connection>",
        operation="write_connection",
        fingerprint="connection",
    )

    missing, wrong = authority_policy_violations(
        [
            unclassified,
            mismatched,
            correctly_classified,
            overly_broad_session_operation,
            connection_site,
        ],
        _TABLE_POLICIES,
    )
    assert missing == [unclassified]
    assert wrong == [
        (mismatched, TablePolicy("blobs", "session", "SessionBlobMutationAuthority")),
        (
            overly_broad_session_operation,
            TablePolicy(
                "sessions",
                "session",
                "SessionMutationAuthority",
                (
                    ("SessionOperationAuthority", frozenset({"insert", "delete"})),
                    ("GuidedSessionMutationAuthority", frozenset({"update"})),
                    ("SessionForkAuthority", frozenset({"update"})),
                    ("SessionInterpretationAuthority", frozenset({"update"})),
                    ("RunDiagnosticsAuditMutationAuthority", frozenset({"update"})),
                    ("SessionComposerMutationAuthority", frozenset({"update"})),
                ),
            ),
        ),
    ]


def test_connections_that_outlive_their_with_block_are_escapes(tmp_path: Path) -> None:
    """A with-target used AFTER its block ended has left the acquisition's custody.

    Observed by lane 6b-2 during its per-authority gate mutations (comment
    9521 on elspeth-e483fe7f85): ``return conn`` after the ``with`` block
    changed only the fingerprint, while a callable hand-off inside the block
    was flagged. The name stays bound after the block, so a return, a yield,
    an attribute store, or a closure that captures it is exactly the
    escaping-reader class D6 step 5 must see. The contained case at the end
    pins that nothing here widens the ordinary in-block use.
    """
    source = tmp_path / "outliving_connections.py"
    source.write_text(
        textwrap.dedent(
            """\
            def returned_after_block(engine):
                with engine.begin() as conn:
                    pass
                return conn

            def yielded_after_block(engine):
                with engine.connect() as conn:
                    pass
                yield conn

            def stored_after_block(engine, holder):
                with engine.connect() as conn:
                    pass
                holder.conn = conn

            def captured_by_closure(engine):
                with engine.connect() as conn:
                    pass

                def later():
                    return conn

                return later

            def contained(engine):
                with engine.begin() as conn:
                    conn.execute("UPDATE sessions SET title = 'x'")
                return None
            """
        )
    )

    connections = [site for site in scan_production_writers([source], anchor=tmp_path) if site.operation == "write_connection"]
    assert Counter((site.symbol, site.connection_escape) for site in connections) == Counter(
        {
            ("returned_after_block", True): 1,
            ("yielded_after_block", True): 1,
            ("stored_after_block", True): 1,
            ("captured_by_closure", True): 1,
            ("contained", False): 1,
        }
    )


def test_all_production_sessions_writers_are_reviewed_typed_authorities() -> None:
    root = _repo_root()
    production_root = root / "src" / "elspeth"
    scanned = scan_production_writers(iter_gate_files(production_root), anchor=root)
    reviewed_read_policy_violations = reviewed_read_connection_policy_violations(_REVIEWED_READ_CONNECTIONS)
    sessions_domain, stale_non_session_connections = subtract_reviewed_identities(
        scanned,
        _REVIEWED_NON_SESSION_CONNECTIONS,
    )
    live, stale_read_connections = subtract_reviewed_read_identities(sessions_domain, _REVIEWED_READ_CONNECTIONS)
    unexpected, stale = inventory_drift(live, _REVIEWED_WRITERS)
    connection_violations = connection_authority_violations(live)
    unresolved_writers = [site for site in live if site.table == "<unresolved-session-write>"]
    unclassified_writers, authority_mismatches = authority_policy_violations(live, _TABLE_POLICIES)

    def describe(site: WriterIdentity) -> str:
        return (
            f"{site.path}:{site.line} {site.symbol} "
            f"{site.operation} {site.table} fp={site.fingerprint}#{site.ordinal} "
            f"authority={site.authority or 'UNCLASSIFIED'} "
            f"connection_escape={site.connection_escape}"
        )

    assert not (
        unexpected
        or stale
        or connection_violations
        or unresolved_writers
        or unclassified_writers
        or authority_mismatches
        or stale_read_connections
        or stale_non_session_connections
        or reviewed_read_policy_violations
    ), (
        "Sessions mutation authority inventory drift.\n"
        "Every production writer must appear exactly once in _REVIEWED_WRITERS "
        "after routing through the named typed authority in _TABLE_POLICIES.\n"
        f"Unexpected/unreviewed ({len(unexpected)}):\n"
        + "\n".join(f"  {describe(site)}" for site in unexpected[:80])
        + f"\nStale reviewed ({len(stale)}):\n"
        + "\n".join(f"  {describe(site)}" for site in stale[:40])
        + f"\nConnections outside exact contained authority ({len(connection_violations)}):\n"
        + "\n".join(f"  {describe(site)}" for site in connection_violations[:40])
        + f"\nUnresolved write executions ({len(unresolved_writers)}):\n"
        + "\n".join(f"  {describe(site)}" for site in unresolved_writers[:40])
        + f"\nWriters without a named authority ({len(unclassified_writers)}):\n"
        + "\n".join(f"  {describe(site)}" for site in unclassified_writers[:80])
        + f"\nWriters under the wrong table authority ({len(authority_mismatches)}):\n"
        + "\n".join(f"  {describe(site)} expected={policy.authority}" for site, policy in authority_mismatches[:80])
        + f"\nStale reviewed read connections ({len(stale_read_connections)}):\n"
        + "\n".join(f"  {describe(site)}" for site in stale_read_connections[:40])
        + f"\nStale non-Sessions connection classifications ({len(stale_non_session_connections)}):\n"
        + "\n".join(f"  {describe(site)}" for site in stale_non_session_connections[:40])
        + f"\nInvalid reviewed read connections ({len(reviewed_read_policy_violations)}):\n"
        + "\n".join(f"  {describe(site)}" for site in reviewed_read_policy_violations[:40])
    )
