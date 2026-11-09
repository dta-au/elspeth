"""Import a shared pipeline and its frozen files into a new session.

A share link minted with import allowed carries a private manifest of the
files its graph uses, each frozen into the payload store under its sha256.
Importing it (``POST /api/sessions/shared/{token}/import``) gives the
recipient a new session holding a copy of the graph and of those files.

Library-fork parity: the new session is live from the start, there is no
staging and no server-side idempotency, and any failure after the session
exists archives it. Order:

1. Verify the token and read everything first — the snapshot, the import
   opt-in and the bytes of every frozen file. Nothing is created until all
   reads succeed.
2. Resolve the YAML seeder (503 when unwired), then create the session.
3. Copy each distinct frozen file into it as an ordinary blob under a CREATE
   lease; the blob service enforces the session and identity quotas. Each
   copy's content hash must equal the manifest's sha256.
4. Seed the graph from the snapshot's public YAML through the ordinary YAML
   importer, re-binding every manifest binding to the recipient's copies and
   recording ``shared_import`` provenance in the seeded state's
   ``composer_meta``.

The server authors no pipeline structure: the graph is the sharer's own,
regenerated from the content-addressed snapshot, and the seeder runs every
check a pasted-YAML import runs.

Layer: L3 (application).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from fastapi import HTTPException, Request
from pydantic import ValidationError

from elspeth.contracts.blobs import (
    BlobQuotaExceededError,
    BlobServiceProtocol,
    IdentityStorageQuotaExceededError,
    StorageAccountingUnavailableError,
    StorageMimeType,
)
from elspeth.contracts.blobs_inline import ContentEncoding
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import generate_public_yaml
from elspeth.web.config import WebSettings
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol
from elspeth.web.sessions.routes.composer.state import (
    ImportStateYamlRequest,
    InlineBlobBindingRequest,
    SharedImportFile,
    SourceBlobRowsBindingRequest,
)
from elspeth.web.sessions.routes.workflow.library import LibraryStateSeeder
from elspeth.web.shareable_reviews.file_manifest import file_binding_from_json
from elspeth.web.shareable_reviews.models import SharedImportResponse
from elspeth.web.shareable_reviews.service import ShareableReviewService, ShareFileEntry

__all__ = ["ImportNotAllowedError", "import_shared_snapshot"]

_FALLBACK_TITLE = "Copy of shared pipeline"


class ImportNotAllowedError(Exception):
    """The share link is view-only: its sharer did not allow import."""


@dataclass(frozen=True, slots=True)
class _FrozenCopy:
    """One frozen file's verified bytes and its manifest entry."""

    entry: ShareFileEntry
    content: bytes


@dataclass(frozen=True, slots=True)
class _InlineSite:
    sha256: str
    encoding: ContentEncoding


@dataclass(frozen=True, slots=True)
class _BindingPlan:
    """Every manifest binding site, pointing at the sha256 of its file."""

    source_blob_ref: dict[str, str]
    source_blob_rows: dict[str, list[str]]
    inline_content: dict[str, _InlineSite]

    def import_request(self, *, public_yaml: str, blob_ids: dict[str, str]) -> ImportStateYamlRequest:
        """The seeder request that re-binds every site to the recipient's copy."""
        return ImportStateYamlRequest(
            yaml=public_yaml,
            source_blob_ids={name: blob_ids[sha256] for name, sha256 in self.source_blob_ref.items()} or None,
            source_blob_rows={
                name: SourceBlobRowsBindingRequest(blob_ids=[blob_ids[sha256] for sha256 in shas])
                for name, shas in self.source_blob_rows.items()
            }
            or None,
            inline_blob_bindings={
                field_path: InlineBlobBindingRequest(blob_id=blob_ids[site.sha256], encoding=site.encoding)
                for field_path, site in self.inline_content.items()
            }
            or None,
        )


def _plan_bindings(entries: Sequence[ShareFileEntry]) -> _BindingPlan:
    """Map every binding site in the manifest to the sha256 of its file.

    The manifest is Tier-1 (our own digest-verified snapshot, already parsed
    by the share service), so a site bound twice or a ``blob_rows`` list with
    a gap is corruption, not something to repair.
    """
    source_blob_ref: dict[str, str] = {}
    rows: dict[str, dict[int, str]] = {}
    inline_content: dict[str, _InlineSite] = {}
    for entry in entries:
        sha256 = entry["sha256"]
        for raw in entry["bindings"]:
            binding = file_binding_from_json(raw)
            if binding.kind == "source_blob_ref" and binding.source_name is not None:
                if binding.source_name in source_blob_ref:
                    raise AuditIntegrityError(f"share manifest binds source {binding.source_name!r} twice")
                source_blob_ref[binding.source_name] = sha256
            elif binding.kind == "source_blob_rows" and binding.source_name is not None and binding.row_index is not None:
                source_rows = rows.setdefault(binding.source_name, {})
                if binding.row_index in source_rows:
                    raise AuditIntegrityError(f"share manifest binds source {binding.source_name!r} row {binding.row_index} twice")
                source_rows[binding.row_index] = sha256
            elif binding.kind == "inline_content" and binding.field_path is not None and binding.encoding is not None:
                if binding.field_path in inline_content:
                    raise AuditIntegrityError(f"share manifest binds {binding.field_path!r} twice")
                inline_content[binding.field_path] = _InlineSite(sha256=sha256, encoding=binding.encoding)
            else:
                raise AuditIntegrityError(f"share manifest binding {raw!r} does not match its kind")
    source_blob_rows: dict[str, list[str]] = {}
    for source_name, by_index in rows.items():
        if sorted(by_index) != list(range(len(by_index))):
            raise AuditIntegrityError(f"share manifest rows for source {source_name!r} are not numbered 0..n-1")
        source_blob_rows[source_name] = [by_index[index] for index in range(len(by_index))]
    return _BindingPlan(source_blob_ref=source_blob_ref, source_blob_rows=source_blob_rows, inline_content=inline_content)


async def _copy_files(
    *,
    request: Request,
    session: SessionRecord,
    session_service: SessionServiceProtocol,
    copies: Sequence[_FrozenCopy],
    payload_digest: str,
) -> dict[str, str]:
    """Create one blob per frozen file in the new session; return sha256 → blob id.

    Quota refusals map exactly as the upload route (``create_blob_upload``)
    maps them.
    """
    blob_service: BlobServiceProtocol = request.app.state.blob_service
    lease = await SessionOperationLease.acquire(
        session_service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=session_service.session_operation_owner_instance_id,
        lease_seconds=session_service.session_operation_lease_seconds,
    )
    try:
        blob_ids: dict[str, str] = {}
        for copy in copies:
            entry = copy.entry
            # The share service's manifest parser admitted only members of
            # STORAGE_MIME_TYPES; mypy cannot follow that frozenset membership
            # test to the Literal, so cast. The blob service re-checks it.
            mime_type = cast(StorageMimeType, entry["mime_type"])
            try:
                record = await blob_service.create_blob(
                    session.id,
                    entry["filename"],
                    copy.content,
                    mime_type,
                    created_by="user",
                    source_description=f"imported from a shared link ({payload_digest})",
                    session_operation_context=lease.context,
                )
            except IdentityStorageQuotaExceededError as exc:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_type": "storage_quota_exceeded",
                        "detail": str(exc),
                        "dimension": "storage",
                        "scope": exc.scope,
                        "limit": str(exc.limit_bytes),
                        "cap": None if exc.cap is None else str(exc.cap),
                        "ceiling": None if exc.ceiling is None else str(exc.ceiling),
                        "usage": str(exc.usage),
                    },
                ) from None
            except StorageAccountingUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None
            except BlobQuotaExceededError as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from None
            # The copy must be the frozen file the sharer shared (spec §2 step 4).
            if record.content_hash != entry["sha256"]:
                raise AuditIntegrityError(
                    f"imported copy of {entry['filename']!r} hashes to {record.content_hash!r}, not the shared {entry['sha256']!r}"
                )
            blob_ids[entry["sha256"]] = str(record.id)
        return blob_ids
    finally:
        await lease.close()


async def import_shared_snapshot(*, token: str, request: Request, user: UserIdentity) -> SharedImportResponse:
    """Import the shared graph behind ``token`` into a new session owned by ``user``.

    Raises:
        InvalidToken: the token is tampered, expired or malformed, or its
            snapshot is unreadable.
        ImportNotAllowedError: the link is view-only.
        PayloadNotFoundError: the snapshot or a frozen file is gone.
        IntegrityError: a frozen file's bytes no longer match its sha256.
        HTTPException: 503 when seeding is unwired, the quota refusals of
            the upload route, and every refusal the YAML seeder raises.

    Everything up to the session's creation only reads. Any failure after
    it archives the new session and re-raises the original failure, even
    if the archive also fails.
    """
    service: ShareableReviewService = request.app.state.shareable_review_service
    payload, snapshot = await service.load_snapshot(token=token)
    if not snapshot.import_allowed:
        raise ImportNotAllowedError("this share link does not allow import")
    payload_store: FilesystemPayloadStore = request.app.state.payload_store
    copies = [
        _FrozenCopy(entry=entry, content=await run_sync_in_worker(payload_store.retrieve, entry["sha256"])) for entry in snapshot.files
    ]
    plan = _plan_bindings(snapshot.files)
    composition = CompositionState.from_dict(snapshot.blob["composition_snapshot"])
    public_yaml = generate_public_yaml(composition)
    # Validate the request shape that does not depend on the new blob ids
    # (the YAML length cap) before anything is created. 422 matches what
    # the YAML-import route answers for the same over-cap body.
    try:
        ImportStateYamlRequest(yaml=public_yaml)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_input=False, include_context=False),
        ) from None
    name = composition.metadata.name.strip()
    title = f"Copy of {name}" if name else _FALLBACK_TITLE
    # Resolve the seeder before creating a session, so incomplete wiring
    # cannot strand one (library-fork precedent).
    try:
        seed: LibraryStateSeeder = request.app.state.library_state_seeder
    except AttributeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error_type": "shared_import_unavailable", "detail": "Importing a shared pipeline is unavailable"},
        ) from exc
    settings: WebSettings = request.app.state.settings
    session_service: SessionServiceProtocol = request.app.state.session_service
    session = await session_service.create_session(user.user_id, title, settings.auth_provider)
    try:
        blob_ids = await _copy_files(
            request=request,
            session=session,
            session_service=session_service,
            copies=copies,
            payload_digest=payload.payload_digest,
        )
        files: list[SharedImportFile] = [{"sha256": copy.entry["sha256"], "blob_id": blob_ids[copy.entry["sha256"]]} for copy in copies]
        seeded = await seed(
            session=session,
            body=plan.import_request(public_yaml=public_yaml, blob_ids=blob_ids),
            request=request,
            user=user,
            composer_meta_updates={
                "shared_import": {
                    "payload_digest": payload.payload_digest,
                    "shared_by_user_id": snapshot.blob["created_by_user_id"],
                    "shared_by_username": snapshot.blob["created_by_username"],
                    "shared_from_session_id": str(payload.session_id),
                    "shared_from_state_id": str(payload.state_id),
                    "files": files,
                }
            },
        )
    except BaseException as import_exc:
        # A failed import must not leave a live, half-built session behind.
        # Preserve the original failure even if cleanup also fails.
        try:
            await session_service.archive_session(session.id)
        except BaseException as archive_exc:
            raise import_exc from archive_exc
        raise
    return SharedImportResponse(session_id=session.id, state_id=seeded.id)
