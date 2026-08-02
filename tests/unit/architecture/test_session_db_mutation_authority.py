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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from elspeth.web.sessions.models import metadata as sessions_metadata

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
        ),
    ),
    TablePolicy("composer_completion_events", "session", "SessionComposerMutationAuthority"),
    TablePolicy("composer_inflight_requests", "session", "SessionComposerProgressAuthority"),
    TablePolicy("composer_progress_snapshots", "session", "SessionComposerProgressAuthority"),
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
    TablePolicy(
        "sessions",
        "session",
        "SessionMutationAuthority",
        (
            ("SessionOperationAuthority", frozenset({"insert", "delete"})),
            ("GuidedSessionMutationAuthority", frozenset({"update"})),
            ("SessionForkAuthority", frozenset({"update"})),
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
        "_SessionComposerMutations.record_preferences_changed",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionMutations.update_composer_preferences",
        "SessionMutationAuthority",
    ),
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
    AuthoritySymbol(
        "src/elspeth/web/preferences/service.py",
        "RepositoryUserPreferenceAuthority.apply_patch",
        "UserPreferenceAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "SkillMarkdownHistoryAuthority",
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
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.start_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.publish_progress",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.finish_request",
        "SessionComposerProgressAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.retire_session_progress",
        "SessionComposerProgressAuthority",
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
)

# Connection acquisition is a separate capability from table mutation.  A
# table authority name alone must never bless an Engine/Connection boundary.
# Entries here are exact (not prefixes) and may only contain the acquisition;
# returned, yielded, or forwarded raw connections remain violations.
_CONTAINED_CONNECTION_AUTHORITIES: tuple[AuthoritySymbol, ...] = (
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.record_preferences_changed",
        "SessionComposerMutationAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/service.py",
        "_SessionMutations.update_composer_preferences",
        "SessionMutationAuthority",
    ),
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
    AuthoritySymbol(
        "src/elspeth/web/preferences/service.py",
        "RepositoryUserPreferenceAuthority.apply_patch._sync",
        "UserPreferenceAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "SkillMarkdownHistoryAuthority",
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
)

# Literal identities for writers that sit behind an exact named authority.
# This manifest grows only after routing a site through its table-specific
# typed authority.
_REVIEWED_WRITERS: tuple[WriterIdentity, ...] = (
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.record_preferences_changed",
        "proposal_events",
        "insert",
        "16c7ae1fb6863cdd",
        1,
        "SessionComposerMutationAuthority",
        line=3486,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionMutations.update_composer_preferences",
        "sessions",
        "update",
        "ef981e78e83eaad9",
        1,
        "SessionMutationAuthority",
        line=3997,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "proposal_events",
        "insert",
        "55c8854837524a3f",
        1,
        "SessionComposerMutationAuthority",
        line=3527,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_composition_proposal",
        "composition_proposals",
        "insert",
        "4d7437366c54fbeb",
        1,
        "SessionComposerMutationAuthority",
        line=3543,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "proposal_events",
        "insert",
        "58a94a42ebf58130",
        1,
        "SessionComposerMutationAuthority",
        line=3650,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.create_pipeline_composition_proposal",
        "composition_proposals",
        "insert",
        "be0a21ec508fea9c",
        1,
        "SessionComposerMutationAuthority",
        line=3661,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "proposal_events",
        "insert",
        "17277db356846ba4",
        1,
        "SessionComposerMutationAuthority",
        line=3791,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.reject_pending_proposal",
        "composition_proposals",
        "update",
        "8b790876eab3ca5d",
        1,
        "SessionComposerMutationAuthority",
        line=3802,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "proposal_events",
        "insert",
        "f381d823a069aec1",
        1,
        "SessionComposerMutationAuthority",
        line=3910,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "proposal_blob_effect_receipts",
        "update",
        "838f74d6c673e89a",
        1,
        "SessionComposerMutationAuthority",
        line=3922,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_SessionComposerMutations.accept_pending_ordinary_proposal",
        "composition_proposals",
        "update",
        "136c26279232b29b",
        1,
        "SessionComposerMutationAuthority",
        line=3934,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "RepositoryUserPreferenceAuthority.apply_patch._sync",
        "user_preferences",
        "insert",
        "8e94ada6ed873608",
        1,
        "UserPreferenceAuthority",
        line=427,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "RepositoryUserPreferenceAuthority.apply_patch._sync",
        "user_preferences",
        "insert",
        "8e94ada6ed873608",
        2,
        "UserPreferenceAuthority",
        line=429,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "skill_markdown_history",
        "insert",
        "7c2e2edb43655500",
        1,
        "SkillMarkdownHistoryAuthority",
        line=55,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/skill_markdown_history.py",
        "RepositorySkillMarkdownHistoryAuthority.upsert_exact",
        "skill_markdown_history",
        "insert",
        "7c2e2edb43655500",
        2,
        "SkillMarkdownHistoryAuthority",
        line=57,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "insert",
        "96893964262bac72",
        1,
        "UserSecretAuthority",
        line=170,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "insert",
        "8b0fd0f595fe372f",
        1,
        "UserSecretAuthority",
        line=178,
    ),
    WriterIdentity(
        "src/elspeth/web/secrets/user_store.py",
        "RepositoryUserSecretAuthority.upsert_encrypted_secret",
        "user_secrets",
        "insert",
        "3ac0a3fe10d1d8c0",
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
        line=2060,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.prepare_blob_replacement",
        "blob_replacement_cleanups",
        "insert",
        "bca2ee74a8a93022",
        1,
        "SessionBlobMutationAuthority",
        line=1251,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.mark_blob_replacement_staged",
        "blob_replacement_cleanups",
        "update",
        "6128417bd9a69f02",
        1,
        "SessionBlobMutationAuthority",
        line=1314,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_replacement",
        "blob_replacement_cleanups",
        "update",
        "2ecd759927d60392",
        1,
        "SessionBlobMutationAuthority",
        line=1368,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_replacement",
        "blobs",
        "update",
        "d2ea73ee0e8472e4",
        1,
        "SessionBlobMutationAuthority",
        line=1376,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.retire_blob_replacement",
        "blob_replacement_cleanups",
        "delete",
        "75415f0d12b7a661",
        1,
        "SessionBlobMutationAuthority",
        line=1428,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryBlobMutations.abort_blob_replacement",
        "blob_replacement_cleanups",
        "delete",
        "75415f0d12b7a661",
        1,
        "SessionBlobMutationAuthority",
        line=1448,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositorySessionMutations.record_plugin_crash_breadcrumb",
        "sessions",
        "update",
        "016ef4c50b5e5390",
        1,
        "SessionMutationAuthority",
        line=517,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "sessions",
        "insert",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=3317,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "session_operation_fences",
        "insert",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=3327,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.create_session_with_initial_fence",
        "session_operation_fences",
        "update",
        "3d19f2b10e4f6a34",
        1,
        "SessionOperationAuthority",
        line=3341,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.acquire",
        "session_operation_fences",
        "update",
        "83981192adddd83f",
        1,
        "SessionOperationAuthority",
        line=3421,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._compare_and_swap_on_connection",
        "session_operation_fences",
        "update",
        "ff923dcba8b3e8e7",
        1,
        "SessionOperationAuthority",
        line=3504,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.renew",
        "session_operation_fences",
        "update",
        "0047af5dc9935ff1",
        1,
        "SessionOperationAuthority",
        line=3564,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.release",
        "session_operation_fences",
        "update",
        "e462792ce8c571f3",
        1,
        "SessionOperationAuthority",
        line=4093,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository.archive_delete",
        "sessions",
        "delete",
        "66cc108182d86009",
        1,
        "SessionOperationAuthority",
        line=4113,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "sessions",
        "insert",
        "b964b22650d92b97",
        1,
        "SessionOperationAuthority",
        line=3869,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "session_operation_fences",
        "insert",
        "d5ab9aa3497d191a",
        1,
        "SessionOperationAuthority",
        line=3884,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._insert_fork_child",
        "session_operation_fences",
        "update",
        "7326f9c03db2a1c7",
        1,
        "SessionOperationAuthority",
        line=3898,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_SessionOperationAuthorityRepository._resume_or_take_over_fork_child",
        "session_operation_fences",
        "update",
        "988ac755147ef9bc",
        1,
        "SessionOperationAuthority",
        line=3959,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.insert_child_state",
        "composition_states",
        "insert",
        "ae78f92031e7eebb",
        1,
        "SessionForkChildMutations",
        line=2709,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkChildSessionMutations.append_child_messages",
        "chat_messages",
        "insert",
        "b3fcd50e04854888",
        1,
        "SessionForkChildMutations",
        line=2762,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_ForkParentGuidedMutations.bind_guided_fork",
        "guided_operations",
        "update",
        "16cc2a98abfd1f5e",
        1,
        "SessionForkParentGuidedMutations",
        line=2895,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.start_request",
        "composer_inflight_requests",
        "insert",
        "56eb1a553056ab83",
        1,
        "SessionComposerProgressAuthority",
        line=277,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.start_request",
        "composer_inflight_requests",
        "update",
        "56eb1a553056ab83",
        1,
        "SessionComposerProgressAuthority",
        line=284,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.start_request",
        "composer_progress_snapshots",
        "insert",
        "4397a63b112f5d23",
        1,
        "SessionComposerProgressAuthority",
        line=302,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.start_request",
        "composer_progress_snapshots",
        "update",
        "4397a63b112f5d23",
        1,
        "SessionComposerProgressAuthority",
        line=309,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.publish_progress",
        "composer_inflight_requests",
        "update",
        "8883bc7e3f74724f",
        1,
        "SessionComposerProgressAuthority",
        line=339,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.publish_progress",
        "composer_progress_snapshots",
        "update",
        "fc96529526c55da5",
        1,
        "SessionComposerProgressAuthority",
        line=349,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.finish_request",
        "composer_inflight_requests",
        "update",
        "6c7edc31abbf6a81",
        1,
        "SessionComposerProgressAuthority",
        line=386,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.finish_request",
        "composer_progress_snapshots",
        "update",
        "4741ea78e4077bd6",
        1,
        "SessionComposerProgressAuthority",
        line=403,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.retire_session_progress",
        "composer_inflight_requests",
        "delete",
        "55bace25d65b2344",
        1,
        "SessionComposerProgressAuthority",
        line=415,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_mutations.py",
        "RepositoryComposerProgressMutations.retire_session_progress",
        "composer_progress_snapshots",
        "delete",
        "4992985ad093271a",
        1,
        "SessionComposerProgressAuthority",
        line=418,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.create_pending_run",
        "runs",
        "insert",
        "6e3fcabf86bb6ffa",
        1,
        "SessionRunMutationAuthority",
        line=656,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryRunMutations.transition_run_status",
        "runs",
        "update",
        "f926157e24accee8",
        1,
        "SessionRunMutationAuthority",
        line=734,
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
        line=4303,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/repository.py",
        "_RepositoryComposerCompletionMutations.record_yaml_export",
        "composer_completion_events",
        "insert",
        "2e42be4b632cb581",
        1,
        "SessionComposerMutationAuthority",
        line=4327,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reserve_guided_operation._sync",
        "guided_operations",
        "insert",
        "1161702a3f59ea98",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5375,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reserve_guided_operation._sync",
        "guided_operations",
        "update",
        "1161702a3f59ea98",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5434,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reconcile_guided_start_operation._sync",
        "guided_operation_admission_blocks",
        "insert",
        "ca00ab3741ac8f83",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5632,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.reconcile_guided_start_operation._sync",
        "guided_operations",
        "update",
        "ca00ab3741ac8f83",
        1,
        "GuidedSessionAdmissionAuthority",
        line=5670,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.renew_guided_operation._sync",
        "guided_operations",
        "update",
        "343692d13bca60b9",
        1,
        "GuidedSessionMutationAuthority",
        line=5776,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.fail_guided_operation_with_audit._sync",
        "sessions",
        "update",
        "0cce6545ca848e15",
        1,
        "GuidedSessionMutationAuthority",
        line=6087,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "chat_messages",
        "update",
        "937f08692f6ed0fa",
        1,
        "SessionForkAuthority",
        line=13785,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "composition_states",
        "delete",
        "937f08692f6ed0fa",
        1,
        "SessionForkAuthority",
        line=13796,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "chat_messages",
        "update",
        "937f08692f6ed0fa",
        2,
        "SessionForkAuthority",
        line=13815,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "guided_operations",
        "insert",
        "937f08692f6ed0fa",
        1,
        "SessionForkAuthority",
        line=13863,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.settle_guided_fork_operation._sync",
        "sessions",
        "update",
        "937f08692f6ed0fa",
        1,
        "SessionForkAuthority",
        line=13917,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.record_nonterminal_event",
        "guided_operation_events",
        "insert",
        "c66670f774b6404d",
        1,
        "GuidedSessionMutationAuthority",
        line=4128,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.bind",
        "guided_operations",
        "update",
        "0d2776483587fc01",
        1,
        "GuidedSessionMutationAuthority",
        line=4168,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.require_no_active_confirmation",
        "guided_operations",
        "update",
        "d2fd5f53fcc3d7de",
        1,
        "GuidedSessionMutationAuthority",
        line=4189,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.claim_confirmation",
        "guided_operations",
        "update",
        "d2fd5f53fcc3d7de",
        1,
        "GuidedSessionMutationAuthority",
        line=4219,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.claim_confirmation",
        "guided_operations",
        "update",
        "95efdd37e97b888e",
        1,
        "GuidedSessionMutationAuthority",
        line=4243,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.complete",
        "guided_operations",
        "update",
        "e7ef88803ab1d8bb",
        1,
        "GuidedSessionMutationAuthority",
        line=4302,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.complete",
        "guided_operation_events",
        "insert",
        "e7ef88803ab1d8bb",
        1,
        "GuidedSessionMutationAuthority",
        line=4332,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.fail",
        "guided_operations",
        "update",
        "9c6096dc61fbf4cd",
        1,
        "GuidedSessionMutationAuthority",
        line=4372,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedSessionMutations.fail",
        "guided_operation_events",
        "insert",
        "9c6096dc61fbf4cd",
        1,
        "GuidedSessionMutationAuthority",
        line=4406,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.reject_pending_proposal",
        "proposal_events",
        "insert",
        "472e557358d79356",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4464,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "_GuidedComposerMutations.reject_pending_proposal",
        "composition_proposals",
        "update",
        "50a069cc3fb3a8a1",
        1,
        "GuidedSessionComposerMutationAuthority",
        line=4475,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl._record_guided_fork_child_terminal_event",
        "guided_operation_events",
        "insert",
        "1a8637d3ecf9263b",
        1,
        "SessionForkAuthority",
        line=5004,
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
        "086c51914be91a18",
        1,
        None,
        line=2222,
    ),
    WriterIdentity(
        "src/elspeth/web/blobs/service.py",
        "BlobServiceImpl.copy_blobs_for_fork._verify_exact_target",
        "<sessions-write-connection>",
        "write_connection",
        "bf513a1ca8d82133",
        1,
        None,
        line=2337,
    ),
    WriterIdentity(
        "src/elspeth/web/preferences/service.py",
        "PreferencesService.get_composer_preferences._sync",
        "<sessions-write-connection>",
        "write_connection",
        "ec5025f8293f8b8f",
        1,
        None,
        line=532,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_guided_operation._sync",
        "<sessions-write-connection>",
        "write_connection",
        "6b957b9ee0b57856",
        1,
        None,
        line=4182,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_pipeline_dispatch_recovery._sync",
        "<sessions-write-connection>",
        "write_connection",
        "e55c3bb74cb8ef7c",
        1,
        None,
        line=6395,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.get_authoritative_composition_proposal._sync",
        "<sessions-write-connection>",
        "write_connection",
        "4cfe2dd7bf8391d1",
        1,
        None,
        line=6111,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
        "SessionServiceImpl.list_composition_proposals._sync",
        "<sessions-write-connection>",
        "write_connection",
        "4df560c0a5d93b07",
        1,
        None,
        line=6529,
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
        line=120,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "begin_write",
        "<sessions-write-connection>",
        "write_connection",
        "2006ebd10a466428",
        1,
        None,
        line=254,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_sqlite_epoch_is_incompatible",
        "<sessions-write-connection>",
        "write_connection",
        "1084a431b73718a3",
        1,
        None,
        line=731,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_landscape_identity_issue",
        "<sessions-write-connection>",
        "write_connection",
        "54f25c9b2650a66b",
        1,
        None,
        line=756,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "_collect_token_outcomes_shape_errors",
        "<sessions-write-connection>",
        "write_connection",
        "f95bb339c5816e92",
        1,
        None,
        line=854,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._verify_sqlite_pragmas",
        "<sessions-write-connection>",
        "write_connection",
        "7b14f45607d6611f",
        1,
        None,
        line=1073,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._create_sqlcipher_engine._creator",
        "<sessions-write-connection>",
        "write_connection",
        "bc4b6272008ed6ec",
        1,
        None,
        line=1193,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._sync_schema_identity",
        "<sessions-write-connection>",
        "write_connection",
        "b92e2e573b8362dd",
        1,
        None,
        line=1214,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._get_sqlite_schema_epoch",
        "<sessions-write-connection>",
        "write_connection",
        "4644a6cc893b4d09",
        1,
        None,
        line=1254,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB._validate_schema",
        "<sessions-write-connection>",
        "write_connection",
        "17cf574862c872cb",
        1,
        None,
        line=1520,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/database.py",
        "LandscapeDB.read_only_connection",
        "<sessions-write-connection>",
        "write_connection",
        "44c4543542ceeb85",
        1,
        None,
        line=1980,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/export_read_model.py",
        "open_export_read_transaction",
        "<sessions-write-connection>",
        "write_connection",
        "a92d195f4d517fb2",
        1,
        None,
        line=420,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/export_read_model.py",
        "open_export_read_transaction",
        "<sessions-write-connection>",
        "write_connection",
        "0c6cc422f2cd9eb1",
        1,
        None,
        line=425,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/leases.py",
        "SchedulerLeaseRepository.heartbeat_lease",
        "<non-session-write-connection>",
        "write_connection",
        "f38cd0992716f10c",
        1,
        None,
        line=743,
        connection_escape=True,
    ),
    WriterIdentity(
        "src/elspeth/core/landscape/scheduler/leases.py",
        "SchedulerLeaseRepository.peer_active_leases",
        "<non-session-write-connection>",
        "write_connection",
        "d14e0f51a84f117b",
        1,
        None,
        line=894,
    ),
    WriterIdentity(
        "src/elspeth/plugins/sinks/database_sink.py",
        "DatabaseSink._inspect_target_contract",
        "<sessions-write-connection>",
        "write_connection",
        "783744ee3e5ef624",
        1,
        None,
        line=427,
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
        line=840,
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
        line=860,
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
        line=913,
    ),
    WriterIdentity(
        "src/elspeth/web/auth/local.py",
        "LocalAuthProvider._get_conn",
        "<non-session-write-connection>",
        "write_connection",
        "df6185754d14a63c",
        1,
        None,
        line=249,
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
    }
)
_NON_SESSION_ENGINE_TYPES = frozenset(
    {
        "elspeth.core.landscape.database.LandscapeDB",
        "elspeth.core.landscape.database.Tier1Engine",
    }
)
_EXPLICIT_NON_SQL_EXECUTE_RECEIVER_TYPES = frozenset(
    {
        "elspeth.web.execution.protocol.ExecutionService",
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


def _raw_sql_is_obviously_read_only(sql: str) -> bool:
    normalized = sql.lstrip().upper()
    if normalized.startswith(("SELECT", "WITH RECURSIVE", "EXPLAIN")):
        return True
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
    normalized = ast.dump(current, annotate_fields=True, include_attributes=False)
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


class _ProductionWriterCollector(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST) -> None:
        self.path = path
        self.tree = tree
        self.import_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.assignment_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.definition_bindings: dict[tuple[int, str], list[_NameBinding]] = {}
        self.attribute_bindings: dict[str, list[_NameBinding]] = {}
        self.shadowed_names: set[tuple[int, str]] = set()
        self.sites: list[WriterIdentity] = []
        self.write_connection_calls: dict[int, tuple[ast.Call, bool]] = {}
        self.classified_execution_calls: set[int] = set()
        self._dependent_write_context_cache: dict[int, tuple[list[ast.stmt], list[ast.Call]]] = {}
        self._collect_aliases()

    def _record_connection(self, acquisition: ast.Call, *, escapes: bool) -> None:
        existing = self.write_connection_calls.get(id(acquisition))
        self.write_connection_calls[id(acquisition)] = (
            acquisition,
            escapes or (existing[1] if existing is not None else False),
        )

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
        if not (
            isinstance(execution.func, ast.Attribute) and execution.func.attr == "execute" and isinstance(execution.func.value, ast.Name)
        ):
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
                if binding.value is None:
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
            attribute_key = ast.dump(expression, include_attributes=False)
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
                ast.dump(context, annotate_fields=True, include_attributes=False),
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
        if isinstance(expression.func, ast.Attribute) and expression.func.attr in {"execution_options"}:
            return self._connection_acquisitions_for_expression(
                use,
                expression.func.value,
                visited=visited,
            )
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
                ("subscript", base, ast.dump(expression.slice, include_attributes=False))
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
        normalized = "\0".join(ast.dump(statement, annotate_fields=True, include_attributes=False) for statement in ordered)
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

        # ``conn = engine.connect()`` and simple aliases in one lexical scope.
        reaching, _, _ = self._visible_reaching_bindings(use, name)
        acquisitions: dict[int, ast.Call] = {}
        for binding in reaching:
            for acquisition in self._connection_acquisitions_for_expression(
                binding.node,
                binding.value,
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
            reaching, complete, _ = self._potentially_reaching_bindings(use, expression.id)
            if not complete or not reaching or any(binding.value is None for binding in reaching):
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
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return _raw_sql_is_obviously_read_only(expression.value)
        return False

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
                self._record_connection(acquisition, escapes=stored_callable)
            if isinstance(func, ast.Attribute) and func.attr in {"execute", "executemany", "exec_driver_sql"}:
                acquisitions = self._connection_acquisitions_for(call)
                statement = call.args[0] if call.args else None
                if self._is_obviously_read_only_statement(
                    statement,
                    use=call,
                ):
                    continue
                statement_domain, force_unresolved = self._statement_database_evidence(statement)
                connection_domain = self._execution_connection_database_domain(call)
                if connection_domain == "non_sessions":
                    self.classified_execution_calls.add(id(call))
                    continue
                if statement_domain == "non_sessions":
                    self._append_site(
                        call,
                        "<unresolved-session-write>",
                        f"unknown_{func.attr}",
                    )
                    for acquisition in acquisitions:
                        self._record_connection(acquisition, escapes=False)
                    continue
                if statement_domain == "unknown" and (
                    force_unresolved or (not acquisitions and id(call) not in self.classified_execution_calls)
                ):
                    self._append_site(
                        call,
                        "<unresolved-session-write>",
                        f"unknown_{func.attr}",
                    )
                for acquisition in acquisitions:
                    self._record_connection(acquisition, escapes=False)
                continue

            # ``with engine.begin() as conn: imported_or_local_helper(conn)``.
            # A helper can hide a prebuilt or dynamically generated write, so
            # forwarding a raw connection is write-capable until a typed
            # authority removes that path.
            for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
                for acquisition in self._connection_acquisitions_for_expression(call, argument):
                    self._record_connection(acquisition, escapes=True)

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
            if operation == "insert" and any(
                "on_conflict_do_" in ast.dump(statement, include_attributes=False) for statement in statements
            ):
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
        for call, escapes in self.write_connection_calls.values():
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
    sites: list[WriterIdentity] = []
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
        sites.extend(_ProductionWriterCollector(relative, tree).collect())
    return sites


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
    root = _repo_root()
    service_path = root / "src/elspeth/web/preferences/service.py"
    authority_symbol = "RepositoryUserPreferenceAuthority.apply_patch._sync"
    live = scan_production_writers([service_path], anchor=root)
    authority_live = [site for site in live if site.symbol.startswith("RepositoryUserPreferenceAuthority.apply_patch")]
    writes = [site for site in authority_live if site.table == "user_preferences"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "user_preferences"]
    connections = [site for site in authority_live if site.operation == "write_connection"]

    assert len(writes) == len(reviewed) == 2
    assert inventory_drift(writes, reviewed) == ([], [])
    assert {site.authority for site in writes} == {"UserPreferenceAuthority"}
    assert connections == [
        WriterIdentity(
            "src/elspeth/web/preferences/service.py",
            authority_symbol,
            "<sessions-write-connection>",
            "write_connection",
            "d375e08da8900262",
            1,
            "UserPreferenceAuthority",
            line=310,
        )
    ]
    assert connection_authority_violations(authority_live) == []
    assert not [site for site in authority_live if site.table == "<unresolved-session-write>"]
    assert not [
        site
        for site in live
        if site.symbol.startswith("PreferencesService.update_composer_preferences")
        and (site.table == "user_preferences" or site.table == "<unresolved-session-write>")
    ]


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
    progress_path = "src/elspeth/web/coordination/composer_progress_mutations.py"
    for method in ("start_request", "publish_progress", "finish_request", "retire_session_progress"):
        assert _authority_for(progress_path, f"RepositoryComposerProgressMutations.{method}") == "SessionComposerProgressAuthority"
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
    assert _authority_for(progress_path, "RepositoryComposerProgressMutations.start_request.helper") == ("SessionComposerProgressAuthority")
    assert _authority_for(progress_path, "RepositoryComposerProgressMutations.start_request_replacement") is None
    assert _authority_for(progress_path, "RepositoryComposerProgressMutations.future_method") is None
    assert _authority_for(path, "RepositoryComposerProgressMutations.start_request") is None
    policies = {policy.table: policy for policy in _TABLE_POLICIES}
    assert policies["chat_messages"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"update"})),
    )
    assert policies["composition_states"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"delete"})),
    )
    assert policies["guided_operations"].operation_authorities == (
        ("SessionForkParentGuidedMutations", frozenset({"update"})),
        ("GuidedSessionAdmissionAuthority", frozenset({"insert", "update"})),
        ("SessionForkAuthority", frozenset({"insert"})),
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


def test_existing_run_and_composer_progress_facet_writer_identities_are_exact_and_bidirectional() -> None:
    root = _repo_root()
    paths = [
        root / "src/elspeth/web/coordination/repository.py",
        root / "src/elspeth/web/coordination/composer_progress_mutations.py",
    ]
    symbols = {
        "_RepositoryRunMutations.create_pending_run",
        "_RepositoryRunMutations.transition_run_status",
        "RepositoryComposerProgressMutations.start_request",
        "RepositoryComposerProgressMutations.publish_progress",
        "RepositoryComposerProgressMutations.finish_request",
        "RepositoryComposerProgressMutations.retire_session_progress",
    }
    live = [site for site in scan_production_writers(paths, anchor=root) if site.symbol in symbols]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(live) == len(reviewed) == 12
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
            "SessionServiceImpl.cancel_all_orphaned_run_records",
            "SessionServiceImpl.mark_landscape_reconciliation_outcomes",
        }
        and site.table in {"runs", "<unresolved-session-write>"}
    ]
    authority_live = [site for site in scanned if site.symbol in symbols]
    run_writes = [site for site in authority_live if site.table == "runs"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in symbols]
    assert len(run_writes) == len(reviewed) == 2
    assert inventory_drift(run_writes, reviewed) == ([], [])
    assert authority_policy_violations(run_writes, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(authority_live) == []


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
    root = _repo_root()
    path = root / "src/elspeth/web/sessions/service.py"
    authorities = {
        "_SessionComposerMutations.record_preferences_changed": "SessionComposerMutationAuthority",
        "_SessionMutations.update_composer_preferences": "SessionMutationAuthority",
    }
    for symbol, authority in authorities.items():
        assert _authority_for("src/elspeth/web/sessions/service.py", symbol) == authority
        assert _contained_connection_authority_for("src/elspeth/web/sessions/service.py", symbol) == authority
    scanned = scan_production_writers([path], anchor=root)
    assert not [
        site
        for site in scanned
        if site.symbol == "SessionServiceImpl.update_composer_preferences._sync" and site.table in {"proposal_events", "sessions"}
    ]
    live = [site for site in scanned if site.symbol in authorities]
    reviewed = [site for site in _REVIEWED_WRITERS if site.symbol in authorities]
    assert len(live) == len(reviewed) == 2
    assert inventory_drift(live, reviewed) == ([], [])
    assert authority_policy_violations(live, _TABLE_POLICIES) == ([], [])
    assert connection_authority_violations(live) == []


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
    )
    assert policies["chat_messages"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"update"})),
    )
    assert policies["composition_states"].operation_authorities == (
        ("SessionForkChildMutations", frozenset({"insert"})),
        ("SessionForkAuthority", frozenset({"delete"})),
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
    assert [(site.symbol, site.table, site.operation) for site in sites] == [
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
    assert [(site.symbol, site.line) for site in sites] == [
        ("module_alias_reader", 5),
        ("direct_alias_reader", 9),
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
    assert [(site.symbol, site.table, site.line) for site in sites] == [
        ("module_select", "<sessions-write-connection>", 12),
        ("module_sqlite", "<sessions-write-connection>", 16),
        ("function_select", "<sessions-write-connection>", 19),
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
    assert sorted((site.symbol, site.line) for site in sites) == [
        ("post_branch_use", 12),
        ("sibling_use", 4),
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
    assert [(site.symbol, site.line) for site in sites] == [
        ("Reader.read", 7),
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
    assert sorted((site.symbol, site.line) for site in sites) == [
        ("locally_shadowed", 10),
        ("locally_shadowed.inner", 14),
        ("writer", 2),
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
        assert [(site.table, site.operation) for site in live] == [
            ("<sessions-write-connection>", "write_connection"),
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
    assert [(site.symbol, site.table, site.line) for site in sites] == [
        ("Outer.Inner.read", "<sessions-write-connection>", 8),
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
    assert [(site.symbol, site.line) for site in sites] == [
        ("shadowed.inner", 15),
        ("writer.inner", 5),
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
    assert [(site.symbol, site.table, site.operation) for site in sites if site.operation != "write_connection"] == [
        ("sessions_writer", "sessions", "update"),
        ("landscape_table_writer", "<unresolved-session-write>", "unknown_execute"),
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
    assert [(site.symbol, site.table, site.operation, site.line) for site in sites] == [
        ("ambiguous", "<sessions-write-connection>", "write_connection", 7),
        ("shadowed", "<sessions-write-connection>", "write_connection", 14),
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
        "a92d195f4d517fb2",
        1,
        None,
        line=420,
        connection_escape=True,
    )
    assert len(_REVIEWED_NON_SESSION_CONNECTIONS) == 20
    assert export_read_transaction in _REVIEWED_NON_SESSION_CONNECTIONS
    expected_session_reachable: tuple[WriterIdentity, ...] = (
        WriterIdentity(
            "src/elspeth/core/schema_shape.py",
            "_collect_sqlite_table_option_issues",
            "<sessions-write-connection>",
            "write_connection",
            "0564af7982a64ad9",
            1,
            None,
            line=705,
        ),
        WriterIdentity(
            "src/elspeth/core/schema_shape.py",
            "_proven_pg_catalog_text_builtin_calls",
            "<sessions-write-connection>",
            "write_connection",
            "f7e670e443783cc3",
            1,
            None,
            line=722,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/doctor.py",
            "_inspect_database",
            "<sessions-write-connection>",
            "write_connection",
            "678f5f038fbf6e2f",
            1,
            None,
            line=376,
            connection_escape=True,
        ),
        WriterIdentity(
            "src/elspeth/web/doctor.py",
            "_initialize_database",
            "<sessions-write-connection>",
            "write_connection",
            "5156e2c4978a3a8f",
            1,
            None,
            line=442,
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
            line=132,
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
            line=222,
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
            line=208,
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
            line=254,
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
            line=454,
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
                ),
            ),
        ),
    ]


def test_all_production_sessions_writers_are_reviewed_typed_authorities() -> None:
    root = _repo_root()
    production_root = root / "src" / "elspeth"
    scanned = scan_production_writers(production_root.rglob("*.py"), anchor=root)
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
