"""Current bytes, current card, and shared approval evidence for prompt files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.interpretation_state import prompt_review_draft_from_options
from elspeth.web.sessions.models import (
    interpretation_events_table,
    prompt_file_approvals_table,
    prompt_review_snapshots_table,
    sessions_table,
)
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    assert_prompt_file_approvals,
    record_prompt_file_approval,
)
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale, verify_prompt_review_snapshot_payload
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env


async def _pending_file_review(env: dict[str, Any], *, file_only: bool = False) -> tuple[SessionServiceImpl, UUID, UUID, UUID]:
    blob = blob_tools._create_blob(env, content="  First line\nSecond line  \n")
    assert blob.success
    blob_id = UUID(blob.data["blob_id"])
    session_id = UUID(env["session_id"])

    def read_blob(_context: Any, requested: UUID) -> tuple[Any, bytes]:
        record = env["authority"].mutate(env["operation"], lambda tx: tx.blobs.read_blob(blob_id=requested))
        return record, Path(record.storage_path).read_bytes()

    service = SessionServiceImpl(
        env["engine"],
        data_dir=Path(env["data_dir"]),
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("prompt-file-test"),
        inline_blob_read=read_blob,
    )

    async def same_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        # The shared in-memory SQLite fixture is scoped to its owning thread.
        return func(*args, **kwargs)

    service._run_sync = same_thread
    base = blob_tools._inline_ref_state().to_dict()
    node = base["nodes"][0]
    record, _bytes = read_blob(env["operation"], blob_id)
    marker = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": record.content_hash}
    options = dict(node["options"])
    options["prompt_template"] = marker
    if file_only:
        del options["system_prompt"]
    node["options"] = _options_with_default_prompt_template_review(node_id="classify", plugin="llm", options=options)
    saved = await service.save_composition_state(
        session_id,
        CompositionStateData(
            sources=base["sources"],
            nodes=base["nodes"],
            edges=base["edges"],
            outputs=base["outputs"],
            metadata_=base["metadata"],
            is_valid=True,
        ),
        provenance="tool_call",
        session_operation_context=env["operation"],
    )
    draft = prompt_review_draft_from_options(node["options"])
    assert draft is not None
    event = await service.create_pending_interpretation_event(
        session_id=session_id,
        composition_state_id=saved.id,
        affected_node_id="classify",
        tool_call_id="prompt-file-test",
        user_term="llm_prompt_template:classify",
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        llm_draft=draft,
        model_identifier="test-model",
        model_version="1",
        provider="test-provider",
        composer_skill_hash="a" * 64,
        session_operation_context=env["operation"],
    )
    return service, session_id, event.id, blob_id


@pytest.mark.asyncio
async def test_card_snapshot_and_shared_approval_match_current_bytes(blob_env: dict[str, Any]) -> None:
    service, session_id, event_id, blob_id = await _pending_file_review(blob_env)
    payload = await service.get_prompt_review_snapshot(session_id, event_id)
    assert payload is not None
    assert payload["bindings"][0]["text"] == "  First line\nSecond line  \n"
    assert payload["is_live_review"] is True
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    with blob_env["engine"].begin() as conn:
        stored = conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
        snapshot = verify_prompt_review_snapshot_payload(stored.snapshot_json)
        with pytest.raises(PromptFileReviewStale):
            assert_prompt_file_approvals(conn, snapshot)
        approval = record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.CARD,
            ),
            now=datetime.now(UTC),
        )
        assert approval.event_id == event_id
        assert conn.execute(prompt_file_approvals_table.select()).one().content_sha256 == record.content_hash


@pytest.mark.asyncio
async def test_prompt_file_approval_update_refused(blob_env: dict[str, Any]) -> None:
    _service, session_id, _event_id, blob_id = await _pending_file_review(blob_env)
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    with blob_env["engine"].begin() as conn:
        approval = record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.CARD,
            ),
            now=datetime.now(UTC),
        )
    with pytest.raises(IntegrityError, match="prompt_file_approvals is append-only"), blob_env["engine"].begin() as conn:
        conn.execute(
            update(prompt_file_approvals_table).where(prompt_file_approvals_table.c.id == str(approval.id)).values(content_sha256="b" * 64)
        )


@pytest.mark.asyncio
async def test_prompt_review_snapshot_update_refused_and_session_delete_cascades(blob_env: dict[str, Any]) -> None:
    _service, session_id, event_id, _blob_id = await _pending_file_review(blob_env)
    with pytest.raises(IntegrityError, match="prompt_review_snapshots is append-only"), blob_env["engine"].begin() as conn:
        conn.execute(
            update(prompt_review_snapshots_table).where(prompt_review_snapshots_table.c.event_id == str(event_id)).values(anchor="b" * 64)
        )
    with blob_env["engine"].connect() as conn:
        assert conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
    with blob_env["engine"].begin() as conn:
        assert conn.execute(delete(sessions_table).where(sessions_table.c.id == str(session_id))).rowcount == 1
    with blob_env["engine"].connect() as conn:
        assert (
            conn.execute(
                prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))
            ).one_or_none()
            is None
        )


@pytest.mark.asyncio
async def test_card_approval_refuses_changed_bytes(blob_env: dict[str, Any]) -> None:
    _service, session_id, _event_id, blob_id = await _pending_file_review(blob_env)
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    Path(record.storage_path).write_bytes(b"Changed before approval")
    with blob_env["engine"].begin() as conn, pytest.raises(PromptFileApprovalStale):
        record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.CARD,
            ),
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_request_hash_is_not_authority(blob_env: dict[str, Any]) -> None:
    _service, session_id, _event_id, blob_id = await _pending_file_review(blob_env)
    with blob_env["engine"].begin() as conn, pytest.raises(PromptFileApprovalStale):
        record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256="0" * 64,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.CARD,
            ),
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_card_resolution_approves_file_and_admission_accepts_it(blob_env: dict[str, Any]) -> None:
    service, session_id, event_id, _blob_id = await _pending_file_review(blob_env)
    resolved, _state = await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event_id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        session_operation_context=blob_env["operation"],
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    with blob_env["engine"].connect() as conn:
        stored = conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
        snapshot = verify_prompt_review_snapshot_payload(stored.snapshot_json)
        assert_prompt_file_approvals(conn, snapshot)


@pytest.mark.asyncio
async def test_editor_save_uses_same_approval_evidence(blob_env: dict[str, Any]) -> None:
    service, session_id, event_id, blob_id = await _pending_file_review(blob_env)
    await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event_id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        session_operation_context=blob_env["operation"],
    )
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    with blob_env["engine"].begin() as conn:
        editor = record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.EDITOR_SAVE,
            ),
            now=datetime.now(UTC),
        )
        assert editor.event_id is None
        assert editor.trigger is PromptApprovalTrigger.EDITOR_SAVE
        stored = conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
        assert_prompt_file_approvals(conn, verify_prompt_review_snapshot_payload(stored.snapshot_json))


@pytest.mark.asyncio
async def test_editor_save_satisfies_pending_file_card(blob_env: dict[str, Any]) -> None:
    """A file-only node: the saved file is the whole pending surface, so the save retires the card."""
    _service, session_id, event_id, blob_id = await _pending_file_review(blob_env, file_only=True)
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    with blob_env["engine"].begin() as conn:
        approval = record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.EDITOR_SAVE,
            ),
            now=datetime.now(UTC),
        )
        assert approval.event_id is None
        choice = conn.execute(
            select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(event_id))
        ).scalar_one()
        assert choice == "superseded"
        stored = conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
        assert_prompt_file_approvals(conn, verify_prompt_review_snapshot_payload(stored.snapshot_json))


@pytest.mark.asyncio
async def test_editor_save_refuses_pending_card_over_unseen_inline_text(blob_env: dict[str, Any]) -> None:
    """The writer itself refuses, whatever its caller checked: the card also covers an inline system prompt."""
    _service, session_id, event_id, blob_id = await _pending_file_review(blob_env)
    record = blob_env["authority"].mutate(blob_env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    with pytest.raises(PromptFileApprovalStale), blob_env["engine"].begin() as conn:
        record_prompt_file_approval(
            conn,
            PromptFileApprovalRequest(
                session_id=session_id,
                node_id="classify",
                field_path="prompt_template",
                blob_id=blob_id,
                content_sha256=record.content_hash,
                actor_user_id="test-user",
                trigger=PromptApprovalTrigger.EDITOR_SAVE,
            ),
            now=datetime.now(UTC),
        )
    with blob_env["engine"].connect() as conn:
        assert (
            conn.execute(select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(event_id))).scalar_one()
            == "pending"
        )
        assert (
            conn.execute(select(prompt_file_approvals_table.c.id).where(prompt_file_approvals_table.c.session_id == str(session_id))).all()
            == []
        )


@pytest.mark.asyncio
async def test_old_event_without_optional_snapshot_still_loads(blob_env: dict[str, Any]) -> None:
    service, session_id, event_id, _blob_id = await _pending_file_review(blob_env)
    with blob_env["engine"].begin() as conn:
        conn.execute(prompt_review_snapshots_table.delete().where(prompt_review_snapshots_table.c.event_id == str(event_id)))
    assert await service.get_prompt_review_snapshot(session_id, event_id) is None
    assert [event.id for event in await service.list_interpretation_events(session_id)] == [event_id]
    assert await service.get_prompt_review_snapshot(UUID(int=1), event_id) is None
