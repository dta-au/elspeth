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
from elspeth.core.templates import MAX_JINJA_TEMPLATE_BYTES, template_size_refusal
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.prompt_editor import is_template_binding_path, prompt_bindings_for_blob, repin_prompt_bindings
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
    PromptFileReviewCardOwed,
    PromptFileReviewCardPending,
    PromptFileUnreadable,
    _current_node_options,
    _current_snapshot,
    assert_editor_save_may_carry_approvals,
    assert_latest_prompt_file_approvals_attest_surface,
    editor_save_settles_pending_card,
    record_prompt_file_approval,
)
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale, PromptReviewSnapshot
from elspeth.web.sessions.protocol import CompositionStateData, SessionCompositionStateCreation, SessionOperationMutationTransaction

_MAX_PROMPT_FILE_BYTES = 256 * 1024
_EDITOR_SAVE_ENCODINGS: frozenset[str] = frozenset({"utf-8"})

# One refusal code, ``prompt_file_stale``, for four states the user gets out of
# in different ways. A message promises only a recovery a test performs: an
# agent edit reopens a prompt review only when it changes the prompt surface,
# and no tool reopens a review on request. The card for a pending review is
# issued by the validate route's repair pass, which the web app runs when it
# loads the pipeline.
_STALE_CARD_OWED = (
    "This step's prompt is waiting for review. Reload the page so the step's review card appears, approve it, then edit the file."
)
_STALE_UNREADABLE = (
    "A prompt file on this step is missing, not ready, or no longer the version the step was set up with. "
    "Ask the assistant to attach that file to the step again, approve the review card that opens, then edit the file."
)
_STALE_NOT_APPROVED = (
    "This step's prompt is not in the form that was last approved, and there is no review card to approve. "
    "If you reverted to an earlier version of the pipeline, revert to the later one. "
    "Otherwise upload the file again, ask the assistant to attach it to this step, and approve the review card that opens."
)
_STALE_REVIEW_DRIFTED = (
    "This step's approved prompt review no longer matches its prompt, so the file cannot be saved here. "
    "An administrator needs to look at this session."
)


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
            if any(binding.encoding not in _EDITOR_SAVE_ENCODINGS for binding in bindings):
                # The editor saves UTF-8; a prompt that decodes this file another way would run
                # different text from what the user saved.
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_encoding_unsupported",
                    "This prompt reads the file with a non-UTF-8 encoding; re-attach it as UTF-8 text before editing it here.",
                )
            if is_template_binding_path(bindings[0].field_path) and len(new_bytes) > MAX_JINJA_TEMPLATE_BYTES:
                # A file at a template position is parsed as one template, so a
                # longer text would save and then fail validation at every turn.
                # A system prompt is plain text: only the file cap above applies.
                raise _prompt_save_refusal(
                    413,
                    "prompt_file_too_large",
                    template_size_refusal(
                        f"{bindings[0].field_path} on step {bindings[0].node_id}", len(new_bytes), MAX_JINJA_TEMPLATE_BYTES
                    ),
                )

            def attested_before_save() -> dict[str, PromptReviewSnapshot | None]:
                # One entry per node this file is bound on. A node may use other
                # prompt files beside this one: the editor shows only this file,
                # so the save is allowed only when it cannot approve anything the
                # user did not see. With a card pending, this file must be the
                # node's whole prompt surface (the save then settles the card).
                # With no card, the node's last approvals must still attest its
                # whole pre-save surface, and for a node with several files every
                # one of them must already be approved. Checked under the session
                # lease; the metadata hook below re-checks the head and, in its
                # own transaction, the attestation it carries forward.
                attested: dict[str, PromptReviewSnapshot | None] = {}
                with blob_service._engine.connect() as conn:
                    for binding in bindings:
                        if editor_save_settles_pending_card(conn, state=state, node_id=binding.node_id, field_path=binding.field_path):
                            attested[binding.node_id] = None
                        elif binding.node_id not in attested:
                            attested[binding.node_id] = assert_latest_prompt_file_approvals_attest_surface(
                                conn, state=state, node_id=binding.node_id
                            )
                return attested

            try:
                attested_snapshots = await run_sync_in_worker(attested_before_save)
            except PromptFileReviewCardPending as exc:
                if exc.only_inline_text:
                    raise _prompt_save_refusal(
                        409,
                        "prompt_file_review_pending_inline",
                        "Approve the review card first, then edit the file.",
                    ) from exc
                raise _prompt_save_refusal(
                    409,
                    "prompt_file_review_pending",
                    "Complete the existing node review before editing its files.",
                ) from exc
            except PromptFileUnreadable as exc:
                raise _prompt_save_refusal(409, "prompt_file_stale", _STALE_UNREADABLE) from exc
            except PromptFileReviewCardOwed as exc:
                raise _prompt_save_refusal(409, "prompt_file_stale", _STALE_CARD_OWED) from exc
            except PromptFileApprovalStale as exc:
                # A resolved review that drifted from its prompt is not reopened by
                # any edit to the step: the message offers no recovery.
                drifted = not all(resolved_prompt_review_matches_anchor(state, affected_node_id=binding.node_id) for binding in bindings)
                raise _prompt_save_refusal(409, "prompt_file_stale", _STALE_REVIEW_DRIFTED if drifted else _STALE_NOT_APPROVED) from exc
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
                # The binding set the request checked, exactly: the file is still
                # bound where the user opened it and nowhere it was not before.
                if current_mixed or current_bindings != bindings:
                    raise _prompt_save_refusal(
                        409, "prompt_file_binding_changed", "This prompt file moved since you opened it. Reload the session."
                    )
                # Checked on the exact pre-save head, before any write: a save
                # approves only the file on screen, never prompt text that
                # drifted from an earlier review.
                if not all(
                    resolved_prompt_review_matches_anchor(current_state, affected_node_id=node_id) for node_id in attested_snapshots
                ):
                    raise _prompt_save_refusal(409, "prompt_file_stale", _STALE_REVIEW_DRIFTED)
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
                for node_id, attested_snapshot in attested_snapshots.items():
                    binding = next(item for item in bindings if item.node_id == node_id)
                    approval_request = PromptFileApprovalRequest(
                        session_id=session_id,
                        node_id=node_id,
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
                            interpretation_events_table.c.affected_node_id == node_id,
                            interpretation_events_table.c.kind == "llm_prompt_template",
                            interpretation_events_table.c.choice == "pending",
                        )
                        .order_by(interpretation_events_table.c.created_at.desc())
                        .limit(1)
                    ).one_or_none()
                    # The save moved the node's review anchor, so EVERY file
                    # binding of the node needs an approval under the new
                    # snapshot or the node stops being admitted to runs. The
                    # saved file is approved by this save. The node's other
                    # files are carried forward with the bytes they already had
                    # approved, which the check below proves in this transaction
                    # before any row is written. A carried row is recorded with
                    # the same EDITOR_SAVE trigger as the saved file's row; its
                    # blob_id differs from the saved blob's row at the same
                    # approved_at.
                    saved_snapshot = _current_snapshot(conn, approval_request, _current_node_options(conn, approval_request))
                    assert_editor_save_may_carry_approvals(conn, prior=attested_snapshot, saved=saved_snapshot, saved_blob_id=blob_id)
                    # Each row is checked against this one snapshot, resolved
                    # above on this connection in this transaction, instead of
                    # re-reading every file of the node per row (N squared
                    # custody reads per save).
                    for file_binding in saved_snapshot.bindings:
                        record_prompt_file_approval(
                            conn,
                            PromptFileApprovalRequest(
                                session_id=session_id,
                                node_id=node_id,
                                field_path=file_binding.field_path,
                                blob_id=file_binding.blob_id,
                                content_sha256=file_binding.content_sha256,
                                actor_user_id=user.user_id,
                                trigger=PromptApprovalTrigger.EDITOR_SAVE,
                            ),
                            now=now,
                            resolved_snapshot=saved_snapshot,
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
                            affected_node_id=node_id,
                            user_term=pending_event.user_term,
                            accepted_value=accepted_value,
                            surfacing_structure_hash=_surfacing_prompt_structure_hash(latest_state, affected_node_id=node_id),
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
                            nodes = _refresh_resolved_prompt_template_review(latest_state, affected_node_id=node_id, file_snapshot=snapshot)
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
            except PromptFileReviewStale as exc:
                # PromptFileApprovalStale and its parent: an approval, binding or
                # custody snapshot moved between the checks above and the commit.
                raise _prompt_save_refusal(409, "prompt_file_stale", "Prompt bindings changed. Reload the session and retry.") from exc
            except (BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError) as exc:
                raise _prompt_save_refusal(409, "prompt_file_retained", str(exc)) from exc
            except BlobQuotaExceededError as exc:
                raise _prompt_save_refusal(413, "prompt_file_too_large", "This save exceeds the session storage limit.") from exc
            return SavePromptFileResponse(changed=True, content_sha256=replacement.content_hash)
        finally:
            await lease.close()

    return router
