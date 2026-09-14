"""The backend review-card surfacer carries the prompt surface the requirement staged.

A multi-query LLM node's ``llm_prompt_template`` review attests the whole
prompt surface the model receives (system prompt, node template, every query
template), and the auto-stager drafts the requirement from
``prompt_review_draft_from_options``. The surfacer used to hand the writer
``options['prompt_template']`` instead — the node-level template the
multi-query path never sends on its own — so every multi-query card showed the
dead template rather than the reviewed surface. Single-prompt nodes review
``prompt_template`` itself and must stay byte-identical.
"""

from __future__ import annotations

from typing import Any

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.composer.service import _backend_surface_args_for_site
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    InterpretationReviewSite,
    interpretation_sites,
    prompt_review_draft_from_options,
)

_NODE_ID = "pair_colours"
_USER_TERM = f"llm_prompt_template:{_NODE_ID}"


def _multi_query_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
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
    options.update(overrides)
    return options


def _pending_prompt_review(draft: str) -> dict[str, Any]:
    return {
        "id": "prompt_template_review",
        "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
        "user_term": _USER_TERM,
        "status": "pending",
        "draft": draft,
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }


def _state_with_options(options: dict[str, Any]) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id=_NODE_ID,
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _prompt_template_site(state: CompositionState) -> InterpretationReviewSite:
    sites = [site for site in interpretation_sites(state) if site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE]
    assert len(sites) == 1
    return sites[0]


def test_multi_query_card_draft_is_the_staged_prompt_surface_not_the_node_template() -> None:
    options = _multi_query_options()
    surface_draft = prompt_review_draft_from_options(options)
    assert surface_draft is not None
    # Instrument control: the two candidate drafts genuinely differ for this shape.
    assert surface_draft != options["prompt_template"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_pending_prompt_review(surface_draft)]
    state = _state_with_options(options)

    surfaced = _backend_surface_args_for_site(state, _prompt_template_site(state))

    assert surfaced == (_NODE_ID, _USER_TERM, surface_draft)


def test_single_prompt_card_draft_is_still_the_prompt_template() -> None:
    prompt = "Rate how clear {{ row.text }} is."
    options: dict[str, Any] = {"prompt_template": prompt}
    # Instrument control: for a single-prompt node the one derivation IS prompt_template.
    assert prompt_review_draft_from_options(options) == prompt
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_pending_prompt_review(prompt)]
    state = _state_with_options(options)

    surfaced = _backend_surface_args_for_site(state, _prompt_template_site(state))

    assert surfaced == (_NODE_ID, _USER_TERM, prompt)
