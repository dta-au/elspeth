"""A stale llm_prompt_template draft names the draft the writer actually compares against.

``_assert_affected_component`` compares an ``llm_prompt_template`` review's
``llm_draft`` against ``prompt_review_draft_from_options`` — the rendered
multi-query prompt SURFACE on a multi-query node, ``prompt_template``
otherwise. The planner-visible expectation must say so. The secret-safe
``ToolArgumentError`` projection keeps only closed-vocabulary expectations, so
the wording is a fixed operator-owned constant: a free-text reword would
canonicalise to "a valid value", and the old wording pointed the planner at
``options.prompt_template`` alone, which is itself stale on a multi-query node.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.composer.protocol import LLM_PROMPT_REVIEW_DRAFT_EXPECTATION, ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools.sessions import _assert_affected_component
from elspeth.web.interpretation_state import prompt_review_draft_from_options

_TARGET = (
    "the current prompt review draft for the node: the rendered multi-query prompt surface on a "
    "multi-query LLM node, otherwise options.prompt_template"
)


def _llm_state(options: dict[str, object]) -> CompositionState:
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
    return CompositionState(
        source=None,
        nodes=(node,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Prompt review draft expectation"),
        version=1,
    )


def _multi_query_options() -> dict[str, object]:
    return {
        "prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply.",
        "queries": {
            "good_pair": {
                "input_fields": {"colour": "colour"},
                "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
            },
        },
        "required_input_fields": ["colour"],
    }


def _single_prompt_options() -> dict[str, object]:
    return {"prompt_template": "Classify {{ row.colour }}.", "required_input_fields": ["colour"]}


def _stale_draft_error(options: dict[str, object], stale_draft: str) -> ToolArgumentError:
    with pytest.raises(ToolArgumentError) as caught:
        _assert_affected_component(
            _llm_state(options),
            "identify_colour",
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            "llm_prompt_template:identify_colour",
            stale_draft,
        )
    return caught.value


def test_multi_query_node_level_template_is_stale_and_names_the_surface_draft() -> None:
    options = _multi_query_options()
    assert prompt_review_draft_from_options(options) != options["prompt_template"]

    exc = _stale_draft_error(options, str(options["prompt_template"]))

    assert exc.argument == "llm_draft"
    assert exc.actual_type == "stale prompt-template draft"
    assert exc.expected == _TARGET
    payload = arg_error_payload(exc, "request_interpretation_review")
    assert _TARGET in payload["error"]
    # The planner echo stays value-free: no node id, no prompt text.
    assert "identify_colour" not in payload["error"]
    assert "colour pair" not in payload["error"]


def test_single_prompt_node_stale_draft_names_the_same_expectation() -> None:
    exc = _stale_draft_error(_single_prompt_options(), "Classify something else.")

    assert exc.expected == _TARGET


def test_expectation_constant_is_the_target_wording() -> None:
    assert LLM_PROMPT_REVIEW_DRAFT_EXPECTATION == _TARGET
