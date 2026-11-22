"""Is an approved file-backed prompt node still admitted to run after it is re-sent unchanged?

Card approval stamps ``approved_prompt_artifact_hash`` into the saved node
options. Re-sending the node unchanged (``upsert_node`` with the node as
``get_pipeline_state`` exports it, or ``set_pipeline`` with
``get_pipeline_state(component="set_pipeline_arguments")``) leaves the file
binding and the resolved prompt review as stored but brings that key back as
``None`` (measured by the text-replacement builder on 5169104f3).
This file answers by execution whether that changes run admission.

"Admission" here is the two gates a run of a file-backed prompt must pass:

* execution validation (``validate_pipeline_for_trained_operator``): its
  ``interpretation_review`` check and ``readiness.execution_ready``; and
* the prompt-file approval gate the execution service applies at approval
  compile and at run start (``ExecutionServiceImpl._assert_state_prompt_file_approvals``):
  ``resolve_prompt_review_snapshot`` over the node's options and the file's
  current bytes, then ``SessionServiceImpl.assert_prompt_file_approvals`` against
  the session database. The service method only adds the blob reads; this
  file reads the same custody bytes directly.

Also compared: the node as ``materialize_state_for_execution`` hands it to the
YAML generator, before and after the re-send.

Controls: a bound file prompt whose prompt card is open (pending) must be
refused by the same checks, and so must an approved one whose file bytes
changed after approval.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import structlog

import tests.unit.web.execution.test_file_prompt_approved_artifact_hash as landed
from elspeth.contracts.blobs import BlobIntegrityError
from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale, resolve_prompt_review_snapshot
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.prompt_review import reviewed_anchor_kwargs
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

blob_env = blob_tools.blob_env

HASH_KEY = "approved_prompt_artifact_hash"
PROMPT_TEXT = "Label {{ row.text }} as good or bad."


# Session helpers, the same as tests/unit/web/sessions/test_text_prompt_replacement_keeps_guarantees.py
# (copied so this file also collects on trees that do not have that module).
def _read_blob(env: dict[str, Any], requested: UUID) -> tuple[Any, bytes]:
    record = env["authority"].mutate(env["operation"], lambda tx: tx.blobs.read_blob(blob_id=requested))
    return record, Path(record.storage_path).read_bytes()


def _service(env: dict[str, Any]) -> SessionServiceImpl:
    service = SessionServiceImpl(
        env["engine"],
        data_dir=Path(env["data_dir"]),
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("file-prompt-admission-test"),
        inline_blob_read=lambda _context, requested: _read_blob(env, requested),
    )

    async def same_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        # The shared in-memory SQLite fixture is scoped to its owning thread.
        return func(*args, **kwargs)

    service._run_sync = same_thread
    return service


def _reviews(state: CompositionState) -> dict[str, str]:
    return {row["id"]: row["status"] for row in state.nodes[0].options["interpretation_requirements"]}


def _row(state: CompositionState, kind: str) -> dict[str, Any]:
    return next(row for row in state.nodes[0].options["interpretation_requirements"] if row["kind"] == kind)


async def _approve(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState, kind: InterpretationKind) -> CompositionState:
    """Save ``state``, open the pending card of ``kind``, approve it as drafted, return what was saved."""
    session_id = UUID(env["session_id"])
    event = await _open_card(service, env, state, kind)
    await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        session_operation_context=env["operation"],
        **await reviewed_anchor_kwargs(service, session_id, event.id, kind),
    )
    current = await service.get_current_state(session_id)
    assert current is not None
    return state_from_record(current)


async def _approve_every_pending_card(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState) -> CompositionState:
    while True:
        pending = [row["kind"] for row in state.nodes[0].options["interpretation_requirements"] if row["status"] == "pending"]
        if not pending:
            return state
        state = await _approve(service, env, state, InterpretationKind(pending[0]))


def _placed_state(env: dict[str, Any], tmp_path: Path) -> CompositionState:
    """The field-declaring fixture with a session-uploaded CSV source and a session-owned sink.

    The source is bound to an uploaded blob with ``set_source_from_blob`` (a
    path source must live under ``blobs/<session>``, and such a path cannot
    round-trip through ``set_pipeline`` without its blob identity), so the
    whole pipeline can be exported and re-sent, and execution validation reads
    a real source file.
    """
    data_dir = Path(env["data_dir"])
    output_path = data_dir / "outputs" / env["session_id"] / "output.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state = blob_tools._field_declaring_sink_state(tmp_path)
    sink = state.outputs[0]
    # set_pipeline re-validates every sink: a file sink must name its collision policy.
    sink_options = {**sink.options, "path": str(output_path), "mode": "write", "collision_policy": "auto_increment"}
    state = replace(state, outputs=(replace(sink, options=sink_options),))
    uploaded = blob_tools._create_blob(env, filename="input.csv", mime_type="text/csv", content="text\nhello\n")
    assert uploaded.success, uploaded.to_dict()
    bound = _tool(
        env,
        "set_source_from_blob",
        {
            "blob_id": uploaded.data["blob_id"],
            "on_success": "rows",
            "on_validation_failure": "discard",
            "options": {"schema": dict(deep_thaw(state.sources["source"].options["schema"]))},
        },
        state,
    )
    assert bound.success, bound.to_dict()
    return bound.updated_state


def _tool(env: dict[str, Any], name: str, arguments: dict[str, Any], state: CompositionState) -> Any:
    return blob_tools.execute_tool(
        name,
        arguments,
        state,
        blob_tools._catalog(),
        data_dir=env["data_dir"],
        session_engine=env["engine"],
        session_id=env["session_id"],
        session_operation_context=env["operation"],
        session_operation_authority=env["authority"],
    )


async def _save(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState) -> CompositionState:
    """Save ``state`` as the compose loop saves a tool's result; return the stored revision."""
    document = state.to_dict()
    await service.save_composition_state(
        UUID(env["session_id"]),
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
    current = await service.get_current_state(UUID(env["session_id"]))
    assert current is not None
    return state_from_record(current)


async def _open_card(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState, kind: InterpretationKind) -> Any:
    """Save ``state`` and open the pending event for its row of ``kind``, as the compose loop does."""
    session_id = UUID(env["session_id"])
    saved = await _save(service, env, state)
    current = await service.get_current_state(session_id)
    assert current is not None
    row = _row(saved, kind.value)
    assert row["status"] == "pending", row
    return await service.create_pending_interpretation_event(
        session_id=session_id,
        composition_state_id=current.id,
        affected_node_id="classify",
        tool_call_id="file-prompt-admission-test",
        user_term=row["user_term"],
        kind=kind,
        llm_draft=row["draft"],
        model_identifier="test-model",
        model_version="1",
        provider="test-provider",
        composer_skill_hash="a" * 64,
        session_operation_context=env["operation"],
    )


async def _bound_file_prompt(service: SessionServiceImpl, env: dict[str, Any], tmp_path: Path) -> tuple[CompositionState, str]:
    """Bind a session file as ``classify``'s prompt with the real tool; return the tool's state and the blob id."""
    # The node is authored the way the composer authors one, with upsert_node:
    # that stages its llm_model_choice review. wire_blob_inline_ref alone
    # stages only the prompt review, and the hand-built fixture node carries
    # no rows, so without this step the model card is never opened and every
    # run is refused for it.
    placed = _placed_state(env, tmp_path)
    authored = _tool(env, "upsert_node", _resend_arguments(env, "upsert_node", placed), placed)
    assert authored.success, authored.to_dict()
    assert "llm_model_choice" in {row["kind"] for row in authored.updated_state.nodes[0].options["interpretation_requirements"]}
    blob = blob_tools._create_blob(env, content=PROMPT_TEXT)
    wired = _tool(
        env,
        "wire_blob_inline_ref",
        {"field_path": "node:classify.options.prompt_template", "blob_id": blob.data["blob_id"]},
        authored.updated_state,
    )
    assert wired.success, wired.to_dict()
    return wired.updated_state, blob.data["blob_id"]


async def _approved_file_prompt(service: SessionServiceImpl, env: dict[str, Any], tmp_path: Path) -> tuple[CompositionState, str]:
    state, blob_id = await _bound_file_prompt(service, env, tmp_path)
    state = await _approve_every_pending_card(service, env, state)
    assert set(_reviews(state).values()) == {"resolved"}, _reviews(state)
    assert state.nodes[0].options["prompt_template"]["blob_ref"] == blob_id
    return state, blob_id


async def _admission(service: SessionServiceImpl, env: dict[str, Any], state: CompositionState) -> dict[str, Any]:
    """Both admission gates for ``state``, each reported as a verdict, never raised."""
    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=Path(env["data_dir"])),
        yaml_generator,
        blob_get_metadata=lambda blob_id: _read_blob(env, blob_id)[0],
        blob_get_content=lambda blob_id: _read_blob(env, blob_id),
        session_id=env["session_id"],
    )
    checks = {check.name: check.passed for check in result.checks}
    verdict: dict[str, Any] = {
        "interpretation_review": checks["interpretation_review"],
        "execution_ready": result.readiness.execution_ready,
        "validation_errors": [error.message for error in result.errors],
    }
    node = state.nodes[0]
    try:
        snapshot = resolve_prompt_review_snapshot(
            session_id=UUID(env["session_id"]),
            node_id=node.id,
            options=node.options,
            read_blob=lambda blob_id: _read_blob(env, blob_id),
        )
        assert snapshot is not None, "a file-bound node always yields a snapshot"
        await service.assert_prompt_file_approvals(snapshot)
        verdict["file_approval_gate"] = "admitted"
    except PromptFileReviewStale as exc:
        verdict["file_approval_gate"] = f"refused {exc.error_code}: {exc}"
    return verdict


def _admitted(verdict: dict[str, Any]) -> bool:
    return verdict["interpretation_review"] is True and verdict["execution_ready"] is True and verdict["file_approval_gate"] == "admitted"


def _materialized_node_options(state: CompositionState) -> dict[str, Any]:
    materialized = materialize_state_for_execution(state)
    assert not isinstance(materialized, InterpretationReviewPending), materialized
    return dict(deep_thaw(materialized.nodes[0].options))


def _resend_arguments(env: dict[str, Any], writer: str, state: CompositionState) -> dict[str, Any]:
    """What the server's export hands the model to send back unchanged."""
    exported = _tool(env, "get_pipeline_state", {"component": "set_pipeline_arguments"}, state)
    assert exported.success, exported.to_dict()
    arguments = dict(deep_thaw(exported.data))
    if writer == "set_pipeline":
        return arguments
    (node,) = [item for item in arguments["nodes"] if item["id"] == "classify"]
    return dict(node)


@pytest.mark.parametrize("writer", ["upsert_node", "set_pipeline"])
@pytest.mark.asyncio
async def test_approved_file_prompt_is_admitted_before_and_after_an_unchanged_resend(
    blob_env: dict[str, Any], tmp_path: Path, writer: str
) -> None:
    service = _service(blob_env)
    approved, blob_id = await _approved_file_prompt(service, blob_env, tmp_path)
    before_review = dict(_row(approved, "llm_prompt_template"))
    before_hash = approved.nodes[0].options[HASH_KEY]

    # (a) approved: admitted.
    before = await _admission(service, blob_env, approved)
    print(f"[{writer}] (a) approved, before re-send: {HASH_KEY}={before_hash!r} admission={before}")
    assert _admitted(before), before

    # (b) re-sent unchanged, exactly as exported, then saved as the compose loop saves it.
    result = _tool(blob_env, writer, _resend_arguments(blob_env, writer, approved), approved)
    assert result.success, result.to_dict()
    resent = await _save(service, blob_env, result.updated_state)
    after_options = resent.nodes[0].options
    after_hash = after_options[HASH_KEY] if HASH_KEY in after_options else "<absent>"
    assert after_options["prompt_template"] == approved.nodes[0].options["prompt_template"]
    assert after_options["prompt_template"]["blob_ref"] == blob_id
    assert _row(resent, "llm_prompt_template") == before_review
    assert set(_reviews(resent).values()) == {"resolved"}, _reviews(resent)

    after = await _admission(service, blob_env, resent)
    print(f"[{writer}] (b) re-sent unchanged: {HASH_KEY} {before_hash!r} -> {after_hash!r} admission={after}")
    assert after == before
    assert _admitted(after), after

    # What execution builds from the node is the same either side of the re-send.
    materialized_before = _materialized_node_options(approved)
    materialized_after = _materialized_node_options(resent)
    print(f"[{writer}] materialized {HASH_KEY} present: before={HASH_KEY in materialized_before} after={HASH_KEY in materialized_after}")
    assert materialized_after == materialized_before
    assert HASH_KEY not in materialized_before


@pytest.mark.asyncio
async def test_control_bound_file_prompt_with_its_card_pending_is_refused(blob_env: dict[str, Any], tmp_path: Path) -> None:
    """(c) The same checks refuse a bound file prompt nobody has approved."""
    service = _service(blob_env)
    state, _blob_id = await _bound_file_prompt(service, blob_env, tmp_path)
    # Every other card approved, so the prompt card is the only thing missing.
    while True:
        pending = [
            row["kind"]
            for row in state.nodes[0].options["interpretation_requirements"]
            if row["status"] == "pending" and row["kind"] != "llm_prompt_template"
        ]
        if not pending:
            break
        state = await _approve(service, blob_env, state, InterpretationKind(pending[0]))
    await _open_card(service, blob_env, state, InterpretationKind.LLM_PROMPT_TEMPLATE)
    current = await service.get_current_state(UUID(blob_env["session_id"]))
    assert current is not None
    state = state_from_record(current)
    assert _row(state, "llm_prompt_template")["status"] == "pending"

    verdict = await _admission(service, blob_env, state)
    print(f"(c) pending card: admission={verdict}")
    assert verdict["interpretation_review"] is False, verdict
    assert verdict["execution_ready"] is False, verdict
    assert verdict["file_approval_gate"].startswith("refused prompt_file_review_stale"), verdict


@pytest.mark.parametrize("resent_first", [False, True], ids=["as-approved", "after-upsert-resend"])
@pytest.mark.asyncio
async def test_control_approved_file_prompt_whose_bytes_changed_is_refused(
    blob_env: dict[str, Any], tmp_path: Path, resent_first: bool
) -> None:
    """(c2) Approved, then the file's stored bytes change: the file approval gate refuses."""
    service = _service(blob_env)
    state, blob_id = await _approved_file_prompt(service, blob_env, tmp_path)
    if resent_first:
        result = _tool(blob_env, "upsert_node", _resend_arguments(blob_env, "upsert_node", state), state)
        assert result.success, result.to_dict()
        state = await _save(service, blob_env, result.updated_state)
    assert _admitted(await _admission(service, blob_env, state))

    record, _content = _read_blob(blob_env, UUID(blob_id))
    Path(record.storage_path).write_bytes(b"Label {{ row.text }} as anything you like.")

    verdict = await _admission(service, blob_env, state)
    print(f"(c2 resent_first={resent_first}) bytes changed: admission={verdict}")
    assert verdict["file_approval_gate"].startswith("refused prompt_file_review_stale"), verdict


@pytest.mark.asyncio
async def test_control_card_resolution_is_what_writes_the_file_approval(blob_env: dict[str, Any], tmp_path: Path) -> None:
    """Approving the prompt card through the service is what turns the file gate from refused to admitted."""
    service = _service(blob_env)
    state, _blob_id = await _bound_file_prompt(service, blob_env, tmp_path)
    while True:
        pending = [
            row["kind"]
            for row in state.nodes[0].options["interpretation_requirements"]
            if row["status"] == "pending" and row["kind"] != "llm_prompt_template"
        ]
        if not pending:
            break
        state = await _approve(service, blob_env, state, InterpretationKind(pending[0]))
    event = await _open_card(service, blob_env, state, InterpretationKind.LLM_PROMPT_TEMPLATE)
    session_id = UUID(blob_env["session_id"])
    pending_state = state_from_record(await service.get_current_state(session_id))
    refused = await _admission(service, blob_env, pending_state)
    assert not _admitted(refused), refused

    await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        session_operation_context=blob_env["operation"],
        **await reviewed_anchor_kwargs(service, session_id, event.id, InterpretationKind.LLM_PROMPT_TEMPLATE),
    )
    approved = state_from_record(await service.get_current_state(session_id))
    admitted = await _admission(service, blob_env, approved)
    print(f"(c3) pending -> approved: refused={refused} admitted={admitted}")
    assert _admitted(admitted), admitted


# ---------------------------------------------------------------------------
# The same question on the landed execution harness, with the REAL preflight.
#
# Adapted from an earlier admission probe. Changed:
# * The source and sink are placed under the execution service's own data
#   directory, so its real ``validate_state`` (``_authoritative_state_preflight``:
#   validate_pipeline, then ``_assert_state_prompt_file_approvals``, then proof
#   diagnostics; what /validate runs) can run instead of being replaced by a
#   canned "valid" result as ``landed._compile_frozen`` does.
# * The marker, review rows and admitted snapshot are asserted, not only noted.
# * Controls are refused by the file-approval gate itself (PromptFileReviewStale),
#   not only by the pending-review materializer.
# ---------------------------------------------------------------------------

execution_fixture = landed.execution_fixture


def _wire_real_collaborators(harness: Any, approved: Any) -> ExecutionServiceImpl:
    """Point the landed execution service at the live session DB and custody, as ``landed._compile_frozen`` does."""
    service = cast(Any, harness.service)
    service._session_service = approved.service
    service._blob_service = approved.blob_service
    service._yaml_generator = yaml_generator
    service._call_async = asyncio.run
    return cast(ExecutionServiceImpl, service)


def _place_under_service_data_dir(harness: Any, approved: Any) -> tuple[Path, Path]:
    """Move the approved revision's source and sink under the execution service's session directories."""
    data_dir = Path(harness.service._settings.data_dir)
    source_path = data_dir / "blobs" / approved.session_id / "input.csv"
    output_path = data_dir / "outputs" / approved.session_id / "output.jsonl"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("text\nhello\n")
    data = state_from_record(approved.record).to_dict()
    for source in data["sources"].values():
        source["options"]["path"] = str(source_path)
    for output in data["outputs"]:
        output["options"].update(path=str(output_path), mode="write", collision_policy="fail_if_exists")
    approved.record = landed._save_state(approved, data)
    return source_path, output_path


def _route_validate(service: ExecutionServiceImpl, approved: Any, state_id: UUID) -> dict[str, Any]:
    """The /validate preflight, unpatched, on revision ``state_id``."""
    record = asyncio.run(approved.service.get_state(state_id))
    with fenced_operation_context(approved.blob_service._engine, approved.session_id) as operation:
        result = asyncio.run(
            service.validate_state(
                state_from_record(record),
                session_operation_context=operation,
                user_id="alice",
                session_id=UUID(approved.session_id),
            )
        )
    return {
        "is_valid": result.is_valid,
        "execution_ready": result.readiness.execution_ready,
        "errors": [(error.error_code, error.message) for error in result.errors],
    }


def _file_gate(service: ExecutionServiceImpl, approved: Any, state_id: UUID) -> Any:
    """The real ``_assert_state_prompt_file_approvals`` on revision ``state_id``."""
    record = asyncio.run(approved.service.get_state(state_id))
    with fenced_operation_context(approved.blob_service._engine, approved.session_id) as operation:
        return service._assert_state_prompt_file_approvals(
            state_from_record(record), session_id=UUID(approved.session_id), session_operation_context=operation
        )


def _resend_landed(approved: Any, writer: str, pre_move_export: dict[str, Any], paths: tuple[Path, Path], tmp_path: Path) -> Any:
    """Re-send node ``first`` (or the pipeline) unchanged and save it; return the saved revision."""
    source_path, output_path = paths
    moved = json.loads(
        json.dumps(pre_move_export)
        .replace(str(tmp_path / "input.csv"), str(source_path))
        .replace(str(tmp_path / "output.jsonl"), str(output_path))
    )
    for output in moved["outputs"]:
        output["options"].update(mode="write", collision_policy="fail_if_exists")
    arguments = next(node for node in moved["nodes"] if node["id"] == "first") if writer == "upsert_node" else moved
    state = state_from_record(approved.record)
    with fenced_operation_context(approved.blob_service._engine, approved.session_id) as operation:
        result = blob_tools.execute_tool(
            writer,
            arguments,
            state,
            blob_tools._catalog(),
            data_dir=str(Path(source_path).parents[2]),
            session_engine=approved.blob_service._engine,
            session_id=approved.session_id,
            session_operation_context=operation,
            session_operation_authority=approved.service.session_operation_authority,
        )
    assert result.success, result.to_dict()
    return landed._save_state(approved, result.updated_state.to_dict())


def _pre_move_export(approved: Any) -> dict[str, Any]:
    """The server's set_pipeline export of the approved revision, taken while its source is a plain path."""
    exported, error = _serialize_set_pipeline_arguments(state_from_record(approved.record))
    assert error is None and exported is not None
    return dict(deep_thaw(exported))


def _first(state_record: Any) -> dict[str, Any]:
    return dict(deep_thaw(next(node for node in state_from_record(state_record).nodes if node.id == "first").options))


@pytest.mark.parametrize("writer", ["upsert_node", "set_pipeline"])
def test_landed_harness_real_preflight_admits_before_and_after_an_unchanged_resend(
    tmp_path: Path, execution_fixture: ExecutionTestCustody, writer: str
) -> None:
    approved = landed._approved_session(tmp_path, "card")
    # The authoring export refuses a path-only source under blobs/, so it is taken before the move.
    pre_move_export = _pre_move_export(approved)
    harness = landed._harness(tmp_path, execution_fixture)
    try:
        service = _wire_real_collaborators(harness, approved)
        paths = _place_under_service_data_dir(harness, approved)
        before_record = approved.record
        stored_before = _first(before_record)

        validate_before = _route_validate(service, approved, before_record.id)
        (snapshot_before,) = _file_gate(service, approved, before_record.id)
        compiled_before = landed._compile_frozen(harness, approved, before_record.id)
        print(
            f"[{writer}] before: stored {HASH_KEY}={stored_before[HASH_KEY]!r} validate={validate_before} snapshot_hash={snapshot_before.effective_artifact_hash}"
        )
        assert validate_before["is_valid"] and validate_before["execution_ready"], validate_before
        assert compiled_before["binding"] is not None

        resent_record = _resend_landed(approved, writer, pre_move_export, paths, tmp_path)
        assert resent_record.id != before_record.id
        stored_after = _first(resent_record)
        assert stored_after["prompt_template"] == stored_before["prompt_template"]
        assert stored_after["interpretation_requirements"] == stored_before["interpretation_requirements"]

        validate_after = _route_validate(service, approved, resent_record.id)
        (snapshot_after,) = _file_gate(service, approved, resent_record.id)
        compiled_after = landed._compile_frozen(harness, approved, resent_record.id)
        print(
            f"[{writer}] after: stored {HASH_KEY} {stored_before[HASH_KEY]!r} -> {stored_after.get(HASH_KEY, '<absent>')!r} "
            f"validate={validate_after} snapshot_hash={snapshot_after.effective_artifact_hash} "
            f"bound={landed._first_options(compiled_after['config'])[HASH_KEY]}"
        )
        assert validate_after == validate_before
        assert snapshot_after == snapshot_before
        assert compiled_after["binding"] is not None
        assert landed._first_options(compiled_after["config"])[HASH_KEY] == landed._first_options(compiled_before["config"])[HASH_KEY]
        assert landed._first_options(compiled_after["config"])[HASH_KEY] == snapshot_after.effective_artifact_hash
    finally:
        harness.loop.close()


@pytest.mark.parametrize("resent_first", [False, True], ids=["as-approved", "after-upsert-resend"])
def test_landed_harness_changed_file_bytes_are_refused_by_the_file_gate(
    tmp_path: Path, execution_fixture: ExecutionTestCustody, resent_first: bool
) -> None:
    """Control (b): approved, then the stored bytes change. Each gate refuses; the materializer alone would not."""
    approved = landed._approved_session(tmp_path, "card")
    pre_move_export = _pre_move_export(approved)
    harness = landed._harness(tmp_path, execution_fixture)
    try:
        service = _wire_real_collaborators(harness, approved)
        paths = _place_under_service_data_dir(harness, approved)
        if resent_first:
            approved.record = _resend_landed(approved, "upsert_node", pre_move_export, paths, tmp_path)
        record = approved.record
        assert _route_validate(service, approved, record.id)["execution_ready"]

        with approved.blob_service._engine.connect() as conn:
            blob = landed._blob_record(conn, UUID(approved.session_id), approved.blob_id)
        Path(blob.storage_path).write_bytes(b"tampered prompt bytes")

        # The pending-review materializer does not see bytes: it still materializes.
        assert not isinstance(materialize_state_for_execution(state_from_record(record)), InterpretationReviewPending)
        # The /validate preflight reads the prompt blob through custody; the integrity failure is a field violation.
        validated = _route_validate(service, approved, record.id)
        print(f"(b resent_first={resent_first}) validate after bytes changed: {validated}")
        assert validated["is_valid"] is False and validated["execution_ready"] is False
        assert [code for code, _message in validated["errors"]] == ["not_ready_inline_blob_content"]
        message = validated["errors"][0][1]
        assert message.startswith("Inline content blob reference at node:") and message.endswith(
            ".options.prompt_template is not_ready: stored file content does not match its record"
        )
        assert "review" not in message.lower()
        # The file gate reads the same bytes through custody first, which refuses before any approval comparison.
        with pytest.raises(BlobIntegrityError):
            _file_gate(service, approved, record.id)
        # Approval compile (preflight replaced as in the landed harness) reads them again in
        # _approval_inputs_from_frozen, which turns the integrity failure into PromptFileReviewStale.
        with pytest.raises((PromptFileReviewStale, BlobIntegrityError, UnresolvedInterpretationPlaceholderError)) as refused:
            landed._compile_frozen(harness, approved, record.id)
        print(f"(b resent_first={resent_first}) compile refused: {refused.value!r}")
        assert isinstance(refused.value, PromptFileReviewStale)
    finally:
        harness.loop.close()


def test_landed_harness_unapproved_file_is_refused_by_preflight_and_file_gate(
    tmp_path: Path, execution_fixture: ExecutionTestCustody
) -> None:
    """Control (c): an unapproved head, checked at each gate to name which one refuses."""
    approved = landed._approved_session(tmp_path, "card")
    harness = landed._harness(tmp_path, execution_fixture)
    try:
        service = _wire_real_collaborators(harness, approved)
        _place_under_service_data_dir(harness, approved)
        head, _text = landed._unapproved_head(approved, tmp_path)
        pending = materialize_state_for_execution(state_from_record(head))
        validated = _route_validate(service, approved, head.id)
        try:
            _file_gate(service, approved, head.id)
            file_gate = "admitted"
        except PromptFileReviewStale as exc:
            file_gate = f"refused {exc.error_code}"
        print(
            f"(c) unapproved head: materializer_pending={isinstance(pending, InterpretationReviewPending)} validate={validated} file_gate={file_gate}"
        )
        assert isinstance(pending, InterpretationReviewPending)
        assert validated["execution_ready"] is False
        assert file_gate == "refused prompt_file_review_stale"
        with pytest.raises(UnresolvedInterpretationPlaceholderError):
            landed._compile_frozen(harness, approved, head.id)
    finally:
        harness.loop.close()
