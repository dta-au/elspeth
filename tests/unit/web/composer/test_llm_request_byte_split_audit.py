"""Per-component request byte accounting on composer LLM audit rows (fix 4A).

``docs/development/composer-performance-remediation.md`` § 4A asks the per-call
LLM ledger to say how big each part of a provider request was before any
context is trimmed. ``build_llm_call_record`` derives
:class:`ComposerLLMRequestByteSplit` in the same single pass that produces
``messages_hash`` / ``tools_spec_hash``:

- one size per sent message, in send order (for the planner: rendered skill,
  catalog reference block, provider request, then the accumulated
  continuation). A string ``content`` counts its UTF-8 bytes; any other message
  counts its canonical encoding as hashed;
- the canonical size of the whole tool-definition array (the bytes its hash
  covers);
- the sum of those components, with no wire-framing term; a total that is not
  the sum is refused.

The hashes are computed from per-message encodings, so they are pinned against
``stable_hash`` of the whole list. The planner tests instrument the physical
SDK boundary (``litellm.acompletion``) and compare per-component content bytes
with what the client was handed. ``_LLM_CALL_PUBLIC_AUDIT_FIELDS`` is a closed
whitelist, so the persisted projection is pinned with a mutation control.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import elspeth.web.composer.audit as composer_audit
from elspeth.contracts.composer_llm_audit import (
    ComposerLLMCall,
    ComposerLLMCallStatus,
    ComposerLLMRequestByteSplit,
)
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.tools._common import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion


def _size(value: object) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _utf8(text: str) -> int:
    return len(text.encode("utf-8"))


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool — non-ASCII so bytes differ from characters",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
    }


_MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "Rendered skill é", "cache_control": {"type": "ephemeral"}},
    {"role": "user", "content": canonical_json({"authoring_aids": {"a": 1}, "schema_contract_evidence": {"schemas": []}})},
    {"role": "user", "content": canonical_json({"intent": "build ☃"})},
]

# A continuation with a non-string content: an assistant tool-call turn.
_TOOL_CALL_TURN: dict[str, Any] = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "list_sources", "arguments": "{}"}}],
}
_TOOL_RESULT: dict[str, Any] = {"role": "tool", "tool_call_id": "call-1", "content": '{"success":true,"note":"ü"}'}


def _record(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ComposerLLMCall:
    return build_llm_call_record(
        model_requested="openrouter/deepseek/deepseek-v4.1-flash",
        messages=messages,
        tools=tools,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
    )


def _valid_split(**overrides: Any) -> ComposerLLMRequestByteSplit:
    message_bytes = tuple(_utf8(message["content"]) for message in _MESSAGES)
    tools_bytes = _size([_tool("a"), _tool("b")])
    values: dict[str, Any] = {
        "message_bytes": message_bytes,
        "tools_bytes": tools_bytes,
        "total_bytes": sum(message_bytes) + tools_bytes,
    }
    values.update(overrides)
    return ComposerLLMRequestByteSplit(**values)


def _llm_call(**overrides: Any) -> ComposerLLMCall:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "model_requested": "openrouter/deepseek/deepseek-v4.1-flash",
        "model_returned": "deepseek/deepseek-v4.1-flash",
        "status": ComposerLLMCallStatus.SUCCESS,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "latency_ms": 5,
        "provider_request_id": "req-1",
        "messages_hash": "a" * 64,
        "tools_spec_hash": "b" * 64,
        "declared_tool_names": ("a", "b"),
        "started_at": now,
        "finished_at": now,
        "error_class": None,
        "error_message": None,
        "temperature": None,
        "seed": None,
    }
    values.update(overrides)
    return ComposerLLMCall(**values)


class TestClosedShape:
    def test_split_whose_total_is_the_component_sum_is_admitted(self) -> None:
        split = _valid_split()

        assert split.total_bytes == sum(split.message_bytes) + (split.tools_bytes or 0)

    def test_split_without_tools_sums_messages_only(self) -> None:
        split = ComposerLLMRequestByteSplit(message_bytes=(3, 0, 7), tools_bytes=None, total_bytes=10)

        assert split.tools_bytes is None

    @pytest.mark.parametrize("delta", [-1, 1], ids=["short", "long"])
    def test_total_that_is_not_the_sum_is_refused(self, delta: int) -> None:
        good = _valid_split()

        with pytest.raises(ValueError, match="does not reconcile"):
            _valid_split(total_bytes=good.total_bytes + delta)

    def test_a_component_that_moved_without_its_total_is_refused(self) -> None:
        good = _valid_split()
        moved = (good.message_bytes[0] + 1, *good.message_bytes[1:])

        with pytest.raises(ValueError, match="does not reconcile"):
            _valid_split(message_bytes=moved)

    @pytest.mark.parametrize(
        ("field_name", "value", "error"),
        [
            pytest.param("message_bytes", [10, 10, 10], TypeError, id="message-bytes-list"),
            pytest.param("message_bytes", (10, True, 10), TypeError, id="message-bytes-bool-entry"),
            pytest.param("message_bytes", (10, "10", 10), TypeError, id="message-bytes-str-entry"),
            pytest.param("message_bytes", (10, -1, 10), ValueError, id="message-bytes-negative"),
            pytest.param("tools_bytes", -1, ValueError, id="tools-bytes-negative"),
            pytest.param("tools_bytes", 12.0, TypeError, id="tools-bytes-float"),
            pytest.param("total_bytes", None, TypeError, id="total-bytes-missing"),
        ],
    )
    def test_malformed_component_is_refused(self, field_name: str, value: object, error: type[Exception]) -> None:
        with pytest.raises(error, match=field_name):
            _valid_split(**{field_name: value})

    def test_missing_component_field_is_refused(self) -> None:
        missing_tools: dict[str, Any] = {"message_bytes": (10,), "total_bytes": 10}

        with pytest.raises(TypeError, match="tools_bytes"):
            ComposerLLMRequestByteSplit(**missing_tools)

    def test_call_refuses_an_unowned_split(self) -> None:
        with pytest.raises(TypeError, match="request_byte_split"):
            _llm_call(request_byte_split=_valid_split().to_dict())

    def test_call_refuses_a_split_that_disagrees_with_the_tools_hash(self) -> None:
        no_tools = ComposerLLMRequestByteSplit(message_bytes=(10,), tools_bytes=None, total_bytes=10)

        with pytest.raises(ValueError, match="must agree"):
            _llm_call(request_byte_split=no_tools)
        with pytest.raises(ValueError, match="must agree"):
            _llm_call(tools_spec_hash=None, declared_tool_names=(), request_byte_split=_valid_split())


class TestCapturePoint:
    def test_string_content_is_sized_by_its_utf8_bytes(self) -> None:
        tools = [_tool("a"), _tool("b")]

        split = _record(_MESSAGES, tools).request_byte_split

        assert split is not None
        assert split.message_bytes == tuple(_utf8(message["content"]) for message in _MESSAGES)
        assert split.tools_bytes == _size(tools)
        assert split.total_bytes == sum(split.message_bytes) + _size(tools)
        # Bytes, not characters: the fixtures carry non-ASCII text.
        assert split.message_bytes[0] > len(_MESSAGES[0]["content"])

    def test_non_string_content_is_sized_by_its_hashed_canonical_encoding(self) -> None:
        messages = [*_MESSAGES, _TOOL_CALL_TURN, _TOOL_RESULT]

        split = _record(messages, None).request_byte_split

        assert split is not None
        assert split.message_bytes[3] == _size(_TOOL_CALL_TURN)
        assert split.message_bytes[4] == _utf8(_TOOL_RESULT["content"])
        assert split.tools_bytes is None
        assert split.total_bytes == sum(split.message_bytes)

    @pytest.mark.parametrize(
        "tools",
        [None, [], [_tool("a"), _tool("b")]],
        ids=["no-tools", "empty-tools", "tools"],
    )
    def test_single_pass_hashes_equal_stable_hash_of_the_whole_lists(self, tools: list[dict[str, Any]] | None) -> None:
        for messages in ([], _MESSAGES, [*_MESSAGES, _TOOL_CALL_TURN, _TOOL_RESULT]):
            call = _record(messages, tools)

            assert call.messages_hash == stable_hash(messages)
            assert call.tools_spec_hash == (stable_hash(tools) if tools is not None else None)


class TestSurvivesToThePersistedProjection:
    def test_public_audit_envelope_exposes_the_split(self) -> None:
        tools = [_tool("a")]
        payload = llm_call_audit_envelope(_record(_MESSAGES, tools))["call"]

        assert isinstance(payload, dict)
        message_bytes = [_utf8(message["content"]) for message in _MESSAGES]
        assert payload["request_byte_split"] == {
            "message_bytes": message_bytes,
            "tools_bytes": _size(tools),
            "total_bytes": sum(message_bytes) + _size(tools),
        }
        # The persisted projection is JSON: it round-trips without loss.
        assert json.loads(json.dumps(payload["request_byte_split"])) == payload["request_byte_split"]

    def test_absence_persists_as_null_not_omitted(self) -> None:
        payload = llm_call_audit_envelope(_llm_call())["call"]

        assert isinstance(payload, dict)
        assert "request_byte_split" in payload
        assert payload["request_byte_split"] is None

    def test_mutation_control_dropping_the_whitelist_entry_loses_the_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The projection pin above must go red when the whitelist entry is removed."""
        mutated = tuple(field for field in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS if field != "request_byte_split")
        assert len(mutated) == len(composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS) - 1
        monkeypatch.setattr(composer_audit, "_LLM_CALL_PUBLIC_AUDIT_FIELDS", mutated)

        payload = llm_call_audit_envelope(_llm_call(request_byte_split=_valid_split()))["call"]

        assert isinstance(payload, dict)
        assert "request_byte_split" not in payload


def _component_bytes_handed_to_client(message: dict[str, Any]) -> int:
    """The instrument: per-component bytes of what the SDK stub received."""
    content = message["content"] if "content" in message else None
    return _utf8(content) if type(content) is str else _size(message)


class TestPlannerLedgerRow:
    @pytest.mark.asyncio
    async def test_each_planner_row_splits_exactly_the_content_handed_to_the_client(
        self,
        tmp_path: Path,
        tool_context: ToolContext,
    ) -> None:
        completion = _ScriptedCompletion(
            _response(("list_sources", {})),
            _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
        )
        recorder = BufferingRecorder()

        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

        assert len(completion.requests) == len(recorder.llm_calls) == 2
        for request, call in zip(completion.requests, recorder.llm_calls, strict=True):
            split = call.request_byte_split
            assert split is not None
            sent_messages = request["messages"]
            sent_tools = request["tools"]
            assert split.message_bytes == tuple(_component_bytes_handed_to_client(message) for message in sent_messages)
            assert split.tools_bytes == _size(sent_tools)
            assert split.total_bytes == sum(split.message_bytes) + _size(sent_tools)
            # The planner's three named components lead every request, each a
            # string the planner canonicalised itself.
            assert [message["role"] for message in sent_messages[:3]] == ["system", "user", "user"]
            assert all(type(message["content"]) is str for message in sent_messages[:3])
            assert set(json.loads(sent_messages[1]["content"])) == {"authoring_aids", "schema_contract_evidence"}
            assert "intent" in json.loads(sent_messages[2]["content"])
            # Single-pass hashes still equal the whole-list stable hashes.
            assert call.messages_hash == stable_hash(sent_messages)
            assert call.tools_spec_hash == stable_hash(sent_tools)
        first, second = (call.request_byte_split for call in recorder.llm_calls)
        assert first is not None and second is not None
        # The prefix is unchanged between calls; only the continuation grows.
        assert len(first.message_bytes) == 3
        assert second.message_bytes[:3] == first.message_bytes
        assert len(second.message_bytes) > 3

    @pytest.mark.asyncio
    async def test_negative_control_a_component_the_client_did_not_receive_breaks_the_instrument(
        self,
        tmp_path: Path,
        tool_context: ToolContext,
    ) -> None:
        """The per-component instrument above must notice one extra sent byte."""
        completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
        recorder = BufferingRecorder()

        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

        split = recorder.llm_calls[0].request_byte_split
        assert split is not None
        tampered = [dict(message) for message in completion.requests[0]["messages"]]
        tampered[2]["content"] += " "
        assert tuple(_component_bytes_handed_to_client(message) for message in tampered) != split.message_bytes
