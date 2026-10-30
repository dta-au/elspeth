"""Prospective live reply framing before aggregate request construction.

The inner historical record keeps standard JSON spelling; only the final
provider messages/tools are sized with RFC8785. Validation can materialize one
credential-bounded quoted record at a time, never the rejected aggregate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast, overload

import rfc8785

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size, utf8_size
from elspeth.contracts.credential_material import (
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
    MeasuredCredentialTextExcess,
    find_credential_material_in_projected_sequence,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_text_measure import JsonTextExtent, standard_json_text_extent
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_PROVENANCE_KEYS,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    GuardedProviderMessage,
    PromptGuardContentPart,
    PromptGuardMode,
    PromptGuardThinkingParts,
    PromptGuardToolArgumentParts,
    validate_prompt_guard_messages,
)
from elspeth.contracts.readonly_json import JsonValue, ReadonlyJsonArray, ReadonlyJsonObject, ReadonlyJsonString
from elspeth.web.composer.llm_response_parsing import AnthropicCacheMarkerPlan, plan_anthropic_cache_markers
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput, _ArrayView, _ObjectView, prepare_provider_input
from elspeth.web.composer.prompt_guard import PromptGuardMount, resolve_prompt_guard_user_prompt, validate_prompt_guard_credentials
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_thinking import scan_native_thinking_blocks
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material


@dataclass(frozen=True, slots=True)
class _ExtendedMessages(Sequence[GuardedProviderMessage]):
    source: Sequence[GuardedProviderMessage] = field(repr=False)
    tail: GuardedProviderMessage = field(repr=False)

    def __len__(self) -> int:
        return len(self.source) + 1

    @overload
    def __getitem__(self, index: int) -> GuardedProviderMessage: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[GuardedProviderMessage]: ...

    def __getitem__(self, index: int | slice) -> GuardedProviderMessage | Sequence[GuardedProviderMessage]:
        if isinstance(index, slice):
            return _MessageSlice(self, range(len(self))[index])
        position = index + len(self) if index < 0 else index
        if position == len(self.source):
            return self.tail
        if not 0 <= position < len(self.source):
            raise IndexError(index)
        return self.source[position]


@dataclass(frozen=True, slots=True)
class _MessageSlice(Sequence[GuardedProviderMessage]):
    source: Sequence[GuardedProviderMessage] = field(repr=False)
    positions: range

    def __len__(self) -> int:
        return len(self.positions)

    @overload
    def __getitem__(self, index: int) -> GuardedProviderMessage: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[GuardedProviderMessage]: ...

    def __getitem__(self, index: int | slice) -> GuardedProviderMessage | Sequence[GuardedProviderMessage]:
        if isinstance(index, slice):
            return _MessageSlice(self.source, self.positions[index])
        return self.source[self.positions[index]]


@dataclass(frozen=True, slots=True)
class _QuotedText(ReadonlyJsonString):
    extent: JsonTextExtent
    prefix: str = field(repr=False)
    suffix: str = field(repr=False)

    @property
    def credential_bytes(self) -> int:
        return self.extent.utf8_bytes + utf8_size(self.prefix, errors="replace") + utf8_size(self.suffix, errors="replace")

    def canonical_string_bytes(self, *, limit: int) -> int:
        if self.extent.unicode_error is not None:
            raise rfc8785.CanonicalizationError("input contains non-UTF-8 codepoints") from self.extent.unicode_error
        prefix = standard_json_text_extent(self.prefix)
        suffix = standard_json_text_extent(self.suffix)
        size = 2 + prefix.utf8_bytes - 2 + self.extent.escaped_utf8_bytes + suffix.utf8_bytes - 2
        return min(limit + 1, size)


def _quoted(message: GuardedProviderMessage) -> bool:
    return message["role"] == "tool" or "tool_calls" in message


def _role(message: GuardedProviderMessage) -> str:
    return "user" if message["role"] == "tool" else cast(str, message["role"])


@dataclass(frozen=True, slots=True)
class _SizedMessage(ReadonlyJsonObject):
    message: GuardedProviderMessage = field(repr=False)
    text: _QuotedText | None = field(repr=False)
    cache_marker: bool

    def _view(self) -> ReadonlyJsonObject:
        if self.text is None:
            return _ObjectView(self.message, PROMPT_GUARD_PROVENANCE_KEYS, self.cache_marker)
        return _QuotedFields(_role(self.message), self.text, self.cache_marker)

    def __len__(self) -> int:
        return len(self._view())

    def __iter__(self) -> Iterator[object]:
        return iter(self._view())

    def __getitem__(self, key: object) -> object:
        return self._view()[key]


@dataclass(frozen=True, slots=True)
class _QuotedFields(ReadonlyJsonObject):
    role: str
    content: ReadonlyJsonString = field(repr=False)
    cache_marker: bool

    def __len__(self) -> int:
        return 2 + int(self.cache_marker)

    def __iter__(self) -> Iterator[object]:
        yield "role"
        yield "content"
        if self.cache_marker:
            yield "cache_control"

    def __getitem__(self, key: object) -> object:
        if key == "role":
            return self.role
        if key == "content":
            return self.content
        if key == "cache_control" and self.cache_marker:
            return {"type": "ephemeral"}
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class _ReplyRoles(Sequence[GuardedProviderMessage]):
    source: Sequence[GuardedProviderMessage] = field(repr=False)

    def __len__(self) -> int:
        return len(self.source)

    @overload
    def __getitem__(self, index: int) -> GuardedProviderMessage: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[GuardedProviderMessage]: ...

    def __getitem__(self, index: int | slice) -> GuardedProviderMessage | Sequence[GuardedProviderMessage]:
        if isinstance(index, slice):
            return _MessageSlice(self, range(len(self))[index])
        message = self.source[index]
        # The fixed historical prefix is not a catalog prefix. Only role and
        # catalog selection are read by the cache planner; no wire is built.
        return {"role": _role(message), "content": ""} if _quoted(message) else message


@dataclass(frozen=True, slots=True)
class _SizedMessages(ReadonlyJsonArray):
    source: Sequence[GuardedProviderMessage] = field(repr=False)
    prefix: str = field(repr=False)
    suffix: str = field(repr=False)
    cache: AnthropicCacheMarkerPlan

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[object]:
        for index, message in enumerate(self.source):
            text = _text(message, self.prefix, self.suffix) if _quoted(message) else None
            yield _SizedMessage(message, text, index in self.cache.message_indexes)


def _text(message: GuardedProviderMessage, prefix: str, suffix: str) -> _QuotedText:
    return _QuotedText(
        standard_json_text_extent(_ObjectView(message, PROMPT_GUARD_PROVENANCE_KEYS), require_string_keys=True), prefix, suffix
    )


def prepare_reply_provider_input(
    *,
    messages: Sequence[GuardedProviderMessage],
    tools: Sequence[Mapping[str, JsonValue]],
    instruction: GuardedProviderMessage,
    model: str,
    mount: PromptGuardMount | None,
    mode: PromptGuardMode,
    max_bytes: int,
    cache_markers: bool,
    prefix: str,
    suffix: str,
    frame_one: Callable[[GuardedProviderMessage], GuardedProviderMessage],
    frame_all: Callable[[], Sequence[GuardedProviderMessage]],
) -> tuple[PreparedProviderInput, Sequence[GuardedProviderMessage]]:
    """Validate and size the real final reply candidate before building it."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise AuditIntegrityError("Reply preparation requires its actual positive input allowance")
    validate_prompt_guard_messages(messages)
    candidate = _ExtendedMessages(messages, instruction)
    genuine = resolve_prompt_guard_user_prompt(None)
    if any(PROMPT_GUARD_THINKING_PARTS_KEY in message and message[PROMPT_GUARD_THINKING_PARTS_KEY] is not None for message in messages):
        # The native exchange is retained intact; no quote/framing clone.
        prepared = prepare_provider_input(
            messages=candidate,
            tools=tools,
            tools_present=bool(tools),
            model=model,
            role="composer",
            mount=mount,
            mode=mode,
            user_prompt=genuine,
            max_bytes=max_bytes,
            cache_markers=cache_markers,
            mark_history_tail=True,
        )
        return prepared, candidate

    # Complete original framing validation precedes all credential findings.
    # Supported source objects have string keys; no sorting or whole quoted
    # scalar encoding is needed to validate their exact standard JSON extent.
    for message in messages:
        if _quoted(message):
            _text(message, prefix, suffix)
    cache = (
        plan_anthropic_cache_markers(_ReplyRoles(candidate), tools, mark_history_tail=True)
        if cache_markers
        else AnthropicCacheMarkerPlan((), None)
    )
    sized_messages = _SizedMessages(candidate, prefix, suffix, cache)
    tool_view = _ArrayView(tools, frozenset(), (cache.tool_index,) if cache.tool_index is not None else ())
    serialization_error: Exception | None = None
    size = None
    try:
        size = bounded_canonical_json_size({"messages": sized_messages, "tools": tool_view}, limit=max_bytes)
    except (TypeError, ValueError, RecursionError) as error:
        serialization_error = error

    def framed(index: int) -> GuardedProviderMessage:
        source = candidate[index]
        return frame_one(source) if _quoted(source) else source

    # The original physical owner scans every remaining native field before
    # the generic messages-root traversal. Quoted records no longer contain
    # a native field; unchanged records retain that exact admission order.
    for source in candidate:
        if not _quoted(source) and "thinking_blocks" in source:
            if source["role"] != "assistant":
                raise AuditIntegrityError("Provider thinking belongs to an assistant message")
            scan_native_thinking_blocks(source["thinking_blocks"])

    def residual(index: int) -> object:
        source = candidate[index]
        quoted = _quoted(source)
        if quoted:
            extent = _text(source, prefix, suffix)
            if extent.credential_bytes > DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes:
                # This value is refusal-only and is never a wire candidate.
                measured = MeasuredCredentialTextExcess(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes, extent.credential_bytes)
                if index in cache.message_indexes:
                    return {"role": _role(source), "content": measured, "cache_control": {"type": "ephemeral"}}
                return {"role": _role(source), "content": measured}
        excluded = PROMPT_GUARD_PROVENANCE_KEYS | ({"thinking_blocks"} if not quoted else set())
        return dict(_ObjectView(framed(index), excluded, index in cache.message_indexes).items())

    finding = find_credential_material_in_projected_sequence((residual(index) for index in range(len(candidate))), length=len(candidate))
    if finding is not None:
        raise CredentialMaterialRefused(surface="composer_provider_request", finding=finding)
    if serialization_error is not None:
        raise serialization_error
    if size is None:
        raise AuditIntegrityError("Reply preparation lost its complete final size")
    if size.exceeded:
        # Admitted input keeps its existing dispatch-time decoded inspection.
        # Only a pending size refusal needs this validation-only preflight.
        require_no_credential_material(genuine if genuine is not None else "", surface="composer_prompt_guard_request")
        genuine_index = None
        for index in range(len(candidate)):
            value = framed(index)
            if genuine is not None and value["role"] == "user" and "content" in value and value["content"] == genuine:
                genuine_index = index
        for index in range(len(candidate)):
            value = framed(index)
            validate_prompt_guard_credentials(
                messages=(value,),
                genuine_user=genuine if index == genuine_index else None,
                trusted_message_indexes=frozenset({0}) if PROMPT_GUARD_TRUST_KEY in value else frozenset(),
                content_parts=(
                    cast(tuple[PromptGuardContentPart, ...], value[PROMPT_GUARD_CONTENT_PARTS_KEY])
                    if PROMPT_GUARD_CONTENT_PARTS_KEY in value
                    else None,
                ),
                tool_argument_parts=(
                    cast(tuple[PromptGuardToolArgumentParts, ...], value[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY])
                    if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in value
                    else None,
                ),
                thinking_parts=(
                    cast(PromptGuardThinkingParts, value[PROMPT_GUARD_THINKING_PARTS_KEY])
                    if PROMPT_GUARD_THINKING_PARTS_KEY in value
                    else None,
                ),
            )
        raise ComposerContextUnsupported("prepared_request_bytes")
    materialized = _ExtendedMessages(frame_all(), instruction)
    prepared = prepare_provider_input(
        messages=materialized,
        tools=tools,
        tools_present=bool(tools),
        model=model,
        role="composer",
        mount=mount,
        mode=mode,
        user_prompt=genuine,
        max_bytes=max_bytes,
        cache_markers=cache_markers,
        mark_history_tail=True,
    )
    if prepared.byte_count != size.admitted_bytes:
        raise AuditIntegrityError("Reply framing changed its prospective final wire size")
    return prepared, materialized
