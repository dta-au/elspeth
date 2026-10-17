"""The deterministic finalizer preserves prose without interpreting its meaning.

END owns semantic publication review. This downstream finalizer retains the
independent preflight and audit contracts without adding an NLP verdict.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, SourceSpec
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from tests.unit.web.composer._helpers import _empty_state, _make_settings, _mock_catalog

_PROSE_ONLY_CORRECTION = "I've corrected the spelling in my explanation. No pipeline settings were edited, and no records were processed."


def _wired_state() -> CompositionState:
    return (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": "/data/inputs/in.csv"},
                on_validation_failure="rejected_records",
            )
        )
        .with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "/data/outputs/out.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        .with_output(
            OutputSpec(
                name="rejected_records",
                plugin="csv",
                options={"path": "/data/outputs/rejected.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    )


def _green_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight_known", [False, True], ids=["unknown", "green"])
@pytest.mark.parametrize(
    "prose",
    [
        pytest.param(_PROSE_ONLY_CORRECTION, id="prose-only-correction"),
        pytest.param("Earlier I wrote `on_validation_failure: discard`; that described the old draft.", id="historical-field"),
        pytest.param('The example says "on_write_failure: quarantine"; it is a quotation.', id="quoted-output-field"),
        pytest.param("I updated the filter. The task is complete.", id="completed-action"),
        pytest.param("Understood, I'll route failures to rejected_records.", id="agreement"),
        pytest.param("I changed the routing so invalid records now reach the review output.", id="consequence"),
        pytest.param("I configured the source with `on_validation_failure: rejected_records`.", id="matching-field"),
        pytest.param("I revised that explanation.", id="explanation-only"),
    ],
)
async def test_finalizer_does_not_add_a_categorical_prose_verdict(prose: str, preflight_known: bool) -> None:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = _wired_state()
    preflight = _green_preflight() if preflight_known else None

    with patch.object(service._preflight, "runtime_preflight") as runtime_preflight:
        result = await service._completion._finalize_no_tool_response(
            content=prose,
            state=state,
            initial_version=state.version,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=preflight,
            runtime_preflight_cache=service._preflight.new_cache(),
            session_scope="session:prose-preservation",
            mutation_success_seen=False,
        )

    assert result.message == prose
    assert result.raw_assistant_content is None
    assert result.state is state
    assert result.runtime_preflight is preflight
    runtime_preflight.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["identity", "title", "relevant", "reverted"])
async def test_finalizer_does_not_infer_claim_meaning_from_mutation_activity(effect: str) -> None:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    initial = _wired_state()
    state = initial
    if effect == "title":
        state = replace(initial, metadata=replace(initial.metadata, name="Renamed draft"), version=initial.version + 1)
    elif effect == "relevant":
        state = initial.with_source(replace(initial.sources["source"], on_validation_failure="discard"))
    elif effect == "reverted":
        state = initial.with_source(replace(initial.sources["source"], on_validation_failure="discard")).with_source(
            initial.sources["source"]
        )
    prose = "I updated the routing. The source on_validation_failure is discard."
    preflight = _green_preflight()

    with patch.object(service._preflight, "runtime_preflight", return_value=preflight):
        result = await service._completion._finalize_no_tool_response(
            content=prose,
            state=state,
            initial_version=initial.version,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=preflight,
            runtime_preflight_cache=service._preflight.new_cache(),
            session_scope="session:prose-preservation",
            mutation_success_seen=True,
        )

    assert result.message == prose
    assert result.raw_assistant_content is None
    assert result.state is state
    assert result.runtime_preflight is preflight
