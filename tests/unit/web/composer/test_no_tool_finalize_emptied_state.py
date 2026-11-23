"""Empty-pipeline notices follow turn-start topology and tool outcomes.

A failed attempt from an empty start can name its blocker after another
change succeeded. Emptying a populated pipeline gets the neutral notice.
The composer's history keeps the model's prose without the notice.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ToolArgumentErrorCategory
from elspeth.web.composer.audit import begin_dispatch, finish_arg_error, finish_success
from elspeth.web.composer.no_tool_policy import AssistantTextSegment, TrustedSystemNoticeSegment, visible_message_segments
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, SourceSpec
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._common import _candidate_rejection_result
from elspeth.web.execution.schemas import (
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes import _composer_chat_history
from tests.unit.web.composer._helpers import _empty_state, _make_settings, _mock_catalog

_PROSE = "Done. I removed the source and the output battery_out, so the pipeline is empty again."
# Literal bytes, not the module constants: this file must collect on a tree without them.
_EMPTIED_NOTICE = "The pipeline is now empty. Add a source and an output to continue."
_FAILED_BUILD_HEADER = "The pipeline is still empty — the composer did not complete a valid build this turn."
_FAILED_BUILD_NEXT_STEP = (
    "To continue: refine your request with more specifics, or reply telling the composer to retry with the plan it described above."
)
_FAILED_BUILD_NOTICE = _FAILED_BUILD_HEADER + " " + _FAILED_BUILD_NEXT_STEP
_SEPARATOR = "\n\n---\n\n[ELSPETH-SYSTEM] "
_FAILED_BUILD_WITH_BLOCKER = _SEPARATOR + _FAILED_BUILD_HEADER + "\n\nCause: {blocker}\n\n" + _FAILED_BUILD_NEXT_STEP
_VALIDATION_REFUSAL = (
    "Source 'source': on_validation_failure requires an explicit non-empty failure choice for a new component. "
    "Supply 'discard' or an eligible named route; null and empty values are not choices. "
    "Omit the field only to preserve an existing component's choice."
)
_REFUSAL_BLOCKER = "set_pipeline returned success=false: " + _VALIDATION_REFUSAL
_ARG_ERROR_BLOCKER = "set_pipeline failed before mutation (ToolArgumentError: missing: outputs)."
_SOURCE = SourceSpec(
    plugin="csv", on_success="battery_out", options={"path": "input.csv", "schema": {"mode": "observed"}}, on_validation_failure="discard"
)
_OUTPUT = OutputSpec(name="battery_out", plugin="csv", options={"path": "output.csv"}, on_write_failure="discard")


def _pipeline_arguments(*, on_validation_failure: str = "discard") -> dict[str, object]:
    return {
        "source": {
            "plugin": _SOURCE.plugin,
            "on_success": _SOURCE.on_success,
            "options": dict(_SOURCE.options),
            "on_validation_failure": on_validation_failure,
        },
        "nodes": [],
        "edges": [],
        "outputs": [{"sink_name": _OUTPUT.name, "plugin": _OUTPUT.plugin, "options": dict(_OUTPUT.options), "on_write_failure": "discard"}],
    }


def _successful_invocation(
    tool_name: str,
    arguments: dict[str, object],
    before: CompositionState,
    after: CompositionState,
    *,
    affected_nodes: tuple[str, ...] = (),
    data: dict[str, object] | None = None,
) -> ComposerToolInvocation:
    audit = begin_dispatch(f"call_{tool_name}", tool_name, arguments, version_before=before.version, actor="test")
    result = ToolResult(success=True, updated_state=after, validation=after.validate(), affected_nodes=affected_nodes, data=data)
    return finish_success(audit, result_payload=result.to_dict(), version_after=after.version)


def _metadata_invocation(before: CompositionState, after: CompositionState) -> ComposerToolInvocation:
    data: dict[str, object] = (
        {"changed_fields": [], "message": "No field changed: the supplied values match the saved metadata."}
        if before == after
        else {"changed_fields": ["name"]}
    )
    return _successful_invocation("set_metadata", {"patch": {"name": after.metadata.name}}, before, after, data=data)


def _refused_pipeline_invocation(state: CompositionState) -> ComposerToolInvocation:
    audit = begin_dispatch(
        "call_refused", "set_pipeline", _pipeline_arguments(on_validation_failure=""), version_before=state.version, actor="test"
    )
    result = _candidate_rejection_result(state, _VALIDATION_REFUSAL, error_code="failure_route_required", rejected_component="source")
    return finish_success(audit, result_payload=result.to_dict(), version_after=state.version)


def _argument_error_invocation(state: CompositionState) -> ComposerToolInvocation:
    arguments = _pipeline_arguments()
    del arguments["outputs"]
    audit = begin_dispatch("call_arg_error", "set_pipeline", arguments, version_before=state.version, actor="test")
    return finish_arg_error(
        audit,
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.MISSING_REQUIRED_PATH,
        error_message="missing: outputs",
        error_payload={"error": "Tool 'set_pipeline' missing required argument(s): outputs"},
    )


def _approval_required_invocation(state: CompositionState) -> ComposerToolInvocation:
    return _successful_invocation(
        "set_pipeline",
        _pipeline_arguments(),
        state,
        state,
        data={
            "status": "APPROVAL_REQUIRED",
            "proposal_id": "00000000-0000-0000-0000-000000000001",
            "tool_name": "set_pipeline",
            "summary": "Replace the pipeline with a CSV source and a CSV output.",
            "message": (
                "The proposal is recorded and awaits human approval; it has not been applied. This result's "
                "validation describes the unchanged current pipeline and does not validate the proposed change. "
                "Send no further pipeline changes until the user accepts or rejects this proposal."
            ),
        },
    )


def _invalid_empty_preflight() -> ValidationResult:
    detail = "No source is configured."
    return ValidationResult(
        is_valid=False,
        checks=[ValidationCheck(name="settings_load", passed=False, detail=detail, affected_nodes=(), outcome_code=None)],
        errors=[ValidationError(component_id=None, component_type=None, message=detail, suggestion=None, error_code=None)],
        readiness=ValidationReadiness(
            authoring_valid=False,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="settings_load", component_id=None, component_type=None, detail=detail, suggestion=None, note=None
                )
            ],
        ),
    )


async def _finalize(
    *,
    content: str,
    turn_start_state: CompositionState,
    state: CompositionState,
    mutation_success_seen: bool,
    tool_invocations: tuple[ComposerToolInvocation, ...] = (),
    preflight: ValidationResult | None = None,
) -> ComposerResult:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    if preflight is None:
        preflight = _invalid_empty_preflight()
    with patch.object(service._preflight, "runtime_preflight", return_value=preflight):
        result = await service._completion._finalize_no_tool_response(
            content=content,
            state=state,
            turn_start_state=turn_start_state,
            initial_version=turn_start_state.version,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=preflight,
            runtime_preflight_cache=service._preflight.new_cache(),
            session_scope="session:emptied-state",
            mutation_success_seen=mutation_success_seen,
            tool_invocations=tool_invocations,
        )
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.is_valid is False
    return result


def _history(result: ComposerResult) -> list[dict[str, str]]:
    record = ChatMessageRecord(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="assistant",
        content=result.message,
        raw_content=result.raw_assistant_content,
        tool_calls=None,
        created_at=datetime.now(UTC),
        composition_state_id=None,
        writer_principal="compose_loop",
    )
    return _composer_chat_history([record])


@pytest.mark.asyncio
async def test_requested_emptying_gets_a_neutral_empty_pipeline_notice() -> None:
    # Live shape: two successful removals (source, then output) leave an empty pipeline.
    initial = replace(_empty_state().with_source(_SOURCE).with_output(_OUTPUT), version=1)
    result = await _finalize(content=_PROSE, turn_start_state=initial, state=replace(_empty_state(), version=3), mutation_success_seen=True)

    assert result.message == _PROSE + _SEPARATOR + _EMPTIED_NOTICE
    assert "did not complete a valid build" not in result.message
    assert "retry" not in result.message
    assert result.raw_assistant_content == _PROSE
    assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
        AssistantTextSegment(_PROSE),
        TrustedSystemNoticeSegment(_EMPTIED_NOTICE),
    )


@pytest.mark.asyncio
async def test_requested_emptying_without_prose_shows_only_the_notice() -> None:
    initial = replace(_empty_state().with_source(_SOURCE), version=1)
    result = await _finalize(content="", turn_start_state=initial, state=replace(_empty_state(), version=2), mutation_success_seen=True)

    assert result.message == "[ELSPETH-SYSTEM] " + _EMPTIED_NOTICE
    assert result.raw_assistant_content == ""
    assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
        TrustedSystemNoticeSegment(_EMPTIED_NOTICE),
    )


@pytest.mark.asyncio
async def test_emptied_state_notice_stays_out_of_the_composer_history() -> None:
    initial = _empty_state()
    result = await _finalize(content=_PROSE, turn_start_state=initial, state=replace(initial, version=3), mutation_success_seen=True)

    assert _history(result) == [{"role": "assistant", "content": _PROSE}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument_error", "blocker"),
    [
        pytest.param(False, _REFUSAL_BLOCKER, id="success-false"),
        pytest.param(True, _ARG_ERROR_BLOCKER, id="arg-error"),
    ],
)
async def test_failed_first_build_after_metadata_names_blocker(argument_error: bool, blocker: str) -> None:
    initial = _empty_state()
    state = initial.with_metadata({"name": "Battery pipeline"})
    failed = _argument_error_invocation(state) if argument_error else _refused_pipeline_invocation(state)
    preflight = _invalid_empty_preflight()
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=state,
        mutation_success_seen=True,
        tool_invocations=(_metadata_invocation(initial, state), failed),
        preflight=preflight,
    )

    assert result.message == _PROSE + _FAILED_BUILD_WITH_BLOCKER.format(blocker=blocker)
    assert _EMPTIED_NOTICE not in result.message
    assert result.raw_assistant_content == _PROSE
    assert result.runtime_preflight is preflight


@pytest.mark.asyncio
async def test_metadata_only_keeps_neutral_empty_notice() -> None:
    initial = _empty_state()
    state = initial.with_metadata({"name": "Battery pipeline"})
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=state,
        mutation_success_seen=True,
        tool_invocations=(_metadata_invocation(initial, state),),
    )

    assert result.message == _PROSE + _SEPARATOR + _EMPTIED_NOTICE


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_first", [False, True], ids=["metadata-first", "proposal-first"])
async def test_metadata_and_pending_pipeline_proposal_keep_neutral_empty_notice(proposal_first: bool) -> None:
    initial = _empty_state()
    state = initial.with_metadata({"name": "Battery pipeline"})
    metadata = _metadata_invocation(initial, state)
    proposal = _approval_required_invocation(initial if proposal_first else state)
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=state,
        mutation_success_seen=True,
        tool_invocations=(proposal, metadata) if proposal_first else (metadata, proposal),
    )

    assert result.message == _PROSE + _SEPARATOR + _EMPTIED_NOTICE


@pytest.mark.asyncio
async def test_successful_build_then_clear_keeps_neutral_empty_notice() -> None:
    initial = _empty_state()
    built = replace(initial.with_source(_SOURCE).with_output(_OUTPUT), version=initial.version + 1)
    cleared_source = built.without_named_source("source")
    assert cleared_source is not None
    cleared = cleared_source.without_output(_OUTPUT.name)
    assert cleared is not None
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=cleared,
        mutation_success_seen=True,
        tool_invocations=(
            _successful_invocation("set_pipeline", _pipeline_arguments(), initial, built, affected_nodes=("source", _OUTPUT.name)),
            _successful_invocation("clear_source", {"source_name": "source"}, built, cleared_source, affected_nodes=("source",)),
            _successful_invocation("remove_output", {"sink_name": _OUTPUT.name}, cleared_source, cleared, affected_nodes=(_OUTPUT.name,)),
        ),
    )

    assert result.message == _PROSE + _SEPARATOR + _EMPTIED_NOTICE


@pytest.mark.asyncio
async def test_removal_from_nonempty_start_with_failed_build_keeps_neutral_notice() -> None:
    initial = _empty_state().with_source(_SOURCE)
    cleared = initial.without_named_source("source")
    assert cleared is not None
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=cleared,
        mutation_success_seen=True,
        tool_invocations=(
            _successful_invocation("clear_source", {"source_name": "source"}, initial, cleared, affected_nodes=("source",)),
            _refused_pipeline_invocation(cleared),
        ),
    )

    assert result.message == _PROSE + _SEPARATOR + _EMPTIED_NOTICE


@pytest.mark.asyncio
async def test_version_unchanged_metadata_keeps_existing_no_mutation_blocker() -> None:
    initial = _empty_state()
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=initial,
        mutation_success_seen=False,
        tool_invocations=(_metadata_invocation(initial, initial),),
    )

    blocker = f"set_metadata succeeded without mutating CompositionState (version stayed {initial.version})."
    assert result.message == _PROSE + _FAILED_BUILD_WITH_BLOCKER.format(blocker=blocker)


@pytest.mark.asyncio
async def test_failed_build_without_a_mutation_keeps_top_branch_notice() -> None:
    initial = _empty_state()
    preflight = _invalid_empty_preflight()
    result = await _finalize(
        content=_PROSE,
        turn_start_state=initial,
        state=initial,
        mutation_success_seen=False,
        tool_invocations=(_refused_pipeline_invocation(initial),),
        preflight=preflight,
    )

    assert result.message == _PROSE + _FAILED_BUILD_WITH_BLOCKER.format(blocker=_REFUSAL_BLOCKER)
    assert result.runtime_preflight is not preflight
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.checks[0].name == "state_exists"


@pytest.mark.asyncio
async def test_turn_without_a_successful_change_keeps_the_failed_build_notice() -> None:
    # Control: nothing changed this turn and the pipeline is empty.
    initial = _empty_state()
    result = await _finalize(content=_PROSE, turn_start_state=initial, state=initial, mutation_success_seen=False)

    assert result.message == _PROSE + _SEPARATOR + _FAILED_BUILD_NOTICE
    assert _EMPTIED_NOTICE not in result.message
    assert result.raw_assistant_content == _PROSE
    assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
        AssistantTextSegment(_PROSE),
        TrustedSystemNoticeSegment(_FAILED_BUILD_NOTICE),
    )
    assert _history(result) == [{"role": "assistant", "content": _PROSE}]
