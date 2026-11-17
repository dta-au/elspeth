"""REST API routes for session-scoped blob management.

All endpoints require authentication and verify session ownership.
Blob ownership is enforced via the session boundary — a blob must
belong to the authenticated user's session.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, cast
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, desc, select

from elspeth.contracts.binary_documents import (
    BINARY_DOCUMENT_FORMAT_BY_MIME,
    BINARY_DOCUMENT_MAX_BYTES,
    BINARY_DOCUMENT_MIME_BY_FORMAT,
    binary_document_signature_matches,
    detect_binary_document_signature,
)
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.prompt_editor import prompt_bindings_for_blob, repin_prompt_bindings
from elspeth.web.blobs.protocol import (
    ALLOWED_MIME_TYPES,
    BINARY_DOCUMENT_MIME_TYPES,
    BlobActiveRunError,
    BlobContentMissingError,
    BlobInProgressForkError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobPendingProposalError,
    BlobQuotaExceededError,
    BlobRecord,
    BlobStateError,
    IdentityStorageQuotaExceededError,
    StorageAccountingUnavailableError,
    StorageMimeType,
)
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.schemas import BlobMetadataResponse, CreateInlineBlobRequest
from elspeth.web.blobs.service import BlobServiceImpl, content_hash, sanitize_filename
from elspeth.web.blobs.sniff import detect_mime_type
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    record_prompt_file_approval,
)
from elspeth.web.sessions.protocol import CompositionStateData, SessionCompositionStateCreation, SessionOperationMutationTransaction

_MAX_PROMPT_FILE_BYTES = 256 * 1024


class SavePromptFileRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    content: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SavePromptFileResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changed: bool
    content_sha256: str


_EDITOR_SAVE_ENCODINGS: frozenset[str] = frozenset({"utf-8", "utf-8-sig"})


def _prompt_save_refusal(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message})


def _blob_response(record: BlobRecord) -> BlobMetadataResponse:
    """Convert a BlobRecord to a response model (excludes storage_path)."""
    return BlobMetadataResponse(
        id=str(record.id),
        session_id=str(record.session_id),
        filename=record.filename,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        content_hash=record.content_hash,
        created_at=record.created_at,
        created_by=record.created_by,
        source_description=record.source_description,
        status=record.status,
        # Inline-blob provenance (Phase 5a Task 2.5). The Tier 1 read
        # guard on ``BlobRecord`` has already validated
        # ``creation_modality`` against the closed enum; the API surface
        # uses the wire-form value (Literal alias) for strict-mode
        # compatibility, so we pass ``.value`` here rather than the enum
        # instance.
        creation_modality=record.creation_modality.value,
        created_from_message_id=record.created_from_message_id,
        creating_model_identifier=record.creating_model_identifier,
        creating_model_version=record.creating_model_version,
        creating_provider=record.creating_provider,
        creating_composer_skill_hash=record.creating_composer_skill_hash,
        creating_arguments_hash=record.creating_arguments_hash,
    )


async def _verify_session_and_get_blob_service(
    session_id: UUID,
    user: UserIdentity,
    request: Request,
) -> BlobServiceImpl:
    """Verify session ownership and return the blob service.

    Returns 404 (not 403) for ownership failures to preserve the
    anti-IDOR pattern from the session routes.
    """
    session_service = request.app.state.session_service
    try:
        session = await session_service.get_session(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found") from None

    settings = request.app.state.settings
    if session.archived_at is not None or session.user_id != user.user_id or session.auth_provider_type != settings.auth_provider:
        raise HTTPException(status_code=404, detail="Session not found")

    blob_service: BlobServiceImpl = request.app.state.blob_service
    return blob_service


async def _get_owned_blob(
    blob_service: BlobServiceImpl,
    session_id: UUID,
    blob_id: UUID,
    *,
    session_operation_context: SessionOperationContext,
) -> BlobRecord:
    """Fetch a blob and verify it belongs to the given session.

    Runs under the route's lease. Returns 404 for missing blobs or session
    mismatches. The service already
    reads as the fence's session, so a foreign blob surfaces as
    ``BlobNotFoundError``; the explicit session comparison stays as the
    route-level statement of the same invariant.
    """
    try:
        blob = await blob_service.get_blob(
            blob_id,
            session_operation_context=session_operation_context,
        )
    except BlobNotFoundError:
        raise HTTPException(status_code=404, detail="Blob not found") from None

    if blob.session_id != session_id:
        raise HTTPException(status_code=404, detail="Blob not found")

    return blob


def create_blobs_router() -> APIRouter:
    """Create the blob management router."""
    router = APIRouter(
        prefix="/api/sessions/{session_id}/blobs",
        tags=["blobs"],
    )

    @router.put("/{blob_id}/prompt-text", response_model=SavePromptFileResponse)
    async def save_prompt_text(
        session_id: UUID,
        blob_id: UUID,
        body: SavePromptFileRequest,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    ) -> SavePromptFileResponse:
        """Save an existing bound text file; the owner's save approves its bytes."""
        try:
            blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        except HTTPException as exc:
            raise _prompt_save_refusal(404, "prompt_file_not_found", "This session or file is unavailable.") from exc
        session_service = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            blob = await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)
            if blob.status != "ready":
                raise _prompt_save_refusal(409, "prompt_file_not_ready", "Wait for this file to become ready, then retry.")
            if blob.mime_type not in ALLOWED_MIME_TYPES:
                raise _prompt_save_refusal(422, "prompt_file_not_text", "Choose a UTF-8 text file for this prompt.")
            try:
                new_bytes = body.content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _prompt_save_refusal(422, "prompt_file_not_text", "Enter valid UTF-8 text.") from exc
            if len(new_bytes) > _MAX_PROMPT_FILE_BYTES:
                raise _prompt_save_refusal(413, "prompt_file_too_large", "Keep this prompt file at or below 256 KiB.")
            old_bytes = await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)
            try:
                old_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _prompt_save_refusal(422, "prompt_file_not_text", "This file is not valid UTF-8 text; use a text file.") from exc
            if body.expected_sha256 != content_hash(old_bytes):
                raise _prompt_save_refusal(409, "prompt_file_stale", "This file changed since you opened it. Reload it and retry.")
            state = await session_service.get_current_state(session_id)
            bindings, mixed = prompt_bindings_for_blob(state, blob)
            if mixed:
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_mixed_use",
                    "This file is also used outside a prompt; create a separate prompt file or unbind those uses.",
                )
            if not bindings:
                raise _prompt_save_refusal(409, "prompt_file_unbound", "Bind this file to a prompt before saving it here.")
            if len(bindings) > 1:
                # A save approves what the user is looking at. A second binding (another node or
                # another prompt field, possibly re-pointed by the assistant) has its own review.
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_shared_binding",
                    "This file backs more than one prompt; give each prompt its own file before editing it here.",
                )
            if any(binding.encoding not in _EDITOR_SAVE_ENCODINGS for binding in bindings):
                # The editor saves UTF-8; a prompt that decodes this file another way would run
                # different text from what the user saved.
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_encoding_unsupported",
                    "This prompt reads the file with a non-UTF-8 encoding; re-attach it as UTF-8 text before editing it here.",
                )
            if new_bytes == old_bytes:
                return SavePromptFileResponse(changed=False, content_sha256=blob.content_hash)
            replacement = replace(
                blob,
                created_by="user",
                size_bytes=len(new_bytes),
                content_hash=content_hash(new_bytes),
                creation_modality=CreationModality.VERBATIM,
                created_from_message_id=None,
                creating_model_identifier=None,
                creating_model_version=None,
                creating_provider=None,
                creating_composer_skill_hash=None,
                creating_arguments_hash=None,
            )
            driver = BlobReplacementCoordinator(
                engine=blob_service._engine,
                data_dir=blob_service._data_dir,
                session_operation_authority=session_service.session_operation_authority,
            )

            def approve_stored_bytes(conn: Connection, stored_bytes: bytes, transaction: SessionOperationMutationTransaction) -> None:
                current_head = conn.execute(
                    select(composition_states_table.c.id)
                    .where(composition_states_table.c.session_id == str(session_id))
                    .order_by(desc(composition_states_table.c.version))
                    .limit(1)
                ).scalar_one_or_none()
                if state is None or current_head != str(state.id):
                    raise PromptFileApprovalStale()
                stored_hash = content_hash(stored_bytes)
                if stored_hash != replacement.content_hash:
                    raise AuditIntegrityError("stored prompt bytes differ from replacement metadata")
                now = datetime.now(UTC)
                transaction.composition_states.append_state(
                    SessionCompositionStateCreation(
                        id=uuid4(),
                        data=CompositionStateData(
                            sources=state.sources,
                            nodes=repin_prompt_bindings(state, bindings, stored_hash),
                            edges=state.edges,
                            outputs=state.outputs,
                            metadata_=state.metadata_,
                            is_valid=False,
                            composer_meta=state.composer_meta,
                        ),
                        provenance="interpretation_resolve",
                        created_at=now,
                        derived_from_state_id=state.id,
                    )
                )
                for binding in bindings:
                    record_prompt_file_approval(
                        conn,
                        PromptFileApprovalRequest(
                            session_id=session_id,
                            node_id=binding.node_id,
                            field_path=binding.field_path,
                            blob_id=blob_id,
                            content_sha256=stored_hash,
                            actor_user_id=user.user_id,
                            trigger=PromptApprovalTrigger.EDITOR_SAVE,
                        ),
                        now=now,
                    )

            try:
                await run_sync_in_worker(
                    lambda: driver.replace_blob(
                        expected=blob,
                        replacement=replacement,
                        content=new_bytes,
                        context=lease.context,
                        max_storage_per_session=request.app.state.settings.max_blob_storage_per_session_bytes,
                        accepting_proposal_id=None,
                        on_metadata_commit=approve_stored_bytes,
                    )
                )
            except PromptFileApprovalStale as exc:
                raise _prompt_save_refusal(409, "prompt_file_stale", "Prompt bindings changed. Reload the session and retry.") from exc
            except (BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError) as exc:
                raise _prompt_save_refusal(409, "prompt_file_retained", str(exc)) from exc
            except BlobQuotaExceededError as exc:
                raise _prompt_save_refusal(413, "prompt_file_too_large", "This save exceeds the session storage limit.") from exc
            return SavePromptFileResponse(changed=True, content_sha256=replacement.content_hash)
        finally:
            await lease.close()

    @router.post("", status_code=201, response_model=BlobMetadataResponse)
    async def create_blob_upload(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        file: UploadFile = File(...),  # noqa: B008
    ) -> BlobMetadataResponse:
        """Create a blob from a multipart file upload."""
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        session_service = request.app.state.session_service
        settings = request.app.state.settings

        # Record the browser-declared MIME type but do NOT reject based on
        # it alone. Browser MIME assignment is Tier 3 (untrusted) — browsers
        # commonly report .csv files as "application/vnd.ms-excel" or other
        # non-standard types. Server-side content sniffing below determines
        # the effective type.
        mime_type = file.content_type or "application/octet-stream"

        # Validate filename at the HTTP boundary (Tier 3) so malformed
        # uploads surface as 422, not an uncaught ValueError propagated
        # from sanitize_filename() deeper in the service layer.
        try:
            original_filename = sanitize_filename(file.filename or "upload")
            require_no_credential_material(original_filename, surface="blob_filename")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        chunks: list[bytes] = []
        total_size = 0
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum size of {settings.max_upload_bytes} bytes",
                )
            chunks.append(chunk)
        content = b"".join(chunks)

        if mime_type in BINARY_DOCUMENT_MIME_TYPES:
            # Binary-document admission (elspeth-0c6a343921). The DECLARED
            # MIME type chooses the signature rule; the byte signature
            # proves agreement — it never reclassifies. No text sniffing,
            # no local PDF parsing (the successful Textract response is the
            # page-count authority).
            if not content:
                raise HTTPException(status_code=422, detail="Binary document upload must not be empty")
            if total_size > BINARY_DOCUMENT_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Binary document exceeds the {BINARY_DOCUMENT_MAX_BYTES}-byte Textract inline ceiling",
                )
            declared_format = BINARY_DOCUMENT_FORMAT_BY_MIME[mime_type]
            if not binary_document_signature_matches(declared_format, content):
                raise HTTPException(
                    status_code=415,
                    detail=f"Content signature does not match declared type '{mime_type}'",
                )
            effective_mime_typed = cast(StorageMimeType, mime_type)
        else:
            # A known binary signature reaching the text path is a declaration
            # mismatch by construction (the matching binary MIME would have
            # taken the branch above) — reject, never reclassify. Without this,
            # an ASCII-only PDF declared text/plain (or octet-stream) sniffs as
            # text and bypasses the binary size/signature admission entirely.
            smuggled_format = detect_binary_document_signature(content)
            if smuggled_format is not None:
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"Content signature matches binary format '{smuggled_format}' but the declared type is "
                        f"'{mime_type}'; declare '{BINARY_DOCUMENT_MIME_BY_FORMAT[smuggled_format]}' instead"
                    ),
                )

            # Server-side MIME detection — reject if declared type doesn't
            # match detected content (defense-in-depth, client MIME is untrusted)
            detected = detect_mime_type(content)
            if detected is not None and detected != mime_type and detected not in ALLOWED_MIME_TYPES:
                raise HTTPException(
                    status_code=415,
                    detail=f"Detected content type '{detected}' does not match declared '{mime_type}' and is not in the allowed set.",
                )
            # Use detected type if available — record the truth, not the claim
            effective_mime = detected if detected is not None else mime_type
            if effective_mime not in ALLOWED_MIME_TYPES:
                raise HTTPException(
                    status_code=415,
                    detail=f"Detected content type '{effective_mime}' is not in the allowed set.",
                )
            # The membership check above narrows effective_mime to one of the
            # AllowedMimeType Literal members (a subset of StorageMimeType);
            # mypy cannot follow a frozenset membership test to a Literal, so
            # cast explicitly.  The cast is safe *because* of the runtime check
            # two lines up — removing the check would also be a type-safety
            # regression.
            effective_mime_typed = cast(StorageMimeType, effective_mime)

        # Both admission arms converge on ONE fenced create: the lease is the
        # session's CREATE fence and the effect runs inside its protecting
        # try, closed in the sole finally.
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            try:
                record = await blob_service.create_blob(
                    session_id=session_id,
                    filename=original_filename,
                    content=content,
                    mime_type=effective_mime_typed,
                    created_by="user",
                    source_description="uploaded",
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
            return _blob_response(record)
        finally:
            await lease.close()

    @router.post("/inline", status_code=201, response_model=BlobMetadataResponse)
    async def create_blob_inline(
        session_id: UUID,
        body: CreateInlineBlobRequest,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    ) -> BlobMetadataResponse:
        """Create a blob from inline text/JSON content.

        Request validation — including filename sanitization, mime_type
        allowlist enforcement, and rejection of unknown fields — is
        performed by CreateInlineBlobRequest at the Pydantic layer.
        This route only enforces size and persistence-layer concerns.
        """
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        require_no_credential_material(body.filename, surface="blob_filename")
        session_service = request.app.state.session_service

        settings = request.app.state.settings
        content_bytes = body.content.encode("utf-8")
        if len(content_bytes) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Content exceeds maximum size of {settings.max_upload_bytes} bytes",
            )

        # This route's MIME vocabulary is text-only (enforced by the request
        # model), so a known binary signature in the pasted content is a
        # declaration mismatch by construction — the same smuggle the upload
        # route's text path rejects. Reject, never reclassify.
        smuggled_format = detect_binary_document_signature(content_bytes)
        if smuggled_format is not None:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Content signature matches binary format '{smuggled_format}' but inline blobs are text-only; "
                    f"upload the file as '{BINARY_DOCUMENT_MIME_BY_FORMAT[smuggled_format]}' instead"
                ),
            )

        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            try:
                record = await blob_service.create_blob(
                    session_id=session_id,
                    filename=body.filename,
                    content=content_bytes,
                    mime_type=body.mime_type,
                    created_by="user",
                    source_description="created inline",
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
            return _blob_response(record)
        finally:
            await lease.close()

    @router.get("", response_model=list[BlobMetadataResponse])
    async def list_blobs(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[BlobMetadataResponse]:
        """List blobs for a session, newest first."""
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        records = await blob_service.list_blobs(session_id, limit=limit, offset=offset)
        return [_blob_response(r) for r in records]

    @router.get("/{blob_id}", response_model=BlobMetadataResponse)
    async def get_blob_metadata(
        session_id: UUID,
        blob_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    ) -> BlobMetadataResponse:
        """Get blob metadata."""
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        session_service = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            blob = await _get_owned_blob(
                blob_service,
                session_id,
                blob_id,
                session_operation_context=lease.context,
            )
            return _blob_response(blob)
        finally:
            await lease.close()

    @router.get("/{blob_id}/content")
    async def download_blob_content(
        session_id: UUID,
        blob_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    ) -> Response:
        """Download blob content.

        Enforces lifecycle state before reading: only ``ready`` blobs
        are downloadable.  Pending/error blobs return 409 (the resource
        exists but its state precludes the operation).
        """
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        session_service = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            blob = await _get_owned_blob(
                blob_service,
                session_id,
                blob_id,
                session_operation_context=lease.context,
            )
            # Lifecycle guard — defense-in-depth (service layer also checks)
            if blob.status != "ready":
                raise HTTPException(
                    status_code=409,
                    detail=f"Blob is in '{blob.status}' state and is not downloadable",
                )
            try:
                content = await blob_service.read_blob_content(
                    blob_id,
                    session_operation_context=lease.context,
                )
            except BlobNotFoundError:
                raise HTTPException(status_code=404, detail="Blob content not found") from None
            except BlobStateError:
                # Use a generic message — blob.status was fetched before the
                # service call and may be stale if a concurrent transition
                # happened between the route guard and the service read.
                raise HTTPException(status_code=409, detail="Blob is not in a downloadable state") from None
            except (BlobIntegrityError, BlobContentMissingError):
                raise HTTPException(status_code=500, detail="Blob content integrity verification failed") from None
            return Response(
                content=content,
                media_type=blob.mime_type,
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quote(blob.filename, safe='')}",
                    "X-Content-SHA256": content_hash(content),
                },
            )
        finally:
            await lease.close()

    @router.get("/{blob_id}/preview")
    async def preview_blob_content(
        session_id: UUID,
        blob_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        limit: int = Query(5000, ge=1, le=50_000),
    ) -> Response:
        """Return a bounded prefix for inline blob preview."""
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        session_service = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            blob = await _get_owned_blob(
                blob_service,
                session_id,
                blob_id,
                session_operation_context=lease.context,
            )
            if blob.status != "ready":
                raise HTTPException(status_code=409, detail=f"Blob is in '{blob.status}' state and is not previewable")
            try:
                content, truncated = await blob_service.read_blob_preview(
                    blob_id,
                    limit_bytes=limit,
                    session_operation_context=lease.context,
                )
            except BlobNotFoundError:
                raise HTTPException(status_code=404, detail="Blob content not found") from None
            except BlobStateError:
                raise HTTPException(status_code=409, detail="Blob is not in a previewable state") from None
            except (BlobIntegrityError, BlobContentMissingError):
                raise HTTPException(status_code=500, detail="Blob preview integrity verification failed") from None
            return Response(
                content=content,
                media_type=blob.mime_type,
                headers={
                    "X-Preview-Truncated": "true" if truncated else "false",
                    "X-Preview-Limit": str(limit),
                },
            )
        finally:
            await lease.close()

    @router.delete("/{blob_id}", status_code=204)
    async def delete_blob(
        session_id: UUID,
        blob_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    ) -> None:
        """Delete a blob and its backing file.

        Idempotent under the session's ARCHIVE fence: a blob that is already
        gone (including one whose deletion cleanup is still pending and is
        finished by this call) returns 204. A preliminary metadata read
        would make that cleanup-only retry unreachable, and the service
        already reads as the fence's session, so a foreign blob is a
        not-found here by construction.
        """
        blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
        session_service = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.ARCHIVE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            await blob_service.delete_blob(
                blob_id,
                session_operation_context=lease.context,
            )
        except BlobNotFoundError:
            return
        except (BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            await lease.close()

    return router
