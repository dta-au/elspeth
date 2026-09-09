"""Web authentication audit repository for Landscape."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, get_args

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.core.ids import generate_id
from elspeth.core.landscape._database_ops import DatabaseOps
from elspeth.core.landscape._helpers import now
from elspeth.core.landscape.schema import auth_events_table

AuthAuditEventType = Literal[
    # Authentication.
    "login",
    "token_issued",
    "auth_failure",
    "logout",
    # Admission and authority. Every one of these is an admin mutation whose
    # row is written synchronously, before the response.
    "identity_activated",
    "identity_disabled",
    "identity_enabled",
    "role_granted",
    "role_revoked",
    "relationship_asserted",
    "relationship_revoked",
    # Workflow governance.
    "approval_requested",
    "approval_decided",
    "review_requested",
    "review_request_cancelled",
    "review_attested",
    "library_published",
    "library_accepted",
    "library_rejected",
    "library_deprecated",
    "library_recalled",
    "quota_set",
    "quota_exceeded",
]
"""Closed vocabulary of auditable authentication and authority events.

The CHECK constraint backing this is closed too, so a MISSING value is a
self-inflicted outage: R4 refuses the mutation whose audit write failed, and
the write fails on the constraint. The list therefore has to be right while
the epoch is open — adding one afterwards is a table rewrite and a second
service-stop window.

Authorization denials deliberately have no member here. ``auth_failure``
exists, ``failure_category`` is an unconstrained ``String(64)`` and
``metadata_json`` is free-form, so ``{route, required_role}`` under
``failure_category='authz_denied'`` is writable with no schema change. And
business-rule refusals keep their own categories rather than being filed as
authorization denials: an authorized caller hitting a rule is not an
escalation attempt, and conflating the two poisons the audit view.
"""

AuthAuditOutcome = Literal["success", "failure"]

# Derived, not restated: a runtime guard that repeats its own contract drifts
# from it silently, and this one decides whether an audit row is written at
# all.
AUTH_AUDIT_EVENT_TYPES: tuple[AuthAuditEventType, ...] = get_args(AuthAuditEventType)
AUTH_AUDIT_SUCCESS: AuthAuditOutcome = "success"
AUTH_AUDIT_FAILURE: AuthAuditOutcome = "failure"
AUTH_AUDIT_OUTCOMES: tuple[AuthAuditOutcome, ...] = (AUTH_AUDIT_SUCCESS, AUTH_AUDIT_FAILURE)
AUTH_AUDIT_PRINCIPAL_MAX_LENGTH = 256
"""Maximum stored length for auth_events.user_id and auth_events.username.

Defence-in-depth for the audit principal. Local-auth usernames are already
bounded at the request boundary (LoginRequest/RegisterRequest), but the
signed-claim paths (OIDC sub/email) reach this layer without that boundary,
and SQLite does not enforce the String(256) column width.
"""


def _bounded_principal(value: str | None) -> str | None:
    """Constrain auth principal text to the auth_events schema length."""
    if value is None:
        return None
    if len(value) <= AUTH_AUDIT_PRINCIPAL_MAX_LENGTH:
        return value
    return value[:AUTH_AUDIT_PRINCIPAL_MAX_LENGTH]


class AuthAuditRepository:
    """Record non-run-scoped web authentication events in Landscape."""

    def __init__(self, ops: DatabaseOps) -> None:
        self._ops = ops

    @staticmethod
    def _auth_event_values(
        *,
        event_type: AuthAuditEventType,
        outcome: AuthAuditOutcome,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str | None,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        if event_type not in AUTH_AUDIT_EVENT_TYPES:
            raise AuditIntegrityError(f"Unsupported auth audit event_type: {event_type!r}")
        if outcome not in AUTH_AUDIT_OUTCOMES:
            raise AuditIntegrityError(f"Unsupported auth audit outcome: {outcome!r}")
        if outcome == AUTH_AUDIT_SUCCESS and failure_category is not None:
            raise AuditIntegrityError("Successful auth audit events must not carry failure_category")
        if outcome == AUTH_AUDIT_FAILURE and failure_category is None:
            raise AuditIntegrityError("Failed auth audit events must carry failure_category")

        event_id = generate_id()
        return event_id, {
            "event_id": event_id,
            "occurred_at": now(),
            "event_type": event_type,
            "outcome": outcome,
            "provider": provider,
            # The durable join to the identity substrate. ``user_id`` is the
            # principal as the request named it and stays whatever the token
            # carried; ``identity_id`` is the row every ownership FK points
            # at, so an identity renamed or re-subjected later is still
            # traceable through its events.
            "identity_id": identity_id,
            "user_id": _bounded_principal(user_id),
            "username": _bounded_principal(username),
            "failure_category": failure_category,
            "request_id": request_id,
            "client_host": client_host,
            "user_agent": user_agent,
            "metadata_json": canonical_json(metadata),
        }

    def record_auth_event(
        self,
        *,
        event_type: AuthAuditEventType,
        outcome: AuthAuditOutcome,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str | None,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        """Record an auth event synchronously before the HTTP response is sent."""
        event_id, values = self._auth_event_values(
            event_type=event_type,
            outcome=outcome,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=metadata,
            identity_id=identity_id,
        )
        self._ops.execute_insert(
            auth_events_table.insert().values(**values),
            context=f"record_auth_event event_type={event_type} outcome={outcome}",
        )
        return event_id

    def record_login_success_and_token_issued(
        self,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        login_metadata: Mapping[str, object],
        token_metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> tuple[str, str]:
        """Record successful login and token issuance in one transaction."""
        login_event_id, login_values = self._auth_event_values(
            event_type="login",
            outcome=AUTH_AUDIT_SUCCESS,
            provider=provider,
            identity_id=identity_id,
            user_id=user_id,
            username=username,
            failure_category=None,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=login_metadata,
        )
        token_event_id, token_values = self._auth_event_values(
            event_type="token_issued",
            outcome=AUTH_AUDIT_SUCCESS,
            provider=provider,
            identity_id=identity_id,
            user_id=user_id,
            username=username,
            failure_category=None,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=token_metadata,
        )
        self._ops.execute_insert(
            auth_events_table.insert().values([login_values, token_values]),
            context="record_login_success_and_token_issued",
        )
        return login_event_id, token_event_id

    def record_login_outcome(
        self,
        *,
        outcome: AuthAuditOutcome,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str | None,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        """Record a local login success or failed credential attempt."""
        return self.record_auth_event(
            event_type="login",
            outcome=outcome,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=metadata,
            identity_id=identity_id,
        )

    def record_token_issued(
        self,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        """Record access-token issuance without storing the bearer token."""
        return self.record_auth_event(
            event_type="token_issued",
            outcome=AUTH_AUDIT_SUCCESS,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=None,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=metadata,
            identity_id=identity_id,
        )

    def record_auth_failure(
        self,
        *,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        """Record an authentication or profile-lookup failure classification."""
        return self.record_auth_event(
            event_type="auth_failure",
            outcome=AUTH_AUDIT_FAILURE,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=metadata,
            identity_id=identity_id,
        )
