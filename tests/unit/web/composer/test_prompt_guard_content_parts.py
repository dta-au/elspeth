"""Lossless guard evidence is nominal, bounded, and kept off the provider wire."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError

import httpx
import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_MAX_TEXT_CHARACTERS,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    PromptGuardToolArgumentParts,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
    detach_prompt_guard_messages,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, _request, enforce_prompt_guard, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.provider_gateway import ProviderGateway, _litellm_acompletion
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.composer.service import _reply_only_messages
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_prompt_guard_clients import _settings

authority = _authority


def _prepare(messages, *, content_parts=None, tool_argument_parts=None, user_prompt="Build a graph", enforce_azure_character_limits=True):
    return _request(
        policy_fingerprint="a" * 64,
        role="composer",
        messages=messages,
        tools=(),
        user_prompt=user_prompt,
        trusted_message_indexes=frozenset(),
        content_parts=content_parts,
        tool_argument_parts=tool_argument_parts,
        enforce_azure_character_limits=enforce_azure_character_limits,
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


def test_detached_wire_preparation_preserves_immutable_producer_evidence():
    part = PromptGuardContentPart("catalog", PromptGuardTrust.REFERENCE)
    messages = [{"role": "tool", "content": "catalog", "metadata": {"observed": ["original"]}, PROMPT_GUARD_CONTENT_PARTS_KEY: (part,)}]
    evidence = detach_prompt_guard_evidence(messages)
    original_parts = evidence.content_parts

    # SDK preparation works on mutable, detached JSON values; its carrier
    # must not claim to freeze those values or the whole transport envelope.
    evidence.messages = deepcopy(evidence.messages)
    evidence.messages[0]["metadata"]["observed"].append("prepared")
    assert messages[0]["metadata"] == {"observed": ["original"]}
    assert evidence.content_parts is original_parts
    assert evidence.content_parts == ((part,),)
    with pytest.raises(FrozenInstanceError):
        part.text = "changed"

    # Changing wire bytes cannot silently retain the producer's coverage.
    evidence.messages[0]["content"] = "changed"
    with pytest.raises(AuditIntegrityError, match="complete wire content"):
        _prepare(evidence.messages, content_parts=evidence.content_parts)


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


def test_size_policy_changes_request_binding_without_changing_complete_documents():
    messages = [{"role": "tool", "content": "x" * 10_001}]
    strict, strict_supported = _prepare(messages)
    debug, debug_supported = _prepare(messages, enforce_azure_character_limits=False)
    assert not strict_supported and debug_supported
    assert strict.documents == debug.documents and "x" * 10_001 in debug.documents[0]
    assert strict.request_hash != debug.request_hash


@pytest.mark.parametrize("overhead_delta", [-1, 0, 1])
def test_size_policy_counts_wrapped_decoded_unicode_without_changing_escaped_wire(overhead_delta):
    prefix = "Message 0, content part 0:\n"
    decoded = "界" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS - len(prefix) + overhead_delta)
    serialized = json.dumps(decoded)
    part = PromptGuardContentPart(serialized, inspection_text=decoded)
    messages = [{"role": "tool", "content": serialized}]
    strict, supported = _prepare(messages, content_parts=((part,),))
    debug, debug_supported = _prepare(messages, content_parts=((part,),), enforce_azure_character_limits=False)
    assert len(serialized) > PROMPT_GUARD_MAX_TEXT_CHARACTERS
    assert max(map(len, strict.documents)) == PROMPT_GUARD_MAX_TEXT_CHARACTERS + overhead_delta
    assert supported is (overhead_delta <= 0) and debug_supported
    assert strict.documents == debug.documents and prefix + decoded in debug.documents
    assert part.text == messages[0]["content"] == serialized and part.trust is None


@pytest.mark.parametrize("invalid", [None, 0, 1, "false"])
def test_size_policy_refuses_non_boolean_enforcement(invalid):
    with pytest.raises(AuditIntegrityError, match="exact enforcement decision"):
        _prepare([], enforce_azure_character_limits=invalid)


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
@pytest.mark.parametrize("blocked", [False, True], ids=["allow", "later-batch-block"])
async def test_complete_large_reply_context_uses_real_guard_before_fenced_sdk(authority, monkeypatch, blocked):
    from elspeth.web.composer import prompt_guard

    engine, _service, owner, coordinator = authority
    user = "USER_COVERAGE_CANARY: review this ordinary graph without changing it."
    catalog_content = json.dumps({"schema": "REGISTERED_REFERENCE_CANARY " * 1000, "result": "REFERENCE_SIBLING_CANARY"})
    original = [
        {"role": "system", "content": "DEPLOYMENT_INSTRUCTION_CANARY", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION},
        {
            "role": "tool",
            "tool_call_id": "catalog-call",
            "content": catalog_content,
            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(catalog_content, reference_paths=frozenset({("schema",)})),
        },
        *[
            {"role": "tool", "tool_call_id": f"evidence-{index}", "content": f"COMPLETE_EVIDENCE_{index:02}:" + "x" * 8500}
            for index in range(40)
        ],
        {"role": "user", "content": user},
    ]
    before = deepcopy(original)
    quoted = _reply_only_messages(original)
    evidence = detach_prompt_guard_evidence(quoted)
    assert original == before
    assert all("".join(part.text for part in message[PROMPT_GUARD_CONTENT_PARTS_KEY]) == message["content"] for message in quoted[1:-1])
    units = []
    requests = []
    request_evidence = []
    original_pack = prompt_guard._pack_documents
    original_request = prompt_guard._request

    def observe_pack(complete_units):
        units.append(tuple(complete_units))
        return original_pack(complete_units)

    def observe_request(**kwargs):
        request_evidence.append(deepcopy(kwargs))
        request, supported = original_request(**kwargs)
        requests.append((request, supported))
        return request, supported

    monkeypatch.setattr(prompt_guard, "_pack_documents", observe_pack)
    monkeypatch.setattr(prompt_guard, "_request", observe_request)
    inspected = []

    def serve(sent):
        body = json.loads(sent.content)
        assert body["userPrompt"] == user
        assert 1 <= len(body["documents"]) <= 5
        assert sum(len(document) for document in body["documents"]) <= 10_000
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [
                    {"attackDetected": blocked and len(inspected) == 2 and index == 0} for index in range(len(body["documents"]))
                ],
            },
        )

    mount = PromptGuardMount(AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)))
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", lambda _settings: mount)
    settings = _make_settings(composer_model="gpt-4o", composer_prompt_guard_mode="required")
    gateway = ProviderGateway(model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    sdk_calls = []

    async def completion(**kwargs):
        assert [check["disposition"] for check in _checks(engine)] == ["allow"]
        with engine.connect() as connection:
            attempts = connection.execute(select(quota_provider_attempts_table)).all()
        assert len(attempts) == 1 and attempts[0].settled_at is None
        assert attempts[0].attempt_id == custody.attempt.attempt_id
        assert attempts[0].operation_id == custody.context.fence.operation_id
        assert attempts[0].operation_epoch == custody.context.fence.operation_epoch
        sdk_calls.append(deepcopy(kwargs))
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": "The graph remains available for review."}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(user):
        operation = gateway._call_llm_with_audit(quoted, [], timeout=10, recorder=recorder, provider_custody=custody)
        if blocked:
            with pytest.raises(PromptGuardRefused):
                await operation
        else:
            await operation

    assert len(requests) == len(units) == len(request_evidence) == 1
    request, supported = requests[0]
    assert supported
    assert request.user_prompt == user
    assert request.documents
    assert all(len(unit) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS for unit in units[0])
    assert all(len(document) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS for document in request.documents)
    size = len(user.encode("utf-8")) + sum(len(document.encode("utf-8")) for document in request.documents)
    assert size > 262_144
    assert "\n".join(request.documents) == "\n".join(units[0])
    complete_inspection = "\n".join(units[0])
    assert all(f"COMPLETE_EVIDENCE_{index:02}:" + "x" * 8500 in complete_inspection for index in range(40))
    assert "REFERENCE_SIBLING_CANARY" in complete_inspection
    assert "REGISTERED_REFERENCE_CANARY" not in complete_inspection and "DEPLOYMENT_INSTRUCTION_CANARY" not in complete_inspection
    assert request_evidence[0]["messages"] == evidence.messages
    assert request_evidence[0]["content_parts"] == evidence.content_parts
    assert request_evidence[0]["trusted_message_indexes"] == evidence.trusted_message_indexes
    actual_documents = tuple(document for body in inspected for document in body["documents"])
    checks = _checks(engine)
    assert len(checks) == 1
    assert checks[0]["operation_id"] == custody.context.fence.operation_id
    assert checks[0]["operation_epoch"] == custody.context.fence.operation_epoch
    assert checks[0]["request_hash"] == request.request_hash
    assert checks[0]["policy_fingerprint"] == mount.client.policy_fingerprint
    assert checks[0]["mode"] == "required" and checks[0]["adapter"] == "azure_prompt_shields"
    assert "CANARY" not in json.dumps(checks)
    with engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        ledger = connection.execute(select(token_usage_ledger_table)).all()
    if blocked:
        assert len(inspected) == 2
        assert actual_documents == request.documents[: len(actual_documents)]
        assert len(actual_documents) < len(request.documents)
        assert (checks[0]["disposition"], checks[0]["reason"]) == ("blocked", "policy_blocked")
        assert sdk_calls == [] and recorder.llm_calls == () and attempts == [] and ledger == []
    else:
        assert actual_documents == request.documents
        assert len(sdk_calls) == len(recorder.llm_calls) == len(attempts) == len(ledger) == 1
        assert sdk_calls[0]["messages"] == evidence.messages
        assert PROMPT_GUARD_CONTENT_PARTS_KEY not in json.dumps(sdk_calls[0])
        assert PROMPT_GUARD_TRUST_KEY not in json.dumps(sdk_calls[0])
        assert (checks[0]["disposition"], checks[0]["reason"]) == ("allow", "clean")
        assert recorder.llm_calls[0].status.value == "success"
        assert attempts[0].attempt_id == recorder.llm_calls[0].call_id
        assert attempts[0].settled_at is not None
        assert attempts[0].ledger_entry_id == ledger[0].entry_id
        assert ledger[0].session_id == custody.context.fence.session_id
        assert ledger[0].source == "composer" and (ledger[0].prompt_tokens, ledger[0].completion_tokens) == (3, 2)
    assert original == before
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("role", ["composer", "advisor"])
@pytest.mark.parametrize("kind", ["atomic", "oversized_part", "user", "surrogate_user", "surrogate_part"])
async def test_size_policy_preserves_other_input_refusals_and_complete_audit(authority, mode, configured, role, kind):
    engine, _service, owner, coordinator = authority
    custody = owner.mint(ProviderInvocationFamily.PRIMARY if role == "composer" else ProviderInvocationFamily.ADVISOR)
    messages = [{"role": "tool", "content": "Ordinary tool evidence"}]
    user = "Build a graph"
    parts = None
    if kind == "atomic":
        messages = [{"role": "tool", "content": "x" * 10_001}]
    elif kind == "oversized_part":
        messages = [{"role": "tool", "content": "x" * 10_001}]
        parts = ((PromptGuardContentPart("x" * 10_001),),)
    elif kind == "user":
        user = "x" * 10_001
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
    operation = enforce_prompt_guard(
        mount=mount if configured else None,
        mode=mode,
        role=role,
        provider_custody=custody,
        messages=messages,
        content_parts=parts,
        user_prompt=user,
    )
    debug_size_only = mode == "debug-when-unconfigured" and not configured and kind in {"atomic", "oversized_part", "user"}
    if debug_size_only:
        await operation
    else:
        with pytest.raises(PromptGuardRefused):
            await operation
    assert observed == []
    checks = _checks(engine)
    assert len(checks) == 1 and checks[0]["role"] == role
    assert (checks[0]["disposition"], checks[0]["reason"]) == (
        ("debug_bypass", "guard_not_configured") if debug_size_only else ("unavailable", "unsupported_input")
    )
    assert custody.attempt is None
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [None, (PromptGuardContentPart("partial"),), ({"text": "x" * 10_001},)])
async def test_unconfigured_debug_size_policy_still_refuses_foreign_or_incomplete_coverage(authority, parts):
    engine, _service, owner, coordinator = authority
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    messages = [{"role": "tool", "content": "x" * 10_001, PROMPT_GUARD_CONTENT_PARTS_KEY: parts}]
    with pytest.raises(AuditIntegrityError):
        evidence = detach_prompt_guard_evidence(messages)
        await enforce_prompt_guard(
            mount=None,
            mode="debug-when-unconfigured",
            role="composer",
            provider_custody=custody,
            messages=evidence.messages,
            content_parts=evidence.content_parts,
            user_prompt="Inspect this evidence.",
        )
    assert _checks(engine) == [] and custody.attempt is None
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
