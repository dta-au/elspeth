"""Request-bound web authentication audit helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast

import jwt as pyjwt
import structlog
from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.auth import (
    AUTH_EVENT_CONSOLE_REQUEST_ID_KEY,
    AUTH_EVENT_ON_BEHALF_OF_KEY,
    AuthProviderType,
    IdentityRole,
    RelationshipType,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.web.auth.models import AccessPending, AuthenticationError, AuthProviderUnavailable, IdentityDisabled
from elspeth.web.auth.sso import SsoLoginError
from elspeth.web.deployment_contract import resolve_deployment_state_mode
from elspeth.web.schema_probe import postgres_engine_kwargs

if TYPE_CHECKING:
    from elspeth.web.config import WebSettings


_slog = structlog.get_logger(__name__)


MAX_AUTH_AUDIT_TEXT_LENGTH = 512
"""Maximum length for caller-controlled auth audit context fields."""


class AuthAuditWriter(Protocol):
    """Interface consumed by auth routes for must-fire auth audit writes."""

    def record_login_success_and_token_issued(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        access_token: str,
    ) -> None: ...

    def record_login_success(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        identity_id: str | None = None,
    ) -> None: ...

    def record_login_failure(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        username: str,
        failure_category: str,
    ) -> None: ...

    def record_token_issued(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        access_token: str,
        issuance_path: str,
        login_request_id: str | None = None,
    ) -> None: ...

    def record_auth_failure(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        failure_category: str,
        failure_stage: str,
        user_id: str | None,
        username: str | None,
        exception_class: str | None,
        identity_id: str | None = None,
    ) -> None: ...

    # Two members with no ``request``: a self-admitting deployment activates
    # inside the login worker, where there is none to read, and a credential
    # deletion retires its identity from whichever surface deleted it.
    def record_identity_admitted(
        self,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        tokens_per_day: int | None,
        storage_bytes: int | None,
    ) -> None: ...

    def record_identity_retired(
        self,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        retired_subject: str,
        reason: str,
    ) -> None: ...

    def record_logout(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
    ) -> None: ...

    # Admin mutations (identity sprint step D). ``request`` is optional
    # because the same mutation reaches the trail from three surfaces: the
    # admin routes (a request), the bootstrap seed inside the login worker
    # (none), and the operator CLI (none). Absent means the request columns
    # are NULL, never invented.
    def record_identity_activated(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str | None,
        cause: AdminActivationCause,
        note: str,
        role: IdentityRole | None,
        role_id: str | None,
        tokens_per_day: int | None,
        storage_bytes: int | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None: ...

    def record_identity_enabled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str,
        note: str,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None: ...

    def record_identity_disabled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str,
        reason: str,
        revoked_relationship_ids: tuple[str, ...],
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None: ...

    def record_role_changed(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str | None,
        actor_identity_id: str,
        change: RoleChange,
        role: IdentityRole,
        role_id: str,
        scope: str | None,
        expires_at: datetime | None,
        note: str | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None: ...

    def record_relationship_changed(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        actor_identity_id: str,
        change: RelationshipChange,
        relationship_id: str,
        from_identity_id: str,
        to_identity_id: str,
        relationship_type: RelationshipType,
        note: str | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None: ...


AdminActivationCause = Literal["admin_activation", "pre_provision", "bootstrap"]
"""How an ``identity_activated`` admin row came to be written.

``admin_activation`` is the tick of approval on a pending row;
``pre_provision`` creates the active row before first login; ``bootstrap``
is the first administrator activating themselves (D20), from the seed at
first login or from the operator CLI.
"""

RoleChange = Literal["granted", "revoked"]
RelationshipChange = Literal["asserted", "revoked"]


class AuthAuditOperation(StrEnum):
    LOGIN_SUCCESS_AND_TOKEN_ISSUED = "login_success_and_token_issued"
    LOGIN_SUCCESS = "login_success"
    TOKEN_ISSUED = "token_issued"
    AUTH_FAILURE = "auth_failure"
    LOGIN_FAILURE = "login_failure"
    IDENTITY_RETIRED = "identity_retired"
    LOGOUT = "logout"
    IDENTITY_ACTIVATED = "identity_activated"
    IDENTITY_ENABLED = "identity_enabled"
    IDENTITY_DISABLED = "identity_disabled"
    ROLE_CHANGED = "role_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"


def _bounded_text(value: str | None, *, max_length: int = MAX_AUTH_AUDIT_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    if len(value) <= max_length:
        return value
    return value[:max_length]


def _client_host(request: Request) -> str | None:
    client = request.client
    if client is None:
        return None
    return _bounded_text(client.host, max_length=128)


def _request_id(request: Request) -> str | None:
    request_id: str = request.state.request_id
    return _bounded_text(request_id, max_length=64)


def _optional_header(request: Request, name: str) -> str | None:
    if name not in request.headers:
        return None
    return request.headers[name]


def _request_metadata(request: Request) -> dict[str, object]:
    return {
        "method": request.method,
        "path": request.url.path,
    }


def _issued_token_claims(access_token: str) -> dict[str, object]:
    try:
        decoded = pyjwt.decode(access_token, options={"verify_signature": False})
    except pyjwt.PyJWTError as exc:
        raise AuditIntegrityError("Issued access token could not be decoded for auth audit metadata") from exc
    return cast(dict[str, object], decoded)


def _issued_identity_id(access_token: str) -> str:
    """The identity a just-issued token authorises, read from its own ``sub``.

    Taken from the token rather than threaded down from the provider on
    purpose: the recorder ALREADY decodes this token for ``iat``/``exp``, and
    the value it records must be the one the token actually carries. A value
    passed alongside could disagree with the token — and the row would then
    attribute a session to an identity the session does not name.

    Unverified decode is correct here: the token was minted by this process
    moments ago, and a signature check would only re-prove what we just did.
    """
    claims = _issued_token_claims(access_token)
    if "sub" not in claims:
        raise AuditIntegrityError("Issued access token missing 'sub' claim for auth audit identity")
    subject = claims["sub"]
    if type(subject) is not str or not subject:
        raise AuditIntegrityError("Issued access token 'sub' claim must be a non-empty string")
    return subject


def _required_int_claim(claims: dict[str, object], claim_name: str) -> int:
    if claim_name not in claims:
        raise AuditIntegrityError(f"Issued access token missing {claim_name!r} claim for auth audit metadata")
    value = claims[claim_name]
    if type(value) is not int:
        raise AuditIntegrityError(f"Issued access token {claim_name!r} claim must be int for auth audit metadata")
    return value


def _token_issued_metadata(
    request: Request,
    *,
    access_token: str,
    issuance_path: str,
    login_request_id: str | None = None,
) -> dict[str, object]:
    claims = _issued_token_claims(access_token)
    metadata = _request_metadata(request)
    metadata["issuance_path"] = issuance_path
    metadata["token_type"] = "bearer"
    metadata["issued_at"] = _required_int_claim(claims, "iat")
    metadata["expires_at"] = _required_int_claim(claims, "exp")
    if login_request_id is not None:
        # The SSO walk writes ``login`` at callback and ``token_issued`` at
        # complete — two requests, possibly two replicas. This is the join:
        # the callback's request id, carried on the handoff row and handed
        # back by ``consume``. Absent for the local path, where both rows
        # share one request and ``request_id`` already joins them.
        metadata["login_request_id"] = login_request_id
    return metadata


def classify_authentication_failure(exc: AuthenticationError) -> str:
    """Classify auth errors without storing their external-data-bearing detail."""
    if type(exc) is AuthProviderUnavailable:
        return "provider_unavailable"
    # SSO refusals carry their category ON THE TYPE (sso.py's closed set), so
    # the classifier reads it rather than restating twelve literals here —
    # a second copy would be the message-prefix drift below in another form.
    if isinstance(exc, SsoLoginError):
        return exc.category
    # Admission outcomes are matched by TYPE. They used to be matched on a
    # message prefix, which put the same literal in the raiser and here with
    # nothing binding the two: rewording the message an operator reads would
    # have silently reclassified the event, and the tests — which built the
    # literal themselves — would not have noticed.
    if type(exc) is AccessPending:
        return "access_pending"
    if type(exc) is IdentityDisabled:
        return "identity_disabled"

    detail = exc.detail
    # Admission outcomes come FIRST and are their own categories. A correct
    # password refused at the D12 wall is not a bad credential, and recording
    # it as one poisons both trails an administrator reads: the queue of
    # people waiting for approval, and the one that would show a brute-force
    # attempt. Same for a disabled identity, which is a revocation taking
    # effect, not a failed guess.
    if detail.startswith("Invalid credentials"):
        return "invalid_credentials"
    if detail.startswith("Email verification required"):
        return "email_unverified"
    if detail.startswith("Invalid tenant") or detail.startswith("Missing tenant claim"):
        return "tenant_claim_invalid"
    if detail.startswith("Missing required") or "group overage marker" in detail or detail.startswith("OIDC profile claim"):
        return "claims_invalid"
    if (
        detail.startswith("Invalid token")
        or detail.startswith("Token header")
        or detail.startswith("No matching key")
        or detail.startswith("JWKS key")
    ):
        return "invalid_token"
    if detail.startswith("JWKS document") or detail.startswith("OIDC discovery document"):
        return "provider_metadata_invalid"
    return "authentication_error"


@dataclass(frozen=True)
class AuthAuditRecorder:
    """Synchronous Landscape writer for web authentication events."""

    landscape_url: str
    landscape_passphrase: str | None
    create_tables: bool

    @classmethod
    def from_settings(
        cls,
        settings: WebSettings,
        deployment_state_mode: Literal["sqlite-single", "external-postgresql"] | None = None,
    ) -> AuthAuditRecorder:
        state_mode = deployment_state_mode or resolve_deployment_state_mode(settings)
        if state_mode == "external-postgresql":
            landscape_url = settings.landscape_url
            assert landscape_url is not None
        else:
            landscape_url = settings.get_landscape_url()
        return cls(
            landscape_url=landscape_url,
            landscape_passphrase=settings.landscape_passphrase,
            create_tables=state_mode == "sqlite-single",
        )

    @contextmanager
    def _open_landscape(self, operation: AuthAuditOperation) -> Iterator[LandscapeDB]:
        try:
            with LandscapeDB.from_url(
                self.landscape_url,
                passphrase=self.landscape_passphrase,
                create_tables=self.create_tables,
                **postgres_engine_kwargs(self.landscape_url),
            ) as db:
                yield db
        except (SchemaCompatibilityError, LandscapeRecordError, SQLAlchemyError, OSError) as exc:
            _slog.error(
                "auth_audit_write_failed",
                operation=operation,
                exception_class=type(exc).__name__,
            )
            raise

    def record_login_success(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        identity_id: str | None = None,
    ) -> None:
        # ``identity_id`` is explicit here because this row is written at the
        # SSO callback, where no token exists yet to derive it from — unlike
        # ``record_token_issued``, which reads it out of the minted token.
        with self._open_landscape(AuthAuditOperation.LOGIN_SUCCESS) as db:
            RecorderFactory(db).auth_audit.record_login_outcome(
                outcome="success",
                provider=provider,
                user_id=user_id,
                username=username,
                failure_category=None,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                metadata=_request_metadata(request),
                identity_id=identity_id,
            )

    def record_login_success_and_token_issued(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        access_token: str,
    ) -> None:
        """Persist the two required login-success events atomically."""
        with self._open_landscape(AuthAuditOperation.LOGIN_SUCCESS_AND_TOKEN_ISSUED) as db:
            RecorderFactory(db).auth_audit.record_login_success_and_token_issued(
                provider=provider,
                identity_id=_issued_identity_id(access_token),
                user_id=user_id,
                username=username,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                login_metadata=_request_metadata(request),
                token_metadata=_token_issued_metadata(
                    request,
                    access_token=access_token,
                    issuance_path="login",
                ),
            )

    def record_token_issued(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        access_token: str,
        issuance_path: str,
        login_request_id: str | None = None,
    ) -> None:
        with self._open_landscape(AuthAuditOperation.TOKEN_ISSUED) as db:
            RecorderFactory(db).auth_audit.record_token_issued(
                provider=provider,
                identity_id=_issued_identity_id(access_token),
                user_id=user_id,
                username=username,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                metadata=_token_issued_metadata(
                    request,
                    access_token=access_token,
                    issuance_path=issuance_path,
                    login_request_id=login_request_id,
                ),
            )

    def record_auth_failure(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        failure_category: str,
        failure_stage: str,
        user_id: str | None,
        username: str | None,
        exception_class: str | None,
        identity_id: str | None = None,
    ) -> None:
        metadata = _request_metadata(request)
        metadata["failure_stage"] = failure_stage
        metadata["exception_class"] = exception_class
        with self._open_landscape(AuthAuditOperation.AUTH_FAILURE) as db:
            RecorderFactory(db).auth_audit.record_auth_failure(
                provider=provider,
                identity_id=identity_id,
                user_id=user_id,
                username=username,
                failure_category=failure_category,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                metadata=metadata,
            )

    def record_login_failure(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        username: str,
        failure_category: str,
    ) -> None:
        with self._open_landscape(AuthAuditOperation.LOGIN_FAILURE) as db:
            RecorderFactory(db).auth_audit.record_login_outcome(
                outcome="failure",
                provider=provider,
                user_id=None,
                username=username,
                failure_category=failure_category,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                metadata=_request_metadata(request),
            )

    def record_identity_admitted(
        self,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        tokens_per_day: int | None,
        storage_bytes: int | None,
    ) -> None:
        """Write the ``identity_activated`` + ``quota_set`` pair for an admission.

        Deliberately NOT request-bound, unlike every method above it. A
        self-admitting deployment activates inside the login worker, which has
        no ``Request`` to read a client host or a request id from; the
        surrounding ``login`` event carries that context and this pair is
        joined to it by ``identity_id``. Inventing request fields here would
        put fabricated provenance in the audit trail.

        Both rows are written under one Landscape open, in the order an
        administrator would read them: the identity was admitted, and this is
        the allowance it was admitted with. An activation whose quota row went
        unaudited would leave the identity's first refusal unexplainable.
        """
        with self._open_landscape(AuthAuditOperation.LOGIN_SUCCESS) as db:
            recorder = RecorderFactory(db).auth_audit
            recorder.record_auth_event(
                event_type="identity_activated",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                # The USERNAME, matching every request-bound event. The
                # admission pair used to put the identity_id here, which meant
                # an administrator querying by either value saw half the
                # trail and no query returned a person's complete history.
                user_id=username,
                username=username,
                failure_category=None,
                request_id=None,
                client_host=None,
                user_agent=None,
                metadata={"activated_by": "registration_mode", "actor": "operator"},
            )
            if tokens_per_day is None or storage_bytes is None:
                # NO quota_set row. The caller writes a policy row only when
                # BOTH container defaults are configured, so this container
                # has no quota regime for the identity and there is no
                # allowance to record. Emitting the event anyway would assert
                # an allowance no row records, and point a later quota refusal
                # at corruption rather than at the missing configuration —
                # exactly backwards for whoever has to diagnose it.
                return
            recorder.record_auth_event(
                event_type="quota_set",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                # The USERNAME, matching every request-bound event. The
                # admission pair used to put the identity_id here, which meant
                # an administrator querying by either value saw half the
                # trail and no query returned a person's complete history.
                user_id=username,
                username=username,
                failure_category=None,
                request_id=None,
                client_host=None,
                user_agent=None,
                metadata={
                    "actor": "operator",
                    "tokens_per_day": tokens_per_day,
                    "storage_bytes": storage_bytes,
                    "source": "container_defaults",
                },
            )

    def record_identity_retired(
        self,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        retired_subject: str,
        reason: str,
    ) -> None:
        """Write the ``identity_disabled`` row for a credential-deletion retirement.

        A retirement IS a disable -- the row's ``access_state`` becomes
        ``disabled`` -- plus a rewrite of the ``(provider, subject)`` binding
        so no login can reach the row again; the event type is the disable's
        and the metadata carries what distinguishes it. Not request-bound: the
        deleting surface (the provider's ``delete_user`` or the ``users
        remove`` command) is the OPERATOR and has no request to read, so no
        request fields are invented. Runs inside the authority's transaction,
        so a retirement this trail cannot hold does not commit.
        """
        with self._open_landscape(AuthAuditOperation.IDENTITY_RETIRED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="identity_disabled",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                request_id=None,
                client_host=None,
                user_agent=None,
                metadata={
                    "actor": "operator",
                    "cause": "credential_deleted",
                    "retired_subject": _bounded_text(retired_subject),
                    "reason": _bounded_text(reason),
                },
            )

    def record_logout(
        self,
        request: Request,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
    ) -> None:
        """Write the ``logout`` row. The client discards the token; nothing is revoked server-side (spec rev2)."""
        with self._open_landscape(AuthAuditOperation.LOGOUT) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="logout",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                request_id=_request_id(request),
                client_host=_client_host(request),
                user_agent=_bounded_text(_optional_header(request, "user-agent")),
                metadata=_request_metadata(request),
            )

    def record_identity_activated(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str | None,
        cause: AdminActivationCause,
        note: str,
        role: IdentityRole | None,
        role_id: str | None,
        tokens_per_day: int | None,
        storage_bytes: int | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None:
        """Write ``identity_activated``, then ``role_granted`` and ``quota_set`` when the activation wrote them.

        One Landscape open, in the order an administrator reads them: the
        identity was admitted, this is the role it was admitted with, and
        this is its allowance. Invoked INSIDE the authority's transaction,
        so an activation this trail cannot hold does not commit.
        """
        provenance = _admin_provenance(
            request, actor_identity_id=actor_identity_id, on_behalf_of=on_behalf_of, console_request_id=console_request_id
        )
        with self._open_landscape(AuthAuditOperation.IDENTITY_ACTIVATED) as db:
            recorder = RecorderFactory(db).auth_audit
            recorder.record_auth_event(
                event_type="identity_activated",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                metadata={**provenance.metadata, "cause": cause, "note": _bounded_text(note)},
                **provenance.request_columns,
            )
            if role is not None and role_id is not None:
                recorder.record_auth_event(
                    event_type="role_granted",
                    outcome="success",
                    provider=provider,
                    identity_id=identity_id,
                    user_id=username,
                    username=username,
                    failure_category=None,
                    metadata={**provenance.metadata, "cause": cause, "role": role, "role_id": role_id, "scope": None, "expires_at": None},
                    **provenance.request_columns,
                )
            if tokens_per_day is not None and storage_bytes is not None:
                recorder.record_auth_event(
                    event_type="quota_set",
                    outcome="success",
                    provider=provider,
                    identity_id=identity_id,
                    user_id=username,
                    username=username,
                    failure_category=None,
                    metadata={
                        **provenance.metadata,
                        "tokens_per_day": tokens_per_day,
                        "storage_bytes": storage_bytes,
                        "source": "container_defaults",
                    },
                    **provenance.request_columns,
                )

    def record_identity_enabled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str,
        note: str,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None:
        provenance = _admin_provenance(
            request, actor_identity_id=actor_identity_id, on_behalf_of=on_behalf_of, console_request_id=console_request_id
        )
        with self._open_landscape(AuthAuditOperation.IDENTITY_ENABLED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="identity_enabled",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                metadata={**provenance.metadata, "note": _bounded_text(note)},
                **provenance.request_columns,
            )

    def record_identity_disabled(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str,
        actor_identity_id: str,
        reason: str,
        revoked_relationship_ids: tuple[str, ...],
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None:
        """The administrative disable; ``record_identity_retired`` is the credential-deletion form of the same event."""
        provenance = _admin_provenance(
            request, actor_identity_id=actor_identity_id, on_behalf_of=on_behalf_of, console_request_id=console_request_id
        )
        with self._open_landscape(AuthAuditOperation.IDENTITY_DISABLED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="identity_disabled",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "cause": "admin_disable",
                    "reason": _bounded_text(reason),
                    "revoked_relationship_ids": list(revoked_relationship_ids),
                },
                **provenance.request_columns,
            )

    def record_role_changed(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        identity_id: str,
        username: str | None,
        actor_identity_id: str,
        change: RoleChange,
        role: IdentityRole,
        role_id: str,
        scope: str | None,
        expires_at: datetime | None,
        note: str | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None:
        provenance = _admin_provenance(
            request, actor_identity_id=actor_identity_id, on_behalf_of=on_behalf_of, console_request_id=console_request_id
        )
        with self._open_landscape(AuthAuditOperation.ROLE_CHANGED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="role_granted" if change == "granted" else "role_revoked",
                outcome="success",
                provider=provider,
                identity_id=identity_id,
                user_id=username,
                username=username,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "role": role,
                    "role_id": role_id,
                    "scope": scope,
                    "expires_at": None if expires_at is None else expires_at.isoformat(),
                    "note": _bounded_text(note),
                },
                **provenance.request_columns,
            )

    def record_relationship_changed(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        actor_identity_id: str,
        change: RelationshipChange,
        relationship_id: str,
        from_identity_id: str,
        to_identity_id: str,
        relationship_type: RelationshipType,
        note: str | None,
        on_behalf_of: str | None,
        console_request_id: str | None,
    ) -> None:
        """The row is anchored on the overseen identity (``to``); the edge's other end is metadata."""
        provenance = _admin_provenance(
            request, actor_identity_id=actor_identity_id, on_behalf_of=on_behalf_of, console_request_id=console_request_id
        )
        with self._open_landscape(AuthAuditOperation.RELATIONSHIP_CHANGED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="relationship_asserted" if change == "asserted" else "relationship_revoked",
                outcome="success",
                provider=provider,
                identity_id=to_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "relationship_id": relationship_id,
                    "relationship_type": relationship_type,
                    "from_identity_id": from_identity_id,
                    "to_identity_id": to_identity_id,
                    "note": _bounded_text(note),
                },
                **provenance.request_columns,
            )


class _AdminProvenanceMetadata(TypedDict):
    """The metadata every admin-mutation row starts from; the two console keys are the L0-pinned names."""

    actor: str
    on_behalf_of: str | None
    console_request_id: str | None
    method: NotRequired[str]
    path: NotRequired[str]


class _RequestColumns(TypedDict):
    """The three request-derived columns of an ``auth_events`` row, ``None`` when there was no request."""

    request_id: str | None
    client_host: str | None
    user_agent: str | None


@dataclass(frozen=True, slots=True)
class _AdminProvenance:
    """What an admin-mutation row says about who asked, and through which request."""

    metadata: _AdminProvenanceMetadata
    request_columns: _RequestColumns


def _admin_provenance(
    request: Request | None,
    *,
    actor_identity_id: str | None,
    on_behalf_of: str | None,
    console_request_id: str | None,
) -> _AdminProvenance:
    """The provenance every admin mutation row carries.

    ``actor`` is the administrator's identity_id, or ``operator`` for the
    two actor-less paths (bootstrap seed, operator CLI). The two console
    keys are ALWAYS present (L0-pinned), ``None`` for a human acting for
    themselves, so a reader never has to guess whether a missing key meant
    "not a console" or "written before the keys existed".
    """
    actor = "operator" if actor_identity_id is None else actor_identity_id
    if request is None:
        return _AdminProvenance(
            metadata={
                "actor": actor,
                AUTH_EVENT_ON_BEHALF_OF_KEY: _bounded_text(on_behalf_of),
                AUTH_EVENT_CONSOLE_REQUEST_ID_KEY: _bounded_text(console_request_id),
            },
            request_columns={"request_id": None, "client_host": None, "user_agent": None},
        )
    return _AdminProvenance(
        metadata={
            "actor": actor,
            AUTH_EVENT_ON_BEHALF_OF_KEY: _bounded_text(on_behalf_of),
            AUTH_EVENT_CONSOLE_REQUEST_ID_KEY: _bounded_text(console_request_id),
            "method": request.method,
            "path": request.url.path,
        },
        request_columns={
            "request_id": _request_id(request),
            "client_host": _client_host(request),
            "user_agent": _bounded_text(_optional_header(request, "user-agent")),
        },
    )
