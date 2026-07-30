import warnings

import pytest
from elspeth_llm_gateway.core.contract import (
    Bounds,
    ChatFunctionDef,
    ChatMessage,
    ChatRequest,
    ChatTool,
    ChatToolCall,
    ChatToolCallFunction,
    JsonSchemaFormat,
    NamedToolChoice,
    NamedToolChoiceFunction,
    ResponseFormat,
    bounds_check,
    build_completion_response,
)
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.sdk.types import CanonicalResponse, CanonicalToolCall, CanonicalUsage, FinishReason
from pydantic import ValidationError

# --- ChatRequest: unknown top-level fields rejected --------------------------


@pytest.mark.parametrize(
    "extra_field",
    [
        {"stream": True},
        {"n": 2},
        {"logprobs": True},
    ],
)
def test_chat_request_rejects_unknown_top_level_field(extra_field):
    with pytest.raises(ValidationError):
        ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            **extra_field,
        )


def test_chat_request_rejects_nested_unknown_field_in_message():
    with pytest.raises(ValidationError):
        ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi", "name": "bob"}],
        )


def test_chat_request_happy_path():
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    assert request.model == "m"
    assert request.messages[0].content == "hi"


def test_chat_request_rejects_empty_messages():
    with pytest.raises(ValidationError):
        ChatRequest(model="m", messages=[])


@pytest.mark.parametrize("value", ["auto", "none", "required"])
def test_chat_request_accepts_valid_string_tool_choice(value):
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], tool_choice=value)
    assert request.tool_choice == value


def test_chat_request_rejects_invalid_string_tool_choice():
    with pytest.raises(ValidationError):
        ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], tool_choice="bogus")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_chat_request_rejects_non_finite_temperature(value):
    with pytest.raises(ValidationError):
        ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], temperature=value)


# --- ChatMessage validators ---------------------------------------------------


def test_chat_message_tool_role_without_tool_call_id_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(role="tool", content="result")


def test_chat_message_tool_role_with_tool_call_id_accepted():
    message = ChatMessage(role="tool", content="result", tool_call_id="call_1")
    assert message.tool_call_id == "call_1"


def test_chat_message_non_tool_role_with_tool_call_id_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="hi", tool_call_id="call_1")


def test_chat_message_tool_calls_on_non_assistant_role_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(
            role="user",
            content="hi",
            tool_calls=[ChatToolCall(id="call_1", type="function", function=ChatToolCallFunction(name="f", arguments="{}"))],
        )


def test_chat_message_tool_calls_on_assistant_role_accepted():
    message = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[ChatToolCall(id="call_1", type="function", function=ChatToolCallFunction(name="f", arguments="{}"))],
    )
    assert message.tool_calls[0].id == "call_1"


@pytest.mark.parametrize("role", ["system", "user", "tool"])
def test_chat_message_content_none_rejected_for_non_assistant_roles(role):
    kwargs = {"role": role, "content": None}
    if role == "tool":
        kwargs["tool_call_id"] = "call_1"
    with pytest.raises(ValidationError):
        ChatMessage(**kwargs)


def test_chat_message_content_none_accepted_for_assistant_with_tool_calls():
    message = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[ChatToolCall(id="call_1", type="function", function=ChatToolCallFunction(name="f", arguments="{}"))],
    )
    assert message.content is None


# --- tool_choice: named choice must reference a declared tool ---------------


def test_named_tool_choice_accepted_when_function_declared():
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "description": None, "parameters": None},
            }
        ],
        tool_choice={"type": "function", "function": {"name": "f"}},
    )
    assert isinstance(request.tool_choice, NamedToolChoice)
    assert request.tool_choice.function.name == "f"


def test_named_tool_choice_rejected_when_function_not_declared():
    with pytest.raises(ValidationError):
        ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "other", "description": None, "parameters": None},
                }
            ],
            tool_choice={"type": "function", "function": {"name": "f"}},
        )


def test_named_tool_choice_rejected_when_no_tools_declared():
    with pytest.raises(ValidationError):
        ChatRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tool_choice={"type": "function", "function": {"name": "f"}},
        )


# --- JsonSchemaFormat: "schema" alias with zero pydantic warnings ------------


def test_json_schema_format_accepts_schema_alias_with_no_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        instance = JsonSchemaFormat(name="my_schema", schema={"type": "object"}, strict=True)
    assert instance.schema_definition == {"type": "object"}


def test_json_schema_format_accepts_populate_by_name():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        instance = JsonSchemaFormat(name="my_schema", schema_definition={"type": "object"})
    assert instance.schema_definition == {"type": "object"}


def test_json_schema_format_forbids_extra():
    with pytest.raises(ValidationError):
        JsonSchemaFormat(name="my_schema", schema={"type": "object"}, extra_field=1)


# --- ResponseFormat: json_schema required iff type is json_schema -----------


def test_response_format_json_schema_required_for_json_schema_type():
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_schema", json_schema=None)


def test_response_format_json_schema_forbidden_for_json_object_type():
    with pytest.raises(ValidationError):
        ResponseFormat(
            type="json_object",
            json_schema=JsonSchemaFormat(name="s", schema={"type": "object"}),
        )


def test_response_format_json_object_without_schema_accepted():
    response_format = ResponseFormat(type="json_object", json_schema=None)
    assert response_format.json_schema is None


def test_response_format_json_schema_with_schema_accepted():
    response_format = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaFormat(name="s", schema={"type": "object"}),
    )
    assert response_format.json_schema is not None


# --- ChatFunctionDef / ChatTool / ChatToolCall basics ------------------------


def test_chat_function_def_defaults():
    function_def = ChatFunctionDef(name="f")
    assert function_def.description is None
    assert function_def.parameters is None


def test_chat_tool_forbids_extra():
    with pytest.raises(ValidationError):
        ChatTool(type="function", function=ChatFunctionDef(name="f"), extra_field=1)


def test_named_tool_choice_function_forbids_extra():
    with pytest.raises(ValidationError):
        NamedToolChoiceFunction(name="f", extra_field=1)


# --- ChatToolCallFunction: arguments must be strict-JSON-parseable ----------


def test_chat_tool_call_function_rejects_non_json_arguments():
    with pytest.raises(ValidationError):
        ChatToolCallFunction(name="f", arguments="NOT JSON")


def test_chat_tool_call_function_accepts_valid_json_arguments():
    function = ChatToolCallFunction(name="f", arguments='{"a": 1}')
    assert function.arguments == '{"a": 1}'


def test_chat_tool_call_function_rejects_duplicate_key_arguments():
    with pytest.raises(ValidationError):
        ChatToolCallFunction(name="f", arguments='{"a":1,"a":2}')


def test_chat_tool_call_function_rejects_non_finite_number_arguments():
    with pytest.raises(ValidationError):
        ChatToolCallFunction(name="f", arguments='{"a": Infinity}')


def test_chat_request_with_malformed_tool_call_arguments_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(
            model="m",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "NOT JSON"}}],
                }
            ],
        )


# --- build_completion_response: golden tests ---------------------------------


def test_build_completion_response_text_only():
    canonical = CanonicalResponse(text="hello", tool_calls=(), finish_reason=FinishReason.STOP, usage=None)
    response = build_completion_response(response_id="resp_1", created=1234, model_alias="gpt-4o", canonical=canonical)
    assert response == {
        "id": "resp_1",
        "object": "chat.completion",
        "created": 1234,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
    }


def test_build_completion_response_tool_call():
    canonical = CanonicalResponse(
        text=None,
        tool_calls=(CanonicalToolCall(call_id="call_1", name="f", arguments_json="{}"),),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=None,
    )
    response = build_completion_response(response_id="resp_2", created=1234, model_alias="gpt-4o", canonical=canonical)
    assert response["choices"][0]["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ],
    }
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert "usage" not in response


def test_build_completion_response_usage_present():
    canonical = CanonicalResponse(
        text="hi",
        tool_calls=(),
        finish_reason=FinishReason.STOP,
        usage=CanonicalUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    response = build_completion_response(response_id="resp_3", created=1234, model_alias="gpt-4o", canonical=canonical)
    assert response["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_build_completion_response_usage_absent():
    canonical = CanonicalResponse(text="hi", tool_calls=(), finish_reason=FinishReason.STOP, usage=None)
    response = build_completion_response(response_id="resp_4", created=1234, model_alias="gpt-4o", canonical=canonical)
    assert "usage" not in response


# --- bounds_check -------------------------------------------------------------


def _bounds(**overrides):
    defaults = {
        "max_messages": 10,
        "max_tools": 10,
        "max_string_chars": 1000,
        "max_schema_bytes": 1000,
        "max_schema_depth": 5,
    }
    defaults.update(overrides)
    return Bounds(**defaults)


def test_bounds_check_happy_path_does_not_raise():
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100)
    bounds_check(request, _bounds())


def test_bounds_check_rejects_too_many_messages():
    messages = [{"role": "user", "content": "hi"}] * 11
    request = ChatRequest(model="m", messages=messages)
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_messages=10))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_oversized_string():
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "x" * 2000}])
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_string_chars=1000))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_too_many_tools():
    tools = [{"type": "function", "function": {"name": f"f{i}"}} for i in range(11)]
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], tools=tools)
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_tools=10))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_oversized_schema():
    deep_schema = {"type": "object"}
    for _ in range(20):
        deep_schema = {"type": "object", "properties": {"nested": deep_schema}}
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "s", "schema": deep_schema},
        },
    )
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_schema_depth=5))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_oversized_schema_bytes():
    big_schema = {"type": "object", "description": "x" * 5000}
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "s", "schema": big_schema},
        },
    )
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_schema_bytes=1000))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_oversized_tool_parameters_schema():
    deep_schema = {"type": "object"}
    for _ in range(20):
        deep_schema = {"type": "object", "properties": {"nested": deep_schema}}
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": deep_schema}}],
    )
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_schema_depth=5))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_oversized_tool_parameters_schema_bytes():
    big_schema = {"type": "object", "description": "x" * 5000}
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": big_schema}}],
    )
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_schema_bytes=1000))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_schema_exceeds_depth_handles_deeply_nested_structure_without_recursion_error():
    # Guards the iterative-stack implementation: a naive recursive walker
    # would raise RecursionError on structures this deep, long before the
    # configured max_schema_depth bound could reject it.
    deep_schema: dict = {"type": "object"}
    for _ in range(5000):
        deep_schema = {"type": "object", "properties": {"nested": deep_schema}}
    request = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema", "json_schema": {"name": "s", "schema": deep_schema}},
    )
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_schema_depth=5))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_bounds_check_rejects_temperature_out_of_range(temperature):
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], temperature=temperature)
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds())
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("temperature", [0.0, 2.0, 1.0])
def test_bounds_check_accepts_temperature_within_range(temperature):
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], temperature=temperature)
    bounds_check(request, _bounds())


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_bounds_check_rejects_non_positive_max_tokens(max_tokens):
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=max_tokens)
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds())
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_rejects_max_tokens_above_cap():
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    with pytest.raises(GatewayError) as exc_info:
        bounds_check(request, _bounds(max_max_tokens=50))
    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


def test_bounds_check_accepts_max_tokens_at_cap():
    request = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=50)
    bounds_check(request, _bounds(max_max_tokens=50))


def test_bounds_defaults():
    bounds = Bounds(
        max_messages=10,
        max_tools=10,
        max_string_chars=1000,
        max_schema_bytes=1000,
        max_schema_depth=5,
    )
    assert bounds.temperature_min == 0.0
    assert bounds.temperature_max == 2.0
    assert bounds.max_max_tokens == 32768


def test_bounds_forbids_extra():
    with pytest.raises(ValidationError):
        Bounds(
            max_messages=10,
            max_tools=10,
            max_string_chars=1000,
            max_schema_bytes=1000,
            max_schema_depth=5,
            extra_field=1,
        )
