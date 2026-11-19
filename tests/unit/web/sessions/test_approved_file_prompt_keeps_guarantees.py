"""An APPROVED file-backed llm prompt keeps the node's authoring guarantees.

Approving a file's ``llm_prompt_template`` card saves
``approved_prompt_artifact_hash`` on the node, computed from the reviewed
bytes (``sessions/pending_interpretation.py``). The authoring probe stands
inert text in for the file, so the config model compared that hash with the
placeholder, refused, and returned the node to zero guarantees: the sink error
the probe fix removed came back the moment the user approved. The probe
projection now omits the hash whenever a prompt position is file-backed.

These tests drive the real tool and the real session service against a
session database and validate the state that approval saved.

Driven for real: ``wire_blob_inline_ref`` (the tool dispatcher),
``SessionServiceImpl.save_composition_state``,
``create_pending_interpretation_event``, ``get_prompt_review_snapshot`` and
``resolve_interpretation_event`` (what ``POST
/{session_id}/interpretations/{event_id}/resolve`` calls), and for the editor
path the real ``PUT .../blobs/{blob_id}/prompt-text`` route. Not driven: the
resolve HTTP route itself (ownership check, compose lock, lease acquisition,
status mapping) and the compose loop that saves the tool's state and opens the
pending event; the tests perform those two writes through the same service
methods.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import structlog

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, prompt_review_draft_from_options
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.prompt_review import reviewed_anchor_for
from tests.unit.web.blobs import test_prompt_editor as prompt_editor
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env

HASH_KEY = "approved_prompt_artifact_hash"
FILE_TEXT = "Classify this row: {{ row.text }}"
OTHER_FILE_TEXT = "Label this row in one word: {{ row.text }}"
_PROBE_CODES = ("contract_probe_deferred", "contract_probe_failed")


def _read_blob(env: dict[str, Any], requested: UUID) -> tuple[Any, bytes]:
    record = env["authority"].mutate(env["operation"], lambda tx: tx.blobs.read_blob(blob_id=requested))
    return record, Path(record.storage_path).read_bytes()


def _service(env: dict[str, Any]) -> SessionServiceImpl:
    service = SessionServiceImpl(
        env["engine"],
        data_dir=Path(env["data_dir"]),
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("approved-file-prompt-test"),
        inline_blob_read=lambda _context, requested: _read_blob(env, requested),
    )

    async def same_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        # The shared in-memory SQLite fixture is scoped to its owning thread.
        return func(*args, **kwargs)

    service._run_sync = same_thread
    return service


async def _save_and_approve_prompt_card(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState) -> CompositionState:
    """Save ``state``, open its pending prompt card, approve it as the card does, return what was saved."""
    session_id = UUID(env["session_id"])
    document = state.to_dict()
    saved = await service.save_composition_state(
        session_id,
        CompositionStateData(
            sources=document["sources"],
            nodes=document["nodes"],
            edges=document["edges"],
            outputs=document["outputs"],
            metadata_=document["metadata"],
            is_valid=True,
        ),
        provenance="tool_call",
        session_operation_context=env["operation"],
    )
    draft = prompt_review_draft_from_options(state.nodes[0].options)
    assert draft is not None, "the state carries a pending llm_prompt_template requirement"
    event = await service.create_pending_interpretation_event(
        session_id=session_id,
        composition_state_id=saved.id,
        affected_node_id="classify",
        tool_call_id="approved-file-prompt-test",
        user_term="llm_prompt_template:classify",
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        llm_draft=draft,
        model_identifier="test-model",
        model_version="1",
        provider="test-provider",
        composer_skill_hash="a" * 64,
        session_operation_context=env["operation"],
    )
    resolved, returned = await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        reviewed_anchor=await reviewed_anchor_for(service, session_id, event.id),
        session_operation_context=env["operation"],
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    current = await service.get_current_state(session_id)
    assert current is not None
    assert current.id == returned.id, "the approved state is the session's current saved state"
    return state_from_record(current)


def _wire_file(env: dict[str, Any], state: CompositionState, *, content: str = FILE_TEXT) -> CompositionState:
    blob = blob_tools._create_blob(env, filename=f"prompt-{len(content)}.txt", content=content)
    assert blob.success, blob.to_dict()
    result = blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": "node:classify.options.prompt_template", "blob_id": blob.data["blob_id"]},
        state,
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
    )
    assert result.success, result.to_dict()
    assert result.validation.is_valid, [entry.to_dict() for entry in result.validation.errors]
    return result.updated_state


def _prompt_reviews(state: CompositionState) -> list[str]:
    requirements = state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY]
    return [row["status"] for row in requirements if row["kind"] == "llm_prompt_template"]


def _verdict(summary: ValidationSummary) -> dict[str, Any]:
    """The measured authoring verdict, in the shape the assertions and a red log both read."""
    return {
        "is_valid": summary.is_valid,
        "errors": [entry.error_code for entry in summary.errors],
        "probe_warnings": [entry.error_code for entry in summary.warnings if entry.error_code in _PROBE_CODES],
        "messages": [entry.message for entry in summary.errors],
    }


def _assert_file_prompt_holds_true_guarantees(state: CompositionState, *, stage: str) -> None:
    options = state.nodes[0].options
    assert options["prompt_template"]["mode"] == "inline_content", stage
    verdict = _verdict(state.validate())
    assert verdict == {"is_valid": True, "errors": [], "probe_warnings": [], "messages": []}, (stage, sorted(options), verdict)
    # The true guarantee set, read off the refusal of a field the llm never emits.
    sink = state.outputs[0]
    demanding = replace(
        state,
        outputs=(replace(sink, options={**sink.options, "schema": {"mode": "flexible", "fields": ["label: str", "never_emitted: str"]}}),),
    )
    refusal = _verdict(demanding.validate())
    assert refusal["errors"] == ["sink_contract_violation"], (stage, refusal)
    assert refusal["probe_warnings"] == [], (stage, refusal)
    named = refusal["messages"][0].split("guarantees: [")[1].split("]")[0].split(", ")
    assert {"text", "label"} <= set(named), (stage, named)
    assert "Missing fields: [never_emitted]" in refusal["messages"][0], stage


def _assert_helper_goes_red_on_a_failed_probe(state: CompositionState) -> None:
    """Mutation control: the helper above cannot pass on a node whose probe fails."""
    node = state.nodes[0]
    malformed = {**node.options["prompt_template"], "sha256": "not-a-hash"}
    mutated = replace(state, nodes=(replace(node, options={**node.options, "prompt_template": malformed}),))
    verdict = _verdict(mutated.validate())
    assert verdict["errors"] == ["sink_contract_violation"]
    assert verdict["probe_warnings"] == ["contract_probe_failed"]
    assert "guarantees: [(none)]" in verdict["messages"][0]
    with pytest.raises(AssertionError):
        _assert_file_prompt_holds_true_guarantees(mutated, stage="mutation control")


def _execution_validate(env: dict[str, Any], state: CompositionState) -> Any:
    """Execution validation with the session's real blob bytes.

    Source and sink paths are moved under the session's data directory (the
    path allowlist requires it); node options are validated as given.
    """
    data_dir = Path(env["data_dir"])
    session_id = env["session_id"]
    source_path = data_dir / "blobs" / session_id / "input.csv"
    output_path = data_dir / "outputs" / session_id / "output.jsonl"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("text\nhello\n", encoding="utf-8")
    source = state.sources["source"]
    sink = state.outputs[0]
    placed = replace(
        state,
        sources={"source": replace(source, options={**source.options, "path": str(source_path)})},
        outputs=(replace(sink, options={**sink.options, "path": str(output_path)}),),
    )
    return validate_pipeline_for_trained_operator(
        placed,
        SimpleNamespace(data_dir=data_dir),
        yaml_generator,
        blob_get_metadata=lambda blob_id: _read_blob(env, blob_id)[0],
        blob_get_content=lambda blob_id: _read_blob(env, blob_id),
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_approving_a_file_prompt_card_saves_the_hash_and_the_pipeline_stays_valid(blob_env: dict[str, Any], tmp_path: Path) -> None:
    service = _service(blob_env)
    wired = _wire_file(blob_env, blob_tools._field_declaring_sink_state(tmp_path))
    assert _prompt_reviews(wired) == ["pending"]
    assert HASH_KEY not in wired.nodes[0].options
    _assert_file_prompt_holds_true_guarantees(wired, stage="after wire, card pending")

    approved = await _save_and_approve_prompt_card(service, blob_env, wired)

    assert _prompt_reviews(approved) == ["resolved"]
    saved_hash = approved.nodes[0].options[HASH_KEY]
    assert type(saved_hash) is str and len(saved_hash) == 64, "approval saves the reviewed text's artifact hash on the node"
    _assert_file_prompt_holds_true_guarantees(approved, stage="after the file card is approved")
    _assert_helper_goes_red_on_a_failed_probe(approved)


@pytest.mark.asyncio
async def test_inline_approval_then_file_then_file_approval_stays_valid_at_every_step(blob_env: dict[str, Any], tmp_path: Path) -> None:
    """Inline prompt approved first, replaced by a file, the file card approved, then a second file bound and approved."""
    service = _service(blob_env)
    state = blob_tools._field_declaring_sink_state(tmp_path)
    node = state.nodes[0]
    inline_pending = replace(
        state,
        nodes=(
            replace(
                node, options=_options_with_default_prompt_template_review(node_id="classify", plugin="llm", options=dict(node.options))
            ),
        ),
    )

    inline_approved = await _save_and_approve_prompt_card(service, blob_env, inline_pending)

    assert _prompt_reviews(inline_approved) == ["resolved"]
    inline_hash = inline_approved.nodes[0].options[HASH_KEY]
    assert _verdict(inline_approved.validate())["is_valid"]

    wired = _wire_file(blob_env, inline_approved)

    assert _prompt_reviews(wired) == ["pending"], "the inline approval does not carry over to the file"
    assert HASH_KEY not in wired.nodes[0].options, "binding a file removes the inline approval's hash"
    _assert_file_prompt_holds_true_guarantees(wired, stage="after wire over an approved inline prompt")

    approved = await _save_and_approve_prompt_card(service, blob_env, wired)

    assert _prompt_reviews(approved) == ["resolved"]
    file_hash = approved.nodes[0].options[HASH_KEY]
    assert file_hash != inline_hash
    _assert_file_prompt_holds_true_guarantees(approved, stage="after the file card is approved")

    rewired = _wire_file(blob_env, approved, content=OTHER_FILE_TEXT)

    assert _prompt_reviews(rewired) == ["pending"]
    assert HASH_KEY not in rewired.nodes[0].options
    _assert_file_prompt_holds_true_guarantees(rewired, stage="after a second file is bound over the approved one")

    reapproved = await _save_and_approve_prompt_card(service, blob_env, rewired)

    assert _prompt_reviews(reapproved) == ["resolved"]
    assert reapproved.nodes[0].options[HASH_KEY] not in (inline_hash, file_hash)
    _assert_file_prompt_holds_true_guarantees(reapproved, stage="after the second file card is approved")


@pytest.mark.asyncio
async def test_execution_validation_refuses_an_approved_file_prompt_re_pointed_without_review(
    blob_env: dict[str, Any], tmp_path: Path
) -> None:
    """What the probe no longer reads off the hash is guarded at run time by the review, not by the saved hash.

    The saved hash has no authority for a file prompt at execution
    (materialization drops it), so this is the refusal that matters: the
    resolved review attests one file, and a marker pointing anywhere else is
    not admitted.
    """
    service = _service(blob_env)
    state = blob_tools._field_declaring_sink_state(tmp_path)
    node = state.nodes[0]
    # The model-choice review is not what is under test; stage it resolved, as
    # tests/unit/web/execution/test_validate_blob_inline.py does, so the review
    # gate's verdict is about the prompt.
    model_choice = {
        "id": "model_choice_review:classify",
        "kind": "llm_model_choice",
        "user_term": "llm_model_choice:classify",
        "status": "resolved",
        "draft": "openai/gpt-4o",
        "event_id": "model-choice-accepted",
        "accepted_value": "openai/gpt-4o",
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": stable_hash("openai/gpt-4o"),
    }
    state = replace(state, nodes=(replace(node, options={**node.options, INTERPRETATION_REQUIREMENTS_KEY: [model_choice]}),))
    approved = await _save_and_approve_prompt_card(service, blob_env, _wire_file(blob_env, state))
    other = blob_tools._create_blob(blob_env, filename="unreviewed.txt", content=OTHER_FILE_TEXT)
    assert other.success, other.to_dict()
    other_record, _bytes = _read_blob(blob_env, UUID(other.data["blob_id"]))
    node = approved.nodes[0]
    re_pointed_marker = {"blob_ref": other.data["blob_id"], "mode": "inline_content", "sha256": other_record.content_hash}
    re_pointed = replace(approved, nodes=(replace(node, options={**node.options, "prompt_template": re_pointed_marker}),))

    admitted = _execution_validate(blob_env, approved)
    refused = _execution_validate(blob_env, re_pointed)

    admitted_checks = {check.name: check.passed for check in admitted.checks}
    assert admitted_checks["interpretation_review"] is True, [error.message for error in admitted.errors]
    assert admitted_checks["blob_inline_refs"] is True, [error.message for error in admitted.errors]
    assert admitted_checks["plugin_instantiation"] is True, [error.message for error in admitted.errors]
    # The probe is blind to the hash and to the re-point alike.
    assert _verdict(re_pointed.validate())["is_valid"]
    assert not refused.is_valid
    assert not refused.readiness.execution_ready
    assert {check.name: check.passed for check in refused.checks}["interpretation_review"] is False


def test_editor_save_stamps_the_hash_and_the_probe_still_computes_guarantees(tmp_path: Path) -> None:
    """The second stamp site: the owner saves the bound file in the node inspector."""
    client, service, _blob_service, session_id, uploaded, _second, _events = prompt_editor._live_prompt_setup(tmp_path)
    saved = prompt_editor._save(client, session_id, uploaded["id"], "Owner edited prompt", uploaded["content_hash"])
    assert saved.status_code == 200, saved.text
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    state = state_from_record(record)
    node = state.nodes[0]
    assert node.options["prompt_template"]["mode"] == "inline_content"
    saved_hash = node.options[HASH_KEY]
    assert type(saved_hash) is str and len(saved_hash) == 64, "editor save stamps the artifact hash on the node"
    # The editor fixture's sink is observed; require the llm's own field so the producer vote decides.
    sink = state.outputs[0]
    demanding = replace(
        state, outputs=(replace(sink, options={**sink.options, "schema": {"mode": "observed", "required_fields": ["llm_response"]}}),)
    )

    summary = demanding.validate()

    codes = [entry.error_code for entry in (*summary.errors, *summary.warnings)]
    assert "contract_probe_failed" not in codes, [entry.message for entry in summary.warnings]
    assert "contract_probe_deferred" not in codes
    assert "sink_contract_violation" not in codes, [entry.message for entry in summary.errors]
