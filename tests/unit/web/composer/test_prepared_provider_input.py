"""Bounded final candidates, immutable ownership and the real dispatch boundary.

These tests use the existing fenced SQL/quota fixture for physical-owner cases.
The LiteLLM boundary is replaced; guard persistence, preparation, admission and
undispatched disposition retain their actual production owners.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import MemberDescriptorType
from typing import Any
from uuid import UUID, uuid4

import litellm
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from elspeth.contracts.credential_material import (
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
    MeasuredCredentialTextExcess,
    find_credential_material,
    find_credential_material_in_projected_sequence,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    PromptGuardDecision,
    PromptGuardTrust,
)
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import prepared_provider_input as preparation
from elspeth.web.composer import provider_gateway, reply_preparation
from elspeth.web.composer import service as composer_service
from elspeth.web.composer.advisor_content import PreparedAdvisorContent
from elspeth.web.composer.continuation_message_builders import reply_instruction_message
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, plan_anthropic_cache_markers
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput, prepare_provider_input
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.prompts import CATALOG_CONTEXT_PREFIX
from elspeth.web.composer.protocol import ComposerAdmissionRefused, ComposerContextUnsupported
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.provider_thinking import attach_provider_thinking
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions._auto_title import _maybe_auto_title_session_required, prepare_auto_title_request
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer.test_prompt_guard_wiring import _Client
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.fixture
def invocation(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Prepared input", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    yield engine, service, owner, coordinator
    authority.release(context)
    engine.dispose()


def _size(messages: object, tools: object = None) -> int:
    return len(canonical_json({"messages": messages, "tools": tools if tools is not None else []}).encode("utf-8"))


def _prepare(messages, *, tools=None, limit=2 * 1024 * 1024, **kwargs) -> PreparedProviderInput:
    return prepare_provider_input(
        messages=messages,
        tools=tools,
        tools_present=tools is not None,
        model="test/model",
        role=kwargs.pop("role", "composer"),
        mount=kwargs.pop("mount", None),
        mode="required",
        user_prompt=kwargs.pop("user_prompt", "Original user input"),
        max_bytes=limit,
        **kwargs,
    )


def _verify(prepared: PreparedProviderInput, **kwargs) -> None:
    prepared.verify(
        model=prepared.model,
        role=prepared.role,
        mount=prepared.mount,
        mode=prepared.mode,
        user_prompt=prepared.genuine_user,
        **kwargs,
    )


@pytest.mark.parametrize(
    "change",
    ("source", "excluded", "cache_indexes", "delete_source", "delete_excluded", "delete_cache_indexes", "reinitialize", "extra"),
)
def test_array_view_preserves_live_sources_defaults_and_closed_bindings(change):
    first = {"role": "system", "content": "Policy", "cache_control": {"old": True}}
    second = {"role": "user", "content": "Original", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.REFERENCE}
    source = [first, second]
    excluded = frozenset({PROMPT_GUARD_TRUST_KEY})
    indexes = (0,)
    view = preparation._ArrayView(source=source, excluded=excluded, cache_indexes=indexes)
    assert view.source is source and view.excluded is excluded and view.cache_indexes is indexes
    projected = tuple(view)
    assert projected[0].source is first and projected[1].source is second
    assert dict(projected[0].items()) == {"role": "system", "content": "Policy", "cache_control": {"type": "ephemeral"}}
    assert dict(projected[1].items()) == {"role": "user", "content": "Original"}
    default = preparation._ArrayView(source, excluded)
    assert default.cache_indexes == ()
    assert dict(next(iter(default)).items())["cache_control"] is first["cache_control"]
    assert type(preparation._ArrayView.__dict__["source"]) is MemberDescriptorType
    assert type(preparation._ArrayView.__dict__["excluded"]) is MemberDescriptorType
    assert type(preparation._ArrayView.__dict__["cache_indexes"]) is MemberDescriptorType
    assert preparation._ArrayView.__getattribute__ is object.__getattribute__
    foreign_source = [dict(message) for message in source]
    foreign_excluded = frozenset(key for key in excluded)
    foreign_indexes = tuple(index for index in indexes)
    equal_view = preparation._ArrayView(foreign_source, foreign_excluded, foreign_indexes)
    assert equal_view == view and equal_view.source is not view.source

    class OtherArrayView(preparation._ArrayView):
        __slots__ = ()

    assert view.__eq__(OtherArrayView(source, excluded, indexes)) is NotImplemented
    assert OtherArrayView(source, excluded, indexes) == OtherArrayView(foreign_source, foreign_excluded, foreign_indexes)
    assert view.__eq__(source) is NotImplemented
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "excluded":
            view.excluded = foreign_excluded
        elif change == "cache_indexes":
            view.cache_indexes = foreign_indexes
        elif change == "delete_source":
            del view.source
        elif change == "delete_excluded":
            del view.excluded
        elif change == "delete_cache_indexes":
            del view.cache_indexes
        elif change == "reinitialize":
            view.__init__(foreign_source, foreign_excluded, foreign_indexes)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source and view.excluded is excluded and view.cache_indexes is indexes
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source, excluded, indexes)

    first["content"] = "Changed policy"
    late = {"role": "assistant", "content": "Later"}
    source.append(late)
    current = tuple(view)
    assert len(view) == 3 and current[-1].source is late
    assert current[0]["content"] == "Changed policy"
    assert "cache_control" not in current[-1]
    expected = [
        {"role": "system", "content": "Changed policy", "cache_control": {"type": "ephemeral"}},
        {"role": "user", "content": "Original"},
        late,
    ]
    measured = preparation.bounded_canonical_json_size({"messages": view, "tools": []}, limit=10_000)
    assert measured.admitted_bytes == _size(expected)


@pytest.mark.parametrize("change", ("source", "tail", "delete_source", "delete_tail", "reinitialize", "extra"))
def test_extended_messages_keep_live_tail_and_slice_bindings(change):
    first = {"role": "system", "content": "Policy"}
    second = {"role": "user", "content": "Question"}
    third = {"role": "assistant", "content": "Answer"}
    source = [first, second, third]
    tail = reply_instruction_message()
    view = reply_preparation._ExtendedMessages(source=source, tail=tail)
    assert view.source is source and view.tail is tail and len(view) == 4
    assert view[0] is first and view[-4] is first and view[-1] is tail
    assert tuple(view) == (first, second, third, tail)
    assert tuple(reversed(view)) == (tail, third, second, first)
    for invalid in (4, -5):
        with pytest.raises(IndexError):
            view[invalid]
    sliced = view[1:]
    nested = sliced[::-1][1:]
    assert type(sliced) is reply_preparation._MessageSlice and sliced.source is view
    assert type(nested) is reply_preparation._MessageSlice and nested.source is view
    assert nested.positions == range(1, 4)[::-1][1:]
    assert nested[0] is third and nested[-1] is second
    assert type(reply_preparation._ExtendedMessages.__dict__["source"]) is MemberDescriptorType
    assert type(reply_preparation._ExtendedMessages.__dict__["tail"]) is MemberDescriptorType
    assert reply_preparation._ExtendedMessages.__getattribute__ is object.__getattribute__
    foreign_source, foreign_tail = list(source), dict(tail)
    equal_view = reply_preparation._ExtendedMessages(foreign_source, foreign_tail)
    assert equal_view == view and equal_view.source is not source and equal_view.tail is not tail

    class OtherExtendedMessages(reply_preparation._ExtendedMessages):
        __slots__ = ()

    assert view.__eq__(OtherExtendedMessages(source, tail)) is NotImplemented
    assert OtherExtendedMessages(source, tail) == OtherExtendedMessages(foreign_source, foreign_tail)
    assert view.__eq__(source) is NotImplemented
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "tail":
            view.tail = foreign_tail
        elif change == "delete_source":
            del view.source
        elif change == "delete_tail":
            del view.tail
        elif change == "reinitialize":
            view.__init__(foreign_source, foreign_tail)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source and view.tail is tail
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source, tail)

    replacement = {"role": "user", "content": "Updated question"}
    late = {"role": "assistant", "content": "Later answer"}
    source[1] = replacement
    source.append(late)
    assert len(view) == 5 and view[-1] is tail and view[-2] is late
    assert nested[-1] is replacement
    assert sliced[-1] is late, "a stored positional slice still borrows the current source"
    tail["content"] = "Updated instruction"
    assert view[-1]["content"] == "Updated instruction"


@pytest.mark.parametrize("change", ("source", "positions", "delete_source", "delete_positions", "reinitialize", "extra"))
def test_message_slice_keeps_original_source_positions_and_live_aliases(change):
    source = [{"role": "user", "content": f"Message {index}"} for index in range(5)]
    positions = range(0, 5, 2)
    view = reply_preparation._MessageSlice(source=source, positions=positions)
    assert view.source is source and view.positions is positions and len(view) == 3
    assert view[0] is source[0] and view[-1] is source[4]
    assert all(actual is source[index] for actual, index in zip(view, positions, strict=True))
    nested = view[::-1][1:]
    assert type(nested) is reply_preparation._MessageSlice and nested.source is source
    assert nested.positions == positions[::-1][1:]
    assert nested[0] is source[2] and nested[-1] is source[0]
    for invalid in (3, -4):
        with pytest.raises(IndexError):
            view[invalid]
    assert type(reply_preparation._MessageSlice.__dict__["source"]) is MemberDescriptorType
    assert type(reply_preparation._MessageSlice.__dict__["positions"]) is MemberDescriptorType
    assert reply_preparation._MessageSlice.__getattribute__ is object.__getattribute__
    foreign_source, foreign_positions = list(source), range(0, 5, 2)
    equal_view = reply_preparation._MessageSlice(foreign_source, foreign_positions)
    assert equal_view == view and equal_view.source is not source

    class OtherMessageSlice(reply_preparation._MessageSlice):
        __slots__ = ()

    assert view.__eq__(OtherMessageSlice(source, positions)) is NotImplemented
    assert OtherMessageSlice(source, positions) == OtherMessageSlice(foreign_source, foreign_positions)
    assert view.__eq__(source) is NotImplemented
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "positions":
            view.positions = foreign_positions
        elif change == "delete_source":
            del view.source
        elif change == "delete_positions":
            del view.positions
        elif change == "reinitialize":
            view.__init__(foreign_source, foreign_positions)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source and view.positions is positions
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source, positions)

    replacement = {"role": "user", "content": "Replacement"}
    source[2] = replacement
    assert view[1] is replacement and nested[0] is replacement
    source.pop()
    assert len(view) == 3, "the selected positional range is fixed while its source remains live"
    with pytest.raises(IndexError):
        view[-1]


@pytest.mark.parametrize("change", ("source", "delete_source", "reinitialize", "extra"))
def test_reply_roles_borrow_current_messages_without_changing_quoted_content(change):
    first = {"role": "system", "content": "Policy"}
    historical = {"role": "tool", "tool_call_id": "history", "content": "Original evidence"}
    tool_calls = {"role": "assistant", "content": "", "tool_calls": []}
    source = [first, historical, tool_calls]
    view = reply_preparation._ReplyRoles(source=source)
    assert view.source is source and len(view) == 3 and view[0] is first
    assert view[1] == {"role": "user", "content": ""}
    assert view[-1] == {"role": "assistant", "content": ""}
    assert historical["content"] == "Original evidence" and tool_calls["tool_calls"] == []
    sliced = view[::-1][1:]
    assert type(sliced) is reply_preparation._MessageSlice and sliced.source is view
    assert tuple(sliced) == ({"role": "user", "content": ""}, first)
    assert type(reply_preparation._ReplyRoles.__dict__["source"]) is MemberDescriptorType
    assert reply_preparation._ReplyRoles.__getattribute__ is object.__getattribute__
    foreign_source = [dict(message) for message in source]
    equal_view = reply_preparation._ReplyRoles(foreign_source)
    assert equal_view == view and equal_view.source is not source

    class OtherReplyRoles(reply_preparation._ReplyRoles):
        __slots__ = ()

    assert view.__eq__(OtherReplyRoles(source)) is NotImplemented
    assert OtherReplyRoles(source) == OtherReplyRoles(foreign_source)
    assert view.__eq__(source) is NotImplemented
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "delete_source":
            del view.source
        elif change == "reinitialize":
            view.__init__(foreign_source)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source)

    replacement = {"role": "user", "content": CATALOG_CONTEXT_PREFIX + "New catalog"}
    source[1] = replacement
    source.append({"role": "user", "content": "Later question"})
    assert len(view) == 4 and view[1] is replacement and view[-1] is source[-1]
    assert sliced[0] is replacement and historical["content"] == "Original evidence"
    plan = plan_anthropic_cache_markers(view, (), mark_history_tail=True)
    assert plan.message_indexes == (0, 1, 3)


@pytest.mark.parametrize(
    "change",
    ("source", "prefix", "suffix", "cache", "delete_source", "delete_prefix", "delete_suffix", "delete_cache", "reinitialize", "extra"),
)
def test_sized_messages_keep_live_framing_cache_owner_and_exact_wire_size(change):
    historical = {"role": "tool", "tool_call_id": "history", "content": 'Evidence é\\"\n'}
    source = [{"role": "system", "content": "Policy"}, historical]
    instruction = reply_instruction_message()
    candidate = reply_preparation._ExtendedMessages(source, instruction)
    prefix, suffix = composer_service._REPLY_ONLY_HISTORY_PREFIX, composer_service._REPLY_ONLY_HISTORY_SUFFIX
    cache = plan_anthropic_cache_markers(reply_preparation._ReplyRoles(candidate), (), mark_history_tail=True)
    view = reply_preparation._SizedMessages(source=candidate, prefix=prefix, suffix=suffix, cache=cache)
    assert view.source is candidate and view.prefix is prefix and view.suffix is suffix and view.cache is cache
    items = tuple(view)
    assert items[0].message is source[0] and items[0].text is None
    assert items[1].message is historical and items[1].text.prefix is prefix and items[1].text.suffix is suffix
    assert items[-1].message is instruction
    prospective = preparation.bounded_canonical_json_size({"messages": view, "tools": []}, limit=10_000)
    packet, _raw = _prepare_reply(source, cache=True)
    assert prospective.admitted_bytes == packet.byte_count == _size(packet.evidence.messages)
    assert packet.cache_plan == cache and packet.cache_plan.message_indexes == (0, 2)
    assert type(reply_preparation._SizedMessages.__dict__["source"]) is MemberDescriptorType
    assert type(reply_preparation._SizedMessages.__dict__["prefix"]) is MemberDescriptorType
    assert type(reply_preparation._SizedMessages.__dict__["suffix"]) is MemberDescriptorType
    assert type(reply_preparation._SizedMessages.__dict__["cache"]) is MemberDescriptorType
    assert reply_preparation._SizedMessages.__getattribute__ is object.__getattribute__
    foreign_source = reply_preparation._ExtendedMessages(list(source), dict(instruction))
    foreign_cache = replace(cache)
    equal_view = reply_preparation._SizedMessages(foreign_source, prefix, suffix, foreign_cache)
    assert equal_view == view and equal_view.source is not candidate and equal_view.cache is not cache

    class OtherSizedMessages(reply_preparation._SizedMessages):
        __slots__ = ()

    assert view.__eq__(OtherSizedMessages(candidate, prefix, suffix, cache)) is NotImplemented
    assert OtherSizedMessages(candidate, prefix, suffix, cache) == OtherSizedMessages(foreign_source, prefix, suffix, foreign_cache)
    assert view.__eq__(candidate) is NotImplemented
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "prefix":
            view.prefix = prefix + "changed"
        elif change == "suffix":
            view.suffix = suffix + "changed"
        elif change == "cache":
            view.cache = foreign_cache
        elif change == "delete_source":
            del view.source
        elif change == "delete_prefix":
            del view.prefix
        elif change == "delete_suffix":
            del view.suffix
        elif change == "delete_cache":
            del view.cache
        elif change == "reinitialize":
            view.__init__(foreign_source, prefix + "changed", suffix + "changed", foreign_cache)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is candidate and view.prefix is prefix and view.suffix is suffix and view.cache is cache
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(candidate, prefix, suffix, cache)

    historical["content"] += " Later 🧵"
    current = preparation.bounded_canonical_json_size({"messages": view, "tools": []}, limit=10_000)
    updated, _raw = _prepare_reply(source, cache=True)
    assert current.admitted_bytes == updated.byte_count == _size(updated.evidence.messages)
    assert current.admitted_bytes != prospective.admitted_bytes and view.cache is cache
    source.append({"role": "user", "content": "Later question"})
    assert len(view) == 4 and tuple(view)[-1].message is instruction


@pytest.mark.parametrize("value", [0, -1, True, False, "2097152", 2.0])
def test_configuration_requires_an_actual_positive_integer(value):
    assert _make_settings().composer_max_prepared_request_bytes == 2 * 1024 * 1024
    with pytest.raises(ValidationError):
        _make_settings(composer_max_prepared_request_bytes=value)


def test_new_fixed_reason_uses_existing_closed_manual_public_projection():
    telemetry = NoopRedactionTelemetry()
    semantic = ComposerContextUnsupported("prepared_request_bytes").to_payload()
    response = {"success": False, "outbound_call_made": False, **semantic}
    public = redact_tool_call_response("request_advisor_hint", response, telemetry=telemetry)
    assert all(public[key] == value for key, value in semantic.items())
    assert public["success"] is False and public["outbound_call_made"] is False
    mismatched = {**response, "detail": ComposerContextUnsupported("graph_resource_limit").to_payload()["detail"]}
    closed = redact_tool_call_response("request_advisor_hint", mismatched, telemetry=telemetry)
    assert closed["failure_code"] != "context_unsupported"
    assert closed["detail"] != mismatched["detail"]


@pytest.mark.parametrize("role", ["composer", "advisor"])
@pytest.mark.parametrize("tools", [None, []])
def test_exact_canonical_boundary_preserves_omitted_tools_and_unicode(role, tools):
    messages = [{"role": "user", "content": 'A quote: " and éλ🧵'}]
    expected = _size(messages, tools)
    prepared = _prepare(messages, tools=tools, limit=expected, role=role)
    assert prepared.byte_count == expected
    assert prepared.evidence.messages == messages and prepared.evidence.messages is not messages
    assert prepared.tools_present is (tools is not None)
    assert (prepared.tools is None) is (tools is None)
    _verify(prepared)
    with pytest.raises(ComposerContextUnsupported) as caught:
        _prepare(messages, tools=tools, limit=expected - 1, role=role)
    assert caught.value.reason == "prepared_request_bytes"
    assert caught.value.to_payload() == ComposerContextUnsupported("prepared_request_bytes").to_payload()


def test_excess_precedes_new_detach_hash_copy_with_positive_instrument_control(monkeypatch):
    messages = [{"role": "user", "content": "z" * 50_000}]
    hits = []

    def forbidden_materialization(value):
        hits.append("materialize")
        raise AssertionError("new request-owned materialization")

    def forbidden_aggregate(value):
        hits.append("aggregate")
        raise AssertionError("full request serialization")

    monkeypatch.setattr(preparation, "_materialize", forbidden_materialization)
    monkeypatch.setattr(preparation, "canonical_json", forbidden_aggregate)
    monkeypatch.setattr(preparation, "deepcopy", forbidden_materialization)
    with pytest.raises(AssertionError, match="request-owned materialization"):
        _prepare(messages)
    assert hits == ["materialize"]
    hits.clear()
    with pytest.raises(ComposerContextUnsupported) as caught:
        _prepare(messages, limit=64)
    assert caught.value.reason == "prepared_request_bytes"
    assert hits == []


def test_cache_plan_sizes_final_overwrite_and_added_tail_before_materializing():
    messages = [
        {"role": "system", "content": "Instruction", "cache_control": {"superseded": "x" * 50_000}},
        {"role": "user", "content": CATALOG_CONTEXT_PREFIX + "Catalog", "cache_control": {"old": True}},
        {"role": "assistant", "content": "History"},
    ]
    tools = [{"type": "function", "function": {"name": "f"}, "cache_control": {"old": "y" * 50_000}}]
    expected_messages, expected_tools = apply_anthropic_cache_markers(messages, tools, mark_history_tail=True)
    assert _size(messages, tools) > _size(expected_messages, expected_tools)
    exact = _size(expected_messages, expected_tools)
    prepared = _prepare(messages, tools=tools, limit=exact, cache_markers=True, mark_history_tail=True)
    assert prepared.evidence.messages == expected_messages and prepared.tools == expected_tools
    assert prepared.byte_count == exact
    assert prepared.cache_plan.message_indexes == (0, 1, 2)
    assert prepared.cache_plan.tool_index == 0
    assert messages[0]["cache_control"] == {"superseded": "x" * 50_000}
    with pytest.raises(ComposerContextUnsupported):
        _prepare(messages, tools=tools, limit=exact - 1, cache_markers=True, mark_history_tail=True)


def test_new_cache_overhead_can_refuse_previously_fitting_raw_wire():
    messages = [{"role": "system", "content": "Instruction"}, {"role": "user", "content": "Catalog"}]
    exact_raw = _size(messages)
    assert _prepare(messages, limit=exact_raw).byte_count == exact_raw
    with pytest.raises(ComposerContextUnsupported):
        _prepare(messages, limit=exact_raw, cache_markers=True)


def test_partition_integrity_precedes_size_and_never_copies(monkeypatch):
    messages = [{"role": "user", "content": "x" * 50_000, PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart("wrong"),)}]

    def forbidden(value):
        raise AssertionError("invalid provenance must precede detachment")

    monkeypatch.setattr(preparation, "_materialize", forbidden)
    with pytest.raises(AuditIntegrityError, match="cover"):
        _prepare(messages, limit=1)


def test_whole_message_trust_conflict_precedes_size():
    messages = [
        {
            "role": "user",
            "content": "x" * 50_000,
            PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.REFERENCE,
            PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart("x" * 50_000),),
        }
    ]
    with pytest.raises(AuditIntegrityError, match="partition"):
        _prepare(messages, limit=1)


def test_complete_advisor_credential_after_large_prefix_outranks_size():
    content = "x" * 300_000 + " https://fixture-user:fixture-pass@example.test"
    with pytest.raises(CredentialMaterialRefused) as caught:
        _prepare(
            [{"role": "user", "content": content}],
            role="advisor",
            advisor_content=(PreparedAdvisorContent(0, content),),
            limit=64,
        )
    assert not isinstance(caught.value, ComposerContextUnsupported)
    assert caught.value.finding.category == "credential_url"


def test_decoded_inspection_credentials_outrank_size_but_latest_genuine_skip_stays():
    decoded = "https://fixture-user:fixture-pass@example.test"
    encoded = "".join(f"\\u{ord(character):04x}" for character in decoded)
    messages = [
        {"role": "user", "content": "Original user input"},
        {"role": "user", "content": encoded, PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart(encoded, inspection_text=decoded),)},
    ]
    with pytest.raises(CredentialMaterialRefused) as caught:
        _prepare(messages, limit=1)
    assert caught.value.finding.category == "credential_url"
    # Existing genuine-user ownership excludes that last exact matching user
    # from content-part inspection; it still checks the genuine wire scalar.
    with pytest.raises(ComposerContextUnsupported):
        _prepare(messages, limit=1, user_prompt=encoded)


@pytest.mark.parametrize("block", [{"type": "redacted_thinking", "data": ""}, {"type": "thinking", "thinking": "ok"}])
def test_native_grammar_error_keeps_original_owner_ahead_of_excess(block):
    with pytest.raises(ComposerAdmissionRefused) as caught:
        _prepare([{"role": "assistant", "content": "x" * 50_000, "thinking_blocks": [block]}], limit=1)
    assert type(caught.value) is ComposerAdmissionRefused
    assert "Provider thinking state" in str(caught.value)


def test_complete_native_credential_after_large_prefix_outranks_size():
    data = "x" * 300_000 + " https://fixture-user:fixture-pass@example.test"
    with pytest.raises(CredentialMaterialRefused) as caught:
        _prepare([{"role": "assistant", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": data}]}], limit=64)
    assert caught.value.finding.category == "credential_url"


@pytest.mark.parametrize("target", ["messages", "tools", "inspection", "trust", "size_type"])
def test_post_preparation_mutation_is_integrity_even_when_size_is_unchanged(target):
    messages = [{"role": "user", "content": "neutral", PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart("neutral"),)}]
    prepared = _prepare(messages, tools=[{"name": "first"}])
    _verify(prepared)
    if target == "messages":
        prepared.evidence.messages[0]["content"] = "changed"
    elif target == "tools":
        prepared.tools[0]["name"] = "other"
    elif target == "inspection":
        prepared.evidence.content_parts = ((PromptGuardContentPart("neutral", inspection_text="changed decoded words"),),)
    elif target == "trust":
        prepared.evidence.trusted_message_indexes = frozenset({0})
    else:
        prepared.byte_count = float(prepared.byte_count)
    with pytest.raises(AuditIntegrityError):
        _verify(prepared)


def test_one_dispatch_copy_is_isolated_and_single_use():
    messages = [{"role": "user", "content": "neutral"}]
    tools = [{"function": {"name": "f"}}]
    prepared = _prepare(messages, tools=tools)
    dispatch = prepared.consume(
        model=prepared.model, role=prepared.role, mount=prepared.mount, mode=prepared.mode, user_prompt=prepared.genuine_user
    )
    _verify(prepared, dispatch=dispatch)
    assert dispatch.evidence.messages is not prepared.evidence.messages and dispatch.tools is not prepared.tools
    assert dispatch.tools[0]["function"] is not prepared.tools[0]["function"]
    with pytest.raises(AuditIntegrityError, match="reused"):
        prepared.consume(
            model=prepared.model, role=prepared.role, mount=prepared.mount, mode=prepared.mode, user_prompt=prepared.genuine_user
        )
    dispatch.evidence.messages[0]["content"] = "changed"
    assert prepared.evidence.messages == messages
    with pytest.raises(AuditIntegrityError):
        _verify(prepared, dispatch=dispatch)


@pytest.mark.parametrize("changed", ["model", "role", "mount", "mode", "genuine_user", "client_policy"])
def test_preparation_cannot_change_selected_owner_or_mutable_mount_policy(changed):
    class PolicyClient(_Client):
        def __init__(self):
            super().__init__(PromptGuardDecision("allow", "clean"))
            self.fingerprint = "b" * 64

        @property
        def policy_fingerprint(self):
            return self.fingerprint

    client = PolicyClient()
    mount = PromptGuardMount(client)
    prepared = _prepare([{"role": "user", "content": "neutral"}], mount=mount)
    arguments = {
        "model": prepared.model,
        "role": prepared.role,
        "mount": mount,
        "mode": prepared.mode,
        "user_prompt": prepared.genuine_user,
    }
    prepared.verify(**arguments)
    if changed == "model":
        arguments["model"] = "another/model"
    elif changed == "role":
        arguments["role"] = "advisor"
    elif changed == "mount":
        arguments["mount"] = PromptGuardMount(client)
    elif changed == "mode":
        arguments["mode"] = "debug-when-unconfigured"
    elif changed == "genuine_user":
        prepared.genuine_user = "Changed actual input"
        arguments["user_prompt"] = prepared.genuine_user
    else:
        client.fingerprint = "c" * 64
    with pytest.raises(AuditIntegrityError):
        prepared.verify(**arguments)


def test_retry_is_fresh_keeps_empty_tools_and_rechecks_current_limit():
    settings = _make_settings(composer_max_prepared_request_bytes=4096)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope("Original user input"):
        previous = gateway.prepare_input([{"role": "user", "content": "neutral"}], [], tools_present=True)
    next_attempt = gateway.reprepare_input(previous)
    assert next_attempt is not previous and next_attempt.evidence.messages is not previous.evidence.messages
    assert next_attempt.tools_present and next_attempt.tools == []
    assert next_attempt.genuine_user == "Original user input"
    assert next_attempt.byte_count == previous.byte_count
    gateway._settings = settings.model_copy(update={"composer_max_prepared_request_bytes": previous.byte_count - 1})
    with pytest.raises(ComposerContextUnsupported):
        gateway.reprepare_input(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family,role",
    [
        (ProviderInvocationFamily.PRIMARY, "composer"),
        (ProviderInvocationFamily.TITLE, "composer"),
        (ProviderInvocationFamily.ADVISOR, "advisor"),
    ],
)
async def test_actual_physical_refusal_has_no_guard_quota_dispatch_or_sdk(invocation, monkeypatch, family, role):
    engine, _sessions, owner, coordinator = invocation
    custody = owner.mint(family)
    client = _Client(PromptGuardDecision("allow", "clean"))
    dispatches = []
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(ComposerContextUnsupported) as caught:
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=PromptGuardMount(client),
            prompt_guard_mode="required",
            prompt_guard_role=role,
            prompt_guard_user_prompt="Original user input",
            on_provider_dispatch=lambda: dispatches.append(True),
            max_prepared_request_bytes=64,
            model="test/model",
            messages=[{"role": "user", "content": "x" * 50_000}],
        )
    assert caught.value.reason == "prepared_request_bytes"
    assert client.requests == [] and sdk_calls == [] and dispatches == []
    assert custody.attempt is None and custody.call is None
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        assert connection.execute(select(chat_messages_table)).all() == []
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["guard", "quota"])
async def test_actual_owner_revalidates_after_each_await_and_releases_undispatched_attempt(invocation, monkeypatch, when):
    engine, _sessions, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    prepared = None

    class MutatingClient(_Client):
        async def inspect(self, request):
            result = await super().inspect(request)
            if when == "guard":
                prepared.evidence.messages[0]["content"] = "Changed user input!"
            return result

    client = MutatingClient(PromptGuardDecision("allow", "clean"))
    mount = PromptGuardMount(client)
    prepared = _prepare([{"role": "user", "content": "Original user input"}], mount=mount)
    actual_admit = ProviderCallCustody.admit
    admitted = []

    async def admit_then_mutate(self, *, model):
        await actual_admit(self, model=model)
        admitted.append(self.attempt)
        if when == "quota":
            prepared.evidence.messages[0]["content"] = "Changed user input!"

    monkeypatch.setattr(ProviderCallCustody, "admit", admit_then_mutate)
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(AuditIntegrityError):
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=mount,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            prompt_guard_user_prompt=prepared.genuine_user,
            prepared_input=prepared,
            model=prepared.model,
            messages=prepared.evidence.messages,
        )
    assert sdk_calls == [] and len(client.requests) == 1
    assert len(admitted) == int(when == "quota")
    with engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).mappings().all()
        usage = connection.execute(select(token_usage_ledger_table)).mappings().all()
    if when == "guard":
        assert attempts == [] and usage == []
    else:
        assert len(attempts) == 1
        assert attempts[0]["settled_at"] is not None
        assert len(usage) == 1 and attempts[0]["ledger_entry_id"] == usage[0]["entry_id"]
        assert (
            usage[0]["prompt_tokens"],
            usage[0]["completion_tokens"],
            usage[0]["cached_prompt_tokens"],
            usage[0]["reasoning_tokens"],
        ) == (0, 0, 0, 0)
    assert not custody.has_undispatched_attempt()
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_tools", [False, True])
async def test_actual_sdk_wire_matches_capture_and_isolated_preparation(invocation, monkeypatch, explicit_tools):
    _engine, _sessions, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("allow", "clean"))
    mount = PromptGuardMount(client)
    prepared = _prepare([{"role": "user", "content": "Original user input"}], mount=mount, tools=[] if explicit_tools else None)
    captured = capture_provider_context(messages=prepared.evidence.messages, tools=prepared.tools)
    sdk_entries: list[dict[str, Any]] = []
    dispatches = []

    async def completion(**kwargs):
        sdk_entries.append(deepcopy(kwargs))
        assert kwargs["messages"] is not prepared.evidence.messages
        kwargs["messages"][0]["content"] = "SDK-owned mutation"
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    transport = {"model": prepared.model, "messages": prepared.evidence.messages}
    if explicit_tools:
        transport["tools"] = prepared.tools
    await provider_gateway._litellm_acompletion(
        provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        prompt_guard_mount=mount,
        prompt_guard_mode="required",
        prompt_guard_role="composer",
        prompt_guard_user_prompt=prepared.genuine_user,
        prepared_input=prepared,
        on_provider_dispatch=lambda: dispatches.append(True),
        **transport,
    )
    assert len(sdk_entries) == 1 and dispatches == [True] and len(client.requests) == 1
    actual = sdk_entries[0]
    assert ("tools" in actual) is explicit_tools
    assert actual["messages"] == prepared.evidence.messages == [{"role": "user", "content": "Original user input"}]
    assert capture_provider_context(messages=actual["messages"], tools=actual.get("tools")) == captured
    assert _size(actual["messages"], actual.get("tools")) == prepared.byte_count
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_warm_native_request_between_retention_and_dispatch_limits_reaches_sdk(invocation, monkeypatch):
    _engine, _sessions, owner, coordinator = invocation
    client = _Client(PromptGuardDecision("allow", "clean"))
    mount = PromptGuardMount(client)
    native = [{"type": "redacted_thinking", "data": "A" * (1100 * 1024)}]
    messages = [
        attach_provider_thinking({"role": "assistant", "content": ""}, native),
        {"role": "user", "content": "Original user input"},
    ]
    prepared = _prepare(messages, mount=mount)
    assert 1024 * 1024 < prepared.byte_count < 2 * 1024 * 1024
    capture = capture_provider_context(messages=prepared.evidence.messages, tools=prepared.tools)
    assert capture.disposition == "unavailable"
    sent = []

    async def completion(**kwargs):
        sent.append(deepcopy(kwargs))
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    await provider_gateway._litellm_acompletion(
        provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        prompt_guard_mount=mount,
        prompt_guard_mode="required",
        prompt_guard_role="composer",
        prompt_guard_user_prompt=prepared.genuine_user,
        prepared_input=prepared,
        model=prepared.model,
        messages=prepared.evidence.messages,
    )
    assert len(sent) == len(client.requests) == 1
    assert sent[0]["messages"][0]["thinking_blocks"] == native
    assert _size(sent[0]["messages"]) == prepared.byte_count
    assert all(len(document) <= 10_000 for document in client.requests[0].documents)
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_title_size_refusal_checks_original_user_before_redacted_truncated_framing(invocation, monkeypatch):
    engine, sessions, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.TITLE)
    user = "Neutral topic. " * 500 + " https://fixture-user:fixture-pass@example.test"
    request = prepare_auto_title_request(user_message=user, model="test/model", temperature=None, seed=None)
    assert request.user_prompt is user
    assert user not in str(request.audit_messages)
    assert "fixture-pass" not in str(request.audit_messages)
    client = _Client(PromptGuardDecision("allow", "clean"))

    async def forbidden_sdk(**kwargs):
        raise AssertionError("Original credential-bearing user must be refused before SDK")

    monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
    with pytest.raises(CredentialMaterialRefused) as caught:
        await _maybe_auto_title_session_required(
            service=sessions,
            session_id=UUID(custody.context.fence.session_id),
            model="test/model",
            temperature=None,
            seed=None,
            session_operation_context=custody.context,
            kwargs=request.kwargs,
            audit_messages=request.audit_messages,
            provider_custody=custody,
            prompt_guard_mount=PromptGuardMount(client),
            prompt_guard_mode="required",
            user_message=user,
            max_prepared_request_bytes=1,
        )
    assert caught.value.finding.category == "credential_url"
    assert client.requests == [] and custody.attempt is None and custody.call is None
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        assert connection.execute(select(chat_messages_table)).all() == []
    coordinator.assert_completed()


def _prepare_reply(messages, *, tools=(), limit=2 * 1024 * 1024, cache=False, frame_all=None, user="Original user input"):
    gateway = ProviderGateway(
        model="anthropic/claude-opus-4-7" if cache else "openai/gpt-4o",
        settings=_make_settings(composer_max_prepared_request_bytes=limit),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )
    instruction = reply_instruction_message()
    with prompt_guard_user_scope(user):
        return gateway.prepare_reply_input(
            messages,
            tools,
            instruction=instruction,
            prefix=composer_service._REPLY_ONLY_HISTORY_PREFIX,
            suffix=composer_service._REPLY_ONLY_HISTORY_SUFFIX,
            frame_one=lambda message: composer_service._reply_only_messages((message,))[0],
            frame_all=frame_all if frame_all is not None else lambda: composer_service._reply_only_messages(messages),
        )


@pytest.mark.parametrize("numbers", [[1.0, -0.0, 2**64], [float("nan"), float("inf"), float("-inf")]])
def test_reply_exact_final_boundary_uses_standard_inner_json_and_canonical_outer_bytes(numbers):
    historical = {"role": "tool", "tool_call_id": "history-1", "content": 'é🧵\\"\n\x00', "numbers": numbers}
    source = [{"role": "system", "content": "Policy"}, historical]
    instruction = reply_instruction_message()
    quoted = (
        composer_service._REPLY_ONLY_HISTORY_PREFIX
        + json.dumps(historical, ensure_ascii=False, sort_keys=True)
        + composer_service._REPLY_ONLY_HISTORY_SUFFIX
    )
    expected = [source[0], {"role": "user", "content": quoted}, {"role": instruction["role"], "content": instruction["content"]}]
    exact = _size(expected)
    packet, _raw = _prepare_reply(source, limit=exact)
    assert packet.byte_count == exact and packet.evidence.messages == expected
    assert packet.tools is None and packet.tools_present is False
    assert packet.genuine_user == "Original user input"
    _verify(packet)
    with pytest.raises(ComposerContextUnsupported) as caught:
        _prepare_reply(source, limit=exact - 1)
    assert caught.value.reason == "prepared_request_bytes"


def test_reply_final_cache_overwrite_can_admit_raw_input_above_the_limit():
    source = [
        {"role": "system", "content": "Policy", "cache_control": {"superseded": "x" * 50_000}},
        {"role": "tool", "tool_call_id": "history-1", "content": "Evidence"},
    ]
    tools = [{"type": "function", "function": {"name": "handoff_to_user"}, "cache_control": {"superseded": "y" * 50_000}}]
    instruction = reply_instruction_message()
    expected = [
        {"role": "system", "content": "Policy", "cache_control": {"type": "ephemeral"}},
        {
            "role": "user",
            "content": composer_service._REPLY_ONLY_HISTORY_PREFIX
            + json.dumps(source[1], ensure_ascii=False, sort_keys=True)
            + composer_service._REPLY_ONLY_HISTORY_SUFFIX,
        },
        {"role": instruction["role"], "content": instruction["content"], "cache_control": {"type": "ephemeral"}},
    ]
    expected_tools = [{"type": "function", "function": {"name": "handoff_to_user"}, "cache_control": {"type": "ephemeral"}}]
    exact = _size(expected, expected_tools)
    assert _size(source, tools) > exact
    packet, _raw = _prepare_reply(source, tools=tools, limit=exact, cache=True)
    assert packet.evidence.messages == expected and packet.tools == expected_tools
    assert packet.byte_count == exact and packet.cache_plan.message_indexes == (0, 2)
    assert packet.cache_plan.tool_index == 0
    assert source[0]["cache_control"] == {"superseded": "x" * 50_000}


def test_reply_excess_does_not_construct_the_new_aggregate_or_detach_source_history(monkeypatch):
    source = [{"role": "system", "content": "Policy"}, {"role": "tool", "tool_call_id": "history", "content": "x" * 50_000}]
    actual_detach = composer_service.detach_prompt_guard_evidence
    detaches, aggregate = [], []

    def forbid_history_clone(messages):
        detaches.append(len(messages))
        assert len(messages) == 1, "whole live history was detached before aggregate admission"
        return actual_detach(messages)

    def forbid_aggregate():
        aggregate.append(True)
        raise AssertionError("reply aggregate was materialized")

    monkeypatch.setattr(composer_service, "detach_prompt_guard_evidence", forbid_history_clone)
    with pytest.raises(AssertionError, match="whole live history"):
        composer_service.detach_prompt_guard_evidence(source)
    with pytest.raises(AssertionError, match="reply aggregate"):
        _prepare_reply(source, frame_all=forbid_aggregate)
    aggregate.clear()
    detaches.clear()
    with pytest.raises(ComposerContextUnsupported):
        _prepare_reply(source, limit=64, frame_all=forbid_aggregate)
    assert aggregate == [] and detaches and set(detaches) == {1}


def test_reply_decoded_preflight_runs_only_for_excess_and_keeps_genuine_input(monkeypatch):
    decoded = "https://fixture-user:fixture-pass@example.test"
    encoded = "".join(f"\\u{ord(character):04x}" for character in decoded)
    source = [
        {"role": "user", "content": encoded, PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart(encoded, inspection_text=decoded),)}
    ]
    actual = reply_preparation.validate_prompt_guard_credentials
    observed = []

    def observe(**arguments):
        observed.append(arguments)
        return actual(**arguments)

    monkeypatch.setattr(reply_preparation, "validate_prompt_guard_credentials", observe)
    packet, _raw = _prepare_reply(source)
    assert packet.genuine_user == "Original user input" and observed == []
    with pytest.raises(CredentialMaterialRefused) as caught:
        _prepare_reply(source, limit=1)
    assert caught.value.finding.category == "credential_url" and observed
    with pytest.raises(ComposerContextUnsupported):
        _prepare_reply(source, limit=1, user=encoded)


@pytest.mark.parametrize("native", [[{"type": "redacted_thinking", "data": ""}], [{"type": "thinking", "thinking": "missing signature"}]])
def test_reply_unquoted_native_admission_precedes_generic_credentials_and_size(native):
    source = [
        {"role": "user", "content": "https://fixture-user:fixture-pass@example.test"},
        {"role": "assistant", "content": "", "thinking_blocks": native},
    ]
    with pytest.raises(ComposerAdmissionRefused) as caught:
        _prepare_reply(source, limit=1)
    assert type(caught.value) is ComposerAdmissionRefused


def test_reply_wrong_role_native_integrity_precedes_generic_credentials_and_size():
    source = [
        {
            "role": "user",
            "content": "https://fixture-user:fixture-pass@example.test",
            "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}],
        }
    ]
    with pytest.raises(AuditIntegrityError, match="assistant message"):
        _prepare_reply(source, limit=1)


def test_reply_owned_string_key_contract_preserves_framing_before_credentials_without_copying(monkeypatch):
    malformed = {"role": "tool", "tool_call_id": "history", "content": "evidence", "attributes": {2: "two", 10: "ten"}}
    # Existing formatter control: numeric ordering changes after its decode,
    # so this old integrity exit must never become a credential/size result.
    with pytest.raises(AuditIntegrityError, match="partition changed"):
        composer_service._reply_only_messages((malformed,))
    source = [{"role": "user", "content": "https://fixture-user:fixture-pass@example.test"}, malformed]

    def forbidden_copy(messages):
        raise AssertionError("invalid owned framing must precede detachment")

    monkeypatch.setattr(composer_service, "detach_prompt_guard_evidence", forbidden_copy)
    with pytest.raises(AuditIntegrityError, match="string JSON object keys"):
        _prepare_reply(source, limit=1)


@pytest.mark.parametrize("key", [2, False, None])
def test_reply_refuses_nonstring_owned_keys_before_large_leaf_resource_or_size(key):
    malformed = {"role": "tool", "tool_call_id": "history", "content": "x" * 300_000, "attributes": {key: "value"}}
    with pytest.raises(AuditIntegrityError, match="string JSON object keys"):
        _prepare_reply([malformed], limit=1)


@pytest.mark.parametrize("budget", ["width", "depth", "nodes", "default"])
def test_projected_credential_sequence_keeps_original_shared_root_and_first_finding(budget):
    original = [{"role": "user", "content": "neutral"}, {"role": "user", "content": "https://fixture-user:fixture-pass@example.test"}]
    policy = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
    if budget == "width":
        policy = replace(policy, max_width=1)
    elif budget == "depth":
        policy = replace(policy, max_depth=1)
    elif budget == "nodes":
        policy = replace(policy, max_nodes=6)
    expected = find_credential_material(original, policy)
    actual = find_credential_material_in_projected_sequence(iter(original), length=len(original), policy=policy)
    assert expected is not None and actual == expected
    assert actual.category == ("credential_url" if budget == "default" else "traversal_limit")


def test_measured_credential_leaf_is_refusal_only_and_does_not_preempt_earlier_findings():
    policy = replace(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY, max_string_bytes=128)
    original = [{"role": "user", "content": "https://fixture-user:fixture-pass@example.test"}, {"role": "user", "content": "x" * 129}]
    marker = MeasuredCredentialTextExcess(128, 129)
    projected = [original[0], {"role": "user", "content": marker}]
    expected = find_credential_material(original, policy)
    assert expected is not None and expected.category == "credential_url"
    assert find_credential_material_in_projected_sequence(iter(projected), length=2, policy=policy) == expected
    leaf_expected = find_credential_material([original[1]], policy)
    assert leaf_expected is not None and leaf_expected.category == "traversal_limit"
    assert find_credential_material_in_projected_sequence(iter(projected[1:]), length=1, policy=policy) == leaf_expected
    ordinary = find_credential_material(marker, policy)
    assert ordinary is not None and ordinary.category == "unsupported_value"
