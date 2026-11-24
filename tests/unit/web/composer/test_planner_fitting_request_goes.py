"""A planner request under the byte maximum always goes; a gateway's length rejection is the size refusal.

The token estimate decides what the planner leaves out, never whether its
request is sent: the byte maximum is the only limit that stops it, as on the
compose loop. When the model gateway rejects a sent request as over the
model's context limit, the planner leaves out any earlier turns and resends
once. A further rejection raises the context budget refusal naming the
provider as the ceiling, and each rejected call is recorded by its closed
reason. A rejection that is not recognised as size stays a provider failure.

Each test drives the real planner against a scripted ``litellm.acompletion``
(``test_pipeline_planner._plan``). The Bedrock forms are built by the installed
litellm's own exception mapping, as they are at runtime on a ``bedrock/`` model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from litellm.exceptions import BadRequestError, ContextWindowExceededError
from litellm.litellm_core_utils.exception_mapping_utils import exception_type
from litellm.llms.bedrock.common_utils import BedrockError
from openai import BadRequestError as OpenAIBadRequestError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_planner_audit import ComposerPlannerCode
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import ContextBudgetPolicy
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerDeclined
from elspeth.web.composer.tools import ToolContext
from elspeth.web.sessions.routes._helpers import _FREEFORM_PLANNER_FAILURE_HTTP, _freeform_planner_failure_code
from tests.unit.web.composer.test_pipeline_planner import _context_policy, _plan, _ScriptedCompletion, _text_response
from tests.unit.web.composer.test_planner_context_budget import _context, _decline, _payload, _sent_bytes, _sent_matches_record

_FALLBACK_RATIO = 1.0  # ``_context_policy`` prices every byte at one token
_OVERSIZED_INTENT = "Build this: " + "z" * 150_000
# Planted in every provider message: it must reach neither the planner error nor the call record.
_CANARY = "PROVIDER-TEXT-CANARY-5b21e0"
_REQUEST = httpx.Request("POST", "https://gateway.invalid/model/invoke")


async def _token_policy_below_the_oversized_intent(tmp_path: Path, tool_context: ToolContext) -> ContextBudgetPolicy:
    """A token setting the oversized intent alone is estimated over, with the byte maximum far above the request."""
    small_request, _ = await _decline(tmp_path, tool_context, _context(users={0: "Build CSV to JSON."}))
    policy = _context_policy(
        max_prompt_tokens=math.ceil((_sent_bytes(small_request) + 10_000) / _FALLBACK_RATIO), max_request_bytes=1_000_000
    )
    # The small request fits; with the oversized intent added, the protected request is estimated over the setting.
    assert _sent_bytes(small_request) / _FALLBACK_RATIO < policy.max_prompt_tokens
    assert (_sent_bytes(small_request) + len(_OVERSIZED_INTENT)) / _FALLBACK_RATIO > policy.max_prompt_tokens
    return policy


async def _plan_once(
    tmp_path: Path,
    tool_context: ToolContext,
    completion: _ScriptedCompletion,
    recorder: BufferingRecorder,
    *,
    policy: ContextBudgetPolicy | None = None,
    intent: str = "Build the requested pipeline.",
) -> PipelinePlannerError:
    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            intent=intent,
            conversation_context=_context(users={0: "Build CSV to JSON."}),
            context_policy=policy,
        )
    return caught.value


# --- A request over the estimate and under the byte maximum is sent ---------------------------


@pytest.mark.asyncio
async def test_a_request_over_the_token_estimate_and_under_the_byte_maximum_is_sent_and_recorded_as_over(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    policy = await _token_policy_below_the_oversized_intent(tmp_path, tool_context)
    completion = _ScriptedCompletion(_text_response("Which output format do you want?"))
    recorder = BufferingRecorder()

    outcome = await _plan_once(tmp_path, tool_context, completion, recorder, policy=policy, intent=_OVERSIZED_INTENT)

    assert type(outcome) is PlannerDeclined
    (request,) = completion.requests
    assert any(_OVERSIZED_INTENT in message["content"] for message in request["messages"] if type(message["content"]) is str)
    (call,) = recorder.llm_calls
    record = call.context_budget
    assert record is not None
    assert record.over_estimate is True
    assert record.max_prompt_tokens == policy.max_prompt_tokens
    assert record.estimated_tokens_after > policy.max_prompt_tokens
    # Sent over the setting only with everything that could be left out left out.
    assert record.shed_history_indices == (0,)
    assert record.request_wire_bytes == _sent_bytes(request) <= policy.max_request_bytes
    written = record.to_dict()
    assert written["over_estimate"] is True and written["max_prompt_tokens"] == policy.max_prompt_tokens


@pytest.mark.asyncio
async def test_a_request_within_the_token_estimate_records_that_it_was_not_over(tmp_path: Path, tool_context: ToolContext) -> None:
    policy = _context_policy()
    request, call = await _decline(tmp_path, tool_context, _context(users={0: "Build CSV to JSON."}), policy=policy)

    record = call.context_budget
    assert record is not None
    assert record.over_estimate is False
    assert record.max_prompt_tokens == policy.max_prompt_tokens
    assert record.shed_history_indices == ()
    assert record.request_wire_bytes == _sent_bytes(request)


@pytest.mark.asyncio
async def test_control_a_request_over_the_byte_maximum_is_still_refused_before_any_provider_call(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    """Generous tokens, so the byte comparison is the only one that can refuse."""
    small_request, _ = await _decline(tmp_path, tool_context, _context(users={0: "Build CSV to JSON."}))
    policy = _context_policy(max_request_bytes=_sent_bytes(small_request) + 10_000)
    completion = _ScriptedCompletion(_text_response("Must not dispatch."))
    recorder = BufferingRecorder()

    refusal = await _plan_once(tmp_path, tool_context, completion, recorder, policy=policy, intent=_OVERSIZED_INTENT)

    assert refusal.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert refusal.detail_codes == ("context_ceiling_bytes", "largest_component_request")
    assert completion.requests == []
    assert recorder.llm_calls == ()


# --- A gateway's length rejection is the size refusal -----------------------------------------


def _bedrock(message: str, *, provider: str = "bedrock") -> Callable[[], BaseException]:
    """The exception the installed litellm raises for a Bedrock (or Bedrock Mantle) error with this text.

    litellm maps a ``bedrock`` provider error whose text names the context
    window to ``ContextWindowExceededError`` before looking at the status
    (``litellm_core_utils/exception_mapping_utils.py``, ``_map_bedrock_exception``).
    """

    def build() -> BaseException:
        response = httpx.Response(400, request=_REQUEST, content=json.dumps({"message": message}).encode())
        try:
            exception_type(
                model="bedrock/anthropic.claude-opus-4-8",
                original_exception=BedrockError(status_code=400, message=message, request=_REQUEST, response=response),
                custom_llm_provider=provider,
            )
        except ContextWindowExceededError as mapped:
            return mapped
        raise AssertionError(f"litellm did not map {message!r} to ContextWindowExceededError")

    return build


_LENGTH_REJECTIONS = [
    # The shapes of test_gateway_capacity_rejection.py, as the planner holds them: the raw SDK exception.
    pytest.param(
        lambda: ContextWindowExceededError(
            message=f"AnthropicError - prompt is too long: 251000 tokens > 200000 maximum {_CANARY}",
            model="test-model",
            llm_provider="anthropic",
        ),
        id="context-window-class",
    ),
    pytest.param(
        lambda: BadRequestError(
            message=f"GatewayError - AnthropicException - prompt is too long: 251000 tokens > 200000 maximum {_CANARY}",
            model="test-model",
            llm_provider="openai",
        ),
        id="gateway-400-wording",
    ),
    pytest.param(
        lambda: OpenAIBadRequestError(
            f"rejected {_CANARY}", response=httpx.Response(400, request=_REQUEST), body={"code": "context_length_exceeded"}
        ),
        id="openai-code-body",
    ),
    # Bedrock, as the installed litellm surfaces it.
    pytest.param(_bedrock(f"Input is too long for requested model. {_CANARY}"), id="bedrock-input-too-long"),
    pytest.param(_bedrock(f"prompt is too long: 251000 tokens > 200000 maximum {_CANARY}"), id="bedrock-prompt-too-long"),
    pytest.param(
        _bedrock(f"Too many input tokens. Max input tokens: 200000, request input token count: 251000 {_CANARY}"),
        id="bedrock-too-many-input-tokens",
    ),
    pytest.param(
        _bedrock(
            json.dumps({"type": "invalid_request_error", "message": f"prompt tokens (251000) exceed model maximum (200000) {_CANARY}"}),
            provider="bedrock_mantle",
        ),
        id="bedrock-mantle",
    ),
    pytest.param(
        lambda: BadRequestError(
            message=f"BedrockException - Input is too long for requested model. {_CANARY}", model="test-model", llm_provider="bedrock"
        ),
        id="bedrock-400-wording",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", _LENGTH_REJECTIONS)
async def test_a_gateway_length_rejection_on_the_planner_path_is_the_size_refusal_naming_the_provider(
    tmp_path: Path, tool_context: ToolContext, rejection: Callable[[], BaseException]
) -> None:
    completion = _ScriptedCompletion(rejection(), rejection())
    recorder = BufferingRecorder()

    refusal = await _plan_once(tmp_path, tool_context, completion, recorder)

    assert refusal.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert refusal.detail_codes[0] == "context_ceiling_provider"
    assert refusal.detail_codes[1].startswith("largest_component_")
    assert len(completion.requests) == 2
    call = recorder.llm_calls[-1]
    assert all(row.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR for row in recorder.llm_calls)
    # Recorded by its closed reason, never by provider text.
    assert call.error_message == "context_length_exceeded"
    assert call.context_budget is not None and call.context_budget.over_estimate is False
    assert _CANARY not in str(refusal)
    assert _CANARY not in canonical_json(call.to_dict())
    # The user is given the size refusal (the route picks the provider sentence for this ceiling).
    assert _freeform_planner_failure_code(refusal) == "context_budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai", "bedrock", "bedrock_mantle"])
async def test_a_413_on_the_planner_path_is_the_size_refusal_naming_the_provider(
    tmp_path: Path, tool_context: ToolContext, route: str
) -> None:
    from tests.unit.web.composer.test_request_too_large import mapped_provider_error

    completion = _ScriptedCompletion(mapped_provider_error(route, 413), mapped_provider_error(route, 413))
    recorder = BufferingRecorder()

    refusal = await _plan_once(tmp_path, tool_context, completion, recorder)

    assert refusal.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert refusal.detail_codes[0] == "context_ceiling_provider"
    assert len(completion.requests) == 2
    assert all(row.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR for row in recorder.llm_calls)
    assert recorder.llm_calls[-1].error_message == "request_too_large"
    assert _freeform_planner_failure_code(refusal) == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_planner_resends_size_rejection_with_all_earlier_turns_left_out(tmp_path: Path, tool_context: ToolContext) -> None:
    rejection = ContextWindowExceededError(message="prompt is too long", model="test-model", llm_provider="anthropic")
    completion = _ScriptedCompletion(rejection, _text_response("Which output format do you want?"))
    recorder = BufferingRecorder()

    with pytest.raises(PlannerDeclined):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            conversation_context=_context(users={0: "Build CSV to JSON."}, assistants={1: "Keep every row."}),
            model_overrides={"max_api_attempts": 1},
        )

    assert len(completion.requests) == len(recorder.llm_calls) == 2
    assert _payload(completion.requests[0])["conversation_context"]["prior_user_requests"]
    resent_context = _payload(completion.requests[1])["conversation_context"]
    assert resent_context["prior_user_requests"] == []
    assert "prior_assistant_messages" not in resent_context
    assert recorder.llm_calls[1].context_budget is not None
    assert recorder.llm_calls[1].context_budget.shed_history_indices == (0, 1)
    for request, call in zip(completion.requests, recorder.llm_calls, strict=True):
        _sent_matches_record(request, call)


@pytest.mark.asyncio
async def test_planner_size_rejection_without_earlier_turns_is_not_resent(tmp_path: Path, tool_context: ToolContext) -> None:
    rejection = ContextWindowExceededError(message="prompt is too long", model="test-model", llm_provider="anthropic")
    completion = _ScriptedCompletion(rejection)
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert caught.value.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert len(completion.requests) == 1


@pytest.mark.asyncio
async def test_control_an_ordinary_bad_request_on_the_planner_path_stays_a_provider_failure(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    completion = _ScriptedCompletion(
        BadRequestError(
            message=f"GatewayError - unsupported value for field 'temperature' {_CANARY}", model="test-model", llm_provider="openai"
        )
    )
    recorder = BufferingRecorder()

    failure = await _plan_once(tmp_path, tool_context, completion, recorder)

    assert failure.code == "PROVIDER_ERROR"
    assert failure.detail_codes == ()
    (call,) = recorder.llm_calls
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_message == "BadRequestError"
    assert _FREEFORM_PLANNER_FAILURE_HTTP[_freeform_planner_failure_code(failure)][0] == 503


# --- What the user sees vs what the record shows ----------------------------------------------


@pytest.mark.asyncio
async def test_a_request_sent_over_the_estimate_and_rejected_for_another_reason_is_recorded_as_over(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    """The user is told the provider failed; the rejected call's record says the request went out over the estimate."""
    policy = await _token_policy_below_the_oversized_intent(tmp_path, tool_context)
    completion = _ScriptedCompletion(
        BadRequestError(message=f"GatewayError - request rejected by policy {_CANARY}", model="test-model", llm_provider="openai")
    )
    recorder = BufferingRecorder()

    failure = await _plan_once(tmp_path, tool_context, completion, recorder, policy=policy, intent=_OVERSIZED_INTENT)

    assert failure.code == "PROVIDER_ERROR"
    assert _FREEFORM_PLANNER_FAILURE_HTTP[_freeform_planner_failure_code(failure)][0] == 503
    assert len(completion.requests) == 1
    (call,) = recorder.llm_calls
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_message == "BadRequestError"
    record = call.context_budget
    assert record is not None
    assert record.over_estimate is True
    assert record.max_prompt_tokens == policy.max_prompt_tokens
    assert _CANARY not in canonical_json(call.to_dict())
