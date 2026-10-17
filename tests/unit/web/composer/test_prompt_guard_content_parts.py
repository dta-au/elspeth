"""Lossless guard evidence is nominal, bounded, and kept off the provider wire."""

from __future__ import annotations

import asyncio
import json

import httpx
import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    PromptGuardToolArgumentParts,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
    detach_prompt_guard_messages,
)
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, _request, enforce_prompt_guard
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_prompt_guard_clients import _settings

authority = _authority


def _prepare(messages, *, content_parts=None, tool_argument_parts=None, user_prompt="Build a graph"):
    return _request(
        policy_fingerprint="a" * 64,
        role="composer",
        messages=messages,
        tools=(),
        user_prompt=user_prompt,
        trusted_message_indexes=frozenset(),
        content_parts=content_parts,
        tool_argument_parts=tool_argument_parts,
    )


def _allow(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
        },
    )


@pytest.mark.parametrize("forged", [None, [], [PromptGuardContentPart("evidence")], ({"text": "evidence"},), "evidence"])
def test_foreign_partition_cannot_mint_trust(forged):
    with pytest.raises(AuditIntegrityError):
        detach_prompt_guard_evidence([{"role": "tool", "content": "evidence", PROMPT_GUARD_CONTENT_PARTS_KEY: forged}])


@pytest.mark.parametrize("texts", [("second", "first"), ("first",), ("firstsecond", "extra")])
def test_parts_must_cover_every_wire_character_in_order(texts):
    parts = tuple(PromptGuardContentPart(text) for text in texts)
    with pytest.raises(AuditIntegrityError, match="complete wire content"):
        detach_prompt_guard_evidence([{"role": "tool", "content": "firstsecond", PROMPT_GUARD_CONTENT_PARTS_KEY: parts}])


def test_partition_is_detached_and_never_reaches_legacy_wire_projection():
    parts = (PromptGuardContentPart("catalog", PromptGuardTrust.REFERENCE), PromptGuardContentPart(" graph"))
    messages = [
        {
            "role": "tool",
            "content": "catalog graph",
            "metadata": {"observed": ["original"]},
            PROMPT_GUARD_CONTENT_PARTS_KEY: parts,
        }
    ]
    evidence = detach_prompt_guard_evidence(messages)
    wire, trusted = detach_prompt_guard_messages(messages)
    assert evidence.content_parts == (parts,)
    assert trusted == evidence.trusted_message_indexes == frozenset()
    assert wire == evidence.messages == [{"role": "tool", "content": "catalog graph", "metadata": {"observed": ["original"]}}]
    messages[0]["metadata"]["observed"].append("later")
    assert evidence.messages[0]["metadata"] == {"observed": ["original"]}
    with pytest.raises(AuditIntegrityError, match="conflicts"):
        detach_prompt_guard_evidence([{**messages[0], PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.REFERENCE}])
    assert "catalog" not in repr(parts[0])
    assert "original" not in repr(evidence)


def test_identical_wire_has_distinct_hashes_for_distinct_coverage_and_trust():
    messages = [{"role": "tool", "content": "reference dynamic"}]
    partitions = (
        (None,),
        ((PromptGuardContentPart("reference dynamic"),),),
        ((PromptGuardContentPart("reference"), PromptGuardContentPart(" dynamic")),),
        ((PromptGuardContentPart("reference", PromptGuardTrust.REFERENCE), PromptGuardContentPart(" dynamic")),),
    )
    requests = [_prepare(messages, content_parts=parts)[0] for parts in partitions]
    assert len({request.request_hash for request in requests}) == len(partitions)
    assert all("dynamic" in "".join(request.documents) for request in requests)
    assert "reference" not in "".join(requests[-1].documents)
    with pytest.raises(AuditIntegrityError, match="complete wire content"):
        _prepare([{**messages[0], "content": "changed"}], content_parts=partitions[-1])


@pytest.mark.parametrize("index", [True, -1, "0"])
def test_tool_argument_parts_require_exact_nonnegative_indexes(index):
    with pytest.raises(AuditIntegrityError):
        PromptGuardToolArgumentParts(index, (PromptGuardContentPart("{}"),))


def test_tool_arguments_cannot_gain_trust_or_hide_missing_and_overlapping_bytes():
    with pytest.raises(AuditIntegrityError, match="untrusted"):
        PromptGuardToolArgumentParts(0, (PromptGuardContentPart("{}", PromptGuardTrust.REFERENCE),))
    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "function": {"arguments": "{}"}}]}
    valid = PromptGuardToolArgumentParts(0, (PromptGuardContentPart("{}"),))
    for entries in (
        (valid, valid),
        (PromptGuardToolArgumentParts(1, valid.parts),),
        (PromptGuardToolArgumentParts(0, (PromptGuardContentPart("{"),)),),
        ({"tool_call_index": 0, "parts": ["{}"]},),
    ):
        with pytest.raises(AuditIntegrityError):
            detach_prompt_guard_evidence([{**message, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: entries}])
    with pytest.raises(AuditIntegrityError, match="conflicts"):
        detach_prompt_guard_evidence(
            [{**message, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: (valid,), PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.REFERENCE}]
        )


@pytest.mark.asyncio
async def test_large_structured_tool_arguments_keep_exact_wire_and_all_untrusted_fields():
    arguments = json.dumps({"nodes": [{"id": f"node-{index}", "description": "Neutral evidence. " * 80} for index in range(12)]})
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "set_pipeline", "arguments": arguments}}],
    }
    raw, raw_supported = _prepare([message])
    assert not raw_supported and len(raw.documents[0]) > 10_000
    partitions = (PromptGuardToolArgumentParts(0, json_content_parts(arguments)),)
    evidence = detach_prompt_guard_evidence([{**message, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: partitions}])
    assert evidence.messages == [message]
    assert "".join(part.text for part in partitions[0].parts) == arguments
    request, supported = _prepare(evidence.messages, tool_argument_parts=evidence.tool_argument_parts)
    assert supported and request.request_hash != raw.request_hash
    documents = "\n".join(request.documents)
    assert "set_pipeline" in documents and "call-1" in documents
    assert all(f"node-{index}" in documents for index in range(12))
    observed = []

    def serve(sent):
        observed.append(json.loads(sent.content))
        return _allow(sent)

    decision = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(request)
    assert decision.disposition == "allow"
    assert tuple(document for body in observed for document in body["documents"]) == request.documents
    assert PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY not in json.dumps(evidence.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [64, 100])
async def test_long_history_is_packed_losslessly_and_sent_to_actual_azure_adapter(turns):
    messages = [{"role": role, "content": f"{turn}:{role}"} for turn in range(turns) for role in ("user", "assistant")]
    request, supported = _prepare(messages)
    assert supported
    expected = "\n".join(json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for message in messages)
    assert "\n".join(request.documents) == expected
    observed = []

    def serve(sent):
        observed.append(json.loads(sent.content))
        return _allow(sent)

    decision = await AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)).inspect(request)
    assert decision.disposition == "allow"
    assert tuple(document for body in observed for document in body["documents"]) == request.documents
    assert all(body["userPrompt"] == "Build a graph" for body in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
@pytest.mark.parametrize("kind", ["atomic", "aggregate", "surrogate_user", "surrogate_part"])
async def test_unrepresentable_input_is_audited_without_truncation_or_transport(authority, mode, kind):
    engine, _service, owner, coordinator = authority
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    messages = [{"role": "tool", "content": "x" * 10_001}]
    user = "Build a graph"
    parts = None
    if kind == "aggregate":
        messages = [{"role": "tool", "content": "x" * 9000} for _ in range(30)]
    elif kind == "surrogate_user":
        user = "\ud800"
    elif kind == "surrogate_part":
        messages = [{"role": "tool", "content": "\ud800"}]
        parts = ((PromptGuardContentPart("\ud800"),),)
    observed = []

    def serve(sent):
        observed.append(sent)
        return _allow(sent)

    mount = PromptGuardMount(AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)))
    with pytest.raises(PromptGuardRefused):
        await enforce_prompt_guard(
            mount=mount,
            mode=mode,
            role="composer",
            provider_custody=custody,
            messages=messages,
            content_parts=parts,
            user_prompt=user,
        )
    assert observed == []
    assert [check["reason"] for check in _checks(engine)] == ["unsupported_input"]
    assert custody.attempt is None
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["blocked", "service_error", "cancelled"])
async def test_later_azure_batch_prevents_sdk_and_durably_settles_one_check(authority, monkeypatch, outcome):
    engine, _service, owner, coordinator = authority
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    observed = []
    entered = asyncio.Event()
    joined = asyncio.Event()
    sdk_calls = []

    async def serve(sent):
        observed.append(json.loads(sent.content))
        if len(observed) == 1:
            return _allow(sent)
        if outcome == "service_error":
            return httpx.Response(503)
        if outcome == "blocked":
            return httpx.Response(
                200,
                json={"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": True}]},
            )
        entered.set()
        try:
            await asyncio.Future()
        finally:
            joined.set()

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("A refused guard must never enter the model SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    parts = (
        PromptGuardContentPart("registered catalog", PromptGuardTrust.REFERENCE),
        *(PromptGuardContentPart(f"node {index}:" + "x" * 6000) for index in range(3)),
    )
    original = [{"role": "tool", "content": "".join(part.text for part in parts), PROMPT_GUARD_CONTENT_PARTS_KEY: parts}]
    mount = PromptGuardMount(AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)))
    task = asyncio.create_task(
        _litellm_acompletion(
            prompt_guard_mount=mount,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            prompt_guard_user_prompt="Build a graph",
            provider_custody=custody,
            messages=original,
            model="test/model",
        )
    )
    if outcome == "cancelled":
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert joined.is_set()
    else:
        with pytest.raises(PromptGuardRefused):
            await task
    assert len(observed) == 2
    sent_documents = "".join(document for body in observed for document in body["documents"])
    assert "registered catalog" not in sent_documents
    assert "node 0:" in sent_documents and "node 1:" in sent_documents
    assert PROMPT_GUARD_CONTENT_PARTS_KEY not in sent_documents
    assert sdk_calls == [] and custody.attempt is None
    checks = _checks(engine)
    assert len(checks) == 1
    assert (checks[0]["disposition"], checks[0]["reason"]) == {
        "blocked": ("blocked", "policy_blocked"),
        "service_error": ("unavailable", "service_error"),
        "cancelled": ("cancelled", "cancelled"),
    }[outcome]
    assert original[0][PROMPT_GUARD_CONTENT_PARTS_KEY] == parts
    coordinator.assert_completed()
