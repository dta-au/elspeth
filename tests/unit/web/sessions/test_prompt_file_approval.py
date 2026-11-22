"""Current bytes, current card, and shared approval evidence for prompt files."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy import Connection, delete, literal_column, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.interpretation_state import prompt_review_draft_from_options
from elspeth.web.sessions import prompt_file_approval as approval_module
from elspeth.web.sessions.models import (
    blobs_table,
    interpretation_events_table,
    prompt_file_approvals_table,
    prompt_review_snapshots_table,
    sessions_table,
)
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRecord,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    assert_prompt_file_approvals,
    record_prompt_file_approval,
    record_prompt_file_card_approvals,
)
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale, verify_prompt_review_snapshot_payload
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.prompt_review import reviewed_anchor_for
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env


async def _pending_file_review(
    env: dict[str, Any],
    *,
    file_only: bool = False,
    pending_vague_term: bool = False,
    structured_system_file: bool = False,
    sibling_file: bool = False,
    query_files: int = 0,
) -> tuple[SessionServiceImpl, UUID, UUID, UUID]:
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
    if structured_system_file:
        # The file backs the system prompt; the user prompt is structured parts.
        options["system_prompt"] = marker
        options["prompt_template"] = "Label each row by a coarse category please."
        options["prompt_template_parts"] = [
            {"kind": "text", "text": "Label each row by "},
            {"kind": "interpretation_ref", "requirement_id": "category_scheme"},
            {"kind": "text", "text": " please."},
        ]
        options["interpretation_requirements"] = [
            {
                "id": "category_scheme",
                "kind": "vague_term",
                "user_term": "category scheme",
                "status": "pending",
                "draft": "a coarse category",
                "event_id": None,
                "accepted_value": None,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ]
    else:
        options["prompt_template"] = marker
    if file_only:
        del options["system_prompt"]
    if sibling_file:
        # A second prompt file on the same node, backing the system prompt.
        sibling = blob_tools._create_blob(env, filename="system.txt", content="You classify rows.\n")
        assert sibling.success
        sibling_record, _sibling_bytes = read_blob(env["operation"], UUID(sibling.data["blob_id"]))
        options["system_prompt"] = {"blob_ref": str(sibling_record.id), "mode": "inline_content", "sha256": sibling_record.content_hash}
    if query_files:
        # Further prompt files on the same node, one per query template.
        queries: dict[str, Any] = {}
        for index in range(query_files):
            query = blob_tools._create_blob(env, filename=f"query_{index}.txt", content=f"Score row field {index}.\n")
            assert query.success
            query_record, _query_bytes = read_blob(env["operation"], UUID(query.data["blob_id"]))
            queries[f"q{index}"] = {
                "template": {"blob_ref": str(query_record.id), "mode": "inline_content", "sha256": query_record.content_hash}
            }
        options["queries"] = queries
    node["options"] = _options_with_default_prompt_template_review(node_id="classify", plugin="llm", options=options)
    if pending_vague_term:
        node["options"]["interpretation_requirements"] = [
            *node["options"]["interpretation_requirements"],
            {
                "id": "coolness",
                "kind": "vague_term",
                "user_term": "coolness",
                "status": "pending",
                "draft": "well-designed and useful",
                "event_id": "event-1",
                "accepted_value": None,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            },
        ]
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
async def test_file_card_snapshot_carries_parts_skeleton_without_values(blob_env: dict[str, Any]) -> None:
    service, session_id, event_id, _blob_id = await _pending_file_review(blob_env, structured_system_file=True)
    payload = await service.get_prompt_review_snapshot(session_id, event_id)
    assert payload is not None
    assert [binding["field_path"] for binding in payload["bindings"]] == ["system_prompt"]
    surface = payload["authored_surface"]
    assert surface["prompt_template_parts"] == [
        {"kind": "text", "text": "Label each row by "},
        {"kind": "interpretation_ref", "requirement_id": "category_scheme"},
        {"kind": "text", "text": " please."},
    ]
    assert surface["interpretation_requirements"] == [{"id": "category_scheme", "event_id": None, "user_term": "category scheme"}]
    assert "a coarse category" not in repr(surface["prompt_template_parts"]) + repr(surface["interpretation_requirements"])
    resolved, _state = await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event_id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        reviewed_anchor=payload["anchor"],
        session_operation_context=blob_env["operation"],
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED


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
        reviewed_anchor=await reviewed_anchor_for(service, session_id, event_id),
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
        reviewed_anchor=await reviewed_anchor_for(service, session_id, event_id),
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
async def test_editor_save_refuses_pending_card_with_pending_vague_term(blob_env: dict[str, Any]) -> None:
    """A file-only node whose card also waits on a vague term: the writer refuses, so the term stays unreviewed."""
    _service, session_id, event_id, blob_id = await _pending_file_review(blob_env, file_only=True, pending_vague_term=True)
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
async def test_editor_save_refuses_pending_card_over_unseen_sibling_file(blob_env: dict[str, Any]) -> None:
    """The writer itself refuses: the pending card also covers a second prompt file the editor did not show."""
    _service, session_id, event_id, blob_id = await _pending_file_review(blob_env, sibling_file=True)
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


def _count_approval_reads(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Count, through the approval writer's own seams, snapshot resolutions and custody byte reads."""
    counts: Counter[str] = Counter()
    real_read = approval_module._read_custody_bytes
    real_resolve = approval_module.resolve_prompt_review_snapshot

    def counting_read(record: Any) -> bytes:
        counts["custody_reads"] += 1
        return real_read(record)

    def counting_resolve(**kwargs: Any) -> Any:
        counts["snapshots"] += 1
        return real_resolve(**kwargs)

    monkeypatch.setattr(approval_module, "_read_custody_bytes", counting_read)
    monkeypatch.setattr(approval_module, "resolve_prompt_review_snapshot", counting_resolve)
    return counts


@pytest.mark.asyncio
async def test_card_approval_resolves_the_snapshot_once_for_all_its_files(
    blob_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving one card with N file-held prompts reads each file once, not once per binding (N squared).

    The per-binding writer re-resolved the whole snapshot for every binding
    while the session lock was held; at 252 bindings that was 63,504 custody
    reads and a lock hold long enough to lose the operation's own lease.
    """
    service, session_id, event_id, _blob_id = await _pending_file_review(blob_env, sibling_file=True, query_files=10)
    anchor = await reviewed_anchor_for(service, session_id, event_id)
    counts = _count_approval_reads(monkeypatch)
    await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event_id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        reviewed_anchor=anchor,
        session_operation_context=blob_env["operation"],
    )
    with blob_env["engine"].connect() as conn:
        approvals = conn.execute(select(prompt_file_approvals_table.c.field_path)).scalars().all()
    bindings = 12  # prompt_template, system_prompt and ten query templates, each its own file
    assert len(approvals) == bindings
    assert dict(counts) == {"snapshots": 1, "custody_reads": bindings}


CardWriter = Callable[..., Sequence[PromptFileApprovalRecord]]


def _per_binding_card_loop(
    conn: Connection,
    requests: Sequence[PromptFileApprovalRequest],
    *,
    now: datetime,
    expected_event_id: UUID,
) -> list[PromptFileApprovalRecord]:
    """The card loop exactly as ``resolve_interpretation_event`` ran it before the one-pass writer.

    Reconstructed from service.py at 16ac68644: one ``record_prompt_file_approval``
    per binding, then the caller's event check on the returned row.
    """
    records: list[PromptFileApprovalRecord] = []
    for request in requests:
        approval = record_prompt_file_approval(conn, request, now=now)
        records.append(approval)
        if approval.event_id != expected_event_id:
            raise PromptFileApprovalStale()
    return records


@dataclass(frozen=True)
class _CardOutcome:
    returned: tuple[PromptFileApprovalRecord, ...] | None
    raised: type[BaseException] | None
    raised_from: type[BaseException] | None
    rows_in_transaction: tuple[tuple[Any, ...], ...]


def _run_card_writer(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    writer: CardWriter,
    requests: Sequence[PromptFileApprovalRequest],
    *,
    now: datetime,
    expected_event_id: UUID,
) -> _CardOutcome:
    """Run one writer in a transaction that is rolled back, recording what it wrote and raised.

    Approval ids come from the same deterministic sequence for every run, so
    two writers that insert the same rows in the same order produce equal rows.
    """
    ids: Iterator[UUID] = iter(UUID(int=index) for index in range(1, 100_000))
    monkeypatch.setattr(approval_module, "uuid4", lambda: next(ids))
    returned: tuple[PromptFileApprovalRecord, ...] | None = None
    raised: type[BaseException] | None = None
    raised_from: type[BaseException] | None = None
    with env["engine"].connect() as conn:
        transaction = conn.begin()
        try:
            try:
                returned = tuple(writer(conn, requests, now=now, expected_event_id=expected_event_id))
            except (PromptFileReviewStale, TypeError, KeyError, ValueError) as exc:
                raised = type(exc)
                raised_from = type(exc.__cause__) if exc.__cause__ is not None else None
            rows = tuple(
                tuple(row)
                for row in conn.execute(select(prompt_file_approvals_table).order_by(literal_column("rowid"))).all()  # insertion order
            )
        finally:
            transaction.rollback()
    return _CardOutcome(returned=returned, raised=raised, raised_from=raised_from, rows_in_transaction=rows)


def _card_requests(env: dict[str, Any], session_id: UUID, event_id: UUID) -> list[PromptFileApprovalRequest]:
    """The requests the resolve route builds: one per binding of the card's snapshot, in snapshot order."""
    with env["engine"].connect() as conn:
        stored = conn.execute(prompt_review_snapshots_table.select().where(prompt_review_snapshots_table.c.event_id == str(event_id))).one()
    return [
        PromptFileApprovalRequest(
            session_id=session_id,
            node_id="classify",
            field_path=binding.field_path,
            blob_id=binding.blob_id,
            content_sha256=binding.content_sha256,
            actor_user_id="test-user",
            trigger=PromptApprovalTrigger.CARD,
        )
        for binding in verify_prompt_review_snapshot_payload(stored.snapshot_json).bindings
    ]


def _set_blob_status(env: dict[str, Any], blob_id: UUID, status: str) -> None:
    with env["engine"].begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == str(blob_id)).values(status=status))


def _overwrite_blob_bytes(env: dict[str, Any], blob_id: UUID) -> None:
    record = env["authority"].mutate(env["operation"], lambda tx: tx.blobs.read_blob(blob_id=blob_id))
    Path(record.storage_path).write_bytes(b"Changed after the card was shown")


# Each case: (fixture size, how the case is broken before both writers run,
# how the request list is changed, whether the caller expects another event,
# the refusal both writers must share as (error type, the error it was raised
# from), and how many rows each had inserted in the transaction when it
# stopped). Sizes: "one" is a single file-held prompt; "several" is
# prompt_template + system_prompt + four query files = 6 bindings. The two
# not-ready cases differ only in which file is not ready: the first file is
# refused by its own row check (no cause), a later one by the snapshot that
# the first binding resolves (raised from the review error), which shows the
# per-binding writer's check order is kept.
_STALE = (PromptFileApprovalStale, None)
_EQUALITY_CASES = [
    pytest.param("one", None, None, False, None, 1, id="one-binding"),
    pytest.param("several", None, None, False, None, 6, id="several-bindings"),
    pytest.param(
        "several",
        None,
        lambda requests: [*requests[:3], replace(requests[3], content_sha256="0" * 64), *requests[4:]],
        False,
        _STALE,
        3,
        id="mid-card-request-hash-refused",
    ),
    pytest.param(
        "several",
        None,
        lambda requests: [
            *requests[:3],
            replace(requests[3], blob_id=requests[4].blob_id, content_sha256=requests[4].content_sha256),
            *requests[4:],
        ],
        False,
        _STALE,
        3,
        id="mid-card-marker-mismatch-refused",
    ),
    pytest.param(
        "several",
        None,
        lambda requests: [*requests[:-1], replace(requests[-1], field_path="queries.missing.template")],
        False,
        _STALE,
        5,
        id="last-binding-not-on-the-node-refused",
    ),
    pytest.param("several", None, None, True, _STALE, 1, id="different-card-event-refused"),
    pytest.param(
        "several",
        None,
        lambda requests: [replace(request, actor_user_id="someone-else") for request in requests],
        False,
        _STALE,
        0,
        id="actor-not-owner-refused",
    ),
    pytest.param(
        "several",
        lambda env, requests: _set_blob_status(env, requests[0].blob_id, "pending"),
        None,
        False,
        _STALE,
        0,
        id="first-file-not-ready-refused-by-its-row-check",
    ),
    pytest.param(
        "several",
        lambda env, requests: _set_blob_status(env, requests[3].blob_id, "pending"),
        None,
        False,
        (PromptFileApprovalStale, PromptFileReviewStale),
        0,
        id="later-file-not-ready-refused-by-the-snapshot",
    ),
    pytest.param(
        "several",
        lambda env, requests: _overwrite_blob_bytes(env, requests[3].blob_id),
        None,
        False,
        (PromptFileApprovalStale, PromptFileApprovalStale),
        0,
        id="later-file-bytes-changed-refused",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "break_state", "change_requests", "other_event", "refusal", "rows_before_refusal"), _EQUALITY_CASES)
async def test_one_pass_card_writer_writes_and_refuses_exactly_as_the_per_binding_loop(
    blob_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    size: str,
    break_state: Callable[[dict[str, Any], list[PromptFileApprovalRequest]], None] | None,
    change_requests: Callable[[list[PromptFileApprovalRequest]], list[PromptFileApprovalRequest]] | None,
    other_event: bool,
    refusal: tuple[type[BaseException], type[BaseException] | None] | None,
    rows_before_refusal: int,
) -> None:
    """Same rows (every column, insertion order), same records, same refusal type, nothing committed on refusal."""
    if size == "one":
        _service, session_id, event_id, _blob_id = await _pending_file_review(blob_env)
    else:
        _service, session_id, event_id, _blob_id = await _pending_file_review(blob_env, sibling_file=True, query_files=4)
    requests = _card_requests(blob_env, session_id, event_id)
    assert len(requests) == (1 if size == "one" else 6)
    if break_state is not None:
        break_state(blob_env, requests)
    if change_requests is not None:
        requests = change_requests(requests)
    expected_event_id = UUID(int=99) if other_event else event_id
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    old = _run_card_writer(blob_env, monkeypatch, _per_binding_card_loop, requests, now=now, expected_event_id=expected_event_id)
    new = _run_card_writer(blob_env, monkeypatch, record_prompt_file_card_approvals, requests, now=now, expected_event_id=expected_event_id)

    assert new == old
    assert (None if old.raised is None else (old.raised, old.raised_from)) == refusal
    assert len(old.rows_in_transaction) == rows_before_refusal
    if refusal is None:
        assert old.returned is not None
        assert [record.field_path for record in old.returned] == [request.field_path for request in requests]
        assert {record.event_id for record in old.returned} == {event_id}
    # Both runs were rolled back: the caller's transaction is what keeps a refused card from writing anything.
    with blob_env["engine"].connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table.c.id)).all() == []


@pytest.mark.asyncio
async def test_a_card_refused_at_its_last_file_writes_no_approval_and_stays_pending(
    blob_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the resolve route's service call: a refusal at the last binding leaves the whole card unapproved."""
    service, session_id, event_id, _blob_id = await _pending_file_review(blob_env, sibling_file=True, query_files=4)
    anchor = await reviewed_anchor_for(service, session_id, event_id)
    last_path = _card_requests(blob_env, session_id, event_id)[-1].field_path
    real_marker = approval_module._binding_marker

    def refuse_last_binding(options: Any, field_path: str) -> Any:
        if field_path == last_path:
            raise PromptFileApprovalStale()
        return real_marker(options, field_path)

    monkeypatch.setattr(approval_module, "_binding_marker", refuse_last_binding)
    with pytest.raises(PromptFileApprovalStale):
        await service.resolve_interpretation_event(
            session_id=session_id,
            event_id=event_id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:test-user",
            reviewed_anchor=anchor,
            session_operation_context=blob_env["operation"],
        )
    with blob_env["engine"].connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table.c.id)).all() == []
        assert (
            conn.execute(select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(event_id))).scalar_one()
            == "pending"
        )


@pytest.mark.asyncio
async def test_card_writer_refuses_requests_that_are_not_one_card(blob_env: dict[str, Any]) -> None:
    """The once-per-card checks are sound only for one session, node and actor under the CARD trigger."""
    _service, session_id, event_id, _blob_id = await _pending_file_review(blob_env, sibling_file=True)
    requests = _card_requests(blob_env, session_id, event_id)
    now = datetime.now(UTC)
    for mixed in (
        [requests[0], replace(requests[1], node_id="other")],
        [requests[0], replace(requests[1], actor_user_id="someone-else")],
        [requests[0], replace(requests[1], session_id=UUID(int=7))],
        [requests[0], replace(requests[1], trigger=PromptApprovalTrigger.EDITOR_SAVE)],
        [replace(request, trigger=PromptApprovalTrigger.EDITOR_SAVE) for request in requests],
    ):
        with blob_env["engine"].connect() as conn, pytest.raises(TypeError, match="share one session, node and actor"):
            record_prompt_file_card_approvals(conn, mixed, now=now, expected_event_id=event_id)
    not_a_datetime: Any = "now"
    with blob_env["engine"].connect() as conn:
        assert record_prompt_file_card_approvals(conn, [], now=now, expected_event_id=event_id) == ()
        with pytest.raises(TypeError, match="owned request and datetime"):
            record_prompt_file_card_approvals(conn, requests, now=not_a_datetime, expected_event_id=event_id)
