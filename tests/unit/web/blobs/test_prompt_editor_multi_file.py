"""Editor saves on an LLM node that uses more than one prompt file.

The editor shows one file. A save changes the node's review anchor, so the
node's other prompt files must come out of the save still approved, and the
save must never approve a file or text the user did not see.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.web.blobs import prompt_text_routes
from elspeth.web.blobs.prompt_editor import PromptBinding
from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.interpretation_surfacing import surface_pending_interpretation_reviews_for_state
from elspeth.web.composer.tools._common import _options_with_default_model_choice_review, _options_with_default_prompt_template_review
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import blobs_table, interpretation_events_table, prompt_file_approvals_table
from elspeth.web.sessions.pending_interpretation import resolved_prompt_review_matches_anchor
from elspeth.web.sessions.prompt_file_approval import (
    PromptApprovalTrigger,
    PromptFileApprovalRequest,
    PromptFileApprovalStale,
    _blob_record,
    _current_node_options,
    _current_snapshot,
    _read_custody_bytes,
    assert_latest_prompt_file_approvals_attest_surface,
)
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_prompt_editor import (
    _admit,
    _approve_card,
    _live_prompt_setup,
    _raise_prompt_card,
    _review_state,
    _save,
    _save_editor_state,
    _save_side_effects,
)
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

_A_BEFORE = b"file A before"
_B_BEFORE = b"file B before"

# Each shape puts file A and file B on two prompt slots of node "first".
_SHAPES = ("roles", "queries_mapping", "queries_list")


def _marker(blob: dict[str, Any]) -> dict[str, str]:
    return {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}


def _paths(shape: str) -> tuple[str, str]:
    if shape == "roles":
        return "prompt_template", "system_prompt"
    if shape == "queries_mapping":
        return "queries.a.template", "queries.b.template"
    return "queries.0.template", "queries.1.template"


def _two_file_node(
    tmp_path: Path, shape: str, *, approve: bool = True, card: bool = True
) -> tuple[Any, Any, Any, str, dict[str, Any], dict[str, Any], UUID | None]:
    """Node "first" with file A and file B on two prompt slots; the model review is resolved.

    With ``approve`` the prompt card that showed BOTH files is approved, so both
    bindings hold an approval that attests the node's surface. Without it the
    card is left pending. Without ``card`` the state holds the pending prompt
    review but no card has been issued for it yet.
    """
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    file_a = _upload_blob(client, session_id, _A_BEFORE, "a.txt", "text/plain")
    file_b = _upload_blob(client, session_id, _B_BEFORE, "b.txt", "text/plain")
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
    if shape == "roles":
        options["prompt_template"] = _marker(file_a)
        options["system_prompt"] = _marker(file_b)
    elif shape == "queries_mapping":
        options["queries"] = {
            "a": {"input_fields": {}, "template": _marker(file_a)},
            "b": {"input_fields": {}, "template": _marker(file_b)},
        }
    else:
        options["queries"] = [
            {"name": "a", "input_fields": {}, "template": _marker(file_a)},
            {"name": "b", "input_fields": {}, "template": _marker(file_b)},
        ]
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
    if not card:
        return client, service, blob_service, session_id, file_a, file_b, None
    event_id = _raise_prompt_card(service, blob_service, session_id, tool_call_id="initial")
    if approve:
        _approve_card(service, blob_service, session_id, event_id)
        assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
    return client, service, blob_service, session_id, file_a, file_b, event_id


def _content(client: Any, session_id: str, blob: dict[str, Any]) -> bytes:
    return client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content


def _latest_approvals(blob_service: Any, session_id: str) -> dict[str, tuple[str, str, str, str]]:
    """Per field path, the latest approval row as (blob_id, content_sha256, trigger, review_anchor)."""
    with blob_service._engine.connect() as conn:
        rows = conn.execute(
            select(prompt_file_approvals_table)
            .where(prompt_file_approvals_table.c.session_id == session_id, prompt_file_approvals_table.c.node_id == "first")
            .order_by(prompt_file_approvals_table.c.approved_at, prompt_file_approvals_table.c.id)
        ).all()
    return {row.field_path: (row.blob_id, row.content_sha256, row.trigger, row.review_anchor) for row in rows}


def _approval_rows(blob_service: Any, session_id: str) -> list[Any]:
    with blob_service._engine.connect() as conn:
        return list(
            conn.execute(
                select(prompt_file_approvals_table)
                .where(prompt_file_approvals_table.c.session_id == session_id)
                .order_by(prompt_file_approvals_table.c.approved_at, prompt_file_approvals_table.c.id)
            ).all()
        )


@pytest.mark.parametrize("shape", _SHAPES)
def test_each_file_of_a_two_file_node_saves_and_both_stay_approved(tmp_path: Path, shape: str) -> None:
    client, service, blob_service, session_id, file_a, file_b, _event = _two_file_node(tmp_path, shape)
    path_a, path_b = _paths(shape)
    card_anchor = _latest_approvals(blob_service, session_id)[path_a][3]
    rows_before = _approval_rows(blob_service, session_id)
    assert sorted((row.field_path, row.trigger) for row in rows_before) == sorted([(path_a, "card"), (path_b, "card")])

    saved_a = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"], field_path=path_a)

    assert saved_a.status_code == 200, saved_a.text
    assert saved_a.json() == {"changed": True, "content_sha256": content_hash(b"file A after")}
    assert _content(client, session_id, file_a) == b"file A after"
    assert _content(client, session_id, file_b) == _B_BEFORE
    after_a = _latest_approvals(blob_service, session_id)
    # A is approved with the saved bytes; B is carried forward with the bytes
    # it already had approved. Both rows name the one post-save anchor.
    assert after_a[path_a][:3] == (file_a["id"], content_hash(b"file A after"), "editor_save")
    assert after_a[path_b][:3] == (file_b["id"], file_b["content_hash"], "editor_save")
    assert after_a[path_a][3] == after_a[path_b][3] != card_anchor
    # Run admission (latest approval of every binding attests the live surface).
    admitted = _admit(service, blob_service, session_id)
    assert [snapshot.node_id for snapshot in admitted] == ["first"]
    assert {binding.field_path for binding in admitted[0].bindings} == {path_a, path_b}
    # The save wrote exactly one row per file binding, in one instant, and both
    # name the anchor of the snapshot the run path just admitted.
    new_rows = [row for row in _approval_rows(blob_service, session_id) if row.id not in {before.id for before in rows_before}]
    assert sorted(row.field_path for row in new_rows) == sorted([path_a, path_b])
    assert len({row.approved_at for row in new_rows}) == 1
    assert {row.review_anchor for row in new_rows} == {admitted[0].anchor}
    assert {row.effective_artifact_hash for row in new_rows} == {admitted[0].effective_artifact_hash}
    assert _review_state(service, session_id)[0]["first"] == "resolved"
    head = asyncio.run(service.get_current_state(UUID(session_id)))
    assert head is not None
    assert resolved_prompt_review_matches_anchor(head, affected_node_id="first")

    # The other file saves the same way afterwards.
    saved_b = _save(client, session_id, file_b["id"], "file B after", file_b["content_hash"], field_path=path_b)

    assert saved_b.status_code == 200, saved_b.text
    assert _content(client, session_id, file_a) == b"file A after"
    assert _content(client, session_id, file_b) == b"file B after"
    after_b = _latest_approvals(blob_service, session_id)
    assert after_b[path_a][:2] == (file_a["id"], content_hash(b"file A after"))
    assert after_b[path_b][:2] == (file_b["id"], content_hash(b"file B after"))
    assert after_b[path_a][3] == after_b[path_b][3] != after_a[path_a][3]
    admitted_after_b = _admit(service, blob_service, session_id)
    assert [snapshot.node_id for snapshot in admitted_after_b] == ["first"]
    assert len(_approval_rows(blob_service, session_id)) == len(rows_before) + 4
    assert after_b[path_a][3] == admitted_after_b[0].anchor


def test_pending_card_over_two_files_refuses_save_then_card_allows_it(tmp_path: Path) -> None:
    client, service, blob_service, session_id, file_a, file_b, event_id = _two_file_node(tmp_path, "roles", approve=False)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_code"] == "prompt_file_review_pending"
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _content(client, session_id, file_b) == _B_BEFORE
    # Head, approval rows, blob metadata and the card's own choice are untouched.
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before
    with pytest.raises(PromptFileReviewStale):
        _admit(service, blob_service, session_id)

    _approve_card(service, blob_service, session_id, event_id)
    saved = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert saved.status_code == 200, saved.text
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_failed_carried_approval_rolls_back_bytes_state_and_every_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The saved file's row, the carried sibling row, the byte write and the new head share one rollback."""
    client, service, blob_service, session_id, file_a, file_b, _event = _two_file_node(tmp_path, "roles")
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])
    real_record = prompt_text_routes.record_prompt_file_approval
    recorded: list[str] = []

    def fail_on_the_second_row(conn: Any, request: Any, *, now: Any) -> Any:
        if recorded:
            raise RuntimeError("fault on the carried approval")
        recorded.append(request.field_path)
        return real_record(conn, request, now=now)

    # Fault injection: the first approval row is really written, then the next one fails.
    monkeypatch.setattr(prompt_text_routes, "record_prompt_file_approval", fail_on_the_second_row)
    with pytest.raises(RuntimeError, match="fault on the carried approval"):
        _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert len(recorded) == 1
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _content(client, session_id, file_b) == _B_BEFORE
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_save_refuses_when_a_sibling_file_was_never_approved(tmp_path: Path) -> None:
    """The node was approved with file A alone; the agent then added file B with no review.

    No card is pending, so only the save-side attestation check stands between
    this save and an approval row for B that nobody reviewed.
    """
    client, service, blob_service, session_id, file_a, _second, events = _live_prompt_setup(tmp_path)
    _approve_card(service, blob_service, session_id, events["first"])
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
    file_b = _upload_blob(client, session_id, _B_BEFORE, "b.txt", "text/plain")
    approved = asyncio.run(service.get_current_state(UUID(session_id)))
    assert approved is not None
    two_files = state_from_record(approved).to_dict()
    two_files["nodes"][0]["options"]["system_prompt"] = _marker(file_b)
    _save_editor_state(service, blob_service, session_id, two_files)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    # The resolved review still names the one-file prompt: no edit to the step reopens it.
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_REVIEW_DRIFTED}
    assert _content(client, session_id, file_a) == b"first before"
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before
    assert "system_prompt" not in _latest_approvals(blob_service, session_id)


def _unreviewed_two_file_node(tmp_path: Path) -> tuple[Any, Any, Any, str, dict[str, Any], dict[str, Any]]:
    """Node "first" with file A and file B, no review card and no approval of any kind."""
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    file_a = _upload_blob(client, session_id, _A_BEFORE, "a.txt", "text/plain")
    file_b = _upload_blob(client, session_id, _B_BEFORE, "b.txt", "text/plain")
    nodes = [
        {
            "id": "first",
            "node_type": "transform",
            "plugin": "llm",
            "options": {"prompt_template": _marker(file_a), "system_prompt": _marker(file_b)},
        }
    ]
    asyncio.run(
        service.save_composition_state(UUID(session_id), CompositionStateData(nodes=nodes, is_valid=True), provenance="session_seed")
    )
    return client, service, blob_service, session_id, file_a, file_b


def test_save_refuses_a_two_file_node_that_has_no_approval_at_all(tmp_path: Path) -> None:
    """A single-file node's first save has nothing to compare; a two-file node gets no such pass."""
    client, _service, blob_service, session_id, file_a, _file_b = _unreviewed_two_file_node(tmp_path)

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_NOT_APPROVED}
    assert _content(client, session_id, file_a) == _A_BEFORE
    with blob_service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []


def test_save_names_the_card_to_wait_for_when_the_review_is_pending_but_not_issued(tmp_path: Path) -> None:
    """The state holds a pending prompt review with no card yet (after a fork or revert, before the surfacing pass).

    The save is refused like any unapproved two-file node, but the way out is
    different: the card appears on reload, and approving it is the whole fix.
    """
    client, service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles", card=False)
    assert _review_state(service, session_id)[0]["first"] == "pending"
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_CARD_OWED}
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before


def test_pre_save_check_refuses_a_never_approved_two_file_node(tmp_path: Path) -> None:
    """The pre-save check itself, not the transaction guard behind it, has no first-save pass for two files."""
    _client, service, blob_service, session_id, _file_a, _file_b = _unreviewed_two_file_node(tmp_path)
    state = asyncio.run(service.get_current_state(UUID(session_id)))
    assert state is not None
    with pytest.raises(PromptFileApprovalStale), blob_service._engine.connect() as conn:
        assert_latest_prompt_file_approvals_attest_surface(conn, state=state, node_id="first")


def test_transaction_guard_alone_refuses_an_unapproved_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the pre-save check forced to pass, the in-transaction guard still refuses and rolls everything back."""
    client, service, blob_service, session_id, file_a, file_b = _unreviewed_two_file_node(tmp_path)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    def pre_save_check_passes(conn: Any, *, state: Any, node_id: str) -> None:
        return None

    # Fault injection: the pre-save attestation check reports "nothing to compare".
    monkeypatch.setattr(prompt_text_routes, "assert_latest_prompt_file_approvals_attest_surface", pre_save_check_passes)
    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    # The message is the transaction hook's, not the pre-save check's.
    assert refused.json()["detail"] == {
        "error_code": "prompt_file_stale",
        "message": "Prompt bindings changed. Reload the session and retry.",
    }
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _content(client, session_id, file_b) == _B_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before
    with blob_service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []


def _node_snapshot(blob_service: Any, session_id: str, blob: dict[str, Any], field_path: str) -> Any:
    request = PromptFileApprovalRequest(
        session_id=UUID(session_id),
        node_id="first",
        field_path=field_path,
        blob_id=UUID(blob["id"]),
        content_sha256=blob["content_hash"],
        actor_user_id="alice",
        trigger=PromptApprovalTrigger.EDITOR_SAVE,
    )
    with blob_service._engine.connect() as conn:
        return _current_snapshot(conn, request, _current_node_options(conn, request))


def _may_carry(blob_service: Any, *, prior: Any, saved: Any, saved_blob: dict[str, Any]) -> None:
    # Imported here: the guard does not exist before this fix.
    from elspeth.web.sessions.prompt_file_approval import assert_editor_save_may_carry_approvals

    with blob_service._engine.connect() as conn:
        assert_editor_save_may_carry_approvals(conn, prior=prior, saved=saved, saved_blob_id=UUID(saved_blob["id"]))


def test_carry_guard_passes_when_the_prior_surface_is_attested(tmp_path: Path) -> None:
    _client, _service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    snapshot = _node_snapshot(blob_service, session_id, file_a, "prompt_template")
    _may_carry(blob_service, prior=snapshot, saved=snapshot, saved_blob=file_a)
    # Nothing to carry and no prior: the saved file is the node's only file.
    only_a = replace(snapshot, bindings=tuple(binding for binding in snapshot.bindings if binding.field_path == "prompt_template"))
    _may_carry(blob_service, prior=None, saved=only_a, saved_blob=file_a)


def test_carry_guard_refuses_a_sibling_when_there_is_no_prior_attestation(tmp_path: Path) -> None:
    _client, _service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    snapshot = _node_snapshot(blob_service, session_id, file_a, "prompt_template")
    with pytest.raises(PromptFileApprovalStale):
        _may_carry(blob_service, prior=None, saved=snapshot, saved_blob=file_a)


@pytest.mark.parametrize("changed", ["content_sha256", "blob_id", "field_path"])
def test_carry_guard_refuses_a_sibling_that_differs_from_what_was_attested(tmp_path: Path, changed: str) -> None:
    _client, _service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    snapshot = _node_snapshot(blob_service, session_id, file_a, "prompt_template")
    other = {"content_sha256": "0" * 64, "blob_id": UUID(int=1), "field_path": "queries.x.template"}[changed]
    saved = replace(
        snapshot,
        bindings=tuple(
            replace(binding, **{changed: other}) if binding.field_path == "system_prompt" else binding for binding in snapshot.bindings
        ),
    )
    with pytest.raises(PromptFileApprovalStale):
        _may_carry(blob_service, prior=snapshot, saved=saved, saved_blob=file_a)


def test_carry_guard_refuses_when_the_prior_snapshot_is_not_what_the_approvals_attest(tmp_path: Path) -> None:
    _client, _service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    snapshot = _node_snapshot(blob_service, session_id, file_a, "prompt_template")
    with pytest.raises(PromptFileApprovalStale):
        _may_carry(blob_service, prior=replace(snapshot, anchor="f" * 64), saved=snapshot, saved_blob=file_a)


def test_carry_guard_refuses_while_a_card_is_pending(tmp_path: Path) -> None:
    _client, _service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles", approve=False)
    snapshot = _node_snapshot(blob_service, session_id, file_a, "prompt_template")
    with pytest.raises(PromptFileApprovalStale):
        _may_carry(blob_service, prior=snapshot, saved=snapshot, saved_blob=file_a)


def test_save_refuses_when_a_sibling_binding_moved_since_its_approval(tmp_path: Path) -> None:
    """The agent re-pointed the system prompt to a new file after both files were approved."""
    client, service, blob_service, session_id, file_a, file_b, _event = _two_file_node(tmp_path, "roles")
    file_c = _upload_blob(client, session_id, b"file C never reviewed", "c.txt", "text/plain")
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    repointed = state_from_record(record).to_dict()
    repointed["nodes"][0]["options"]["system_prompt"] = _marker(file_c)
    _save_editor_state(service, blob_service, session_id, repointed)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_REVIEW_DRIFTED}
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before
    # B's approval is still the card's; C never got one.
    assert _latest_approvals(blob_service, session_id)["system_prompt"][:3] == (file_b["id"], file_b["content_hash"], "card")


def test_transaction_guard_alone_refuses_a_drifted_review_with_its_own_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the pre-save check forced to pass, the in-transaction drift check refuses before any write."""
    client, service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    file_c = _upload_blob(client, session_id, b"file C never reviewed", "c.txt", "text/plain")
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    repointed = state_from_record(record).to_dict()
    repointed["nodes"][0]["options"]["system_prompt"] = _marker(file_c)
    _save_editor_state(service, blob_service, session_id, repointed)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    def pre_save_check_passes(conn: Any, *, state: Any, node_id: str) -> None:
        return None

    # Fault injection: the pre-save attestation check reports "nothing to compare".
    monkeypatch.setattr(prompt_text_routes, "assert_latest_prompt_file_approvals_attest_surface", pre_save_check_passes)
    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_REVIEW_DRIFTED}
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before


def test_save_refuses_when_a_sibling_files_bytes_changed_since_its_approval(tmp_path: Path) -> None:
    """File B's stored bytes no longer match what was approved (same length, different content)."""
    client, service, blob_service, session_id, file_a, file_b, _event = _two_file_node(tmp_path, "roles")
    with blob_service._engine.connect() as conn:
        storage_path = Path(conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == file_b["id"])).scalar_one())
    tampered = b"file B BEFORE"
    assert len(tampered) == len(_B_BEFORE) and storage_path.read_bytes() == _B_BEFORE
    storage_path.chmod(0o600)
    storage_path.write_bytes(tampered)
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_UNREADABLE}
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before


def test_binding_set_changed_before_commit_is_refused_and_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-transaction re-check requires the SAME binding set the request checked, not merely the opened one.

    The route computes the file's bindings once for the request and again on
    the head inside the save transaction. The second answer here gains a
    binding the request never saw; the save must refuse and write nothing.
    """
    client, service, blob_service, session_id, file_a, file_b, _event = _two_file_node(tmp_path, "roles")
    before = _save_side_effects(service, blob_service, session_id, file_a["id"])
    real_bindings = prompt_text_routes.prompt_bindings_for_blob
    calls: list[int] = []

    def gains_a_binding_on_recheck(state: Any, blob: Any) -> tuple[tuple[PromptBinding, ...], bool]:
        bindings, mixed = real_bindings(state, blob)
        calls.append(len(bindings))
        if len(calls) == 2:
            return (*bindings, PromptBinding("elsewhere", "system_prompt", "utf-8")), mixed
        return bindings, mixed

    monkeypatch.setattr(prompt_text_routes, "prompt_bindings_for_blob", gains_a_binding_on_recheck)
    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])

    assert calls == [1, 1]
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_code"] == "prompt_file_binding_changed"
    assert _content(client, session_id, file_a) == _A_BEFORE
    assert _content(client, session_id, file_b) == _B_BEFORE
    assert _save_side_effects(service, blob_service, session_id, file_a["id"]) == before


# --- Each recovery a ``prompt_file_stale`` message promises, performed end to end ---


def _issue_owed_card(service: Any, blob_service: Any, session_id: str) -> UUID:
    """The validate route's repair pass over the current head; returns the one pending prompt card."""
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        asyncio.run(
            surface_pending_interpretation_reviews_for_state(
                state_from_record(record),
                sessions_service=service,
                session_id=session_id,
                current_state_id=str(record.id),
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                model_identifier="test-model",
                model_version="test-model",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                only_missing_evidence=True,
                session_operation_context=operation,
            )
        )
    with blob_service._engine.connect() as conn:
        pending = conn.execute(
            select(interpretation_events_table.c.id).where(
                interpretation_events_table.c.session_id == session_id,
                interpretation_events_table.c.kind == InterpretationKind.LLM_PROMPT_TEMPLATE.value,
                interpretation_events_table.c.choice == "pending",
            )
        ).all()
    assert len(pending) == 1, pending
    return UUID(str(pending[0][0]))


def _agent_attaches(service: Any, blob_service: Any, session_id: str, tmp_path: Path, slot: str, blob: dict[str, Any]) -> None:
    """The assistant's ``wire_blob_inline_ref`` tool handler, then the state save a composer turn makes."""
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        wired = blob_tools.execute_tool(
            "wire_blob_inline_ref",
            {"field_path": f"node:first.options.{slot}", "blob_id": blob["id"]},
            state_from_record(record),
            blob_tools._catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
        )
    assert wired.success, wired.to_dict()
    _save_editor_state(service, blob_service, session_id, wired.updated_state.to_dict())


def _head_id(service: Any, session_id: str) -> str:
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    return str(record.id)


def _revert(client: Any, session_id: str, state_id: str) -> None:
    reverted = client.post(f"/api/sessions/{session_id}/state/revert", json={"operation_id": str(uuid4()), "state_id": state_id})
    assert reverted.status_code == 200, reverted.text


def test_card_owed_recovery_the_card_is_issued_then_approved_then_the_save_works(tmp_path: Path) -> None:
    client, service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles", card=False)
    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert refused.json()["detail"]["message"] == prompt_text_routes._STALE_CARD_OWED

    card = _issue_owed_card(service, blob_service, session_id)
    _approve_card(service, blob_service, session_id, card)

    saved = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert saved.status_code == 200, saved.text
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_unreadable_recovery_after_reverting_past_an_editor_save_reattaching_the_file_works(tmp_path: Path) -> None:
    """Revert to the version before an editor save: that version pins the file's old bytes."""
    client, service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    before_save = _head_id(service, session_id)
    first_save = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert first_save.status_code == 200, first_save.text
    current_hash = first_save.json()["content_sha256"]
    _revert(client, session_id, before_save)

    refused = _save(client, session_id, file_a["id"], "file A again", current_hash)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_UNREADABLE}

    _agent_attaches(service, blob_service, session_id, tmp_path, "prompt_template", file_a)
    assert _review_state(service, session_id)[0]["first"] == "pending"
    _approve_card(service, blob_service, session_id, _issue_owed_card(service, blob_service, session_id))

    saved = _save(client, session_id, file_a["id"], "file A again", current_hash)
    assert saved.status_code == 200, saved.text
    assert _content(client, session_id, file_a) == b"file A again"
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def _reverted_behind_a_later_approval(tmp_path: Path) -> tuple[Any, Any, Any, str, dict[str, Any], str]:
    """Files A and B approved; then file C replaces B and is approved; then the user reverts to the A+B version."""
    client, service, blob_service, session_id, file_a, _file_b, _event = _two_file_node(tmp_path, "roles")
    earlier = _head_id(service, session_id)
    file_c = _upload_blob(client, session_id, b"file C before", "c.txt", "text/plain")
    _agent_attaches(service, blob_service, session_id, tmp_path, "system_prompt", file_c)
    _approve_card(service, blob_service, session_id, _issue_owed_card(service, blob_service, session_id))
    later = _head_id(service, session_id)
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
    _revert(client, session_id, earlier)
    # The reverted review is resolved, a run is refused, and no card is open or owed.
    assert _review_state(service, session_id)[0]["first"] == "resolved"
    with pytest.raises(PromptFileReviewStale):
        _admit(service, blob_service, session_id)
    with blob_service._engine.connect() as conn:
        assert (
            conn.execute(
                select(interpretation_events_table.c.id).where(
                    interpretation_events_table.c.session_id == session_id, interpretation_events_table.c.choice == "pending"
                )
            ).all()
            == []
        )

    refused = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error_code": "prompt_file_stale", "message": prompt_text_routes._STALE_NOT_APPROVED}
    return client, service, blob_service, session_id, file_a, later


def test_out_of_date_approval_recovery_reverting_to_the_later_version_works(tmp_path: Path) -> None:
    client, service, blob_service, session_id, file_a, later = _reverted_behind_a_later_approval(tmp_path)

    _revert(client, session_id, later)

    saved = _save(client, session_id, file_a["id"], "file A after", file_a["content_hash"])
    assert saved.status_code == 200, saved.text
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_out_of_date_approval_recovery_attaching_a_newly_uploaded_copy_works(tmp_path: Path) -> None:
    client, service, blob_service, session_id, _file_a, _later = _reverted_behind_a_later_approval(tmp_path)

    copy = _upload_blob(client, session_id, _A_BEFORE, "a-again.txt", "text/plain")
    _agent_attaches(service, blob_service, session_id, tmp_path, "prompt_template", copy)
    _approve_card(service, blob_service, session_id, _issue_owed_card(service, blob_service, session_id))

    saved = _save(client, session_id, copy["id"], "file A after", copy["content_hash"])
    assert saved.status_code == 200, saved.text
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]
