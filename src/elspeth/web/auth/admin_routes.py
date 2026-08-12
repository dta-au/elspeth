"""Dev-admin user management routes -- /api/auth/admin/users.

A deliberately small, env-gated surface for SHORT-TERM dev deployments
where no IdP (Entra/OIDC) is wired up: the one local-auth user named by
``WebSettings.dev_admin_user`` may list, create, and delete users and
reset passwords. When the flag is unset (the default, and the required
state for IdP deployments) every route here answers 404, matching how
routes.py hides /login and /register on the wrong provider.

Passwords are always server-generated and returned exactly once in the
response body; they are never logged. Mutations are recorded via
structlog only -- this surface is off in any deployment whose audit
trail matters (the flag cannot be set unless auth_provider is local).
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from elspeth.core.landscape.auth_audit_repository import AUTH_AUDIT_PRINCIPAL_MAX_LENGTH
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.local import LocalAuthProvider, LocalAuthRegistrationConflict
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.auth.routes import _mark_sensitive_auth_response_uncacheable
from elspeth.web.config import WebSettings
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


async def _require_dev_admin(request: Request) -> UserIdentity:
    """Admit only the flagged dev-admin user; hide the surface otherwise.

    Order matters: the flag check comes first so a disabled deployment
    404s even for unauthenticated probes (the surface does not exist);
    only an enabled deployment distinguishes 401 (no/bad credentials)
    from 404 (authenticated but not the admin -- hidden, not forbidden).
    """
    settings: WebSettings = request.app.state.settings
    if settings.auth_provider != "local" or settings.dev_admin_user is None:
        raise HTTPException(status_code=404, detail="Not found")
    user = await get_current_user(request)
    if user.user_id != settings.dev_admin_user:
        raise HTTPException(status_code=404, detail="Not found")
    return user


def create_dev_admin_router() -> APIRouter:
    """Create the dev-admin user management router."""
    router = APIRouter(prefix="/api/auth/admin/users", tags=["dev-admin"])

    @router.get("", response_model=UserListResponse)
    async def list_users(
        request: Request,
        admin: UserIdentity = Depends(_require_dev_admin),  # noqa: B008
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
        admin: UserIdentity = Depends(_require_dev_admin),  # noqa: B008
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
        admin: UserIdentity = Depends(_require_dev_admin),  # noqa: B008
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
        request: Request,
        admin: UserIdentity = Depends(_require_dev_admin),  # noqa: B008
    ) -> Response:
        if user_id == admin.user_id:
            # The admin deleting themself would orphan the surface mid-session.
            raise HTTPException(status_code=400, detail="The dev admin account cannot delete itself")
        provider: LocalAuthProvider = request.app.state.auth_provider
        deleted = await run_sync_in_worker(provider.delete_user, user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="User not found")
        _slog.info("dev_admin_user_deleted", actor=admin.user_id, target=user_id)
        return Response(status_code=204)

    return router
