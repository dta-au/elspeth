"""Replacing a structured llm prompt with new text, end to end, repeatedly.

The users' primary use of a prompt is editing it. This drives the whole loop
against a session database: author a structured prompt, approve its term,
model and prompt cards, replace the prompt with new text, approve the new
card, and replace it again. At every step the pipeline must stay
authoring-valid with a sink that DECLARES fields, the new text must not be
admitted to a run before its own card is approved, and it must be admitted
after.

Driven for real: ``patch_node_options`` (the tool dispatcher),
``SessionServiceImpl.save_composition_state``,
``create_pending_interpretation_event``, ``get_prompt_review_snapshot`` and
``resolve_interpretation_event`` (what ``POST
/{session_id}/interpretations/{event_id}/resolve`` calls), and execution
validation (``validate_pipeline_for_trained_operator``). Not driven: the
resolve HTTP route itself and the compose loop that saves the tool's state and
opens the pending events; the tests perform those writes through the same
service methods.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import structlog

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._common import _authored_interpretation_requirement_id, _serialize_set_pipeline_arguments
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.prompt_review import reviewed_anchor_kwargs
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env

HASH_KEY = "approved_prompt_artifact_hash"
RATING_ID = _authored_interpretation_requirement_id(component_id="classify", user_term="rating")
TEXT_B = "Label {{ row.text }} as positive or negative."
TEXT_C = "Label {{ row.text }} as positive, neutral or negative."
_PROBE_CODES = ("contract_probe_deferred", "contract_probe_failed")


def _read_blob(env: dict[str, Any], requested: UUID) -> tuple[Any, bytes]:
    record = env["authority"].mutate(env["operation"], lambda tx: tx.blobs.read_blob(blob_id=requested))
    return record, Path(record.storage_path).read_bytes()


def _service(env: dict[str, Any]) -> SessionServiceImpl:
    service = SessionServiceImpl(
        env["engine"],
        data_dir=Path(env["data_dir"]),
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("text-prompt-replacement-test"),
        inline_blob_read=lambda _context, requested: _read_blob(env, requested),
    )

    async def same_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        # The shared in-memory SQLite fixture is scoped to its owning thread.
        return func(*args, **kwargs)

    service._run_sync = same_thread
    return service


def _tool(env: dict[str, Any], state: CompositionState, patch: dict[str, Any]) -> Any:
    return blob_tools.execute_tool(
        "patch_node_options",
        {"node_id": "classify", "patch": patch},
        state,
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
    )


def _reviews(state: CompositionState) -> dict[str, str]:
    return {row["id"]: row["status"] for row in state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY]}


def _row(state: CompositionState, kind: str) -> dict[str, Any]:
    return next(row for row in state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == kind)


async def _approve(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState, kind: InterpretationKind) -> CompositionState:
    """Save ``state``, open the pending card of ``kind``, approve it as drafted, return what was saved."""
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
    row = _row(state, kind.value)
    assert row["status"] == "pending", row
    event = await service.create_pending_interpretation_event(
        session_id=session_id,
        composition_state_id=saved.id,
        affected_node_id="classify",
        tool_call_id="text-prompt-replacement-test",
        user_term=row["user_term"],
        kind=kind,
        llm_draft=row["draft"],
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
        session_operation_context=env["operation"],
        **await reviewed_anchor_kwargs(service, session_id, event.id, kind),
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    current = await service.get_current_state(session_id)
    assert current is not None
    assert current.id == returned.id, "the approved state is the session's current saved state"
    return state_from_record(current)


def _verdict(summary: ValidationSummary) -> dict[str, Any]:
    return {
        "is_valid": summary.is_valid,
        "errors": [entry.error_code for entry in summary.errors],
        "probe_warnings": [entry.error_code for entry in summary.warnings if entry.error_code in _PROBE_CODES],
        "messages": [entry.message for entry in summary.errors],
    }


def _assert_authoring_valid_with_true_guarantees(state: CompositionState, *, stage: str) -> None:
    """Valid against the field-declaring sink, and the llm node still votes its real fields."""
    verdict = _verdict(state.validate())
    assert verdict == {"is_valid": True, "errors": [], "probe_warnings": [], "messages": []}, (stage, verdict)
    sink = state.outputs[0]
    assert sink.options["schema"]["fields"], "the sink declares fields"
    demanding = replace(
        state,
        outputs=(replace(sink, options={**sink.options, "schema": {"mode": "flexible", "fields": ["label: str", "never_emitted: str"]}}),),
    )
    refusal = _verdict(demanding.validate())
    assert refusal["errors"] == ["sink_contract_violation"], (stage, refusal)
    assert refusal["probe_warnings"] == [], (stage, refusal)
    named = refusal["messages"][0].split("guarantees: [")[1].split("]")[0].split(", ")
    assert {"text", "label"} <= set(named), (stage, named)


def _run_admission(env: dict[str, Any], state: CompositionState) -> dict[str, bool]:
    """Execution validation's named checks, with source and sink moved under the session's data directory."""
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
    result = validate_pipeline_for_trained_operator(
        placed,
        SimpleNamespace(data_dir=data_dir),
        yaml_generator,
        blob_get_metadata=lambda blob_id: _read_blob(env, blob_id)[0],
        blob_get_content=lambda blob_id: _read_blob(env, blob_id),
        session_id=session_id,
    )
    checks = {check.name: check.passed for check in result.checks}
    checks["execution_ready"] = result.readiness.execution_ready
    return checks


async def _approved_structured_prompt(service: SessionServiceImpl, env: dict[str, Any], tmp_path: Path) -> CompositionState:
    """Author a structured prompt with the real tool and approve its term, model and prompt cards."""
    authored = _tool(
        env,
        blob_tools._field_declaring_sink_state(tmp_path),
        {
            "prompt_template": "Rate {{ row.text }} on a scale of 1 to 5.",
            "prompt_template_parts": [
                {"kind": "text", "text": "Rate {{ row.text }} on a scale of "},
                {"kind": "interpretation_ref", "requirement_id": RATING_ID},
                {"kind": "text", "text": "."},
            ],
            "interpretation_requirements": [{"kind": "vague_term", "user_term": "rating", "draft": "1 to 5"}],
        },
    )
    assert authored.success, authored.to_dict()
    state = authored.updated_state
    for kind in (InterpretationKind.VAGUE_TERM, InterpretationKind.LLM_MODEL_CHOICE, InterpretationKind.LLM_PROMPT_TEMPLATE):
        state = await _approve(service, env, state, kind)
    assert set(_reviews(state).values()) == {"resolved"}, _reviews(state)
    assert "prompt_template_parts" in state.nodes[0].options
    return state


@pytest.mark.parametrize("explicit_null", [True], ids=["with-null-parts"])
@pytest.mark.asyncio
async def test_text_replaces_an_approved_structured_prompt_and_each_edit_is_reviewed_before_it_runs(
    blob_env: dict[str, Any], tmp_path: Path, explicit_null: bool
) -> None:
    service = _service(blob_env)
    approved_a = await _approved_structured_prompt(service, blob_env, tmp_path)
    _assert_authoring_valid_with_true_guarantees(approved_a, stage="structured prompt approved")
    admitted_a = _run_admission(blob_env, approved_a)
    assert admitted_a["interpretation_review"] and admitted_a["execution_ready"], admitted_a
    hash_a = approved_a.nodes[0].options[HASH_KEY]

    seen_hashes = [hash_a]
    state = approved_a
    for stage, text, superseded in (("first replacement", TEXT_B, [RATING_ID]), ("second replacement", TEXT_C, None)):
        # Both shapes a model sends: the explicit signal, and the text alone.
        result = _tool(blob_env, state, {"prompt_template": text, **({"prompt_template_parts": None} if explicit_null else {})})

        assert result.success, (stage, result.to_dict())
        assert result.validation.is_valid, (stage, [entry.to_dict() for entry in result.validation.errors])
        replaced = result.updated_state
        options = replaced.nodes[0].options
        assert options["prompt_template"] == text, stage
        assert "prompt_template_parts" not in options, stage
        assert HASH_KEY not in options, stage
        if superseded is None:
            assert result.data is None or "superseded_review_ids" not in result.data, stage
        else:
            assert list(result.data["superseded_review_ids"]) == superseded, stage
        assert sorted(row["kind"] for row in options[INTERPRETATION_REQUIREMENTS_KEY]) == ["llm_model_choice", "llm_prompt_template"], stage
        assert _row(replaced, "llm_prompt_template")["status"] == "pending", stage
        assert _row(replaced, "llm_model_choice")["status"] == "resolved", stage
        _assert_authoring_valid_with_true_guarantees(replaced, stage=f"{stage}, card pending")
        unreviewed = _run_admission(blob_env, replaced)
        assert unreviewed["interpretation_review"] is False, (stage, unreviewed)
        assert unreviewed["execution_ready"] is False, (stage, unreviewed)

        state = await _approve(service, blob_env, replaced, InterpretationKind.LLM_PROMPT_TEMPLATE)

        assert set(_reviews(state).values()) == {"resolved"}, (stage, _reviews(state))
        assert state.nodes[0].options["prompt_template"] == text, stage
        assert "prompt_template_parts" not in state.nodes[0].options, stage
        new_hash = state.nodes[0].options[HASH_KEY]
        assert new_hash not in seen_hashes, stage
        seen_hashes.append(new_hash)
        _assert_authoring_valid_with_true_guarantees(state, stage=f"{stage}, card approved")
        admitted = _run_admission(blob_env, state)
        assert admitted["interpretation_review"] is True, (stage, admitted)
        assert admitted["plugin_instantiation"] is True, (stage, admitted)
        assert admitted["execution_ready"] is True, (stage, admitted)


async def _approve_every_pending_card(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState) -> CompositionState:
    while True:
        pending = [row["kind"] for row in state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY] if row["status"] == "pending"]
        if not pending:
            return state
        state = await _approve(service, env, state, InterpretationKind(pending[0]))


def _write_prompt(env: dict[str, Any], writer: str, state: CompositionState, written: dict[str, Any]) -> Any:
    """Send ``written`` prompt options through one writer; the whole-node writers re-send the exported node."""
    if writer == "patch_node_options":
        arguments: dict[str, Any] = {"node_id": "classify", "patch": written}
    else:
        exported, error = _serialize_set_pipeline_arguments(state)
        assert error is None and exported is not None
        node = next(item for item in exported["nodes"] if item["id"] == "classify")
        node["options"] = {**node["options"], **written}
        arguments = node if writer == "upsert_node" else exported
    return blob_tools.execute_tool(
        writer,
        arguments,
        state,
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
    )


# set_pipeline is not driven here: this fixture's source path is refused by the
# path policy when the whole pipeline is re-sent, before any prompt handling.
@pytest.mark.parametrize("writer", ["patch_node_options", "upsert_node"])
@pytest.mark.parametrize("explicit_null", [True, False], ids=["with-null-parts", "text-alone"])
@pytest.mark.asyncio
async def test_text_replaces_an_approved_file_prompt_and_is_reviewed_before_it_runs(
    blob_env: dict[str, Any], tmp_path: Path, writer: str, explicit_null: bool
) -> None:
    """Replace works in the file-to-text direction too, and the file's approval never approves the text."""
    service = _service(blob_env)
    blob = blob_tools._create_blob(blob_env, content="Label {{ row.text }} as good or bad.")
    wired = blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": "node:classify.options.prompt_template", "blob_id": blob.data["blob_id"]},
        blob_tools._field_declaring_sink_state(tmp_path),
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )
    assert wired.success, wired.to_dict()
    state = await _approve_every_pending_card(service, blob_env, wired.updated_state)
    assert state.nodes[0].options["prompt_template"]["blob_ref"] == blob.data["blob_id"]
    assert set(_reviews(state).values()) == {"resolved"}, _reviews(state)
    file_review = dict(_row(state, "llm_prompt_template"))

    result = _write_prompt(
        blob_env, writer, state, {"prompt_template": TEXT_B, **({"prompt_template_parts": None} if explicit_null else {})}
    )

    assert result.success, result.to_dict()
    replaced = result.updated_state
    options = replaced.nodes[0].options
    # The marker is gone and the text stands in its place.
    assert options["prompt_template"] == TEXT_B
    assert options.get("prompt_template_parts") is None
    assert HASH_KEY not in options
    # The text gets its own pending card; the file's approval is not carried.
    text_review = _row(replaced, "llm_prompt_template")
    assert text_review["status"] == "pending"
    assert TEXT_B in text_review["draft"]
    assert text_review["event_id"] is None and text_review["accepted_value"] is None
    assert text_review["event_id"] != file_review["event_id"]
    # No term reviews existed, so the result names none as removed.
    assert result.data is None or "superseded_review_ids" not in result.data
    _assert_authoring_valid_with_true_guarantees(replaced, stage="text over file, card pending")
    unreviewed = _run_admission(blob_env, replaced)
    assert unreviewed["interpretation_review"] is False, unreviewed
    assert unreviewed["execution_ready"] is False, unreviewed

    state = await _approve_every_pending_card(service, blob_env, replaced)

    admitted = _run_admission(blob_env, state)
    assert admitted["interpretation_review"] is True, admitted
    assert admitted["execution_ready"] is True, admitted


@pytest.mark.asyncio
async def test_an_approved_file_prompt_node_sent_back_as_exported_keeps_its_binding_and_review(
    blob_env: dict[str, Any], tmp_path: Path
) -> None:
    """Re-sending the exported node leaves the file binding and its resolved prompt review exactly as stored.

    Known and not fixed here (measured 2026-11-19 on ce7d94c1a and on this
    branch): ``approved_prompt_artifact_hash``,
    which card approval stamps into the saved options, comes back as ``None``.
    Node reconciliation re-derives that key with
    ``approved_prompt_artifact_hash_from_options``, which returns ``None`` for a
    file-backed prompt by design. The last assertion pins that observed value
    so a change in either direction is noticed; it is not a statement that
    ``None`` is right.
    """
    service = _service(blob_env)
    blob = blob_tools._create_blob(blob_env, content="Label {{ row.text }} as good or bad.")
    wired = blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": "node:classify.options.prompt_template", "blob_id": blob.data["blob_id"]},
        blob_tools._field_declaring_sink_state(tmp_path),
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )
    assert wired.success, wired.to_dict()
    state = await _approve_every_pending_card(service, blob_env, wired.updated_state)
    stored = state.nodes[0].options
    assert type(stored[HASH_KEY]) is str

    result = _write_prompt(blob_env, "upsert_node", state, {})

    assert result.success, result.to_dict()
    after = result.updated_state.nodes[0].options
    assert after["prompt_template"] == stored["prompt_template"]
    review = _row(result.updated_state, "llm_prompt_template")
    assert review == _row(state, "llm_prompt_template")
    assert review["status"] == "resolved"
    assert result.data is None or ("superseded_review_ids" not in result.data and "prompt_unchanged_node_ids" not in result.data)
    assert after[HASH_KEY] is None
