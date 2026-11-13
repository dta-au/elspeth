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


# ---------------------------------------------------------------------------
# Finding 8: the transform config cross-checks a guardrail ARN's partition and
# region against ``region`` through the same shared function as the profile.
# ---------------------------------------------------------------------------

_MISMATCHED_ARNS = {
    "other_region": "arn:aws:bedrock:us-west-2:123456789012:guardrail/abc123",
    "govcloud_partition": "arn:aws-us-gov:bedrock:us-east-1:123456789012:guardrail/abc123",
}


def _transform_config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "guardrail_identifier": "privateguardrail",
        "guardrail_version": "7",
        "region": "us-east-1",
        "fields": ["prompt"],
        "schema": {"mode": "observed"},
    }
    values.update(overrides)
    return values


def test_shared_arn_binding_validator_exists() -> None:
    from elspeth.plugins.transforms.aws import guardrail_profiles

    assert hasattr(guardrail_profiles, "validate_guardrail_arn_binding")


@pytest.mark.parametrize("arn_name", sorted(_MISMATCHED_ARNS))
@pytest.mark.parametrize("plugin_name", ["prompt_shield", "content_safety"])
def test_transform_config_rejects_arn_contradicting_region(arn_name: str, plugin_name: str) -> None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.transforms.aws.bedrock_content_safety import AWSBedrockContentSafety
    from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield

    plugin_cls: Any = AWSBedrockPromptShield if plugin_name == "prompt_shield" else AWSBedrockContentSafety
    config = _transform_config(guardrail_identifier=_MISMATCHED_ARNS[arn_name])
    rejected = False
    try:
        plugin_cls(config)
    except PluginConfigError:
        rejected = True
    assert rejected, f"{plugin_name} accepted a guardrail ARN that contradicts region us-east-1 ({arn_name})"


def test_transform_config_accepts_matching_arn_and_bare_identifier() -> None:
    from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield

    AWSBedrockPromptShield(_transform_config(guardrail_identifier="arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123"))
    AWSBedrockPromptShield(_transform_config(guardrail_identifier="abc123"))


@pytest.mark.parametrize("arn_name", sorted(_MISMATCHED_ARNS))
def test_profile_and_transform_share_the_arn_binding_message(arn_name: str) -> None:
    from elspeth.plugins.transforms.aws import guardrail_profiles

    assert hasattr(guardrail_profiles, "validate_guardrail_arn_binding")
    with pytest.raises(ValueError, match="partition and region"):
        guardrail_profiles.validate_guardrail_arn_binding(_MISMATCHED_ARNS[arn_name], region="us-east-1")
