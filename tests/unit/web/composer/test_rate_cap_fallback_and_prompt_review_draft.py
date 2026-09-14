"""Rate-cap fallback guidance survives canonicalization; prompt review drafts follow the surface.

Two request_interpretation_review boundary properties:

* The interpretation-review rate caps exist only because the planner has a
  fallback (ADR-037): write a direct interpretation into the prompt template
  instead of asking again. The secret-safe ToolArgumentError projection maps
  caller prose to a closed vocabulary, so the fallback must be a fixed,
  value-free canonical expectation or it never reaches the planner.
* For an ``llm_prompt_template`` site the draft the boundary check returns is
  the operator-reviewed text — the rendered multi-query prompt surface on a
  multi-query node — exactly what the writer derives with
  ``prompt_review_draft_from_options``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools.sessions import (
    RATE_CAP_PER_SESSION_DAY_CODE,
    RATE_CAP_PER_TERM_CODE,
    _assert_affected_component,
    _check_interpretation_rate_limits,
)
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, prompt_review_draft_from_options

_FALLBACK = "use a direct interpretation in the prompt template instead"
_NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def _vague_term_event(*, composition_state_id: UUID, user_term: str, index: int) -> InterpretationEventRecord:
    return InterpretationEventRecord(
        id=uuid4(),
        session_id=uuid4(),
        composition_state_id=composition_state_id,
        affected_node_id="rate_node",
        tool_call_id=f"call_prior_{index}",
        user_term=user_term,
        kind=InterpretationKind.VAGUE_TERM,
        llm_draft=f"Draft {index}",
        accepted_value=None,
        choice=InterpretationChoice.PENDING,
        created_at=_NOW,
        resolved_at=None,
        actor="composer-llm",
        model_identifier="test/composer",
        model_version="test-v1",
        provider="test",
        composer_skill_hash="a" * 64,
        arguments_hash=None,
        hash_domain_version=None,
        interpretation_source=InterpretationSource.USER_APPROVED,
        runtime_model_identifier_at_resolve=None,
        runtime_model_version_at_resolve=None,
        approved_prompt_artifact_hash=None,
    )


async def _raise_rate_cap(events: list[InterpretationEventRecord], *, composition_state_id: UUID) -> ToolArgumentError:
    async def _list_events(*_: Any, **__: Any) -> list[InterpretationEventRecord]:
        return events

    with pytest.raises(ToolArgumentError) as excinfo:
        await _check_interpretation_rate_limits(
            session_id=uuid4(),
            user_term="cool",
            composition_state_id=composition_state_id,
            list_events_fn=_list_events,
            per_term_cap=3,
            per_session_day_cap=10,
            now=_NOW,
        )
    return excinfo.value


@pytest.mark.asyncio
async def test_per_term_rate_cap_error_carries_the_prompt_template_fallback() -> None:
    state_id = uuid4()
    events = [_vague_term_event(composition_state_id=state_id, user_term="cool", index=index) for index in range(3)]

    exc = await _raise_rate_cap(events, composition_state_id=state_id)

    assert exc.code == RATE_CAP_PER_TERM_CODE
    payload = arg_error_payload(exc, "request_interpretation_review")
    assert _FALLBACK in payload["error"]
    assert "per-term" in payload["error"]
    # Value-free: neither the term nor the counts reach the planner echo.
    assert "cool" not in payload["error"]
    assert "3" not in payload["error"]


@pytest.mark.asyncio
async def test_per_session_day_rate_cap_error_carries_the_prompt_template_fallback() -> None:
    events = [_vague_term_event(composition_state_id=uuid4(), user_term=f"term_{index}", index=index) for index in range(10)]

    exc = await _raise_rate_cap(events, composition_state_id=uuid4())

    assert exc.code == RATE_CAP_PER_SESSION_DAY_CODE
    payload = arg_error_payload(exc, "request_interpretation_review")
    assert _FALLBACK in payload["error"]
    assert "per session per UTC day" in payload["error"]
    assert "10" not in payload["error"]


def _multi_query_llm_state() -> tuple[CompositionState, dict[str, object]]:
    options: dict[str, object] = {
        "prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply.",
        "system_prompt": "Reply with only the value asked for and nothing else.",
        "queries": {
            "good_pair": {
                "input_fields": {"colour": "colour"},
                "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
            },
            "hex_code": {
                "input_fields": {"colour": "colour"},
                "template": "What is the approximate hex code of the colour {{ row.colour }}?",
            },
        },
        "required_input_fields": ["colour"],
    }
    surface_draft = prompt_review_draft_from_options(options)
    assert surface_draft is not None
    assert surface_draft != options["prompt_template"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "prompt_template_review:identify_colour",
            "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
            "user_term": "llm_prompt_template:identify_colour",
            "status": "pending",
            "draft": surface_draft,
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }
    ]
    node = NodeSpec(
        id="identify_colour",
        node_type="transform",
        plugin="llm",
        input="input",
        on_success="out",
        on_error="quarantine",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = CompositionState(
        source=None,
        nodes=(node,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Multi-query review draft"),
        version=1,
    )
    return state, options


def test_prompt_template_review_draft_is_the_multi_query_surface() -> None:
    state, options = _multi_query_llm_state()

    resolved = _assert_affected_component(
        state,
        "identify_colour",
        InterpretationKind.LLM_PROMPT_TEMPLATE,
        "llm_prompt_template:identify_colour",
    )

    assert resolved == prompt_review_draft_from_options(options)


def test_prompt_template_review_accepts_the_current_surface_as_llm_draft() -> None:
    state, options = _multi_query_llm_state()
    surface_draft = prompt_review_draft_from_options(options)

    resolved = _assert_affected_component(
        state,
        "identify_colour",
        InterpretationKind.LLM_PROMPT_TEMPLATE,
        "llm_prompt_template:identify_colour",
        surface_draft,
    )

    assert resolved == surface_draft


def test_prompt_template_review_rejects_the_node_level_template_as_stale_on_a_multi_query_node() -> None:
    state, options = _multi_query_llm_state()

    with pytest.raises(ToolArgumentError, match="stale prompt-template draft"):
        _assert_affected_component(
            state,
            "identify_colour",
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            "llm_prompt_template:identify_colour",
            str(options["prompt_template"]),
        )
