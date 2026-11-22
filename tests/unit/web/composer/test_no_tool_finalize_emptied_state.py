"""A turn that empties the pipeline is not reported as a failed build.

When a change succeeded this turn and the pipeline is now structurally empty
(for example the user asked to remove the source and the output), the notice
appended to the reply states that the pipeline is empty and what it needs.
It must not say the composer failed to build or offer a retry. A turn where
nothing changed keeps the failed-build notice. Either notice is user-facing
only: the composer's own history keeps the model's prose without it.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from elspeth.web.composer.no_tool_policy import AssistantTextSegment, TrustedSystemNoticeSegment, visible_message_segments
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerServiceImpl
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
_FAILED_BUILD_NOTICE = (
    _FAILED_BUILD_HEADER
    + " To continue: refine your request with more specifics, or reply telling the composer to retry with the plan it described above."
)
_SEPARATOR = "\n\n---\n\n[ELSPETH-SYSTEM] "


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


async def _finalize(*, content: str, mutation_success_seen: bool, versions_advanced: int) -> ComposerResult:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    initial = _empty_state()
    state = replace(initial, version=initial.version + versions_advanced)
    preflight = _invalid_empty_preflight()
    with patch.object(service._preflight, "runtime_preflight", return_value=preflight):
        result = await service._completion._finalize_no_tool_response(
            content=content,
            state=state,
            initial_version=initial.version,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=preflight,
            runtime_preflight_cache=service._preflight.new_cache(),
            session_scope="session:emptied-state",
            mutation_success_seen=mutation_success_seen,
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
    result = await _finalize(content=_PROSE, mutation_success_seen=True, versions_advanced=2)

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
    result = await _finalize(content="", mutation_success_seen=True, versions_advanced=1)

    assert result.message == "[ELSPETH-SYSTEM] " + _EMPTIED_NOTICE
    assert result.raw_assistant_content == ""
    assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
        TrustedSystemNoticeSegment(_EMPTIED_NOTICE),
    )


@pytest.mark.asyncio
async def test_emptied_state_notice_stays_out_of_the_composer_history() -> None:
    result = await _finalize(content=_PROSE, mutation_success_seen=True, versions_advanced=2)

    assert _history(result) == [{"role": "assistant", "content": _PROSE}]


@pytest.mark.asyncio
async def test_turn_without_a_successful_change_keeps_the_failed_build_notice() -> None:
    # Control: nothing changed this turn and the pipeline is empty.
    result = await _finalize(content=_PROSE, mutation_success_seen=False, versions_advanced=0)

    assert result.message == _PROSE + _SEPARATOR + _FAILED_BUILD_NOTICE
    assert _EMPTIED_NOTICE not in result.message
    assert result.raw_assistant_content == _PROSE
    assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
        AssistantTextSegment(_PROSE),
        TrustedSystemNoticeSegment(_FAILED_BUILD_NOTICE),
    )
    assert _history(result) == [{"role": "assistant", "content": _PROSE}]
