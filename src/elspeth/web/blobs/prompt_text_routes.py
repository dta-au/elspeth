"""Prompt text editor route with owner-scoped blob lease and atomic approval."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, desc, select

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.prompt_editor import prompt_bindings_for_blob, repin_prompt_bindings
from elspeth.web.blobs.protocol import (
    ALLOWED_MIME_TYPES,
    BlobActiveRunError,
    BlobInProgressForkError,
    BlobPendingProposalError,
    BlobQuotaExceededError,
)
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.routes import _get_owned_blob, _verify_session_and_get_blob_service
from elspeth.web.blobs.service import content_hash
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.interpretation_state import parse_interpretation_requirements, prompt_review_draft_from_options
from elspeth.web.sessions.models import composition_states_table, interpretation_events_table
from elspeth.web.sessions.pending_interpretation import (
    _refresh_resolved_prompt_template_review,
    _resolve_prompt_template_review,
    _surfacing_prompt_structure_hash,
    resolved_prompt_review_matches_anchor,
)
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    _current_node_options,
    _current_snapshot,
    record_prompt_file_approval,
)
from elspeth.web.sessions.prompt_file_review import _surface_slots, prompt_file_binding_paths
from elspeth.web.sessions.protocol import CompositionStateData, SessionCompositionStateCreation, SessionOperationMutationTransaction

_MAX_PROMPT_FILE_BYTES = 256 * 1024
_EDITOR_SAVE_ENCODINGS: frozenset[str] = frozenset({"utf-8"})


class SavePromptFileRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    content: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str
    field_path: str


class SavePromptFileResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changed: bool
    content_sha256: str


def _prompt_save_refusal(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message})


def create_prompt_text_router() -> APIRouter:
    router = APIRouter(prefix="/api/sessions/{session_id}/blobs", tags=["blobs"])

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
            if (bindings[0].node_id, bindings[0].field_path) != (body.node_id, body.field_path):
                raise _prompt_save_refusal(
                    409, "prompt_file_binding_changed", "This prompt file moved since you opened it. Reload the session."
                )
            node = next(node for node in state.nodes or () if node["id"] == bindings[0].node_id)
            if len(prompt_file_binding_paths(node["options"])) > 1:
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_node_multi_binding",
                    "This node uses more than one prompt file; re-point it through the agent and approve the review card.",
                )
            if any(binding.encoding not in _EDITOR_SAVE_ENCODINGS for binding in bindings):
                # The editor saves UTF-8; a prompt that decodes this file another way would run
                # different text from what the user saved.
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_encoding_unsupported",
                    "This prompt reads the file with a non-UTF-8 encoding; re-attach it as UTF-8 text before editing it here.",
                )
            with blob_service._engine.connect() as conn:
                pending_card = conn.execute(
                    select(interpretation_events_table.c.id)
                    .where(
                        interpretation_events_table.c.session_id == str(session_id),
                        interpretation_events_table.c.affected_node_id == body.node_id,
                        interpretation_events_table.c.kind == "llm_prompt_template",
                        interpretation_events_table.c.choice == "pending",
                    )
                    .limit(1)
                ).scalar_one_or_none()
            if pending_card is not None and any(
                path != body.field_path and type(value) is str for path, value in _surface_slots(node["options"])
            ):
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_review_pending_inline",
                    "Approve the review card first, then edit the file.",
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
                current_row = conn.execute(select(composition_states_table).where(composition_states_table.c.id == current_head)).one()
                current_state = session_service._row_to_state_record(current_row)
                current_bindings, current_mixed = prompt_bindings_for_blob(current_state, replacement)
                if (
                    current_mixed
                    or len(current_bindings) != 1
                    or (current_bindings[0].node_id, current_bindings[0].field_path) != (body.node_id, body.field_path)
                ):
                    raise _prompt_save_refusal(
                        409, "prompt_file_binding_changed", "This prompt file moved since you opened it. Reload the session."
                    )
                # Checked on the exact pre-save head, before any write: a save
                # approves only the file on screen, never prompt text that
                # drifted from an earlier review.
                if not resolved_prompt_review_matches_anchor(current_state, affected_node_id=body.node_id):
                    raise _prompt_save_refusal(
                        409,
                        "prompt_file_stale",
                        "This prompt changed since it was reviewed. Ask the assistant to reopen its review, then edit the file.",
                    )
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
                    approval_request = PromptFileApprovalRequest(
                        session_id=session_id,
                        node_id=binding.node_id,
                        field_path=binding.field_path,
                        blob_id=blob_id,
                        content_sha256=stored_hash,
                        actor_user_id=user.user_id,
                        trigger=PromptApprovalTrigger.EDITOR_SAVE,
                    )
                    pending_event = conn.execute(
                        select(interpretation_events_table.c.id, interpretation_events_table.c.user_term)
                        .where(
                            interpretation_events_table.c.session_id == str(session_id),
                            interpretation_events_table.c.affected_node_id == binding.node_id,
                            interpretation_events_table.c.kind == "llm_prompt_template",
                            interpretation_events_table.c.choice == "pending",
                        )
                        .order_by(interpretation_events_table.c.created_at.desc())
                        .limit(1)
                    ).one_or_none()
                    record_prompt_file_approval(
                        conn,
                        approval_request,
                        now=now,
                    )
                    if pending_event is not None:
                        latest_row = conn.execute(
                            select(composition_states_table)
                            .where(composition_states_table.c.session_id == str(session_id))
                            .order_by(desc(composition_states_table.c.version))
                            .limit(1)
                        ).one()
                        latest_state = session_service._row_to_state_record(latest_row)
                        options = _current_node_options(conn, approval_request)
                        choice = conn.execute(
                            select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == pending_event.id)
                        ).scalar_one()
                        if choice == "pending":
                            raise PromptFileApprovalStale()
                        snapshot = _current_snapshot(conn, approval_request, options)
                        accepted_value = prompt_review_draft_from_options(options)
                        if accepted_value is None:
                            raise PromptFileApprovalStale()
                        sources, nodes, _ = _resolve_prompt_template_review(
                            latest_state,
                            event_id=pending_event.id,
                            affected_node_id=binding.node_id,
                            user_term=pending_event.user_term,
                            accepted_value=accepted_value,
                            surfacing_structure_hash=_surfacing_prompt_structure_hash(latest_state, affected_node_id=binding.node_id),
                            file_snapshot=snapshot,
                        )
                        transaction.composition_states.append_state(
                            SessionCompositionStateCreation(
                                id=uuid4(),
                                data=CompositionStateData(
                                    sources=sources,
                                    nodes=nodes,
                                    edges=latest_state.edges,
                                    outputs=latest_state.outputs,
                                    metadata_=latest_state.metadata_,
                                    is_valid=False,
                                    composer_meta=latest_state.composer_meta,
                                ),
                                provenance="interpretation_resolve",
                                created_at=now,
                                derived_from_state_id=latest_state.id,
                            )
                        )
                    else:
                        latest_row = conn.execute(
                            select(composition_states_table)
                            .where(composition_states_table.c.session_id == str(session_id))
                            .order_by(desc(composition_states_table.c.version))
                            .limit(1)
                        ).one()
                        latest_state = session_service._row_to_state_record(latest_row)
                        options = _current_node_options(conn, approval_request)
                        snapshot = _current_snapshot(conn, approval_request, options)
                        resolved = any(
                            item["kind"] == "llm_prompt_template" and item["status"] == "resolved"
                            for item in parse_interpretation_requirements(options) or ()
                        )
                        if resolved:
                            nodes = _refresh_resolved_prompt_template_review(
                                latest_state, affected_node_id=binding.node_id, file_snapshot=snapshot
                            )
                            transaction.composition_states.append_state(
                                SessionCompositionStateCreation(
                                    id=uuid4(),
                                    data=CompositionStateData(
                                        sources=latest_state.sources,
                                        nodes=nodes,
                                        edges=latest_state.edges,
                                        outputs=latest_state.outputs,
                                        metadata_=latest_state.metadata_,
                                        is_valid=False,
                                        composer_meta=latest_state.composer_meta,
                                    ),
                                    provenance="interpretation_resolve",
                                    created_at=now,
                                    derived_from_state_id=latest_state.id,
                                )
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

    return router
