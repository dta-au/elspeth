"""Exact internal control history fits the ordinary complete-context guard."""

import json
from copy import deepcopy

import pytest

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_controls import (
    CONTINUATION_CALL_NOT_ALONE,
    CONTINUATION_CONTROL_RESULT,
    CONTINUATION_CONTROL_TOOL,
    ContinuationControlRefused,
    _argument_refusal,
)
from elspeth.web.composer.provider_context import capture_provider_context

_ID = "c" * 256
_RAW = ' {\n "notes" : "  Continue this same request.  "\n}\n'


def _history(arguments=_RAW, result=CONTINUATION_CONTROL_RESULT):
    return [
        {"role": "user", "content": "Continue this same request."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": _ID, "type": "function", "function": {"name": CONTINUATION_CONTROL_TOOL, "arguments": arguments}}],
        },
        {"role": "tool", "tool_call_id": _ID, "content": result},
    ]


def test_exact_control_context_retains_original_raw_id_and_constant():
    messages = _history()
    original = deepcopy(messages)
    retained = capture_provider_context(messages=messages, tools=None)
    assert retained.disposition == "retained" and retained.reasons == ()
    assert json.loads(retained.canonical)["messages"] == original
    assert messages == original


@pytest.mark.parametrize("arguments", ['{"notes":"a","notes":"b"}', '{"notes":', '{"notes":""}', '{"extra":true}'])
def test_exact_control_refusal_preserves_all_original_argument_bytes(arguments):
    messages = _history(arguments, canonical_json(_argument_refusal().to_tool_payload()))
    retained = capture_provider_context(messages=messages, tools=[])
    assert retained.disposition == "retained"
    assert json.loads(retained.canonical)["messages"] == messages
    wrong = capture_provider_context(messages=_history(arguments), tools=[])
    assert wrong.disposition == "unavailable" and wrong.reasons == ("unsupported",)


def test_mixed_control_requires_exact_whole_batch_refusal():
    refusal = canonical_json(
        ContinuationControlRefused(
            CONTINUATION_CALL_NOT_ALONE,
            "checkpoint_and_continue must be the only tool call in its batch; every call in this batch was refused before effects.",
        ).to_tool_payload()
    )
    messages = _history(result=refusal)
    messages[1]["tool_calls"].append({"id": "read-1", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}})
    messages.append({"role": "tool", "tool_call_id": "read-1", "content": refusal})
    assert capture_provider_context(messages=messages, tools=None).disposition == "retained"
    messages[2]["content"] = CONTINUATION_CONTROL_RESULT
    assert capture_provider_context(messages=messages, tools=None).reasons == ("unsupported",)


@pytest.mark.parametrize("control_first", [False, True])
def test_mixed_control_peer_success_cannot_claim_whole_batch_refusal(control_first):
    refusal = canonical_json(
        ContinuationControlRefused(
            CONTINUATION_CALL_NOT_ALONE,
            "checkpoint_and_continue must be the only tool call in its batch; every call in this batch was refused before effects.",
        ).to_tool_payload()
    )
    messages = _history(result=refusal)
    messages[1]["tool_calls"].append({"id": "read-1", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}})
    if not control_first:
        messages[1]["tool_calls"].reverse()
    messages.append({"role": "tool", "tool_call_id": "read-1", "content": refusal})
    assert capture_provider_context(messages=messages, tools=None).disposition == "retained"
    messages[-1]["content"] = '{"success":true}'
    refused = capture_provider_context(messages=messages, tools=None)
    assert refused.disposition == "unavailable" and refused.reasons == ("unsupported",)


@pytest.mark.parametrize(
    "arguments",
    [
        '{"password":"synthetic-private-value"}',
        r'{"pass\u0077ord":"synthetic-private-value"}',
        r'{"notes":"safe","notes":"password\u003dsynthetic-private-value"}',
        r'{"pass\u0077ord":"synthetic-private-value",',
    ],
)
def test_control_refusal_never_clears_encoded_or_shadowed_credentials(arguments):
    retained = capture_provider_context(messages=_history(arguments, canonical_json(_argument_refusal().to_tool_payload())), tools=None)
    assert retained.disposition == "unavailable"
    assert retained.reasons[0] in ("recognized_credential", "credential_field")
    assert retained.canonical is None and "synthetic-private-value" not in repr(retained)


@pytest.mark.parametrize(
    "arguments,reason",
    [
        (r'{"notes":"\x"}', "malformed_json"),
        (r'{"notes":"\ud800"}', "bounds"),
        (r'{"notes":"data\u003aimage/png;base64,c3ludGhldGlj"}', "unsupported"),
    ],
)
def test_control_history_keeps_escape_scalar_and_image_refusals(arguments, reason):
    context = capture_provider_context(messages=_history(arguments, canonical_json(_argument_refusal().to_tool_payload())), tools=None)
    assert context.disposition == "unavailable" and context.reasons == (reason,) and context.canonical is None


@pytest.mark.parametrize("mutation", ["changed_constant", "foreign_id", "missing_reply", "unknown_control"])
def test_control_context_refuses_unmatched_or_changed_protocol(mutation):
    messages = _history()
    if mutation == "changed_constant":
        messages[2]["content"] += " Another claim."
    elif mutation == "foreign_id":
        messages[2]["tool_call_id"] = "foreign"
    elif mutation == "missing_reply":
        messages.pop()
    else:
        messages[1]["tool_calls"][0]["function"]["name"] += "_impostor"
    retained = capture_provider_context(messages=messages, tools=None)
    assert retained.disposition == "unavailable" and retained.reasons == ("unsupported",)


def test_control_context_keeps_the_complete_aggregate_bound():
    arguments = json.dumps({"notes": "x" * 1_048_577})
    retained = capture_provider_context(messages=_history(arguments, canonical_json(_argument_refusal().to_tool_payload())), tools=None)
    assert retained.disposition == "unavailable" and retained.reasons == ("bounds",) and retained.canonical is None


@pytest.mark.parametrize("arguments,reason", [('{"a":1,"a":2}', "duplicate_keys"), ('{"a":', "malformed_json")])
def test_ordinary_tool_does_not_inherit_control_refusal_handling(arguments, reason):
    messages = _history(arguments, canonical_json(_argument_refusal().to_tool_payload()))
    messages[1]["tool_calls"][0]["function"]["name"] = "get_pipeline_state"
    retained = capture_provider_context(messages=messages, tools=None)
    assert retained.disposition == "unavailable" and retained.reasons == (reason,)
