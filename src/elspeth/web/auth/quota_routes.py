"""Live quota status and audited administrator policy controls."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import MAX_AUTH_AUDIT_TEXT_LENGTH, AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.coordination.quota_policy_authority import (
    MAX_QUOTA_VALUE,
    IdentityQuotaStatus,
    QuotaPolicyChange,
    QuotaPolicyRefusal,
    QuotaSetterNotAdmin,
    QuotaTargetNotFound,
    RepositoryQuotaPolicyAuthority,
)


class IdentityQuotaView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity_id: str
    tokens_per_day: int | None
    storage_bytes: int | None
    container_tokens_per_day: int | None
    container_storage_bytes: int | None
    tokens_used_today: int | None
    storage_bytes_used: int


class SetQuotaBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    dimension: Literal["tokens", "storage"]
    value: int = Field(ge=1, le=MAX_QUOTA_VALUE)
    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    console_request_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class RevokeQuotaBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    console_request_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


def _authority(request: Request) -> RepositoryQuotaPolicyAuthority:
    authority: RepositoryQuotaPolicyAuthority = request.app.state.quota_policy_authority
    return authority


def _view(status: IdentityQuotaStatus) -> IdentityQuotaView:
    return IdentityQuotaView(
        identity_id=status.identity_id,
        tokens_per_day=None if status.identity_policy is None else status.identity_policy.tokens_per_day,
        storage_bytes=None if status.identity_policy is None else status.identity_policy.storage_bytes,
        container_tokens_per_day=None if status.container_policy is None else status.container_policy.tokens_per_day,
        container_storage_bytes=None if status.container_policy is None else status.container_policy.storage_bytes,
        tokens_used_today=status.tokens_used_today,
        storage_bytes_used=status.storage_bytes_used,
    )


def _changed_view(change: QuotaPolicyChange) -> IdentityQuotaView:
    return _view(
        IdentityQuotaStatus(
            identity_id=change.identity_id,
            identity_policy=change.policy,
            container_policy=change.container_policy,
            tokens_used_today=change.tokens_used_today,
            storage_bytes_used=change.storage_bytes_used,
        )
    )


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _hidden() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


async def _require_admin(request: Request, user: Annotated[UserIdentity, Depends(get_current_user)]) -> UserIdentity:
    identity_authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    if not await run_sync_in_worker(identity_authority.holds_active_role, identity_id=user.user_id, role="admin"):
        raise _hidden()
    return user


def _refused(exc: QuotaPolicyRefusal) -> HTTPException:
    if type(exc) is QuotaSetterNotAdmin:
        return _hidden()
    name = type(exc).__name__
    code = "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")
    status = 404 if type(exc) is QuotaTargetNotFound else 409
    return HTTPException(status_code=status, detail={"error_type": code, "detail": str(exc)})


def create_quota_router() -> APIRouter:
    router = APIRouter(prefix="/api/workflow/quota", tags=["workflow-quota"])

    @router.get("/me", response_model=IdentityQuotaView)
    async def my_quota(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> IdentityQuotaView:
        try:
            status = await run_sync_in_worker(_authority(request).status, identity_id=user.user_id)
        except QuotaTargetNotFound as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(status)

    @router.get("/identities/{identity_id}", response_model=IdentityQuotaView)
    async def identity_quota(
        identity_id: str,
        request: Request,
        response: Response,
        admin: Annotated[UserIdentity, Depends(_require_admin)],
    ) -> IdentityQuotaView:
        try:
            status = await run_sync_in_worker(_authority(request).status, identity_id=identity_id)
        except QuotaTargetNotFound as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(status)

    @router.post("/identities/{identity_id}", response_model=IdentityQuotaView)
    async def set_identity_quota(
        identity_id: str,
        body: SetQuotaBody,
        request: Request,
        response: Response,
        admin: Annotated[UserIdentity, Depends(_require_admin)],
    ) -> IdentityQuotaView:
        settings: WebSettings = request.app.state.settings
        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        authority = _authority(request)

        def record(change: QuotaPolicyChange) -> None:
            recorder.record_quota_set(request, provider=settings.auth_provider, change=change)

        try:
            change = await run_sync_in_worker(
                authority.set_identity_policy,
                actor=IdentityAdminActor(
                    identity_id=admin.user_id,
                    on_behalf_of=body.on_behalf_of,
                    console_request_id=body.console_request_id,
                ),
                identity_id=identity_id,
                dimension=body.dimension,
                value=body.value,
                default_tokens_per_day=settings.quota_default_tokens_per_day,
                default_storage_bytes=settings.quota_default_storage_bytes,
                record=record,
            )
        except QuotaPolicyRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _changed_view(change)

    @router.post("/identities/{identity_id}/revoke", response_model=IdentityQuotaView)
    async def revoke_identity_quota(
        identity_id: str,
        body: RevokeQuotaBody,
        request: Request,
        response: Response,
        admin: Annotated[UserIdentity, Depends(_require_admin)],
    ) -> IdentityQuotaView:
        settings: WebSettings = request.app.state.settings
        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        authority = _authority(request)

        def record(change: QuotaPolicyChange) -> None:
            recorder.record_quota_set(request, provider=settings.auth_provider, change=change)

        try:
            change = await run_sync_in_worker(
                authority.revoke_identity_policy,
                actor=IdentityAdminActor(
                    identity_id=admin.user_id,
                    on_behalf_of=body.on_behalf_of,
                    console_request_id=body.console_request_id,
                ),
                identity_id=identity_id,
                record=record,
            )
        except QuotaPolicyRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _changed_view(change)

    return router
