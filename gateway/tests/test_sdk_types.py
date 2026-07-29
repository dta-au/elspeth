import pytest
from elspeth_llm_gateway.sdk import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalToolCall,
    CanonicalToolDef,
    CanonicalUsage,
    Capability,
    FinishReason,
    ResponseFormatSpec,
)
from pydantic import ValidationError

# --- Capability / FinishReason vocabulary -----------------------------------


def test_capability_vocabulary_is_closed():
    assert {c.value for c in Capability} == {
        "text",
        "tools",
        "json_object",
        "json_schema",
        "seed",
        "usage",
    }


def test_finish_reason_vocabulary_is_closed():
    assert {f.value for f in FinishReason} == {
        "stop",
        "length",
        "tool_calls",
        "content_filter",
    }


# --- CanonicalToolDef --------------------------------------------------------


def test_canonical_tool_def_happy_path():
    tool = CanonicalToolDef(
        name="lookup",
        description="looks things up",
        parameters_schema={"type": "object"},
    )
    assert tool.name == "lookup"
    assert tool.parameters_schema == {"type": "object"}


def test_canonical_tool_def_is_frozen():
    tool = CanonicalToolDef(name="lookup", description=None, parameters_schema={})
    with pytest.raises(ValidationError):
        tool.name = "other"


def test_canonical_tool_def_forbids_extra():
    with pytest.raises(ValidationError):
        CanonicalToolDef(name="lookup", description=None, parameters_schema={}, extra_field=1)


# --- CanonicalToolCall --------------------------------------------------------


def test_canonical_tool_call_happy_path():
    call = CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}")
    assert call.call_id == "c1"
    assert call.arguments_json == "{}"


def test_canonical_tool_call_is_frozen():
    call = CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}")
    with pytest.raises(ValidationError):
        call.call_id = "c2"


def test_canonical_tool_call_forbids_extra():
    with pytest.raises(ValidationError):
        CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}", extra_field=1)


# --- CanonicalMessage --------------------------------------------------------


def test_canonical_message_happy_path_defaults():
    msg = CanonicalMessage(role="user", content="hi")
    assert msg.role == "user"
    assert msg.content == "hi"
    assert msg.tool_calls == ()
    assert msg.tool_call_id is None


def test_canonical_message_happy_path_with_tool_calls():
    call = CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}")
    msg = CanonicalMessage(role="assistant", content=None, tool_calls=(call,))
    assert msg.tool_calls == (call,)


def test_canonical_message_is_frozen():
    msg = CanonicalMessage(role="user", content="hi")
    with pytest.raises(ValidationError):
        msg.content = "bye"


def test_canonical_message_forbids_extra():
    with pytest.raises(ValidationError):
        CanonicalMessage(role="user", content="hi", extra_field=1)


def test_canonical_message_role_is_restricted():
    with pytest.raises(ValidationError):
        CanonicalMessage(role="function", content="hi")


# --- ResponseFormatSpec -------------------------------------------------------


def test_response_format_spec_happy_path():
    spec = ResponseFormatSpec(kind="json_object")
    assert spec.kind == "json_object"
    assert spec.schema_name is None
    assert spec.schema is None


def test_response_format_spec_json_schema_happy_path():
    spec = ResponseFormatSpec(kind="json_schema", schema_name="Foo", schema={"type": "object"})
    assert spec.schema_name == "Foo"
    assert spec.schema == {"type": "object"}


def test_response_format_spec_is_frozen():
    spec = ResponseFormatSpec(kind="json_object")
    with pytest.raises(ValidationError):
        spec.kind = "json_schema"


def test_response_format_spec_forbids_extra():
    with pytest.raises(ValidationError):
        ResponseFormatSpec(kind="json_object", extra_field=1)


# --- CanonicalRequest ---------------------------------------------------------


def test_canonical_request_happy_path():
    msg = CanonicalMessage(role="user", content="hi")
    req = CanonicalRequest(
        model_target={"provider": "agency", "name": "foo"},
        model_alias="gpt-4o",
        messages=(msg,),
        temperature=0.5,
        seed=42,
        max_tokens=100,
    )
    assert req.model_alias == "gpt-4o"
    assert req.messages == (msg,)
    assert req.tools == ()
    assert req.tool_choice is None
    assert req.tool_choice_function is None
    assert req.response_format is None


def test_canonical_request_happy_path_with_tools_and_response_format():
    msg = CanonicalMessage(role="user", content="hi")
    tool = CanonicalToolDef(name="lookup", description=None, parameters_schema={})
    fmt = ResponseFormatSpec(kind="json_object")
    req = CanonicalRequest(
        model_target={"provider": "agency", "name": "foo"},
        model_alias="gpt-4o",
        messages=(msg,),
        temperature=None,
        seed=None,
        max_tokens=None,
        tools=(tool,),
        tool_choice="auto",
        tool_choice_function="lookup",
        response_format=fmt,
    )
    assert req.tools == (tool,)
    assert req.response_format == fmt


def test_canonical_request_is_frozen():
    msg = CanonicalMessage(role="user", content="hi")
    req = CanonicalRequest(
        model_target={},
        model_alias="gpt-4o",
        messages=(msg,),
        temperature=None,
        seed=None,
        max_tokens=None,
    )
    with pytest.raises(ValidationError):
        req.model_alias = "other"


def test_canonical_request_forbids_extra():
    msg = CanonicalMessage(role="user", content="hi")
    with pytest.raises(ValidationError):
        CanonicalRequest(
            model_target={},
            model_alias="gpt-4o",
            messages=(msg,),
            temperature=None,
            seed=None,
            max_tokens=None,
            extra_field=1,
        )


# --- CanonicalUsage ------------------------------------------------------------


def test_canonical_usage_happy_path():
    usage = CanonicalUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    assert usage.total_tokens == 7


def test_canonical_usage_is_frozen():
    usage = CanonicalUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    with pytest.raises(ValidationError):
        usage.total_tokens = 8


def test_canonical_usage_forbids_extra():
    with pytest.raises(ValidationError):
        CanonicalUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7, extra_field=1)


def test_canonical_usage_arithmetic_validator_rejects_mismatch():
    with pytest.raises(ValidationError):
        CanonicalUsage(prompt_tokens=1, completion_tokens=1, total_tokens=5)


@pytest.mark.parametrize(
    "prompt_tokens,completion_tokens,total_tokens",
    [
        (-1, 1, 0),
        (1, -1, 0),
        (1, 1, -2),
    ],
)
def test_canonical_usage_rejects_negative_counts(prompt_tokens, completion_tokens, total_tokens):
    with pytest.raises(ValidationError):
        CanonicalUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


# --- CanonicalResponse ---------------------------------------------------------


def test_canonical_response_happy_path_text():
    resp = CanonicalResponse(text="hello", finish_reason=FinishReason.STOP)
    assert resp.text == "hello"
    assert resp.tool_calls == ()
    assert resp.usage is None


def test_canonical_response_happy_path_tool_calls():
    call = CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}")
    resp = CanonicalResponse(text=None, tool_calls=(call,), finish_reason=FinishReason.TOOL_CALLS)
    assert resp.tool_calls == (call,)
    assert resp.text is None


def test_canonical_response_happy_path_with_usage():
    usage = CanonicalUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    resp = CanonicalResponse(text="hi", finish_reason=FinishReason.STOP, usage=usage)
    assert resp.usage == usage


def test_canonical_response_is_frozen():
    resp = CanonicalResponse(text="hello", finish_reason=FinishReason.STOP)
    with pytest.raises(ValidationError):
        resp.text = "bye"


def test_canonical_response_forbids_extra():
    with pytest.raises(ValidationError):
        CanonicalResponse(text="hello", finish_reason=FinishReason.STOP, extra_field=1)


def test_canonical_response_rejects_both_text_and_tool_calls():
    call = CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}")
    with pytest.raises(ValidationError):
        CanonicalResponse(
            text="hello",
            tool_calls=(call,),
            finish_reason=FinishReason.STOP,
        )


def test_canonical_response_rejects_neither_text_nor_tool_calls():
    with pytest.raises(ValidationError):
        CanonicalResponse(text=None, tool_calls=(), finish_reason=FinishReason.STOP)
