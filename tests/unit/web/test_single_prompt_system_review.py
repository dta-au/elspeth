"""System persona changes must invalidate single-query prompt approval."""

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    InterpretationReviewIntegrityError,
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    reconcile_authoritative_reviews,
)
from tests.unit.web.test_interpretation_state import _resolved_prompt_review, _state_with_llm


def _approved_options(*, structured: bool, system: str | None) -> dict[str, object]:
    prompt = "Describe {{ row.text }}"
    options: dict[str, object] = {"prompt_template": prompt, "system_prompt": system}
    if structured:
        options[PROMPT_TEMPLATE_PARTS_KEY] = [{"kind": "text", "text": prompt}]
    anchor = prompt_review_anchor_hash_from_options(options)
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_resolved_prompt_review(anchor or stable_hash(prompt), prompt)]
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return options


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("original,changed", [("Decorator", "Assistant"), (None, "Assistant"), ("Decorator", None)])
def test_execution_rejects_system_drift_after_single_prompt_approval(structured: bool, original: str | None, changed: str | None) -> None:
    options = _approved_options(structured=structured, system=original)
    unchanged = materialize_state_for_execution(_state_with_llm(options))
    assert isinstance(unchanged, CompositionState)
    assert unchanged.nodes[0].options["approved_prompt_artifact_hash"] == options["approved_prompt_artifact_hash"]

    with pytest.raises(InterpretationReviewIntegrityError, match="prompt-template review hash drifted"):
        materialize_state_for_execution(_state_with_llm({**options, "system_prompt": changed}))


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("original,changed", [("Decorator", "Assistant"), (None, "Assistant"), ("Decorator", None)])
def test_reconciliation_requires_review_after_single_prompt_system_change(
    structured: bool, original: str | None, changed: str | None
) -> None:
    options = _approved_options(structured=structured, system=original)
    previous = _state_with_llm(options)
    unchanged = reconcile_authoritative_reviews(previous, previous)
    assert isinstance(materialize_state_for_execution(unchanged), CompositionState)

    proposed = _state_with_llm({**options, "system_prompt": changed})
    reconciled = reconcile_authoritative_reviews(previous, proposed)
    assert "approved_prompt_artifact_hash" not in reconciled.nodes[0].options
    assert isinstance(materialize_state_for_execution(reconciled), InterpretationReviewPending)
