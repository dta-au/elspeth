"""Content-addressed retained provider contexts in the recorder codec (v2).

Every retained provider call repeats the system prompt, skill text, tool specs
and the growing history prefix. The v2 recorder document stores each distinct
message and tools list once, keyed by its sha256, and each call holds refs.
Decode refuses dangling, forged or unreferenced blocks, and a v1 document.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_audit_buffer import (
    RECORDER_CODEC,
    RecorderDrainCursors,
    RecorderSnapshot,
    decode_recorder_snapshot,
    encode_recorder_snapshot,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
from elspeth.web.composer.provider_context import capture_provider_context
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record

_SYSTEM = {"role": "system", "content": "You are the composer. " + "Stable operator instructions. " * 2000}
_TOOLS = [
    {"type": "function", "function": {"name": f"tool_{index}", "description": "d" * 500, "parameters": {"type": "object"}}}
    for index in range(40)
]


def _calls(count: int):
    base, _metadata = actual_record()
    history: list[dict] = [_SYSTEM, {"role": "user", "content": "Build the pipeline."}]
    calls = []
    for index in range(count):
        context = capture_provider_context(messages=list(history), tools=_TOOLS)
        assert context.disposition == "retained"
        calls.append(replace(base, call_id=f"00000000-0000-4000-8000-{index:012d}", private_request=context))
        history.append({"role": "assistant", "content": f"Step {index} reply."})
        history.append({"role": "user", "content": f"Continue with step {index + 1}."})
    return tuple(calls)


def _snapshot(count: int) -> RecorderSnapshot:
    return RecorderSnapshot(
        invocations=(),
        llm_calls=_calls(count),
        planner_attempts=(),
        withheld_replies=(),
        visible_tool_events=(),
        private_candidates=(),
        private_authorings=(),
        drained=RecorderDrainCursors(),
    )


def _rewrite(chunk: PrivateChunk, change) -> PrivateChunk:
    document = json.loads(chunk.canonical)
    change(document)
    return encode_private_chunk(codec=RECORDER_CODEC, canonical=canonical_json(document))


def test_v2_roundtrip_is_exact_identity():
    snapshot = _snapshot(3)
    chunk = encode_recorder_snapshot(snapshot)
    assert chunk.reference.codec == "composer.audit-buffer.v2"
    restored = decode_recorder_snapshot(chunk)
    assert restored == snapshot
    assert [call.private_request for call in restored.llm_calls] == [call.private_request for call in snapshot.llm_calls]
    assert encode_recorder_snapshot(restored).canonical == chunk.canonical


def test_each_distinct_message_and_tools_list_is_stored_once():
    document = json.loads(encode_recorder_snapshot(_snapshot(3)).canonical)
    # system + first user + 2 x (assistant, user) for the two later calls, plus one tools list.
    assert len(document["blocks"]) == 2 + 2 * 2 + 1
    for key, block in document["blocks"].items():
        assert hashlib.sha256(canonical_json(block).encode("utf-8")).hexdigest() == key


def test_a_dangling_block_ref_is_an_integrity_refusal_not_a_key_error():
    chunk = encode_recorder_snapshot(_snapshot(2))

    def dangle(document):
        document["snapshot"]["llm_calls"][1]["private_request"]["messages"][0] = "0" * 64

    with pytest.raises(AuditIntegrityError, match="absent block"):
        decode_recorder_snapshot(_rewrite(chunk, dangle))


def test_a_block_whose_bytes_do_not_match_its_address_is_refused():
    chunk = encode_recorder_snapshot(_snapshot(2))

    def forge(document):
        key = next(iter(document["blocks"]))
        document["blocks"][key] = {"role": "user", "content": "substituted"}

    with pytest.raises(AuditIntegrityError, match="content address"):
        decode_recorder_snapshot(_rewrite(chunk, forge))


def test_an_unreferenced_block_is_refused():
    chunk = encode_recorder_snapshot(_snapshot(1))
    orphan = {"role": "user", "content": "never sent"}

    def add_orphan(document):
        document["blocks"][hashlib.sha256(canonical_json(orphan).encode("utf-8")).hexdigest()] = orphan

    with pytest.raises(AuditIntegrityError, match="unreferenced block"):
        decode_recorder_snapshot(_rewrite(chunk, add_orphan))


def test_a_v1_document_is_refused():
    """Fresh stores at epoch 80: no v1 reference can even be constructed, and
    a document under any other codec is refused before decode."""
    chunk = encode_recorder_snapshot(_snapshot(1))
    with pytest.raises(ValueError, match=r"composer\.audit-buffer\.v2"):
        encode_private_chunk(codec="composer.audit-buffer.v1", canonical=chunk.canonical)
    other = encode_private_chunk(codec="composer.evidence.v2", canonical=chunk.canonical)
    with pytest.raises(AuditIntegrityError, match="wrong codec"):
        decode_recorder_snapshot(other)


def test_a_retained_context_that_is_not_the_capture_document_keeps_its_exact_bytes():
    from elspeth.contracts.composer_provider_context import PrivateProviderContext

    base, _metadata = actual_record()
    exact = PrivateProviderContext("retained", canonical_json({"messages": [{"role": "user", "content": "plain"}]}))
    snapshot = replace(_snapshot(0), llm_calls=(replace(base, private_request=exact),))
    chunk = encode_recorder_snapshot(snapshot)
    assert json.loads(chunk.canonical)["blocks"] == {}
    assert decode_recorder_snapshot(chunk) == snapshot


def test_retained_size_is_linear_in_the_number_of_calls():
    """20 calls with a ~70 KB stable prefix: v2 stores the prefix once.

    The v1 shape embedded every call's full provider context, so its size was
    the sum over calls of (prefix + growing history).
    """
    snapshot = _snapshot(20)
    v2 = encode_recorder_snapshot(snapshot).reference.byte_length
    v1_shape = sum(len(call.private_request.canonical.encode("utf-8")) for call in snapshot.llm_calls)
    one_call = len(snapshot.llm_calls[0].private_request.canonical.encode("utf-8"))
    print(f"recorder bytes: v2={v2} v1-shape-private-requests={v1_shape} one-call-context={one_call}")
    assert v1_shape > 19 * one_call
    # Linear: the stable prefix once, plus a bounded per-call increment.
    assert v2 < one_call + 20 * 4_000
