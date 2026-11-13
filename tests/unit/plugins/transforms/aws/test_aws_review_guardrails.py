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


# ---------------------------------------------------------------------------
# Finding 4: guardrail rows have a row deadline and a shutdown check, a
# retryable service failure goes to the engine's retry path, and a terminal
# failure says whether it was a malformed response or a service error.
# ---------------------------------------------------------------------------


def _started_guardrail(fields: list[str]) -> tuple[Any, Any]:
    from tests.fixtures.factories import make_context
    from tests.fixtures.mock_audit import mock_item_audit_authority

    from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield

    transform = AWSBedrockPromptShield(_transform_config(fields=fields))
    transform._sdk_client = object()
    context = make_context(landscape=FakeExecution(), **mock_item_audit_authority())
    transform.on_start(context)
    return transform, context


def _safe_decision() -> Any:
    from elspeth.plugins.transforms.aws.guardrails_client import GuardrailDecision, GuardrailUsage

    return GuardrailDecision(
        detected=False,
        intervened=False,
        matched_filters=(),
        usage=GuardrailUsage((("contentPolicyUnits", 1),)),
        request_id="request-1",
    )


def _row(**values: str) -> Any:
    from elspeth.testing import make_pipeline_row

    return make_pipeline_row(dict(values))


def test_guardrail_row_deadline_symbols_exist() -> None:
    from elspeth.plugins.transforms.aws import _guardrail_transform

    assert hasattr(_guardrail_transform, "GUARDRAIL_ROW_DEADLINE_SECONDS")
    transform, _context = _started_guardrail(["prompt"])
    assert hasattr(transform, "_row_deadline_seconds")
    assert hasattr(transform, "_monotonic")


def test_row_deadline_stops_scanning_remaining_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient

    transform, context = _started_guardrail(["first", "second", "third"])
    now = [1000.0]
    transform._row_deadline_seconds = 60.0
    transform._monotonic = lambda: now[0]
    calls: list[str] = []

    def apply(_client: object, **kwargs: object) -> Any:
        calls.append(str(kwargs["text"]))
        now[0] += 61.0  # a degraded endpoint spends the whole row budget on one field
        return _safe_decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(first="a", second="b", third="c"), context)

    assert calls == ["a"], "no field call may start once the row deadline has passed"
    assert result.status == "error", "a row with unscanned fields must never validate"
    assert result.reason is not None
    assert result.reason["reason"] == "retry_timeout"
    assert result.reason["error_type"] == "row_deadline_exceeded"
    assert result.reason["field"] == "second"
    assert result.retryable is False


def test_shutdown_before_first_field_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient

    transform, context = _started_guardrail(["prompt"])
    context.shutdown_event = threading.Event()
    context.shutdown_event.set()
    calls: list[str] = []

    def apply(_client: object, **kwargs: object) -> Any:
        calls.append(str(kwargs["text"]))
        return _safe_decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(prompt="a"), context)

    assert calls == []
    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "shutdown_requested"
    assert result.reason["field"] == "prompt"


def test_shutdown_between_fields_stops_the_field_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient

    transform, context = _started_guardrail(["first", "second"])
    context.shutdown_event = threading.Event()
    calls: list[str] = []

    def apply(_client: object, **kwargs: object) -> Any:
        calls.append(str(kwargs["text"]))
        context.shutdown_event.set()
        return _safe_decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(first="a", second="b"), context)

    assert calls == ["a"]
    assert result.status == "error", "a row stopped by shutdown must never validate"
    assert result.reason is not None
    assert result.reason["reason"] == "shutdown_requested"
    assert result.reason["field"] == "second"


def test_transform_close_stops_the_field_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient

    transform, context = _started_guardrail(["first", "second"])

    class _ClosableSDK:
        def close(self) -> None:
            return None

    transform._sdk_client = _ClosableSDK()
    calls: list[str] = []

    def apply(_client: object, **kwargs: object) -> Any:
        calls.append(str(kwargs["text"]))
        transform.close()
        return _safe_decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(first="a", second="b"), context)

    assert calls == ["a"]
    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "shutdown_requested"


@pytest.mark.parametrize("failing_field", ["first", "second"])
def test_retryable_service_error_goes_to_engine_retry(monkeypatch: pytest.MonkeyPatch, failing_field: str) -> None:
    from elspeth.contracts.errors import PluginRetryableError
    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient, GuardrailServiceError

    transform, context = _started_guardrail(["first", "second"])

    def apply(_client: object, **kwargs: object) -> Any:
        if kwargs["text"] == failing_field:
            raise GuardrailServiceError(retryable=True)
        return _safe_decision()

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    raised: BaseException | None = None
    result: Any = None
    try:
        result = transform.process(_row(first="first", second="second"), context)
    except PluginRetryableError as error:
        raised = error

    assert result is None, f"a throttled field must not end in a row result, got {result!r}"
    assert isinstance(raised, PluginRetryableError)
    assert raised.retryable is True


def test_terminal_service_error_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient, GuardrailServiceError

    transform, context = _started_guardrail(["prompt"])

    def apply(_client: object, **_kwargs: object) -> Any:
        raise GuardrailServiceError(retryable=False)

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(prompt="a"), context)

    assert result.status == "error"
    assert result.reason == {"reason": "api_call_failed", "field": "prompt", "error_type": "service_error"}
    assert result.retryable is False


def test_malformed_response_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient

    transform, context = _started_guardrail(["prompt"])

    def apply(_client: object, **_kwargs: object) -> Any:
        raise GuardrailResponseError

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", apply)
    result = transform.process(_row(prompt="a"), context)

    assert result.status == "error"
    assert result.reason == {"reason": "api_call_failed", "field": "prompt", "error_type": "malformed_response"}
    assert result.retryable is False
