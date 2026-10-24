import json

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardTrust,
)
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
from elspeth.web.composer.continuation_message_shelf import (
    decode_message_shelf,
    encode_message_shelf,
)


def cold_owner(owner, messages):
    chunk = encode_message_shelf(owner.snapshot_for_continuation(messages))
    cold = decode_message_shelf(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    return AdvisorTranscriptOwner.for_continuation(
        cold,
        session_id="session",
        actor_user_id="actor",
        request_id="original",
        request="Build and inspect",
    )


def tool_message(arguments):
    return {
        "role": "assistant",
        "content": "Inspect",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "get_blob_content",
                    "arguments": arguments,
                },
            }
        ],
        "thinking_blocks": [
            {"type": "thinking", "thinking": "native-first", "signature": "signature-1"},
            {"type": "redacted_thinking", "data": "opaque-second"},
        ],
    }


def test_pending_adoption_excluded_aliases_native_and_elided_messages_survive_cold_roundtrip():
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    excluded = {"role": "user", "content": "Build and inspect"}
    owner.begin_messages([excluded])
    received = tool_message('{"blob_id":"blob","offset_chars":0}')
    owner.record_provider_reply(provider_call_id="physical-1", message=received, admission="received_unadmitted")
    # Received reply is not yet part of the provider list. It still needs a shelf slot.
    restored, messages = cold_owner(owner, [excluded])
    assert restored._excluded[0] is messages[0]
    assert restored._observed[0].pending_adoption
    assert restored._observed[0].message["thinking_blocks"][0]["thinking"] == "native-first"
    actual = tool_message('{"blob_id":"blob","offset_chars":0,"limit_chars":4}')
    messages.append(actual)
    restored.observe_messages(messages)
    restored.admit_provider_reply("physical-1")
    assert not restored._observed[0].pending_adoption
    assert restored._observed[0].message is actual
    assert restored.current_tool_strings("call-1", "get_blob_content")[:2] == (
        '{"blob_id":"blob","offset_chars":0}',
        '{"blob_id":"blob","offset_chars":0,"limit_chars":4}',
    )
    # Elision from current provider context must not erase the append-only event.
    del messages[-1]
    again, current = cold_owner(restored, messages)
    assert len(current) == 1
    assert len(again._observed) == 1
    assert again.current_tool_strings("call-1", "get_blob_content")[:2] == restored.current_tool_strings("call-1", "get_blob_content")[:2]


def test_read_delivery_and_equal_human_words_remain_separate_events():
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    reply = tool_message("{}")
    read = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": canonical_json(
            {
                "success": True,
                "data": {"content": "page"},
            }
        ),
    }
    one = {"role": "user", "content": "Keep the middle amendment"}
    two = {"role": "user", "content": "Keep the middle amendment"}
    messages = [reply, read, one, two]
    owner.observe_messages(messages)
    owner.confirm_read_delivery(tool_call_id="call-1", provider_call_id="physical-2")
    expected = owner.snapshot()
    restored, actual_messages = cold_owner(owner, messages)
    assert restored.snapshot() == expected
    assert actual_messages[2] is not actual_messages[3]
    events = json.loads(restored.snapshot().provider_json)["events"]
    humans = [event for event in events if event["content"] == "Keep the middle amendment"]
    assert len(humans) == 2
    tool = next(event for event in events if event["role"] == "tool")
    assert tool["primary_delivery"] == {
        "status": "confirmed_in_successful_prepared_request",
        "provider_call_id": "physical-2",
    }


def test_nominal_guard_authority_is_not_reconstructed_from_saved_json():
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    message = {"role": "system", "content": "owned instruction", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION}
    owner.begin_messages([message])
    with pytest.raises(AuditIntegrityError, match="producer materialization"):
        owner.snapshot_for_continuation([message])


def test_nested_object_order_and_finite_numeric_boundaries_survive_cold_roundtrip():
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    message = {
        "role": "user",
        "content": "Keep these plain retained values",
        "retained_numbers": {
            "small": 1e-6,
            "nested": {"tiny": 1e-7, "max_safe_integer": 2**53 - 1},
        },
    }
    owner.observe_messages([message])
    expected = owner.snapshot()
    restored, messages = cold_owner(owner, [message])
    assert restored.snapshot() == expected
    assert messages == [message]
    assert tuple(messages[0]) == ("role", "content", "retained_numbers")
    retained = messages[0]["retained_numbers"]
    assert tuple(retained) == ("small", "nested")
    assert tuple(retained["nested"]) == ("tiny", "max_safe_integer")
    assert retained["small"] == 1e-6
    assert retained["nested"]["tiny"] == 1e-7
    assert retained["nested"]["max_safe_integer"] == 2**53 - 1


@pytest.mark.parametrize("target", ["message", "event"])
@pytest.mark.parametrize(
    "inner_text",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":1e999}',
        "[]",
        '{ "a": 1 }',
        '{"a":9007199254740992}',
    ],
    ids=["duplicate-key", "nan", "overflow", "list-shape", "noncompact", "unsafe-integer"],
)
def test_bound_outer_chunk_refuses_invalid_inner_object(target, inner_text):
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    message = {"role": "user", "content": "Keep this plain event"}
    owner.observe_messages([message])
    snapshot = owner.snapshot_for_continuation([message])
    assert decode_message_shelf(encode_message_shelf(snapshot)) == snapshot
    raw = snapshot.model_dump(mode="json")
    if target == "message":
        raw["messages_json"][0] = inner_text
    else:
        raw["observed"][0]["event_json"] = inner_text
    chunk = encode_private_chunk(codec="composer.transcript.v1", canonical=canonical_json(raw))
    with pytest.raises(AuditIntegrityError):
        decode_message_shelf(chunk)


@pytest.mark.parametrize(
    "guard_key",
    [
        PROMPT_GUARD_TRUST_KEY,
        PROMPT_GUARD_CONTENT_PARTS_KEY,
        PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
        PROMPT_GUARD_THINKING_PARTS_KEY,
    ],
)
def test_bound_outer_chunk_refuses_saved_guard_provenance(guard_key):
    owner = AdvisorTranscriptOwner(request="Build and inspect")
    message = {"role": "user", "content": "Keep this plain event"}
    owner.observe_messages([message])
    snapshot = owner.snapshot_for_continuation([message])
    assert decode_message_shelf(encode_message_shelf(snapshot)) == snapshot
    raw = snapshot.model_dump(mode="json")
    raw["messages_json"][0] = json.dumps({**message, guard_key: "saved-marker"}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    chunk = encode_private_chunk(codec="composer.transcript.v1", canonical=canonical_json(raw))
    with pytest.raises(AuditIntegrityError, match="violates its owned codec"):
        decode_message_shelf(chunk)
