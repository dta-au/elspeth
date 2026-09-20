"""Local account management routes -- /api/auth/admin/users.

On local-auth deployments, active human administrators with a live,
deployment-wide admin role may manage credentials, as may the configured
``WebSettings.dev_admin_user``. External-auth deployments hide these routes.

Passwords are server-generated, returned once, and never logged. Credential
creation and reset retain their operational log events; identity admission
and retirement use their existing audited authority paths.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from elspeth.core.landscape.auth_audit_repository import AUTH_AUDIT_PRINCIPAL_MAX_LENGTH
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.local import LocalAuthProvider, LocalAuthRegistrationConflict
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.auth.routes import _mark_sensitive_auth_response_uncacheable
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import (
    LOCAL_DELETION_REASON_MAX_LENGTH,
    LastActiveAdminProtected,
    RepositoryIdentityAuthority,
)
from elspeth.web.validation import has_visible_content

_slog = structlog.get_logger(__name__)

# 24 urlsafe-base64 characters (~107 bits); well under bcrypt's 72-byte cap.
_GENERATED_PASSWORD_BYTES = 18


def _generate_password() -> str:
    return secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES)


class CreateUserRequest(BaseModel):
    """Request body for POST /api/auth/admin/users (no password: server-generated)."""

    username: str = Field(max_length=AUTH_AUDIT_PRINCIPAL_MAX_LENGTH)
    display_name: str
    email: str | None = None

    @field_validator("username", "display_name")
    @classmethod
    def _must_not_be_blank(cls, v: str) -> str:
        if not has_visible_content(v):
            raise ValueError("must contain at least one visible character")
        return v

    @field_validator("email")
    @classmethod
    def _email_must_be_deliverable_when_present(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not has_visible_content(trimmed):
            raise ValueError("must contain at least one visible character")
        local, separator, domain = trimmed.partition("@")
        if separator != "@" or not local or not domain:
            raise ValueError("must be a valid email address")
        return trimmed


class DeleteUserRequest(BaseModel):
    """Request body for DELETE /api/auth/admin/users/{user_id}.

    A body, not a query parameter: a reason can name a person or an incident,
    and query strings are written to access logs.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=LOCAL_DELETION_REASON_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _must_state_a_reason(cls, v: str) -> str:
        if not has_visible_content(v):
            raise ValueError("must contain at least one visible character")
        return v


class _StrictResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class UserSummary(_StrictResponse):
    """One account row in GET /api/auth/admin/users."""

    user_id: str
    display_name: str
    email: str | None = None
    email_verified: bool


class UserListResponse(_StrictResponse):
    """Response for GET /api/auth/admin/users."""

    users: list[UserSummary]


class GeneratedPasswordResponse(_StrictResponse):
    """Response carrying a server-generated password, shown exactly once."""

    user_id: str
    password: str


def dev_admin_surface_enabled(settings: WebSettings) -> bool:
    """Whether this deployment has the dev-admin credential surface at all."""
    return settings.auth_provider == "local" and settings.dev_admin_user is not None


def is_dev_admin(settings: WebSettings, user: UserIdentity) -> bool:
    """Whether ``user`` is the configured dev admin.

    Compared against USERNAME, not user_id. ``dev_admin_user`` names a
    local-auth account (its validator says so), while ``user_id`` is now the
    identity_id -- an opaque uuid that no operator ever configures. Comparing
    the two would silently 404 the configured dev admin out of their surface.
    """
    return dev_admin_surface_enabled(settings) and user.username == settings.dev_admin_user


async def can_manage_local_accounts(request: Request, user: UserIdentity) -> bool:
    """Shared, live credential capability for the directory and mutation routes."""
    settings: WebSettings = request.app.state.settings
    if settings.auth_provider != "local":
        return False
    if is_dev_admin(settings, user):
        return True
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    administrators = await run_sync_in_worker(authority.active_human_admin_ids)
    return user.user_id in administrators


async def _require_local_account_admin(request: Request) -> UserIdentity:
    """Authenticate local callers and re-check credential authority per request."""
    settings: WebSettings = request.app.state.settings
    if settings.auth_provider != "local":
        raise HTTPException(status_code=404, detail="Not found")
    user = await get_current_user(request)
    try:
        allowed = await can_manage_local_accounts(request, user)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Local account administration is unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=404, detail="Not found")
    return user


def create_dev_admin_router() -> APIRouter:
    """Create the local account management router."""
    router = APIRouter(prefix="/api/auth/admin/users", tags=["dev-admin"])

    @router.get("", response_model=UserListResponse)
    async def list_users(
        request: Request,
        admin: UserIdentity = Depends(_require_local_account_admin),  # noqa: B008
    ) -> UserListResponse:
        provider: LocalAuthProvider = request.app.state.auth_provider
        accounts = await run_sync_in_worker(provider.list_users)
        return UserListResponse(
            users=[
                UserSummary(
                    user_id=account.user_id,
                    display_name=account.display_name,
                    email=account.email,
                    email_verified=account.email_verified,
                )
                for account in accounts
            ]
        )

    @router.post("", response_model=GeneratedPasswordResponse, status_code=201)
    async def create_user(
        body: CreateUserRequest,
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_local_account_admin),  # noqa: B008
    ) -> GeneratedPasswordResponse:
        provider: LocalAuthProvider = request.app.state.auth_provider
        password = _generate_password()
        try:
            await run_sync_in_worker(
                provider.create_user,
                body.username,
                password,
                body.display_name,
                body.email,
            )
        except LocalAuthRegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _slog.info("dev_admin_user_created", actor=admin.user_id, target=body.username)
        _mark_sensitive_auth_response_uncacheable(response)
        return GeneratedPasswordResponse(user_id=body.username, password=password)

    @router.post("/{user_id}/reset-password", response_model=GeneratedPasswordResponse)
    async def reset_password(
        user_id: str,
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_local_account_admin),  # noqa: B008
    ) -> GeneratedPasswordResponse:
        provider: LocalAuthProvider = request.app.state.auth_provider
        password = _generate_password()
        try:
            await run_sync_in_worker(provider.set_password, user_id, password)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        _slog.info("dev_admin_password_reset", actor=admin.user_id, target=user_id)
        _mark_sensitive_auth_response_uncacheable(response)
        return GeneratedPasswordResponse(user_id=user_id, password=password)

    @router.delete("/{user_id}", status_code=204)
    async def delete_user(
        user_id: str,
        body: DeleteUserRequest,
        request: Request,
        admin: UserIdentity = Depends(_require_local_account_admin),  # noqa: B008
    ) -> Response:
        # ``user_id`` here is the LOCAL ACCOUNT name in the path, and the
        # thing to compare it with is the admin's username — ``admin.user_id``
        # is the identity_id and would never match, silently disarming this
        # guard and letting the admin delete their own credentials.
        if user_id == admin.username:
            # Keep the administrator's own credential usable mid-session.
            raise HTTPException(status_code=400, detail="An administrator cannot delete their own account")
        provider: LocalAuthProvider = request.app.state.auth_provider
        try:
            deletion = await run_sync_in_worker(provider.delete_user, user_id, reason=body.reason)
        except LastActiveAdminProtected as exc:
            # Deleting the account retires its identity, and this one is the
            # container's last active human administrator (R5). Decided
            # before the credential was touched, so nothing has changed. The
            # same closed code the identity surface answers with, so a client
            # switches on one vocabulary.
            raise HTTPException(
                status_code=409,
                detail={"refusal": "last_active_admin_protected", "detail": str(exc)},
            ) from exc
        if not deletion.removed_anything:
            raise HTTPException(status_code=404, detail="User not found")
        # ``credential_deleted`` false with ``identity_retired`` true is the
        # recovery of an earlier deletion whose retirement did not commit: the
        # account is already gone and this call finished the job, so it is a
        # success, not a "not found".
        _slog.info(
            "dev_admin_user_deleted",
            actor=admin.user_id,
            target=user_id,
            credential_deleted=deletion.credential_deleted,
            identity_retired=deletion.identity_retired,
        )
        return Response(status_code=204)

    return router
