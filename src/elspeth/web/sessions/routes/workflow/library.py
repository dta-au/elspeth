"""Shared library publication, curation, browsing and fork routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, NotRequired, Protocol, TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.library_authority import (
    MAX_LIBRARY_TITLE_LENGTH,
    CuratorAuthorityRequired,
    LibraryAuthorityRefusal,
    LibraryCompartmentNotConfigured,
    LibraryCurated,
    LibraryCuratorIsPublisher,
    LibraryEntryAlreadyCurated,
    LibraryEntryNeedsProfileBoundSource,
    LibraryEntryNotForkable,
    LibraryEntryNotFound,
    LibraryEntryRecord,
    LibraryEntryState,
    LibraryForkerNotActive,
    LibraryNoteTooLong,
    LibraryPublished,
    LibraryPublisherNotActive,
    LibraryRejectionNoteRequired,
    RepositoryLibraryAuthority,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership
from elspeth.web.sessions.routes.composer.state import ImportStateYamlRequest, LibraryForkMetaUpdates
from elspeth.web.sessions.schemas import CompositionStateResponse

LibraryView = Literal["accepted", "queue", "mine"]
LibraryAction = Literal["accepted", "rejected", "deprecated", "recalled"]


class LibraryErrorDetail(TypedDict):
    error_type: str
    detail: str
    sources: NotRequired[list[str]]
    current_state: NotRequired[LibraryEntryState]


_ERROR_TYPES: dict[type[LibraryAuthorityRefusal], str] = {
    LibraryCompartmentNotConfigured: "compartment_not_configured",
    LibraryEntryNeedsProfileBoundSource: "library_entry_needs_profile_bound_source",
    LibraryPublisherNotActive: "library_publisher_not_active",
    LibraryForkerNotActive: "library_forker_not_active",
    LibraryCuratorIsPublisher: "library_curator_is_publisher",
    LibraryEntryAlreadyCurated: "library_entry_already_curated",
    LibraryRejectionNoteRequired: "library_rejection_note_required",
    LibraryNoteTooLong: "library_note_too_long",
    LibraryEntryNotForkable: "library_entry_not_forkable",
}


class PublishLibraryEntryRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_LIBRARY_TITLE_LENGTH)


class CurateLibraryEntryRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # The authority returns the named library_note_too_long refusal. A
    # Pydantic maximum here would turn that documented 409 into a 422.
    note: str | None = None


class LibraryEntryView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    entry_id: str
    published_from_session_id: str | None
    payload_digest: str
    compartment_id: str
    title: str
    version: int
    published_by_identity_id: str
    curated_by_identity_id: str | None
    published_at: datetime
    accepted_at: datetime | None
    rejected_at: datetime | None
    rejection_note: str | None
    deprecated_at: datetime | None
    recalled_at: datetime | None
    note: str | None
    state: LibraryEntryState


class LibraryListResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    view: LibraryView
    entries: list[LibraryEntryView]


class LibraryForkResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    session_id: UUID
    state_id: str


class LibraryStateSeeder(Protocol):
    """App wiring injects the existing YAML import validators as one seed seam."""

    async def __call__(
        self,
        *,
        session: SessionRecord,
        body: ImportStateYamlRequest,
        request: Request,
        user: UserIdentity,
        composer_meta_updates: LibraryForkMetaUpdates,
    ) -> CompositionStateResponse: ...


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _authority(request: Request) -> RepositoryLibraryAuthority:
    authority: RepositoryLibraryAuthority = request.app.state.library_authority
    return authority


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _hidden() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _require_governance(request: Request) -> None:
    if _settings(request).workflow_governance != "on":
        raise HTTPException(
            status_code=409,
            detail={"error_type": "workflow_governance_off", "detail": "Workflow governance is off"},
        )


async def _governed_user(request: Request, user: Annotated[UserIdentity, Depends(get_current_user)]) -> UserIdentity:
    _require_governance(request)
    return user


async def _curator(request: Request, user: Annotated[UserIdentity, Depends(get_current_user)]) -> UserIdentity:
    _require_governance(request)
    if not await _live_curator_fast_screen(request, user):
        raise _hidden()
    return user


async def _live_curator_fast_screen(request: Request, user: UserIdentity) -> bool:
    """Hide curator routes early; the library authority checks again under lock."""
    identity_authority = _identity_authority(request)
    identity = await run_sync_in_worker(identity_authority.read_identity_summary, identity_id=user.user_id)
    if identity is None or identity.access_state != "active" or identity.kind != "human":
        return False
    if identity.provider != _settings(request).auth_provider:
        return False
    return await run_sync_in_worker(identity_authority.holds_active_role, identity_id=user.user_id, role="curator")


def _refused(exc: LibraryAuthorityRefusal) -> HTTPException:
    if type(exc) is CuratorAuthorityRequired:
        return _hidden()
    if type(exc) is LibraryEntryNotFound:
        return HTTPException(status_code=404, detail={"error_type": "library_entry_not_found", "detail": str(exc)})
    detail: LibraryErrorDetail = {"error_type": _ERROR_TYPES[type(exc)], "detail": str(exc)}
    if isinstance(exc, LibraryEntryNeedsProfileBoundSource):
        detail["sources"] = list(exc.source_names)
    if isinstance(exc, (LibraryEntryAlreadyCurated, LibraryEntryNotForkable)):
        detail["current_state"] = exc.current_state
    return HTTPException(status_code=409, detail=detail)


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _view(entry: LibraryEntryRecord, *, viewer: str) -> LibraryEntryView:
    return LibraryEntryView(
        entry_id=entry.entry_id,
        published_from_session_id=entry.published_from_session_id if entry.published_by_identity_id == viewer else None,
        payload_digest=entry.payload_digest,
        compartment_id=entry.compartment_id,
        title=entry.title,
        version=entry.version,
        published_by_identity_id=entry.published_by_identity_id,
        curated_by_identity_id=entry.curated_by_identity_id,
        published_at=entry.published_at,
        accepted_at=entry.accepted_at,
        rejected_at=entry.rejected_at,
        rejection_note=entry.rejection_note,
        deprecated_at=entry.deprecated_at,
        recalled_at=entry.recalled_at,
        note=entry.note,
        state=entry.state,
    )


async def _curate(
    request: Request,
    response: Response,
    *,
    entry_id: str,
    curator: UserIdentity,
    note: str | None,
    action: LibraryAction,
) -> LibraryEntryView:
    recorder = _recorder(request)
    authority = _authority(request)
    writers = {
        "accepted": recorder.record_library_accepted,
        "rejected": recorder.record_library_rejected,
        "deprecated": recorder.record_library_deprecated,
        "recalled": recorder.record_library_recalled,
    }
    mutations = {
        "accepted": authority.accept,
        "rejected": authority.reject,
        "deprecated": authority.deprecate,
        "recalled": authority.recall,
    }

    def record(event: LibraryCurated) -> None:
        writers[event.action](
            request,
            provider=_settings(request).auth_provider,
            entry_id=event.entry.entry_id,
            publisher_identity_id=event.entry.published_by_identity_id,
            actor_identity_id=event.actor_identity_id,
            payload_digest=event.entry.payload_digest,
            entry_compartment_id=event.entry.compartment_id,
            note=event.note,
        )

    try:
        entry = await run_sync_in_worker(mutations[action], entry_id=entry_id, curator=curator.user_id, note=note, record=record)
    except LibraryAuthorityRefusal as exc:
        raise _refused(exc) from exc
    _uncacheable(response)
    return _view(entry, viewer=curator.user_id)


def create_library_router() -> APIRouter:
    router = APIRouter(tags=["library"])

    @router.post("/api/sessions/{session_id}/library/publish", status_code=201, response_model=LibraryEntryView)
    async def publish_entry(
        session_id: UUID,
        body: PublishLibraryEntryRequest,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(_governed_user)],
    ) -> LibraryEntryView:
        session = await _verify_session_ownership(session_id, user, request)
        service: SessionServiceProtocol = request.app.state.session_service
        settings = _settings(request)
        recorder = _recorder(request)

        def record(event: LibraryPublished) -> None:
            recorder.record_library_published(
                request,
                provider=settings.auth_provider,
                entry_id=event.entry.entry_id,
                publisher_identity_id=event.entry.published_by_identity_id,
                actor_identity_id=event.actor_identity_id,
                payload_digest=event.entry.payload_digest,
                entry_compartment_id=event.entry.compartment_id,
                title=event.entry.title,
                version=event.entry.version,
                published_from_session_id=event.entry.published_from_session_id,
            )

        try:
            lease = await SessionOperationLease.acquire(
                service.session_operation_authority,
                session_id=session.id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
        except SessionOperationConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error_type": "session_operation_conflict", "detail": "The session is being changed"},
            ) from exc
        async with lease:
            # The exclusive COMPOSE fence keeps the state current until the
            # exact public projection is stored and its audit callback runs.
            state_record = await service.get_current_state(session.id)
            if state_record is None:
                raise HTTPException(status_code=404, detail="No composition state exists")
            state = state_from_record(state_record)
            try:
                entry = await run_sync_in_worker(
                    _authority(request).publish,
                    session_id=str(session.id),
                    state=state,
                    title=body.title,
                    published_by=user.user_id,
                    compartment_id=settings.compartment_id,
                    record=record,
                )
            except LibraryAuthorityRefusal as exc:
                raise _refused(exc) from exc
        _uncacheable(response)
        return _view(entry, viewer=user.user_id)

    @router.get("/api/library", response_model=LibraryListResponse)
    async def list_entries(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(_governed_user)],
        view: Annotated[LibraryView, Query()] = "accepted",
    ) -> LibraryListResponse:
        authority = _authority(request)
        if view == "queue":
            if not await _live_curator_fast_screen(request, user):
                raise _hidden()
            try:
                entries = await run_sync_in_worker(
                    authority.curation_queue,
                    curator_identity_id=user.user_id,
                    provider=_settings(request).auth_provider,
                )
            except CuratorAuthorityRequired as exc:
                raise _hidden() from exc
        elif view == "mine":
            entries = await run_sync_in_worker(authority.published_by, identity_id=user.user_id)
        else:
            entries = await run_sync_in_worker(authority.browse)
        _uncacheable(response)
        return LibraryListResponse(view=view, entries=[_view(entry, viewer=user.user_id) for entry in entries])

    @router.post("/api/library/{entry_id}/accept", response_model=LibraryEntryView)
    async def accept_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: Annotated[UserIdentity, Depends(_curator)],
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="accepted")

    @router.post("/api/library/{entry_id}/reject", response_model=LibraryEntryView)
    async def reject_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: Annotated[UserIdentity, Depends(_curator)],
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="rejected")

    @router.post("/api/library/{entry_id}/deprecate", response_model=LibraryEntryView)
    async def deprecate_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: Annotated[UserIdentity, Depends(_curator)],
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="deprecated")

    @router.post("/api/library/{entry_id}/recall", response_model=LibraryEntryView)
    async def recall_entry(
        entry_id: str,
        body: CurateLibraryEntryRequest,
        request: Request,
        response: Response,
        curator: Annotated[UserIdentity, Depends(_curator)],
    ) -> LibraryEntryView:
        return await _curate(request, response, entry_id=entry_id, curator=curator, note=body.note, action="recalled")

    @router.post("/api/library/{entry_id}/fork", status_code=201, response_model=LibraryForkResponse)
    async def fork_entry(
        entry_id: str,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(_governed_user)],
    ) -> LibraryForkResponse:
        settings = _settings(request)
        try:
            # This authority read locks and checks acceptance in one
            # transaction. A recall committed first refuses; an authorized
            # fork can finish even if a later recall commits while it seeds.
            source = await run_sync_in_worker(
                _authority(request).authorize_fork_source,
                entry_id=entry_id,
                forker_identity_id=user.user_id,
                provider=settings.auth_provider,
            )
        except LibraryAuthorityRefusal as exc:
            raise _refused(exc) from exc
        # The app supplies the ordinary YAML importer as a typed seam. Resolve
        # it before creating a session, so incomplete wiring cannot strand one.
        try:
            seed: LibraryStateSeeder = request.app.state.library_state_seeder
        except AttributeError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error_type": "library_seed_unavailable", "detail": "Library fork seeding is unavailable"},
            ) from exc
        service: SessionServiceProtocol = request.app.state.session_service
        session = await service.create_session(user.user_id, f"Fork of {source.entry.title}", settings.auth_provider)
        try:
            seeded = await seed(
                session=session,
                body=ImportStateYamlRequest(yaml=source.payload_yaml, source_blob_ids=None),
                request=request,
                user=user,
                composer_meta_updates={
                    "library_fork": {
                        "entry_id": source.entry.entry_id,
                        "payload_digest": source.entry.payload_digest,
                        "published_from_session_id": source.entry.published_from_session_id,
                        "compartment_id": source.entry.compartment_id,
                        "version": source.entry.version,
                    }
                },
            )
        except BaseException as seed_exc:
            # A failed import must not leave a live, empty session behind.
            # Preserve the original failure even if cleanup also fails.
            try:
                await service.archive_session(session.id)
            except BaseException as archive_exc:
                raise seed_exc from archive_exc
            raise
        _uncacheable(response)
        return LibraryForkResponse(session_id=session.id, state_id=seeded.id)

    return router
