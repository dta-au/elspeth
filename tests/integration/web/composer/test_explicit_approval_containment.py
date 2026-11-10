"""Real-loop coverage for explicit-approval proposal containment.

In ``explicit_approve`` mode every staged composition edit is previewed against
the committed pipeline and recorded with that pipeline as its base. Two such
proposals can never both be accepted: accepting one moves the base and the
other fails the accept-time base check for ever. Until proposals are
cumulative, the loop keeps at most one composition proposal outstanding and
refuses the rest with an ordinary, model-visible tool refusal before any of
them is previewed or published.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.tools import execute_tool as real_execute_tool
from elspeth.web.sessions.models import blobs_table, chat_messages_table, composition_states_table
from tests.integration.web.composer.test_freeform_proposal_prevalidation import (
    _count_rows,
    _fake_llm_response,
    _Harness,
    _harness,
    _incremental_base_state,
    _inline_pipeline_args,
    _persisted_tool_content,
    _ScriptedLLM,
    _tool_turn,
    _valid_pipeline_args,
)

_USER = "proposal-prevalidation-user"


def _metadata_call(call_id: str, name: str) -> dict[str, Any]:
    return {"id": call_id, "name": "set_metadata", "arguments": {"patch": {"name": name}}}


def _tool_feedback(llm: _ScriptedLLM, snapshot_index: int, call_id: str) -> dict[str, Any]:
    matches = [m for m in llm.message_snapshots[snapshot_index] if m.get("role") == "tool" and m["tool_call_id"] == call_id]
    assert len(matches) == 1
    payload = json.loads(matches[0]["content"])
    assert type(payload) is dict
    return payload


def _persisted_tool_call_ids(harness: _Harness) -> list[str]:
    with harness.engine.connect() as conn:
        return list(
            conn.execute(
                select(chat_messages_table.c.tool_call_id)
                .where(chat_messages_table.c.session_id == harness.session_id)
                .where(chat_messages_table.c.role == "tool")
            ).scalars()
        )


async def _compose(harness: _Harness, llm: _ScriptedLLM, state: Any, message: str = "Change the pipeline.") -> Any:
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        return await harness.service.compose(
            message,
            [],
            state,
            session_id=harness.session_id,
            user_id=_USER,
            user_message_id=harness.user_message_id,
        )


def _assert_refused(payload: dict[str, Any], *, error_code: str, version: int) -> str:
    assert payload["success"] is False
    assert payload["version"] == version
    assert payload["affected_nodes"] == []
    assert "validation_delta" not in payload
    first = payload["validation"]["errors"][0]
    assert first["component"] == "rejected_mutation"
    assert first["error_code"] == error_code
    message = first["message"]
    assert type(message) is str
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_tool",
    ("set_metadata", "set_pipeline", "set_pipeline_inline"),
    ids=("two-incremental", "incremental-plus-set-pipeline", "incremental-plus-inline-set-pipeline"),
)
async def test_batch_with_two_staged_edits_is_refused_before_effects_then_one_complete_change_stages(
    tmp_path: Path, second_tool: str
) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    second_arguments = {
        "set_metadata": {"patch": {"name": "second"}},
        "set_pipeline": _valid_pipeline_args(tmp_path, metadata_name="second"),
        # The one call type with a pre-publication custody effect: its inline
        # blob must not be materialised for a refused call.
        "set_pipeline_inline": _inline_pipeline_args(tmp_path),
    }[second_tool]
    second_tool = "set_metadata" if second_tool == "set_metadata" else "set_pipeline"
    early_llm = _ScriptedLLM(
        _fake_llm_response(
            tool_calls=(
                _metadata_call("call_first", "first"),
                {"id": "call_state", "name": "get_pipeline_state", "arguments": {}},
                {"id": "call_second", "name": second_tool, "arguments": second_arguments},
            )
        ),
        _fake_llm_response(content="Refused."),
    )
    with (
        patch("elspeth.web.composer.tool_batch.execute_tool", wraps=real_execute_tool) as handlers,
        patch("elspeth.web.composer.tool_batch.build_set_pipeline_candidate") as builder,
    ):
        refused_result = await _compose(harness, early_llm, state)
    # The discovery call in the same batch still ran; neither staged edit was
    # previewed and no canonical candidate was built.
    assert builder.call_count == 0
    assert [call.args[0] for call in handlers.call_args_list] == ["get_pipeline_state"]

    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert refused_result.state is state
    for call_id in ("call_first", "call_second"):
        message = _assert_refused(
            _tool_feedback(early_llm, 1, call_id),
            error_code="approval_batch_not_atomic",
            version=state.version,
        )
        assert "set_pipeline" in message
        assert "nothing was applied and no proposal was recorded" in message
        assert json.loads(_persisted_tool_content(harness, call_id))["validation"]["errors"][0]["error_code"] == (
            "approval_batch_not_atomic"
        )
    assert _tool_feedback(early_llm, 1, "call_state")["success"] is True
    # One audited outcome per attempted call, in order, none of them advancing the version.
    assert [inv.tool_call_id for inv in refused_result.tool_invocations] == ["call_first", "call_state", "call_second"]
    assert all(inv.status is ComposerToolStatus.SUCCESS for inv in refused_result.tool_invocations)
    assert all(inv.version_before == inv.version_after == state.version for inv in refused_result.tool_invocations)
    assert sorted(_persisted_tool_call_ids(harness)) == ["call_first", "call_second", "call_state"]

    # The model's follow-up — one complete provider-authored change — stages exactly one proposal.
    llm = _ScriptedLLM(
        _tool_turn("call_complete", "set_pipeline", _valid_pipeline_args(tmp_path, metadata_name="complete")),
        _fake_llm_response(content="One complete change awaits approval."),
    )
    await _compose(harness, llm, state, "Submit it as one change.")
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [(proposal.tool_call_id, proposal.tool_name, proposal.status) for proposal in proposals] == [
        ("call_complete", "set_pipeline", "pending")
    ]
    assert _count_rows(harness.engine, composition_states_table) == 0


@pytest.mark.asyncio
async def test_later_provider_round_is_refused_while_a_proposal_is_pending(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_staged", "set_metadata", {"patch": {"name": "staged"}}),
        _tool_turn("call_later", "set_metadata", {"patch": {"name": "later"}}),
        _fake_llm_response(content="The first change awaits approval."),
    )
    with patch("elspeth.web.composer.tool_batch.execute_tool", wraps=real_execute_tool) as handlers:
        result = await _compose(harness, llm, state)

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [proposal.tool_call_id for proposal in proposals] == ["call_staged"]
    # Only the staged call was previewed; the refused one never reached a handler.
    assert handlers.call_count == 1
    message = _assert_refused(_tool_feedback(llm, 2, "call_later"), error_code="approval_proposal_pending", version=state.version)
    assert str(proposals[0].id) in message
    assert "accept or reject" in message
    assert [inv.tool_call_id for inv in result.tool_invocations] == ["call_staged", "call_later"]
    assert sorted(_persisted_tool_call_ids(harness)) == ["call_later", "call_staged"]
    assert result.state is state


@pytest.mark.asyncio
@pytest.mark.parametrize("later_tool", ("set_metadata", "set_pipeline"))
async def test_later_compose_request_is_refused_while_a_proposal_is_pending_and_admitted_after_rejection(
    tmp_path: Path, later_tool: str
) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    first = _ScriptedLLM(
        _tool_turn("call_staged", "set_metadata", {"patch": {"name": "staged"}}),
        _fake_llm_response(content="The change awaits approval."),
    )
    await _compose(harness, first, state)
    (pending,) = await harness.sessions.list_composition_proposals(UUID(harness.session_id))

    later_arguments = (
        {"patch": {"name": "later"}} if later_tool == "set_metadata" else _valid_pipeline_args(tmp_path, metadata_name="later")
    )
    second = _ScriptedLLM(
        _tool_turn("call_later", later_tool, later_arguments),
        _fake_llm_response(content="A proposal is already waiting."),
    )
    result = await _compose(harness, second, state, "Also rename it.")
    message = _assert_refused(_tool_feedback(second, 1, "call_later"), error_code="approval_proposal_pending", version=state.version)
    assert str(pending.id) in message
    assert [inv.tool_call_id for inv in result.tool_invocations] == ["call_later"]
    assert [p.id for p in await harness.sessions.list_composition_proposals(UUID(harness.session_id))] == [pending.id]

    # Once the user settles the pending proposal the next single change stages normally.
    await harness.sessions.reject_composition_proposal(
        session_id=UUID(harness.session_id),
        proposal_id=pending.id,
        actor=f"user:{_USER}",
    )
    third = _ScriptedLLM(
        _tool_turn("call_after_reject", later_tool, later_arguments),
        _fake_llm_response(content="The new change awaits approval."),
    )
    await _compose(harness, third, state, "Try again.")
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id), status="pending")
    assert [proposal.tool_call_id for proposal in proposals] == ["call_after_reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("set_metadata", "set_pipeline"))
async def test_single_staged_edit_records_one_proposal_with_truthful_feedback(tmp_path: Path, tool_name: str) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    arguments = {"patch": {"name": "single"}} if tool_name == "set_metadata" else _valid_pipeline_args(tmp_path)
    llm = _ScriptedLLM(_tool_turn("call_single", tool_name, arguments), _fake_llm_response(content="Awaiting approval."))
    result = await _compose(harness, llm, state)

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [(proposal.tool_call_id, proposal.status) for proposal in proposals] == [("call_single", "pending")]
    payload = _tool_feedback(llm, 1, "call_single")
    assert payload["success"] is True
    assert payload["version"] == state.version
    assert set(payload["data"]) == {"status", "proposal_id", "tool_name", "summary", "message"}
    assert payload["data"]["status"] == "APPROVAL_REQUIRED"
    assert payload["data"]["proposal_id"] == str(proposals[0].id)
    message = payload["data"]["message"]
    # The staged result must not read as an applied, validated change.
    assert "recorded" in message
    assert "not been applied" in message
    assert "unchanged current pipeline" in message
    assert "does not validate the proposed change" in message
    assert "accepts or rejects" in message
    assert result.state is state


@pytest.mark.asyncio
async def test_auto_commit_batches_and_pending_rows_are_unaffected(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    staged = _ScriptedLLM(
        _tool_turn("call_staged", "set_metadata", {"patch": {"name": "staged"}}),
        _fake_llm_response(content="Awaiting approval."),
    )
    await _compose(harness, staged, state)
    assert len(await harness.sessions.list_composition_proposals(UUID(harness.session_id), status="pending")) == 1

    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor=f"user:{_USER}",
    )
    auto = _ScriptedLLM(
        _fake_llm_response(tool_calls=(_metadata_call("call_auto_1", "auto-one"), _metadata_call("call_auto_2", "auto-two"))),
        _fake_llm_response(content="Done."),
    )
    real_list = harness.sessions.list_composition_proposals

    async def list_without_pending_filter(session_id: UUID, *, status: str | None = None) -> Any:
        # The reused-id preflight legitimately lists every proposal; only the
        # containment's pending-status lookup is forbidden in auto_commit.
        assert status is None, "auto_commit must not consult pending proposals"
        return await real_list(session_id)

    with patch.object(harness.sessions, "list_composition_proposals", new=list_without_pending_filter):
        result = await _compose(harness, auto, state)

    assert result.state.version == state.version + 2
    assert result.state.metadata.name == "auto-two"
    assert _tool_feedback(auto, 1, "call_auto_1")["success"] is True
    assert _tool_feedback(auto, 1, "call_auto_2")["success"] is True
    assert [p.tool_call_id for p in await harness.sessions.list_composition_proposals(UUID(harness.session_id))] == ["call_staged"]


@pytest.mark.asyncio
async def test_destructive_blob_proposals_keep_staging_beside_one_composition_proposal(tmp_path: Path) -> None:
    from tests.helpers.session_fences import fenced_operation_context

    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    blob_ids = []
    for index in range(2):
        with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
            blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
                UUID(harness.session_id), f"retained-{index}.txt", b"original", "text/plain", session_operation_context=operation
            )
        blob_ids.append(str(blob.id))
    llm = _ScriptedLLM(
        _fake_llm_response(
            tool_calls=(
                {"id": "call_blob_0", "name": "delete_blob", "arguments": {"blob_id": blob_ids[0]}},
                _metadata_call("call_metadata", "with-blobs"),
                {"id": "call_blob_1", "name": "delete_blob", "arguments": {"blob_id": blob_ids[1]}},
            )
        ),
        # A pending blob-only proposal does not block this later round, but the
        # pending composition proposal does.
        _tool_turn("call_refused", "set_metadata", {"patch": {"name": str(uuid4())}}),
        _fake_llm_response(content="Awaiting approval."),
    )
    await _compose(harness, llm, state)
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert sorted((proposal.tool_call_id, proposal.tool_name) for proposal in proposals) == [
        ("call_blob_0", "delete_blob"),
        ("call_blob_1", "delete_blob"),
        ("call_metadata", "set_metadata"),
    ]
    message = _assert_refused(_tool_feedback(llm, 2, "call_refused"), error_code="approval_proposal_pending", version=state.version)
    (composition_proposal,) = (proposal for proposal in proposals if proposal.tool_name == "set_metadata")
    assert str(composition_proposal.id) in message


@pytest.mark.asyncio
async def test_pending_proposal_cap_still_bounds_blob_proposals_and_excludes_immediate_calls(tmp_path: Path) -> None:
    """Ten blob deletions sit exactly at the per-turn cap; discovery and ``create_blob`` are not counted."""
    from tests.helpers.session_fences import fenced_operation_context

    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    deletions = []
    for index in range(10):
        with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
            blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
                UUID(harness.session_id), f"retained-{index}.txt", b"original", "text/plain", session_operation_context=operation
            )
        deletions.append({"id": f"call_delete_{index}", "name": "delete_blob", "arguments": {"blob_id": str(blob.id)}})
    llm = _ScriptedLLM(
        _fake_llm_response(
            tool_calls=(
                {"id": "call_state", "name": "get_pipeline_state", "arguments": {}},
                {
                    "id": "call_create_blob",
                    "name": "create_blob",
                    "arguments": {"filename": "immediate.txt", "mime_type": "text/plain", "content": "immediate content"},
                },
                *deletions,
            )
        ),
        _fake_llm_response(content="Awaiting approval."),
    )
    result = await _compose(harness, llm, state)
    assert len(result.tool_invocations) == 12
    assert _tool_feedback(llm, 1, "call_create_blob")["success"] is True
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert sorted(proposal.tool_call_id for proposal in proposals) == sorted(call["id"] for call in deletions)
    # The immediate create_blob really allocated its blob beside the ten retained ones.
    assert _count_rows(harness.engine, blobs_table) == 11


@pytest.mark.asyncio
async def test_pending_blob_only_proposal_does_not_block_a_composition_proposal(tmp_path: Path) -> None:
    from tests.helpers.session_fences import fenced_operation_context

    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
        blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
            UUID(harness.session_id), "retained.txt", b"original", "text/plain", session_operation_context=operation
        )
    llm = _ScriptedLLM(
        _tool_turn("call_blob", "delete_blob", {"blob_id": str(blob.id)}),
        _tool_turn("call_metadata", "set_metadata", {"patch": {"name": "after-blob"}}),
        _fake_llm_response(content="Awaiting approval."),
    )
    await _compose(harness, llm, state)
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [proposal.tool_call_id for proposal in proposals] == ["call_blob", "call_metadata"]
