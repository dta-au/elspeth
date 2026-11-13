"""Regression tests for the 2026-11-13 AWS transform review, Bedrock guardrail findings.

Finding 5: a wrong-typed provider value must not escape the Tier-3 parser.
Finding 8: the transform config must cross-check a guardrail ARN against region.
Finding 4: guardrail rows need a deadline, a shutdown check and retryable throttles.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.plugins.transforms.aws.test_guardrails_client import (
    PROMPT_FILTERS,
    FakeExecution,
    _client,
    response,
)

from elspeth.contracts import CallStatus
from elspeth.plugins.transforms.aws.guardrails_client import GuardrailResponseError

# ---------------------------------------------------------------------------
# Finding 5: unhashable / wrong-typed provider values raise GuardrailResponseError
# and the call that was made is still recorded.
# ---------------------------------------------------------------------------


def _filter0(value: dict[str, Any]) -> dict[str, Any]:
    filters: list[dict[str, Any]] = value["assessments"][0]["contentPolicy"]["filters"]
    return filters[0]


WRONG_TYPE_MUTATORS = {
    "confidence_list": lambda value: _filter0(value).update(confidence=["HIGH"]),
    "confidence_dict": lambda value: _filter0(value).update(confidence={"HIGH": 1}),
    "confidence_int": lambda value: _filter0(value).update(confidence=1),
    "filter_action_list": lambda value: _filter0(value).update(action=["NONE"]),
    "filter_action_dict": lambda value: _filter0(value).update(action={"NONE": 1}),
    "filter_strength_list": lambda value: _filter0(value).update(filterStrength=["HIGH"]),
    "filter_strength_dict": lambda value: _filter0(value).update(filterStrength={"HIGH": 1}),
    "top_action_list": lambda value: value.update(action=["NONE"]),
    "top_action_dict": lambda value: value.update(action={"NONE": 1}),
}


@pytest.mark.parametrize("mutator_name", sorted(WRONG_TYPE_MUTATORS))
def test_wrong_typed_provider_value_is_a_recorded_malformed_response(mutator_name: str) -> None:
    malformed = response()
    WRONG_TYPE_MUTATORS[mutator_name](malformed)
    sdk_calls: list[dict[str, object]] = []

    class WrongTypeSDK:
        def apply_guardrail(self, **kwargs: object) -> dict[str, Any]:
            sdk_calls.append(kwargs)
            return malformed

    execution = FakeExecution()
    raised: BaseException | None = None
    try:
        _client(WrongTypeSDK(), execution, []).apply_guardrail(text="marker", source="INPUT", required_filters=PROMPT_FILTERS)
    except Exception as error:
        raised = error

    assert len(sdk_calls) == 1
    assert isinstance(raised, GuardrailResponseError), f"expected GuardrailResponseError, got {type(raised).__name__}"
    assert len(execution.calls) == 1, "the SDK call was made, so it must be recorded"
    assert execution.calls[0]["status"] is CallStatus.ERROR
    assert execution.calls[0]["error"].to_dict() == {"type": "malformed_response", "retryable": False}
