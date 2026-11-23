"""One editor save reads each of the node's prompt files a bounded number of times.

The save writes one approval row per file binding of the node, under the
session lock and inside the metadata-commit transaction. Re-resolving the whole
prompt-file snapshot for every one of those rows made one save read N files N
times (N squared custody reads). The read count must grow linearly in N.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.tools._common import _options_with_default_model_choice_review, _options_with_default_prompt_template_review
from elspeth.web.sessions import prompt_file_approval as approval_module
from elspeth.web.sessions.models import prompt_file_approvals_table
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    _blob_record,
    _current_node_options,
    _current_snapshot,
    _read_custody_bytes,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_prompt_editor import _admit, _approve_card, _raise_prompt_card, _save
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob
from tests.unit.web.composer import test_blob_inline_tools as blob_tools


def _marker(blob: dict[str, Any]) -> dict[str, str]:
    return {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}


def _approved_n_file_node(tmp_path: Path, files: int) -> tuple[Any, Any, Any, str, list[dict[str, Any]]]:
    """Node "first" with ``files`` query templates, each its own prompt file, and its prompt card approved."""
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blobs = [_upload_blob(client, session_id, f"file {index} before".encode(), f"q{index}.txt", "text/plain") for index in range(files)]
    engine = blob_service._engine

    def read_blob(_context: Any, blob_id: UUID) -> tuple[Any, bytes]:
        with engine.connect() as conn:
            record = _blob_record(conn, UUID(session_id), blob_id)
        return record, _read_custody_bytes(record)

    service._inline_blob_read = read_blob
    state = blob_tools._inline_ref_state().to_dict()
    node = state["nodes"][0]
    node["id"] = "first"
    options = dict(node["options"])
    del options["system_prompt"]
    del options["prompt_template"]
    options["queries"] = {f"q{index}": {"input_fields": {}, "template": _marker(blob)} for index, blob in enumerate(blobs)}
    options = _options_with_default_model_choice_review(node_id="first", plugin="llm", options=options)
    node["options"] = _options_with_default_prompt_template_review(node_id="first", plugin="llm", options=options)
    with fenced_operation_context(engine, session_id) as operation:
        saved = asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=state["sources"],
                    nodes=state["nodes"],
                    edges=state["edges"],
                    outputs=state["outputs"],
                    metadata_=state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
        model_event = asyncio.run(
            service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=saved.id,
                affected_node_id="first",
                tool_call_id="model-first",
                user_term="llm_model_choice:first",
                kind=InterpretationKind.LLM_MODEL_CHOICE,
                llm_draft=node["options"]["model"],
                model_identifier="test-model",
                model_version="1",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        )
        asyncio.run(
            service.resolve_interpretation_event(
                session_id=UUID(session_id),
                event_id=model_event.id,
                choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
                amended_value=None,
                actor="user:alice",
                session_operation_context=operation,
            )
        )
    event_id = _raise_prompt_card(service, blob_service, session_id, tool_call_id="initial")
    _approve_card(service, blob_service, session_id, event_id)
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
    return client, service, blob_service, session_id, blobs


def _reads_for_one_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: int) -> Counter[str]:
    """Save file 0 of an approved ``files``-file node; count custody reads and snapshot resolutions during the save."""
    client, service, blob_service, session_id, blobs = _approved_n_file_node(tmp_path / f"n{files}", files)
    counts: Counter[str] = Counter()
    real_read = approval_module._read_custody_bytes
    real_resolve = approval_module.resolve_prompt_review_snapshot

    def counting_read(record: Any) -> bytes:
        counts["custody_reads"] += 1
        return real_read(record)

    def counting_resolve(**kwargs: Any) -> Any:
        counts["snapshots"] += 1
        return real_resolve(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(approval_module, "_read_custody_bytes", counting_read)
        patch.setattr(approval_module, "resolve_prompt_review_snapshot", counting_resolve)
        saved = _save(client, session_id, blobs[0]["id"], "file 0 after", blobs[0]["content_hash"], field_path="queries.q0.template")
    assert saved.status_code == 200, saved.text
    # The save still approved every file binding, and the node is still admitted to runs.
    with blob_service._engine.connect() as conn:
        rows = conn.execute(
            select(prompt_file_approvals_table.c.field_path, prompt_file_approvals_table.c.content_sha256).where(
                prompt_file_approvals_table.c.session_id == session_id,
                prompt_file_approvals_table.c.trigger == "editor_save",
            )
        ).all()
    assert sorted(row.field_path for row in rows) == sorted(f"queries.q{index}.template" for index in range(files))
    assert dict(rows)["queries.q0.template"] == content_hash(b"file 0 after")
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
    return counts


def test_editor_save_custody_reads_grow_linearly_with_the_node_s_prompt_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quadrupling the node's prompt files at most quadruples one save's custody reads and snapshot resolutions."""
    small = _reads_for_one_save(tmp_path, monkeypatch, 2)
    large = _reads_for_one_save(tmp_path, monkeypatch, 8)
    assert small["custody_reads"] > 0
    # Linear in N: reads(8) <= 4 * reads(2) + a small constant. Per-binding
    # re-resolution is N^2 + 3N (10 at N=2, 88 at N=8) and fails this.
    assert large["custody_reads"] <= 4 * small["custody_reads"] + 4, (small, large)
    # The snapshot count does not grow with the number of files at all.
    assert large["snapshots"] == small["snapshots"], (small, large)
    # Exactly three resolutions per save: the pre-save attestation, the
    # post-save snapshot every approval row reuses, and the review refresh.
    assert dict(small) == {"snapshots": 3, "custody_reads": 6}
    assert dict(large) == {"snapshots": 3, "custody_reads": 24}


def test_reused_snapshot_applies_only_to_its_connection_node_and_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the reuse block any other connection, node or option set resolves afresh, and so does every call after it."""
    _client, _service, blob_service, session_id, _blobs = _approved_n_file_node(tmp_path, 2)
    request = PromptFileApprovalRequest(
        session_id=UUID(session_id),
        node_id="first",
        field_path="queries.q0.template",
        blob_id=UUID(int=0),
        content_sha256="0" * 64,
        actor_user_id="alice",
        trigger=PromptApprovalTrigger.EDITOR_SAVE,
    )
    resolves: list[str] = []
    real_resolve = approval_module.resolve_prompt_review_snapshot

    def counting_resolve(**kwargs: Any) -> Any:
        resolves.append(kwargs["node_id"])
        return real_resolve(**kwargs)

    with blob_service._engine.connect() as conn, blob_service._engine.connect() as other_conn:
        options = _current_node_options(conn, request)
        snapshot = _current_snapshot(conn, request, options)
        changed_options = {**options, "queries": {"q0": options["queries"]["q0"]}}
        monkeypatch.setattr(approval_module, "resolve_prompt_review_snapshot", counting_resolve)
        # Through the module so this file still collects on a tree without the block.
        with approval_module.reuse_resolved_prompt_snapshot(conn, request, snapshot):
            assert _current_snapshot(conn, request, options) is snapshot
            assert resolves == []
            # The test engine shares one SQLite connection: end this one's read
            # transaction before the other connection begins its own.
            conn.rollback()
            assert _current_snapshot(other_conn, request, options) is not snapshot
            other_conn.rollback()
            assert _current_snapshot(conn, request, changed_options).bindings != snapshot.bindings
            with pytest.raises(PromptFileApprovalStale):
                # Another node: resolved afresh, and refused as having no prompt files.
                _current_snapshot(conn, replace(request, node_id="other"), {})
        assert resolves == ["first", "first", "other"]
        assert _current_snapshot(conn, request, options) is not snapshot
        assert resolves == ["first", "first", "other", "first"]
