"""Identity administration -- /api/auth/admin/{identities,roles,relationships}.

The authorization path for SSO deployments (spec §Routes [rev2]). The dev-admin
surface next door is local-only and manages CREDENTIALS; this one manages
ADMISSION -- who the container lets in, with which role, overseen by whom --
and every mutation writes its ``auth_events`` row before the response.

Who may call it
---------------
An identity holding an active, deployment-wide ``admin`` role row, checked
against the sessions store on EVERY request and never cached: a revoked
admin's next request is refused. There is no configuration shortcut. The
first admin comes from the D20 bootstrap (the ``sso_admin_subjects`` seed at
first login, or ``elspeth composer users bootstrap-admin``); after that the
admin API is the only path. A caller without the role sees 404, the same as
the dev-admin surface: hidden, not forbidden, so the surface does not
confirm its own existence to a probe.

The authority is the arbiter
----------------------------
Every rule with teeth lives in ``RepositoryIdentityAuthority`` and is
enforced inside its transaction: the actor's admin role is re-proved there,
``on_behalf_of`` / ``console_request_id`` are accepted only from a
``service`` identity (checked against the actor's STORED kind, never the
request), self-disable and last-admin protection, R8's admin/workload
exclusion, org-tree cycles. These routes parse the request, hand the
authority an actor and a record callback, and translate its closed refusal
set into status codes. They add no rule of their own.

What a pending row shows
------------------------
A ``pending`` identity has not been admitted; until it is, the list exposes
its subject and organisation and nothing else (spec rev2.2), so the queue an
administrator reviews does not become a directory of everyone who ever
tried to log in. ``raw_claims_json`` is never returned for any state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from elspeth.contracts.auth import (
    ActivationRole,
    AuthProviderType,
    IdentityAccessState,
    IdentityProviderType,
    IdentityRole,
    RelationshipType,
)
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import MAX_AUTH_AUDIT_TEXT_LENGTH, AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import (
    AdminAuthorityRequired,
    IdentityActivated,
    IdentityAdminActor,
    IdentityAuthorityRefusal,
    IdentityDisabled,
    IdentityEnabled,
    IdentityNotFound,
    IdentitySummary,
    RelationshipChanged,
    RelationshipEdge,
    RelationshipNotFound,
    RepositoryIdentityAuthority,
    RoleChanged,
    RoleGrant,
    RoleNotFound,
)

MAX_PAGE_SIZE = 200
"""Upper bound on one page of identities, roles or relationships."""


# ── Wire shapes ──────────────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """Tier 1 base: no coercion, no extras. These bodies name people and grants."""

    model_config = ConfigDict(strict=True, extra="forbid")


class _Provenance(_StrictModel):
    """The organisation console's provenance (spec rev2.2).

    Accepted on every mutation body but honoured only when the ACTING
    identity is a ``service`` identity -- the authority checks the actor's
    stored kind and refuses a human who sets either. Both are recorded on
    the audit row, ``None`` for a human acting for themselves.
    """

    on_behalf_of: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    console_request_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class ActivateIdentityRequest(_Provenance):
    """POST /identities/{id}/activate -- the tick of approval (D12)."""

    role: ActivationRole
    note: str = Field(min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class PreProvisionIdentityRequest(_Provenance):
    """POST /identities -- an active row by ``(provider, subject)`` before first login (rev2.2)."""

    provider: IdentityProviderType
    subject: str = Field(min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    username: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    organisation_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    role: ActivationRole
    note: str = Field(min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class EnableIdentityRequest(_Provenance):
    note: str = Field(min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class DisableIdentityRequest(_Provenance):
    reason: str = Field(min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class GrantRoleRequest(_Provenance):
    identity_id: str = Field(min_length=1, max_length=64)
    role: IdentityRole
    scope: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    # The one field family parsed from a string: strict mode would refuse an
    # ISO-8601 body value, and an aware instant is the only shape accepted.
    expires_at: AwareDatetime | None = Field(default=None, strict=False)
    note: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class RevokeRequest(_Provenance):
    """POST /roles/{id}/revoke and /relationships/{id}/revoke."""

    note: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class AssertRelationshipRequest(_Provenance):
    from_identity_id: str = Field(min_length=1, max_length=64)
    to_identity_id: str = Field(min_length=1, max_length=64)
    relationship_type: RelationshipType
    effective_from: AwareDatetime | None = Field(default=None, strict=False)
    effective_until: AwareDatetime | None = Field(default=None, strict=False)
    note: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)


class IdentityView(_StrictModel):
    """One identity as an administrator sees it. Never ``raw_claims_json``.

    For a ``pending`` row every field after ``organisation_id`` that would
    identify the person beyond their subject is ``None`` (spec rev2.2).
    """

    identity_id: str
    provider: IdentityProviderType
    kind: Literal["human", "service"]
    subject: str
    organisation_id: str | None
    access_state: IdentityAccessState
    username: str | None
    display_name: str | None
    email: str | None
    first_seen_at: datetime
    last_login_at: datetime | None
    pre_provisioned_at: datetime | None
    activated_at: datetime | None
    activated_by_identity_id: str | None
    disabled_at: datetime | None
    disabled_by_identity_id: str | None
    disable_reason: str | None


class IdentityListResponse(_StrictModel):
    identities: list[IdentityView]
    access_state: IdentityAccessState
    limit: int
    offset: int
    # The admin UI shows an advisory when exactly one active human admin
    # holds the container (spec rev2.2); the count is read in the same
    # request so the advisory and the list agree.
    active_human_admin_count: int


class RoleView(_StrictModel):
    role_id: str
    identity_id: str
    role: IdentityRole
    scope: str | None
    expires_at: datetime | None
    note: str | None
    granted_by_identity_id: str | None
    granted_at: datetime
    revoked_at: datetime | None


class RoleListResponse(_StrictModel):
    roles: list[RoleView]
    limit: int
    offset: int


class RelationshipView(_StrictModel):
    relationship_id: str
    from_identity_id: str
    to_identity_id: str
    relationship_type: RelationshipType
    asserted_by_identity_id: str
    asserted_at: datetime
    effective_from: datetime | None
    effective_until: datetime | None
    note: str | None
    revoked_at: datetime | None
    revoked_by_identity_id: str | None


class RelationshipListResponse(_StrictModel):
    relationships: list[RelationshipView]
    limit: int
    offset: int


class ActivationResponse(_StrictModel):
    identity: IdentityView
    role: RoleView | None
    quota_written: bool


class DisableResponse(_StrictModel):
    identity: IdentityView
    revoked_relationship_ids: list[str]


# ── Projections ──────────────────────────────────────────────────────────


def _identity_view(summary: IdentitySummary) -> IdentityView:
    pending = summary.access_state == "pending"
    return IdentityView(
        identity_id=summary.identity_id,
        provider=summary.provider,
        kind=summary.kind,
        subject=summary.subject,
        organisation_id=summary.organisation_id,
        access_state=summary.access_state,
        username=None if pending else summary.username,
        display_name=None if pending else summary.display_name,
        email=None if pending else summary.email,
        first_seen_at=summary.first_seen_at,
        last_login_at=None if pending else summary.last_login_at,
        pre_provisioned_at=summary.pre_provisioned_at,
        activated_at=summary.activated_at,
        activated_by_identity_id=summary.activated_by_identity_id,
        disabled_at=summary.disabled_at,
        disabled_by_identity_id=summary.disabled_by_identity_id,
        disable_reason=summary.disable_reason,
    )


def _role_view(grant: RoleGrant) -> RoleView:
    return RoleView(
        role_id=grant.role_id,
        identity_id=grant.identity_id,
        role=grant.role,
        scope=grant.scope,
        expires_at=grant.expires_at,
        note=grant.note,
        granted_by_identity_id=grant.granted_by_identity_id,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
    )


def _relationship_view(edge: RelationshipEdge) -> RelationshipView:
    return RelationshipView(
        relationship_id=edge.relationship_id,
        from_identity_id=edge.from_identity_id,
        to_identity_id=edge.to_identity_id,
        relationship_type=edge.relationship_type,
        asserted_by_identity_id=edge.asserted_by_identity_id,
        asserted_at=edge.asserted_at,
        effective_from=edge.effective_from,
        effective_until=edge.effective_until,
        note=edge.note,
        revoked_at=edge.revoked_at,
        revoked_by_identity_id=edge.revoked_by_identity_id,
    )


# ── Request plumbing ─────────────────────────────────────────────────────


def _authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _provider(request: Request) -> AuthProviderType:
    settings: WebSettings = request.app.state.settings
    return settings.auth_provider


def _hidden() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


async def _require_identity_admin(request: Request) -> UserIdentity:
    """Admit only a live holder of a deployment-wide ``admin`` role.

    Checked against the store per request (spec: never cached). The
    authority re-proves it inside every mutation's transaction as well; this
    dependency is what keeps a non-admin from even reading the queue.
    """
    user = await get_current_user(request)
    if not await run_sync_in_worker(_authority(request).holds_active_role, identity_id=user.user_id, role="admin"):
        raise _hidden()
    return user


def _actor(user: UserIdentity, provenance: _Provenance) -> IdentityAdminActor:
    return IdentityAdminActor(
        identity_id=user.user_id,
        on_behalf_of=provenance.on_behalf_of,
        console_request_id=provenance.console_request_id,
    )


_NOT_FOUND_REFUSALS: frozenset[type[IdentityAuthorityRefusal]] = frozenset({IdentityNotFound, RoleNotFound, RelationshipNotFound})


def _refusal_code(exc: IdentityAuthorityRefusal) -> str:
    """``LastActiveAdminProtected`` -> ``last_active_admin_protected``: the closed code a client can switch on."""
    name = type(exc).__name__
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")


def _refused(exc: IdentityAuthorityRefusal) -> HTTPException:
    """Translate the authority's closed refusal set.

    Exact types, not ``isinstance``: the set is owned and closed, and a
    new refusal class should land in the 409 arm by default rather than be
    silently promoted to "not found".
    """
    if type(exc) is AdminAuthorityRequired:
        # The actor lost admin between the dependency and the transaction,
        # or a human sent console provenance. Hidden, like the dependency.
        return _hidden()
    if type(exc) in _NOT_FOUND_REFUSALS:
        return HTTPException(status_code=404, detail={"refusal": _refusal_code(exc), "detail": str(exc)})
    return HTTPException(status_code=409, detail={"refusal": _refusal_code(exc), "detail": str(exc)})


def _uncacheable(response: Response) -> None:
    """Live authority reads and admission writes must not be retained by caches."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


# ── Router ───────────────────────────────────────────────────────────────


def create_identity_admin_router() -> APIRouter:
    """Create the identity-administration router; mounted for every provider."""
    router = APIRouter(prefix="/api/auth/admin", tags=["identity-admin"])

    # ── identities ───────────────────────────────────────────────────

    @router.get("/identities", response_model=IdentityListResponse)
    async def list_identities(
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
        access_state: Annotated[IdentityAccessState, Query()] = "pending",
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> IdentityListResponse:
        """The admission queue first: ``pending`` is the default filter (spec rev2.2)."""
        authority = _authority(request)
        summaries = await run_sync_in_worker(authority.list_identities, access_state=access_state, limit=limit, offset=offset)
        admins = await run_sync_in_worker(authority.count_active_human_admins)
        _uncacheable(response)
        return IdentityListResponse(
            identities=[_identity_view(summary) for summary in summaries],
            access_state=access_state,
            limit=limit,
            offset=offset,
            active_human_admin_count=admins,
        )

    @router.post("/identities", response_model=ActivationResponse, status_code=201)
    async def pre_provision_identity(
        request: Request,
        response: Response,
        body: PreProvisionIdentityRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> ActivationResponse:
        settings: WebSettings = request.app.state.settings
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: IdentityActivated) -> None:
            _record_activation(recorder, request, provider, event, cause="pre_provision", settings=settings)

        try:
            event = await run_sync_in_worker(
                _authority(request).pre_provision_identity,
                actor=_actor(admin, body),
                provider=body.provider,
                subject=body.subject,
                username=body.username,
                organisation_id=body.organisation_id,
                role=body.role,
                note=body.note,
                quota_tokens_per_day=settings.quota_default_tokens_per_day,
                quota_storage_bytes=settings.quota_default_storage_bytes,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return await _activation_response(request, event)

    @router.post("/identities/{identity_id}/activate", response_model=ActivationResponse)
    async def activate_identity(
        request: Request,
        response: Response,
        identity_id: str,
        body: ActivateIdentityRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> ActivationResponse:
        """The tick of approval: ``pending`` becomes ``active`` with a role and a note (D12, D20)."""
        settings: WebSettings = request.app.state.settings
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: IdentityActivated) -> None:
            _record_activation(recorder, request, provider, event, cause="admin_activation", settings=settings)

        try:
            event = await run_sync_in_worker(
                _authority(request).activate_identity,
                actor=_actor(admin, body),
                identity_id=identity_id,
                role=body.role,
                note=body.note,
                quota_tokens_per_day=settings.quota_default_tokens_per_day,
                quota_storage_bytes=settings.quota_default_storage_bytes,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return await _activation_response(request, event)

    @router.post("/identities/{identity_id}/enable", response_model=IdentityView)
    async def enable_identity(
        request: Request,
        response: Response,
        identity_id: str,
        body: EnableIdentityRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> IdentityView:
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: IdentityEnabled) -> None:
            recorder.record_identity_enabled(
                request,
                provider=provider,
                identity_id=event.record.identity_id,
                username=event.record.username,
                actor_identity_id=event.actor_identity_id,
                note=event.note,
                on_behalf_of=event.on_behalf_of,
                console_request_id=event.console_request_id,
            )

        try:
            event = await run_sync_in_worker(
                _authority(request).enable_identity,
                actor=_actor(admin, body),
                identity_id=identity_id,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return await _identity_view_for(request, event.record.identity_id)

    @router.post("/identities/{identity_id}/disable", response_model=DisableResponse)
    async def disable_identity(
        request: Request,
        response: Response,
        identity_id: str,
        body: DisableIdentityRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> DisableResponse:
        """Refused for the actor's own identity and for the last active human admin (spec)."""
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: IdentityDisabled) -> None:
            recorder.record_identity_disabled(
                request,
                provider=provider,
                identity_id=event.record.identity_id,
                username=event.record.username,
                actor_identity_id=event.actor_identity_id,
                reason=event.reason,
                revoked_relationship_ids=tuple(edge.relationship_id for edge in event.revoked_relationships),
                on_behalf_of=event.on_behalf_of,
                console_request_id=event.console_request_id,
            )

        try:
            event = await run_sync_in_worker(
                _authority(request).disable_identity,
                actor=_actor(admin, body),
                identity_id=identity_id,
                reason=body.reason,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return DisableResponse(
            identity=await _identity_view_for(request, event.record.identity_id),
            revoked_relationship_ids=[edge.relationship_id for edge in event.revoked_relationships],
        )

    # ── roles ────────────────────────────────────────────────────────

    @router.get("/roles", response_model=RoleListResponse)
    async def list_roles(
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
        identity_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        include_revoked: Annotated[bool, Query()] = False,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RoleListResponse:
        grants = await run_sync_in_worker(
            _authority(request).list_roles,
            identity_id=identity_id,
            include_revoked=include_revoked,
            limit=limit,
            offset=offset,
        )
        _uncacheable(response)
        return RoleListResponse(roles=[_role_view(grant) for grant in grants], limit=limit, offset=offset)

    @router.post("/roles", response_model=RoleView, status_code=201)
    async def grant_role(
        request: Request,
        response: Response,
        body: GrantRoleRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> RoleView:
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: RoleChanged) -> None:
            _record_role_change(recorder, request, provider, event, change="granted")

        try:
            grant = await run_sync_in_worker(
                _authority(request).grant_role,
                actor=_actor(admin, body),
                identity_id=body.identity_id,
                role=body.role,
                scope=body.scope,
                expires_at=body.expires_at,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _role_view(grant)

    @router.post("/roles/{role_id}/revoke", response_model=RoleView)
    async def revoke_role(
        request: Request,
        response: Response,
        role_id: str,
        body: RevokeRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> RoleView:
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: RoleChanged) -> None:
            _record_role_change(recorder, request, provider, event, change="revoked")

        try:
            grant = await run_sync_in_worker(
                _authority(request).revoke_role,
                actor=_actor(admin, body),
                role_id=role_id,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _role_view(grant)

    # ── relationships ────────────────────────────────────────────────

    @router.get("/relationships", response_model=RelationshipListResponse)
    async def list_relationships(
        request: Request,
        response: Response,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
        identity_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        include_revoked: Annotated[bool, Query()] = False,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RelationshipListResponse:
        edges = await run_sync_in_worker(
            _authority(request).list_relationships,
            identity_id=identity_id,
            include_revoked=include_revoked,
            limit=limit,
            offset=offset,
        )
        _uncacheable(response)
        return RelationshipListResponse(
            relationships=[_relationship_view(edge) for edge in edges],
            limit=limit,
            offset=offset,
        )

    @router.post("/relationships", response_model=RelationshipView, status_code=201)
    async def assert_relationship(
        request: Request,
        response: Response,
        body: AssertRelationshipRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> RelationshipView:
        """Assert ``from`` oversees ``to`` (D11): the manual org chart, one edge at a time."""
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: RelationshipChanged) -> None:
            _record_relationship_change(recorder, request, provider, event, change="asserted")

        try:
            edge = await run_sync_in_worker(
                _authority(request).assert_relationship,
                actor=_actor(admin, body),
                from_identity_id=body.from_identity_id,
                to_identity_id=body.to_identity_id,
                relationship_type=body.relationship_type,
                effective_from=body.effective_from,
                effective_until=body.effective_until,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _relationship_view(edge)

    @router.post("/relationships/{relationship_id}/revoke", response_model=RelationshipView)
    async def revoke_relationship(
        request: Request,
        response: Response,
        relationship_id: str,
        body: RevokeRequest,
        admin: UserIdentity = Depends(_require_identity_admin),  # noqa: B008
    ) -> RelationshipView:
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: RelationshipChanged) -> None:
            _record_relationship_change(recorder, request, provider, event, change="revoked")

        try:
            edge = await run_sync_in_worker(
                _authority(request).revoke_relationship,
                actor=_actor(admin, body),
                relationship_id=relationship_id,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _relationship_view(edge)

    return router


# ── Audit callbacks and response assembly ────────────────────────────────


def _record_activation(
    recorder: AuthAuditWriter,
    request: Request,
    provider: AuthProviderType,
    event: IdentityActivated,
    *,
    cause: Literal["admin_activation", "pre_provision"],
    settings: WebSettings,
) -> None:
    """Runs INSIDE the authority's transaction: an activation the trail cannot hold does not commit."""
    recorder.record_identity_activated(
        request,
        provider=provider,
        identity_id=event.record.identity_id,
        username=event.record.username,
        actor_identity_id=event.actor_identity_id,
        cause=cause,
        note=event.note,
        role=None if event.role is None else event.role.role,
        role_id=None if event.role is None else event.role.role_id,
        tokens_per_day=settings.quota_default_tokens_per_day if event.quota_written else None,
        storage_bytes=settings.quota_default_storage_bytes if event.quota_written else None,
        on_behalf_of=event.on_behalf_of,
        console_request_id=event.console_request_id,
    )


def _record_role_change(
    recorder: AuthAuditWriter,
    request: Request,
    provider: AuthProviderType,
    event: RoleChanged,
    *,
    change: Literal["granted", "revoked"],
) -> None:
    recorder.record_role_changed(
        request,
        provider=provider,
        identity_id=event.grant.identity_id,
        # A role row names its identity by id; a display name would need a
        # second read on the connection the grant is being written on.
        username=None,
        actor_identity_id=event.actor_identity_id,
        change=change,
        role=event.grant.role,
        role_id=event.grant.role_id,
        scope=event.grant.scope,
        expires_at=event.grant.expires_at,
        note=event.note,
        on_behalf_of=event.on_behalf_of,
        console_request_id=event.console_request_id,
    )


def _record_relationship_change(
    recorder: AuthAuditWriter,
    request: Request,
    provider: AuthProviderType,
    event: RelationshipChanged,
    *,
    change: Literal["asserted", "revoked"],
) -> None:
    recorder.record_relationship_changed(
        request,
        provider=provider,
        actor_identity_id=event.actor_identity_id,
        change=change,
        relationship_id=event.edge.relationship_id,
        from_identity_id=event.edge.from_identity_id,
        to_identity_id=event.edge.to_identity_id,
        relationship_type=event.edge.relationship_type,
        # The edge carries the assertion's note; a revoke has no note of its own.
        note=event.edge.note,
        on_behalf_of=event.on_behalf_of,
        console_request_id=event.console_request_id,
    )


async def _identity_view_for(request: Request, identity_id: str) -> IdentityView:
    summary = await run_sync_in_worker(_authority(request).read_identity_summary, identity_id=identity_id)
    if summary is None:
        # The row was just written inside a committed transaction; its
        # absence now is a store integrity failure, not a client error.
        raise HTTPException(status_code=500, detail="identity row vanished after a committed mutation")
    return _identity_view(summary)


async def _activation_response(request: Request, event: IdentityActivated) -> ActivationResponse:
    return ActivationResponse(
        identity=await _identity_view_for(request, event.record.identity_id),
        role=None if event.role is None else _role_view(event.role),
        quota_written=event.quota_written,
    )
