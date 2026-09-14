"""Blob-marker LLM prompt/model values and the typed review-drift error.

Finding #1 (state half): an ``inline_content`` blob marker in an LLM node's
``prompt_template`` or ``model`` reads as "no value" through
``_node_str_option``. The chosen policy refuses LLM-authored blobs in those
fields at wire time (``wire_blob_inline_ref``) and at run admission
(``InlineBlobPromptSurfaceAdmissionError``). A user-uploaded marker is
therefore user-verbatim content (ADR-034) with no review to stage, so it
enumerates no site. A marker sitting under a RESOLVED review is drift: that
review attested text the marker is not, so the strict materializer refuses it
with the typed integrity error instead of minting a site no card can clear.

Finding #18: the strict materializer's drift guards raised bare ``ValueError``,
which /execute mapped to 404 and /validate let escape as a 500. They now raise
``InterpretationReviewIntegrityError`` (still a ``ValueError``) carrying the
component and review kind, with the message bytes unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.service import unsurfaceable_pending_interpretation_review_sites
from elspeth.web.composer.source_demand import source_data_contract_artifact_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.interpretation_state import (
    GATE_CONDITION_AUTHORED_USER_TERM,
    INTERPRETATION_REQUIREMENTS_KEY,
    RAW_HTML_CLEANUP_USER_TERM,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    SOURCE_AUTHORING_KEY,
    InterpretationReviewIntegrityError,
    interpretation_sites,
    materialize_state_for_execution,
    model_choice_artifact_hash,
    pending_execution_interpretation_sites,
    pipeline_decision_artifact_hash,
    reconcile_authoritative_reviews,
    source_component_id,
)
from tests.unit.web.composer.test_authoring_reconciliation import _node as _reconciliation_node
from tests.unit.web.composer.test_authoring_reconciliation import _requirement as _reconciliation_requirement
from tests.unit.web.composer.test_authoring_reconciliation import _state as _reconciliation_state
from tests.unit.web.composer.test_authoring_reconciliation import _two_vague_term_fixtures
from tests.unit.web.test_interpretation_state import (
    _gate,
    _gate_state,
    _pipeline_decision_options,
    _state_with_cleanup_node,
    _state_with_llm,
)
from tests.unit.web.test_interpretation_state_source_data_contract import _resolved_contract_options
from tests.unit.web.test_interpretation_state_source_data_contract import _state as _contract_state

_NODE_ID = "rate_coolness"
_PROMPT = "Rate {{ row.text }}"
_MODEL = "gpt-4o-mini"
_MARKER: dict[str, Any] = {
    "blob_ref": "11111111-1111-1111-1111-111111111111",
    "mode": "inline_content",
    "sha256": "a" * 64,
}


def _resolved_prompt_review(anchor: str) -> dict[str, object]:
    return {
        "id": "prompt-template-review",
        "kind": "llm_prompt_template",
        "user_term": "rating prompt",
        "status": "resolved",
        "draft": _PROMPT,
        "event_id": "event-prompt",
        "accepted_value": _PROMPT,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": anchor,
    }


def _resolved_model_review(anchor: str) -> dict[str, object]:
    return {
        "id": "model-choice-review",
        "kind": "llm_model_choice",
        "user_term": "model",
        "status": "resolved",
        "draft": _MODEL,
        "event_id": "event-model",
        "accepted_value": _MODEL,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": anchor,
    }


def _site_keys(state: CompositionState) -> list[tuple[str, str, InterpretationKind]]:
    return [(site.component_id, site.component_type, site.kind) for site in pending_execution_interpretation_sites(state)]


def _assert_no_site_and_materializes(state: CompositionState) -> None:
    # Every consumer of the enumeration must agree there is no debt: the
    # execution gate, the composer surfacer / orphan gate, and the paste/import
    # gate, which raised InvariantError when a marker prompt minted a site the
    # card writer cannot eventize.
    assert _site_keys(state) == []
    assert list(interpretation_sites(state)) == []
    assert unsurfaceable_pending_interpretation_review_sites(state) == ()
    assert isinstance(materialize_state_for_execution(state), CompositionState)


def _assert_drift_refused(state: CompositionState, kind: InterpretationKind) -> None:
    # No site (nothing a card could clear), so the gate is the materializer.
    assert _site_keys(state) == []
    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(state)
    assert caught.value.component_id == _NODE_ID
    assert caught.value.component_type == "transform"
    assert caught.value.kind is kind


# --- Finding #1: blob-marker prompt_template / model --------------------------


def test_marker_prompt_template_without_prompt_review_is_not_review_debt() -> None:
    """ADR-034 user-uploaded prompt: no review to stage, and no InvariantError."""
    state = _state_with_llm(
        {
            "prompt_template": _MARKER,
            "model": _MODEL,
            INTERPRETATION_REQUIREMENTS_KEY: [_resolved_model_review(model_choice_artifact_hash(_MODEL))],
        }
    )

    _assert_no_site_and_materializes(state)


def test_marker_prompt_template_over_resolved_prompt_review_is_refused_as_drift() -> None:
    """A resolved review attested a text prompt; a marker is not that text."""
    state = _state_with_llm(
        {
            "prompt_template": _MARKER,
            "model": _MODEL,
            INTERPRETATION_REQUIREMENTS_KEY: [
                _resolved_prompt_review(stable_hash(_PROMPT)),
                _resolved_model_review(model_choice_artifact_hash(_MODEL)),
            ],
        }
    )

    _assert_drift_refused(state, InterpretationKind.LLM_PROMPT_TEMPLATE)


def test_marker_model_without_model_review_is_not_review_debt() -> None:
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            "model": _MARKER,
            INTERPRETATION_REQUIREMENTS_KEY: [_resolved_prompt_review(stable_hash(_PROMPT))],
        }
    )

    _assert_no_site_and_materializes(state)


def test_marker_model_over_resolved_model_review_is_refused_as_drift() -> None:
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            "model": _MARKER,
            INTERPRETATION_REQUIREMENTS_KEY: [
                _resolved_prompt_review(stable_hash(_PROMPT)),
                _resolved_model_review(model_choice_artifact_hash(_MODEL)),
            ],
        }
    )

    _assert_drift_refused(state, InterpretationKind.LLM_MODEL_CHOICE)


def test_reviewed_text_prompt_and_model_still_materialize() -> None:
    """Control: the debt arm must not over-block a fully reviewed text node."""
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            "model": _MODEL,
            INTERPRETATION_REQUIREMENTS_KEY: [
                _resolved_prompt_review(stable_hash(_PROMPT)),
                _resolved_model_review(model_choice_artifact_hash(_MODEL)),
            ],
        }
    )

    assert _site_keys(state) == []
    assert isinstance(materialize_state_for_execution(state), CompositionState)


def test_null_model_is_still_an_absent_model() -> None:
    """Control: an explicit null is the absent value, not review debt."""
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            "model": None,
            INTERPRETATION_REQUIREMENTS_KEY: [_resolved_prompt_review(stable_hash(_PROMPT))],
        }
    )

    assert _site_keys(state) == []
    assert isinstance(materialize_state_for_execution(state), CompositionState)


# --- Finding #18: drift guards raise the owned integrity error ----------------


def test_prompt_template_drift_raises_typed_integrity_error() -> None:
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            INTERPRETATION_REQUIREMENTS_KEY: [_resolved_prompt_review(stable_hash("different prompt"))],
        }
    )

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(state)

    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == f"llm node {_NODE_ID!r} prompt-template review hash drifted"
    assert caught.value.component_id == _NODE_ID
    assert caught.value.component_type == "transform"
    assert caught.value.kind is InterpretationKind.LLM_PROMPT_TEMPLATE


def test_model_choice_drift_raises_typed_integrity_error() -> None:
    state = _state_with_llm(
        {
            "prompt_template": _PROMPT,
            "model": _MODEL,
            INTERPRETATION_REQUIREMENTS_KEY: [
                _resolved_prompt_review(stable_hash(_PROMPT)),
                _resolved_model_review(model_choice_artifact_hash("some-other-model")),
            ],
        }
    )

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(state)

    assert str(caught.value) == f"llm node {_NODE_ID!r} model-choice review hash drifted"
    assert caught.value.component_id == _NODE_ID
    assert caught.value.component_type == "transform"
    assert caught.value.kind is InterpretationKind.LLM_MODEL_CHOICE


def test_pipeline_decision_drift_raises_typed_integrity_error() -> None:
    state = _state_with_cleanup_node(_pipeline_decision_options(status="resolved", artifact_hash=stable_hash("old node shape")))
    node_id = state.nodes[0].id

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(state)

    assert str(caught.value) == f"node {node_id!r} pipeline-decision review hash drifted"
    assert caught.value.component_id == node_id
    assert caught.value.component_type == "transform"
    assert caught.value.kind is InterpretationKind.PIPELINE_DECISION


# --- Finding #18 (red-team): every resolved-review drift raise is typed --------
#
# The hash-comparison raises were typed first, but a RESOLVED pipeline decision
# whose node no longer passes the decision's semantic check (or no longer
# yields an artifact) still raised a plain ValueError out of the materializer,
# so /validate 500'd on it. The reconcile guards for the same drift were plain
# ValueError as well. Each case below pins the class, the carried component and
# kind, and the unchanged message bytes.


def _assert_integrity_error(
    error: BaseException,
    *,
    message: str,
    component_id: str,
    component_type: str,
    kind: InterpretationKind,
) -> None:
    assert isinstance(error, InterpretationReviewIntegrityError)
    assert str(error) == message
    assert error.component_id == component_id
    assert error.component_type == component_type
    assert error.kind is kind


def _resolved_pipeline_decision(user_term: str, *, accepted_artifact_hash: str) -> dict[str, object]:
    return {
        "id": f"score_gate:{user_term}",
        "kind": InterpretationKind.PIPELINE_DECISION.value,
        "user_term": user_term,
        "status": "resolved",
        "draft": "Review this decision.",
        "event_id": "event-decision",
        "accepted_value": "Approved.",
        "accepted_artifact_hash": accepted_artifact_hash,
        "resolved_prompt_template_hash": None,
    }


def test_pipeline_decision_semantics_drift_raises_typed_integrity_error() -> None:
    reviewed = _state_with_cleanup_node(_pipeline_decision_options(status="resolved")).nodes[0]
    accepted_hash = pipeline_decision_artifact_hash(reviewed, (reviewed,), user_term=RAW_HTML_CLEANUP_USER_TERM)
    options = _pipeline_decision_options(status="resolved", artifact_hash=accepted_hash)
    options["select_only"] = False
    state = _state_with_cleanup_node(options)

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(state)

    _assert_integrity_error(
        caught.value,
        message="interpretation_state: raw-html cleanup decision requires field_mapper.select_only=true on node 'drop_raw_html'",
        component_id="drop_raw_html",
        component_type="transform",
        kind=InterpretationKind.PIPELINE_DECISION,
    )


def test_pipeline_decision_artifact_that_cannot_be_derived_raises_typed_integrity_error() -> None:
    gate = _gate(
        options={
            INTERPRETATION_REQUIREMENTS_KEY: [
                _resolved_pipeline_decision(REQUIRED_CONTROL_AUTO_WIRED_USER_TERM, accepted_artifact_hash="0" * 64),
            ]
        }
    )

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        materialize_state_for_execution(_gate_state(gate))

    _assert_integrity_error(
        caught.value,
        message="pipeline_decision_artifact_hash: required_control_auto_wired requires a plugin-bearing node",
        component_id="score_gate",
        component_type="transform",
        kind=InterpretationKind.PIPELINE_DECISION,
    )


def test_reconcile_node_review_hash_drift_raises_typed_integrity_error() -> None:
    options: dict[str, object] = {
        "prompt_template": _PROMPT,
        "model": _MODEL,
        INTERPRETATION_REQUIREMENTS_KEY: [_resolved_model_review(model_choice_artifact_hash("some-other-model"))],
    }

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(_state_with_llm(options), _state_with_llm(dict(options)))

    _assert_integrity_error(
        caught.value,
        message="resolved interpretation requirement 'model-choice-review' hash drifted",
        component_id=_NODE_ID,
        component_type="transform",
        kind=InterpretationKind.LLM_MODEL_CHOICE,
    )


def test_reconcile_resolved_decision_the_previous_node_no_longer_implements_raises_typed_integrity_error() -> None:
    requirement = _resolved_pipeline_decision(GATE_CONDITION_AUTHORED_USER_TERM, accepted_artifact_hash="0" * 64)
    gate = _gate(condition="   ", options={INTERPRETATION_REQUIREMENTS_KEY: [requirement]})

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(_gate_state(gate), _gate_state(gate))

    _assert_integrity_error(
        caught.value,
        message="reconcile_authoritative_reviews: authored gate-condition decision requires a non-empty condition on gate 'score_gate'",
        component_id="score_gate",
        component_type="transform",
        kind=InterpretationKind.PIPELINE_DECISION,
    )


def test_reconcile_proposed_node_that_no_longer_implements_a_decision_stays_a_plain_edit_refusal() -> None:
    """Control: only the PREVIOUS node's derivation is drift of the resolved row."""
    reviewed = _gate()
    accepted_hash = pipeline_decision_artifact_hash(reviewed, (reviewed,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)
    requirement = _resolved_pipeline_decision(GATE_CONDITION_AUTHORED_USER_TERM, accepted_artifact_hash=accepted_hash)
    previous = _gate(options={INTERPRETATION_REQUIREMENTS_KEY: [requirement]})
    proposed = _gate(condition="   ", options={INTERPRETATION_REQUIREMENTS_KEY: [dict(requirement)]})

    with pytest.raises(ValueError) as caught:
        reconcile_authoritative_reviews(_gate_state(previous), _gate_state(proposed))

    assert type(caught.value) is ValueError
    assert str(caught.value) == (
        "reconcile_authoritative_reviews: authored gate-condition decision requires a non-empty condition on gate 'score_gate'"
    )


def test_reconcile_vague_term_hash_drift_raises_typed_integrity_error() -> None:
    previous, proposed = _two_vague_term_fixtures(tone_hash=stable_hash("chilly"), audience_hash=stable_hash("new users"))

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(previous, proposed)

    _assert_integrity_error(
        caught.value,
        message="resolved vague-term review 'vague:tone' hash drifted",
        component_id="model",
        component_type="transform",
        kind=InterpretationKind.VAGUE_TERM,
    )


def test_reconcile_vague_term_prompt_drift_raises_typed_integrity_error() -> None:
    previous, proposed = _two_vague_term_fixtures(
        tone_hash=stable_hash("warm"),
        audience_hash=stable_hash("new users"),
        previous_prompt="Tone: chilly, audience: new users",
    )

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(previous, proposed)

    _assert_integrity_error(
        caught.value,
        message="resolved vague-term review 'vague:tone' prompt drifted from its parts render",
        component_id="model",
        component_type="transform",
        kind=InterpretationKind.VAGUE_TERM,
    )


def test_reconcile_source_data_contract_evidence_drift_raises_typed_integrity_error() -> None:
    options = _resolved_contract_options(["colour"])
    requirements = options[INTERPRETATION_REQUIREMENTS_KEY]
    requirements[0]["accepted_artifact_hash"] = source_data_contract_artifact_hash(["size"])
    previous = _contract_state(options, required=["colour"])

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(previous, _contract_state(_resolved_contract_options(["colour"]), required=["colour"]))

    _assert_integrity_error(
        caught.value,
        message="resolved interpretation requirement 'source-data-contract-source' evidence drifted",
        component_id=source_component_id("source"),
        component_type="source",
        kind=InterpretationKind.SOURCE_DATA_CONTRACT,
    )


def test_reconcile_invented_source_hash_drift_raises_typed_integrity_error() -> None:
    source_authoring = {
        "modality": "llm_generated",
        "content_hash": "a" * 64,
        "review_event_id": "event-1",
        "resolved_kind": InterpretationKind.INVENTED_SOURCE.value,
    }
    resolved = _reconciliation_requirement(
        requirement_id="source_review:inline_source_data",
        kind=InterpretationKind.INVENTED_SOURCE,
        user_term="inline_source_data",
        status="resolved",
        draft="generated rows",
        accepted_value="approved",
        accepted_artifact_hash="b" * 64,
    )
    source_options: dict[str, object] = {
        "blob_ref": "2e9e41eb-e34d-4918-b334-3c1e9ee0f8ff",
        "path": "/data/blobs/generated.csv",
        SOURCE_AUTHORING_KEY: source_authoring,
        INTERPRETATION_REQUIREMENTS_KEY: [resolved],
    }
    state = _reconciliation_state(nodes=(_reconciliation_node(plugin="passthrough"),), source_options=source_options)

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(state, state)

    _assert_integrity_error(
        caught.value,
        message="resolved interpretation requirement 'source_review:inline_source_data' hash drifted",
        component_id=source_component_id("source"),
        component_type="source",
        kind=InterpretationKind.INVENTED_SOURCE,
    )
