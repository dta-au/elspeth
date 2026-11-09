"""Live quota status and audited administrator policy controls."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from elspeth.web.validation import reject_credential_material


class IdentityQuotaView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity_id: str
    tokens_per_day: str | None
    storage_bytes: str | None
    container_tokens_per_day: str | None
    container_storage_bytes: str | None
    tokens_used_today: str | None
    storage_bytes_used: str
    policy_state: Literal["active", "never_issued", "suspended"]
    can_edit: bool
    container_tokens_used_today: str | None
    container_storage_bytes_used: str


class SetQuotaBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    dimension: Literal["tokens", "storage"]
    value: int = Field(ge=1, le=MAX_QUOTA_VALUE)
    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    console_request_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)

    @field_validator("value", mode="before", json_schema_input_type=int | str)
    @classmethod
    def _exact_quantity(cls, value: object) -> int:
        # JSON numbers remain accepted for existing clients only when every
        # JavaScript implementation can represent them exactly.
        if type(value) is int and 1 <= value <= 2**53 - 1:
            return value
        if type(value) is str and value.isascii() and value.isdecimal() and 1 <= len(value) <= 19:
            return int(value)
        raise ValueError("quota value must be a decimal string or an exact safe integer")

    @model_validator(mode="after")
    def _reject_credential_material(self) -> SetQuotaBody:
        reject_credential_material(self.model_dump(mode="json"))
        return self


class RevokeQuotaBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    console_request_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)

    @model_validator(mode="after")
    def _reject_credential_material(self) -> RevokeQuotaBody:
        reject_credential_material(self.model_dump(mode="json"))
        return self


def _authority(request: Request) -> RepositoryQuotaPolicyAuthority:
    authority: RepositoryQuotaPolicyAuthority = request.app.state.quota_policy_authority
    return authority


def _quantity(value: int | None) -> str | None:
    return None if value is None else str(value)


def _view(status: IdentityQuotaStatus, settings: WebSettings) -> IdentityQuotaView:
    return IdentityQuotaView(
        identity_id=status.identity_id,
        tokens_per_day=_quantity(None if status.identity_policy is None else status.identity_policy.tokens_per_day),
        storage_bytes=_quantity(None if status.identity_policy is None else status.identity_policy.storage_bytes),
        container_tokens_per_day=_quantity(None if status.container_policy is None else status.container_policy.tokens_per_day),
        container_storage_bytes=_quantity(None if status.container_policy is None else status.container_policy.storage_bytes),
        tokens_used_today=_quantity(status.tokens_used_today),
        storage_bytes_used=str(status.storage_bytes_used),
        policy_state=status.policy_state,
        can_edit=status.policy_state != "never_issued"
        or (settings.quota_default_tokens_per_day is not None and settings.quota_default_storage_bytes is not None),
        container_tokens_used_today=_quantity(status.container_tokens_used_today),
        container_storage_bytes_used=str(status.container_storage_bytes_used),
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
        return _view(status, request.app.state.settings)

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
        return _view(status, request.app.state.settings)

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
            await run_sync_in_worker(
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
        return _view(await run_sync_in_worker(authority.status, identity_id=identity_id), settings)

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
            await run_sync_in_worker(
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
        return _view(await run_sync_in_worker(authority.status, identity_id=identity_id), settings)

    return router
