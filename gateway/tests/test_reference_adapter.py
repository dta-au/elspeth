import pytest
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter
from elspeth_llm_gateway.sdk.protocol import InvokePlan, UpstreamFailure
from elspeth_llm_gateway.sdk.types import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalToolCall,
    CanonicalToolDef,
    Capability,
    FinishReason,
    ResponseFormatSpec,
)

# --- descriptor() ------------------------------------------------------------


def test_descriptor_identity_and_all_capabilities():
    adapter = ReferenceV1InvokeAdapter()
    descriptor = adapter.descriptor()
    assert descriptor.name == "reference_v1_invoke"
    assert descriptor.version == "0.1.0"
    assert descriptor.adapter_api_major == 1
    assert descriptor.capabilities == frozenset(
        {
            Capability.TEXT,
            Capability.TOOLS,
            Capability.JSON_OBJECT,
            Capability.JSON_SCHEMA,
            Capability.SEED,
            Capability.USAGE,
        }
    )


# --- validate_configuration() -------------------------------------------------


def test_validate_configuration_accepts_empty_dict():
    adapter = ReferenceV1InvokeAdapter()
    adapter.validate_configuration({})


def test_validate_configuration_rejects_unknown_keys():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.validate_configuration({"unexpected": "value"})


# --- build_invoke(): golden full-request mapping -----------------------------


def test_build_invoke_golden_full_request():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a", "provider": "agency"},
        model_alias="gpt-4o",
        messages=(
            CanonicalMessage(role="system", content="be nice"),
            CanonicalMessage(role="user", content="hi there"),
            CanonicalMessage(
                role="assistant",
                content=None,
                tool_calls=(CanonicalToolCall(call_id="call_1", name="lookup", arguments_json='{"q": "x"}'),),
            ),
            CanonicalMessage(role="tool", content="42", tool_call_id="call_1"),
        ),
        temperature=0.5,
        seed=7,
        max_tokens=256,
        tools=(CanonicalToolDef(name="lookup", description="looks stuff up", parameters_schema={"type": "object"}),),
        tool_choice="auto",
        tool_choice_function=None,
        response_format=ResponseFormatSpec(kind="json_schema", schema_name="Answer", json_schema={"type": "object", "properties": {}}),
    )

    plan = adapter.build_invoke(request)

    assert isinstance(plan, InvokePlan)
    assert plan.path == "v1/invoke"
    assert plan.headers == {}
    assert plan.body == {
        "conversation": [
            {"speaker": "system", "text": "be nice"},
            {"speaker": "user", "text": "hi there"},
            {
                "speaker": "assistant",
                "operations": [{"ref": "call_1", "operation": "lookup", "payload": {"q": "x"}}],
            },
            {"speaker": "tool", "text": "42", "operation_ref": "call_1"},
        ],
        "generation": {
            "target": "backend-a",
            "temperature": 0.5,
            "seed": 7,
            "max_output": 256,
        },
        "directive_policy": {"mode": "auto"},
        "directives": [{"operation": "lookup", "about": "looks stuff up", "payload_schema": {"type": "object"}}],
        "format": {"kind": "schema", "schema": {"type": "object", "properties": {}}},
    }


def test_build_invoke_minimal_request_omits_optional_fields():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
    )

    plan = adapter.build_invoke(request)

    assert plan.body == {
        "conversation": [{"speaker": "user", "text": "hi"}],
        "generation": {"target": "backend-a"},
    }
    assert "directives" not in plan.body
    assert "format" not in plan.body


def test_build_invoke_json_object_format():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
        response_format=ResponseFormatSpec(kind="json_object"),
    )

    plan = adapter.build_invoke(request)

    assert plan.body["format"] == {"kind": "object"}


@pytest.mark.parametrize("mode", ["auto", "none", "required"])
def test_build_invoke_tool_choice_directive_policy_mode(mode):
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
        tool_choice=mode,
    )

    plan = adapter.build_invoke(request)

    assert plan.body["directive_policy"] == {"mode": mode}


def test_build_invoke_tool_choice_named_carries_operation():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
        tool_choice="named",
        tool_choice_function="lookup",
    )

    plan = adapter.build_invoke(request)

    assert plan.body["directive_policy"] == {"mode": "named", "operation": "lookup"}


def test_build_invoke_named_without_function_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
        tool_choice="named",
        tool_choice_function=None,
    )

    with pytest.raises(ValueError):
        adapter.build_invoke(request)


def test_build_invoke_absent_tool_choice_omits_directive_policy():
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(CanonicalMessage(role="user", content="hi"),),
        temperature=None,
        seed=None,
        max_tokens=None,
    )

    plan = adapter.build_invoke(request)

    assert "directive_policy" not in plan.body


def test_build_invoke_tool_round_trip():
    """Assistant tool_calls message and matching tool-result message map to
    operations / operation_ref conversation entries."""
    adapter = ReferenceV1InvokeAdapter()
    request = CanonicalRequest(
        model_target={"target": "backend-a"},
        model_alias="gpt-4o",
        messages=(
            CanonicalMessage(role="user", content="what is 2+2"),
            CanonicalMessage(
                role="assistant",
                content=None,
                tool_calls=(CanonicalToolCall(call_id="c1", name="calc", arguments_json='{"expr": "2+2"}'),),
            ),
            CanonicalMessage(role="tool", content="4", tool_call_id="c1"),
        ),
        temperature=None,
        seed=None,
        max_tokens=None,
        tools=(CanonicalToolDef(name="calc", description=None, parameters_schema={"type": "object"}),),
    )

    plan = adapter.build_invoke(request)

    assert plan.body["conversation"][1] == {
        "speaker": "assistant",
        "operations": [{"ref": "c1", "operation": "calc", "payload": {"expr": "2+2"}}],
    }
    assert plan.body["conversation"][2] == {"speaker": "tool", "text": "4", "operation_ref": "c1"}


# --- parse_success(): halt -> finish_reason -----------------------------------


def test_parse_success_complete_maps_to_stop():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"result": {"text": "hello"}, "halt": "complete"})
    assert response.text == "hello"
    assert response.tool_calls == ()
    assert response.finish_reason == FinishReason.STOP
    assert response.usage is None


def test_parse_success_truncated_maps_to_length():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"result": {"text": "cut off"}, "halt": "truncated"})
    assert response.finish_reason == FinishReason.LENGTH
    assert response.text == "cut off"


def test_parse_success_operations_maps_to_tool_calls():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success(
        {
            "result": {"invocations": [{"ref": "call_9", "operation": "lookup", "payload": {"q": "x"}}]},
            "halt": "operations",
        }
    )
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.text is None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.call_id == "call_9"
    assert call.name == "lookup"
    assert call.arguments_json == '{"q": "x"}'


def test_parse_success_screened_with_text():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"result": {"text": "partially screened"}, "halt": "screened"})
    assert response.finish_reason == FinishReason.CONTENT_FILTER
    assert response.text == "partially screened"


def test_parse_success_screened_without_text_yields_empty_string():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"result": {}, "halt": "screened"})
    assert response.finish_reason == FinishReason.CONTENT_FILTER
    assert response.text == ""


def test_parse_success_screened_without_result_yields_empty_string():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"halt": "screened"})
    assert response.finish_reason == FinishReason.CONTENT_FILTER
    assert response.text == ""


def test_parse_success_unknown_halt_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {"text": "x"}, "halt": "mystery"})


# --- parse_success(): malformed result shapes raise ValueError, not KeyError -


def test_parse_success_missing_result_key_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"halt": "complete"})


def test_parse_success_missing_text_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {}, "halt": "truncated"})


def test_parse_success_operations_missing_invocations_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {}, "halt": "operations"})


def test_parse_success_operations_empty_invocations_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {"invocations": []}, "halt": "operations"})


@pytest.mark.parametrize("missing_field", ["ref", "operation", "payload"])
def test_parse_success_invocation_missing_field_raises_value_error(missing_field):
    adapter = ReferenceV1InvokeAdapter()
    invocation = {"ref": "c1", "operation": "lookup", "payload": {"q": "x"}}
    del invocation[missing_field]
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {"invocations": [invocation]}, "halt": "operations"})


# --- parse_success(): accounting -> usage -------------------------------------


def test_parse_success_accounting_missing_input_units_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {"text": "hi"}, "halt": "complete", "accounting": {"output_units": 5}})


def test_parse_success_accounting_missing_output_units_raises_value_error():
    adapter = ReferenceV1InvokeAdapter()
    with pytest.raises(ValueError):
        adapter.parse_success({"result": {"text": "hi"}, "halt": "complete", "accounting": {"input_units": 5}})


def test_parse_success_usage_present():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success(
        {
            "result": {"text": "hi"},
            "halt": "complete",
            "accounting": {"input_units": 10, "output_units": 5},
        }
    )
    assert response.usage is not None
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 15


def test_parse_success_usage_absent():
    adapter = ReferenceV1InvokeAdapter()
    response = adapter.parse_success({"result": {"text": "hi"}, "halt": "complete"})
    assert response.usage is None


# --- classify_error(): fault.kind -> ErrorClassification ----------------------


def test_classify_error_throttle():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=429, body={"fault": {"kind": "throttle"}}))
    assert classification.code == "upstream_rate_limited"
    assert classification.retryable is True


def test_classify_error_overloaded():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=503, body={"fault": {"kind": "overloaded"}}))
    assert classification.code == "upstream_unavailable"
    assert classification.retryable is True


def test_classify_error_screening():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=400, body={"fault": {"kind": "screening"}}))
    assert classification.code == "content_policy_rejected"
    assert classification.retryable is False


def test_classify_error_too_long():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=400, body={"fault": {"kind": "too_long"}}))
    assert classification.code == "context_length_exceeded"
    assert classification.retryable is False


def test_classify_error_unknown_kind():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=500, body={"fault": {"kind": "mystery"}}))
    assert classification.code == "upstream_response_invalid"
    assert classification.retryable is False


def test_classify_error_missing_fault():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=500, body={}))
    assert classification.code == "upstream_response_invalid"
    assert classification.retryable is False


def test_classify_error_no_body():
    adapter = ReferenceV1InvokeAdapter()
    classification = adapter.classify_error(UpstreamFailure(status=500, body=None))
    assert classification.code == "upstream_response_invalid"
    assert classification.retryable is False
